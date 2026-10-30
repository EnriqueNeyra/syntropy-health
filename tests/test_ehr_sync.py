"""Health record syncs on a bad day: failures, short answers, long histories and second sign-ins, against the simulator
misbehaving (app.simulator.server.faults) and, where a transport has to fail, a fake server."""

import asyncio
import json
import time
from email.utils import formatdate

import httpx
import pytest

from app.connectors import smart
from app.core.db import db, read
from app.services import sync as sync_svc
from app.simulator import server as sim
from app.store import connections, records
from tests.conftest import connect_institution

BANNER = "banner-health"    # Oracle Health in the simulator: gives a refresh token


@pytest.fixture(autouse=True)
def faults_and_no_waiting(monkeypatch):
    sim.reset_faults()
    waits = []

    async def no_wait(seconds):
        waits.append(seconds)
    monkeypatch.setattr(smart, "_sleep", no_wait)
    yield waits
    sim.reset_faults()


def _labs(client):
    profile = client.get("/api/connections").json()["profile_id"]
    return records.list_records(profile, "labs", dedupe=False, limit=100_000)["items"]


def _status(cid):
    return connections.get(cid)["status"]


def _sync(client, cid):
    return client.post(f"/api/connections/{cid}/sync").json()


def _credentials(cid):
    return connections.get(cid, include_credentials=True)["credentials"]


def _expire(cid):
    connections.update(cid, credentials={**_credentials(cid), "expires_at": 1})


def _stored(cid):
    return connections.get(cid)["record_count"]


def _one_lab(client, cid):
    lab = next(i for i in _labs(client) if i["connection_id"] == cid)
    return lab, f"Observation/{lab['resource_id']}"


EMPTY = {"resourceType": "Bundle", "type": "searchset", "total": 0, "entry": []}


# ---------------------------------------------------------------------------
# Nothing is removed on an empty or partial answer (F006, F026, F037)
# ---------------------------------------------------------------------------

def test_an_empty_answer_never_removes_anything(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    before = _stored(cid)
    sim.faults.answers.append({"type": "Observation", "category": "laboratory", "status": 200, "body": EMPTY, "times": 3})
    for _ in range(3):
        res = _sync(client, cid)
        assert res["removed"] == 0 and res["status"] == "success", res
    assert _stored(cid) == before
    assert res["held"]["labs"] > 0     # kept, and the run says so


def test_a_single_allergy_going_quiet_is_not_a_removal(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    before = client.get("/api/records", params={"category": "allergies", "dedupe": "false"}).json()["total"]
    assert before
    sim.faults.answers.append({"type": "AllergyIntolerance", "status": 200, "body": EMPTY, "times": 6})
    for _ in range(3):
        assert _sync(client, cid)["removed"] == 0
    assert client.get("/api/records", params={"category": "allergies", "dedupe": "false"}).json()["total"] == before


@pytest.mark.parametrize("severity", ["fatal", "error"])
def test_a_200_carrying_an_error_is_a_failure_not_an_empty_answer(client, severity):
    cid = connect_institution(client, BANNER)["connection_id"]
    before = _stored(cid)
    sim.faults.answers.append({"type": "Condition", "outcome": severity})
    res = _sync(client, cid)
    assert res["status"] == "partial" and res["queries"]["Condition"] == "error:outcome", res
    assert res["removed"] == 0 and _stored(cid) == before
    assert any("Conditions" in w for w in res["warnings"])


def test_an_outcome_inside_a_bundle_fails_the_query_too(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    bundle = {**EMPTY, "entry": [{"resource": {"resourceType": "OperationOutcome", "issue": [{"severity": "fatal", "code": "timeout"}]},
                                  "search": {"mode": "outcome"}}]}
    sim.faults.answers.append({"type": "Immunization", "status": 200, "body": bundle})
    res = _sync(client, cid)
    assert res["queries"]["Immunization"] == "error:outcome" and res["status"] == "partial"


def test_a_warning_on_an_ordinary_empty_search_stays_an_empty_answer(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    bundle = {**EMPTY, "entry": [{"resource": {"resourceType": "OperationOutcome",
                                               "issue": [{"severity": "warning", "code": "processing", "details": {"text": "No results (4101)"}}]}}]}
    sim.faults.answers.append({"type": "Goal", "status": 200, "body": bundle})
    res = _sync(client, cid)
    assert res["queries"]["Goal"] == "empty" and res["status"] == "success", res


def test_a_gateways_own_json_is_not_an_empty_answer(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    sim.faults.answers.append({"type": "Device", "status": 200, "body": {"message": "maintenance"}})
    res = _sync(client, cid)
    assert res["queries"]["Device"] == "error:invalid-json" and res["removed"] == 0


def test_the_source_saying_it_left_things_out_blocks_removal_without_alarming_anyone(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    lab, key = _one_lab(client, cid)
    sim.faults.hidden.add(key)
    bundle_note = {"resourceType": "OperationOutcome", "issue": [{"severity": "warning", "code": "suppressed"}]}
    sim.faults.answers.append({"type": "Observation", "category": "laboratory", "status": 200, "times": 4, "body": {
        "resourceType": "Bundle", "type": "searchset", "entry": [
            {"resource": bundle_note}, {"resource": {**lab_raw(client, lab), "id": "another"}}]}})
    for _ in range(3):
        res = _sync(client, cid)
        assert res["status"] == "success" and res["removed"] == 0, res
    assert client.get(f"/api/records/{lab['id']}").status_code == 200


def lab_raw(client, lab):
    return client.get(f"/api/records/{lab['id']}").json()["raw"]


def test_a_record_leaves_only_when_a_second_complete_answer_agrees(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    lab, key = _one_lab(client, cid)
    sim.faults.hidden.add(key)

    first = _sync(client, cid)
    assert first["removed"] == 0 and first["status"] == "success"
    assert client.get(f"/api/records/{lab['id']}").status_code == 200    # one answer proves nothing

    second = _sync(client, cid)
    assert second["removed"] == 1 and second["pruned"] == 1
    assert client.get(f"/api/records/{lab['id']}").status_code == 404
    assert lab["id"] not in {i["id"] for i in _labs(client)}
    items = client.get(f"/api/connections/{cid}/removed").json()["items"]
    assert [(i["id"], i["reason"]) for i in items] == [(lab["id"], "missing")]
    assert client.get(f"/api/connections/{cid}").json()["removed_count"] == 1


def test_one_good_answer_between_misses_starts_the_count_again(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    lab, key = _one_lab(client, cid)
    sim.faults.hidden.add(key)
    _sync(client, cid)
    sim.faults.hidden.clear()
    _sync(client, cid)
    sim.faults.hidden.add(key)
    assert _sync(client, cid)["removed"] == 0
    assert client.get(f"/api/records/{lab['id']}").status_code == 200


def test_a_record_that_comes_back_keeps_its_place_in_history(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    lab, key = _one_lab(client, cid)
    with db() as c:
        c.execute("UPDATE clinical_records SET first_seen_at = 1000 WHERE id = ?", (lab["id"],))
    sim.faults.hidden.add(key)
    _sync(client, cid)
    _sync(client, cid)
    sim.faults.hidden.clear()
    back = _sync(client, cid)
    assert back["inserted"] == 1
    assert client.get(f"/api/records/{lab['id']}").json()["first_seen_at"] == 1000
    assert client.get(f"/api/connections/{cid}/removed").json()["items"] == []


def test_many_records_vanishing_at_once_wait_before_they_are_believed(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    labs = [i for i in _labs(client) if i["connection_id"] == cid]
    half = labs[: len(labs) // 2]
    assert len(half) > records.SHRINK_FLOOR
    sim.faults.hidden.update(f"Observation/{i['resource_id']}" for i in half)
    _sync(client, cid)
    held = _sync(client, cid)
    assert held["removed"] == 0 and held["held"]["labs"] == len(half), held
    assert len([i for i in _labs(client) if i["connection_id"] == cid]) == len(labs)

    with db() as c:
        c.execute("UPDATE clinical_records SET missing_since = missing_since - ? WHERE connection_id = ? AND missing_since IS NOT NULL",
                  ((records.SHRINK_WAIT_DAYS + 1) * 86400, cid))
    gone = _sync(client, cid)
    assert gone["removed"] == len(half)


def test_a_retraction_the_source_states_applies_at_once_even_if_other_queries_failed(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    lab, key = _one_lab(client, cid)
    sim.faults.retracted.add(key)
    sim.faults.answers.append({"type": "Condition", "status": 500})
    res = _sync(client, cid)
    assert res["status"] == "partial" and res["removed"] == 1, res
    assert client.get(f"/api/records/{lab['id']}").status_code == 404
    item = client.get(f"/api/connections/{cid}/removed").json()["items"][0]
    assert item["reason"] == "retracted" and item["title"] == lab["title"]


def test_a_person_can_put_a_removed_record_back_and_it_stays(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    lab, key = _one_lab(client, cid)
    sim.faults.retracted.add(key)
    assert _sync(client, cid)["removed"] == 1

    assert client.post(f"/api/connections/{cid}/removed/{lab['id']}/restore").status_code == 200
    back = client.get(f"/api/records/{lab['id']}").json()
    assert back["title"] == lab["title"] and back["kept_at"]
    assert _sync(client, cid)["removed"] == 0
    assert client.get(f"/api/records/{lab['id']}").status_code == 200
    assert any(e["action"] == "record.restored" for e in client.get("/api/audit").json()["events"])
    assert client.post(f"/api/connections/{cid}/removed/{lab['id']}/restore").status_code == 404


def test_removed_records_reach_no_read_path(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    lab, key = _one_lab(client, cid)
    sim.faults.retracted.add(key)
    _sync(client, cid)
    assert lab["id"] not in {i["id"] for i in client.get("/api/timeline", params={"categories": "labs", "limit": 500}).json()["items"]}
    assert lab["id"] not in {p["record_id"] for p in client.get("/api/observations/series", params={"code": lab["code"]}).json()["points"]}
    assert lab["resource_id"] not in {r.get("id") for r in records.raw_resources(connections.get(cid)["profile_id"])}


def test_removed_records_go_with_their_source_and_after_a_long_time(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    _, key = _one_lab(client, cid)
    sim.faults.retracted.add(key)
    _sync(client, cid)
    with db() as c:
        c.execute("UPDATE clinical_records_removed SET removed_at = ?", (time.time() - (records.REMOVED_KEEP_DAYS + 1) * 86400,))
    sim.faults.retracted.clear()
    sim.faults.hidden.add(key)
    _sync(client, cid)
    assert records.removed_count(cid) == 0

    other = connect_institution(client, "epic-duke-health")["connection_id"]
    _, key = _one_lab(client, other)
    sim.faults.retracted.add(key)
    _sync(client, other)
    assert records.removed_count(other) == 1
    assert client.delete(f"/api/connections/{other}", params={"delete_data": True}).status_code == 200
    assert records.removed_count(other) == 0


def test_a_resource_the_reader_chokes_on_keeps_its_old_copy(client, monkeypatch):
    from app.connectors import fhir_normalize
    cid = connect_institution(client, BANNER)["connection_id"]
    lab, _ = _one_lab(client, cid)
    real = fhir_normalize.NORMALIZERS["Observation"]

    def choke(res):
        if res["id"] == lab["resource_id"]:
            raise ValueError("malformed")
        return real(res)
    monkeypatch.setitem(fhir_normalize.NORMALIZERS, "Observation", choke)
    for _ in range(3):
        res = _sync(client, cid)
        assert res["removed"] == 0 and res["status"] == "partial"
    assert any("couldn't be read" in w for w in res["warnings"])
    assert client.get(f"/api/records/{lab['id']}").status_code == 200


def test_a_transient_failure_on_the_broad_query_lets_the_narrow_one_keep_its_records_but_prunes_nothing(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    before = _stored(cid)
    for rtype, category in (("Condition", None), ("AllergyIntolerance", None)):
        sim.faults.answers.append({"type": rtype, "category": category, "status": 500, "times": 1})
    for _ in range(2):
        sim.faults.answers[0]["times"] = sim.faults.answers[1]["times"] = 1
        res = _sync(client, cid)
        assert res["status"] == "partial", res
        assert res["queries"]["Condition"] == "error:500" and res["queries"]["AllergyIntolerance"] == "error:500"
        assert res["removed"] == 0
    assert _stored(cid) == before


def test_first_import_still_gets_the_narrow_querys_records_when_the_broad_one_fails(client):
    sim.faults.answers.append({"type": "Condition", "status": 500, "broad_only": True})
    cid = connect_institution(client, BANNER)["connection_id"]
    run = client.get(f"/api/connections/{cid}").json()["last_run"]
    assert run["stats"]["queries"]["Condition"] == "error:500" and run["status"] == "partial"
    assert run["stats"]["by_category"].get("conditions")


def test_only_what_a_narrow_answer_covers_can_go_when_the_broad_query_is_refused(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    conditions = client.get("/api/records", params={"category": "conditions", "dedupe": "false"}).json()["items"]
    problems = [c for c in conditions if "problem-list-item" in json.dumps(lab_raw(client, c).get("category"))]
    assert problems
    target = problems[0]
    with db() as c:     # stored as an encounter diagnosis: a problem-list search could never return it
        c.execute("UPDATE clinical_records SET raw_json = replace(raw_json, 'problem-list-item', 'encounter-diagnosis') WHERE id = ?",
                  (conditions[-1]["id"],))
    sim.faults.answers.append({"type": "Condition", "status": 403, "broad_only": True})
    sim.faults.hidden.update({f"Condition/{target['resource_id']}", f"Condition/{conditions[-1]['resource_id']}"})
    _sync(client, cid)
    res = _sync(client, cid)
    assert res["queries"]["Condition"] == "ok" and res["removed"] == 1, res
    assert client.get(f"/api/records/{target['id']}").status_code == 404
    assert client.get(f"/api/records/{conditions[-1]['id']}").status_code == 200


# ---------------------------------------------------------------------------
# Refusals and queries a server has never answered (F037)
# ---------------------------------------------------------------------------

def test_a_query_the_server_has_never_answered_isnt_a_problem(client):
    sim.faults.answers.append({"type": "Observation", "category": "survey", "status": 400})
    sim.faults.answers.append({"type": "Device", "status": 404})
    cid = connect_institution(client, BANNER)["connection_id"]
    assert client.get(f"/api/connections/{cid}").json()["last_sync_status"] == "success"
    res = _sync(client, cid)
    assert res["status"] == "success" and res["queries"]["Device"] == "unsupported"


def test_one_refused_category_doesnt_stop_corrections_to_the_others(client):
    sim.faults.answers.append({"type": "Observation", "category": "survey", "status": 400})
    cid = connect_institution(client, BANNER)["connection_id"]
    lab, key = _one_lab(client, cid)
    sim.faults.hidden.add(key)
    _sync(client, cid)
    res = _sync(client, cid)
    assert res["removed"] == 1 and res["status"] == "success", res


def test_a_query_that_used_to_work_and_stopped_is_reported_and_protects_its_records(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    before = _stored(cid)
    sim.faults.answers.append({"type": "Observation", "category": "laboratory", "status": 404})
    for _ in range(3):
        res = _sync(client, cid)
        assert res["status"] == "partial" and res["removed"] == 0, res
    assert any("Lab results" in w for w in res["warnings"])
    assert _stored(cid) == before


def test_a_forbidden_query_is_named_in_words(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    sim.faults.answers.append({"type": "Immunization", "status": 403})
    res = _sync(client, cid)
    assert res["status"] == "partial" and "Immunizations: this source didn't allow them to be read." in res["warnings"]


def test_a_later_fallbacks_refusal_doesnt_undo_an_earlier_answer(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    # Empty, empty, then the last alternative is refused: the source answered, so this is no warning.
    sim.faults.answers.append({"type": "MedicationRequest", "status": 200, "body": EMPTY, "times": 2})
    sim.faults.answers.append({"type": "MedicationRequest", "status": 400})
    res = _sync(client, cid)
    assert res["queries"]["MedicationRequest"] == "empty" and not [w for w in res["warnings"] if "Medications" in w]


def test_extra_observation_categories_are_quiet_when_refused(client):
    for category in ("exam", "imaging", "procedure", "activity", "therapy", "sdoh", "functional-status"):
        sim.faults.answers.append({"type": "Observation", "category": category, "status": 403})
    cid = connect_institution(client, BANNER)["connection_id"]
    res = _sync(client, cid)
    assert res["status"] == "success" and res["queries"]["Observation?category=imaging"] == "forbidden", res


def test_extra_observation_categories_bring_their_records_in(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    patient = connections.get(cid)["patient_ref"]
    finding = {"resourceType": "Observation", "id": "exam-1", "status": "final", "subject": {"reference": f"Patient/{patient}"},
               "category": [{"coding": [{"code": "exam"}]}], "code": {"text": "Heart sounds"}, "valueString": "Normal",
               "effectiveDateTime": "2026-01-01"}
    sim.faults.answers.append({"type": "Observation", "category": "exam", "status": 200,
                               "body": {**EMPTY, "total": 1, "entry": [{"resource": finding}]}})
    res = _sync(client, cid)
    assert res["queries"]["Observation?category=exam"] == "ok" and res["inserted"] == 1


# ---------------------------------------------------------------------------
# Token renewal (F032, F033, F098)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_a_renewal_blip_isnt_a_lost_sign_in(client, status):
    cid = connect_institution(client, BANNER)["connection_id"]
    _expire(cid)
    sim.faults.token.append({"status": status, "body": {"error": "busy"}, "times": 1})
    res = _sync(client, cid)
    assert res["status"] == "error" and res["kind"] == "network", res
    conn = connections.get(cid)
    assert conn["status"] == "active" and "Reconnect" not in conn["last_error"] and "try again" in conn["last_error"]
    assert conn["next_retry_at"] > time.time()
    assert cid not in {c["id"] for c in sync_svc.due_connections(0, 0)}
    with db() as c:
        c.execute("UPDATE connections SET next_retry_at = 1 WHERE id = ?", (cid,))
    assert cid in {c["id"] for c in sync_svc.due_connections(24, 24)}
    assert _sync(client, cid)["status"] == "success"
    conn = connections.get(cid)
    assert conn["next_retry_at"] is None and conn["sync_failures"] == 0 and conn["last_error"] is None


def test_retry_after_on_the_token_endpoint_is_respected(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    _expire(cid)
    sim.faults.token.append({"status": 429, "body": {"error": "slow_down"}, "retry_after": "7200", "times": 1})
    _sync(client, cid)
    assert connections.get(cid)["next_retry_at"] > time.time() + 7000


@pytest.mark.parametrize("body", [{"error": "temporarily_unavailable"}, {"error": "server_error"}])
def test_oauths_own_temporary_errors_are_temporary(client, body):
    cid = connect_institution(client, BANNER)["connection_id"]
    _expire(cid)
    sim.faults.token.append({"status": 400, "body": body, "times": 1})
    assert _sync(client, cid)["kind"] == "network"


@pytest.mark.parametrize("status,body", [(400, {"error": "invalid_grant"}), (401, {"error": "invalid_client"}),
                                         (400, {"error": "invalid_request"}), (403, {"error": "access_denied"})])
def test_a_real_refusal_still_asks_to_reconnect(client, status, body):
    cid = connect_institution(client, BANNER)["connection_id"]
    _expire(cid)
    sim.faults.token.append({"status": status, "body": body, "times": 1})
    assert _sync(client, cid)["kind"] == "auth"
    assert _status(cid) == "needs_reauth"
    assert cid not in {c["id"] for c in sync_svc.due_connections(0, 0)}


@pytest.mark.parametrize("body", [{"token_type": "Bearer"}, ["not", "an", "object"]])
def test_a_token_answer_without_a_token_is_temporary_and_readable(client, body):
    cid = connect_institution(client, BANNER)["connection_id"]
    _expire(cid)
    sim.faults.token.append({"status": 200, "body": body, "times": 1})
    res = _sync(client, cid)
    assert res["kind"] == "network" and "access token" in res["error"]


def test_epics_signed_renewal_is_classified_the_same_way(client):
    cid = connect_institution(client, "epic-duke-health")["connection_id"]
    assert _credentials(cid)["dynamic_client"]
    _expire(cid)
    sim.faults.token.append({"status": 503, "body": {}, "times": 1})
    assert _sync(client, cid)["kind"] == "network"
    assert _status(cid) == "active"
    sim.faults.token.append({"status": 400, "body": {"error": "invalid_client"}, "times": 1})
    assert _sync(client, cid)["kind"] == "auth"
    assert _status(cid) == "needs_reauth"


def test_a_lasting_outage_is_shown_after_a_few_tries_and_backs_off(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    delays = []
    for attempt in range(connections.FAILURES_BEFORE_ALERT):
        _expire(cid)
        sim.faults.token.append({"status": 503, "body": {}, "times": 1})
        _sync(client, cid)
        delays.append(connections.get(cid)["next_retry_at"] - time.time())
        assert _status(cid) == ("error" if attempt == connections.FAILURES_BEFORE_ALERT - 1 else "active")
    assert delays == sorted(delays)


def test_rotated_tokens_are_saved_even_if_the_sync_then_fails(client):
    sim.faults.rotate_refresh = True
    cid = connect_institution(client, BANNER)["connection_id"]
    old = _credentials(cid)
    connections.update(cid, credentials={**old, "access_token": "dead"})    # not past its time, so the first read finds out
    sim.faults.answers.append({"type": "Patient", "read": True, "status": 404})
    res = _sync(client, cid)
    assert res["status"] == "error"
    assert _credentials(cid)["refresh_token"] != old["refresh_token"]
    sim.faults.answers.clear()
    assert _sync(client, cid)["status"] == "success"
    assert _status(cid) == "active"


def test_a_long_sync_can_outlast_several_tokens(client):
    sim.faults.rotate_refresh = True
    cid = connect_institution(client, BANNER)["connection_id"]
    first = _credentials(cid)["refresh_token"]
    sim.faults.expire_access_after = 8
    res = _sync(client, cid)
    assert res["status"] == "success", res
    assert _credentials(cid)["refresh_token"] != first


def _with_notes_by_link(client):
    """An Oracle Health hospital (so a refresh token) that the simulator has serving its notes through Binary."""
    from app import directory
    from app.simulator import generator
    from app.simulator.personas import PERSONAS
    found = directory.search(None, "cerner", 200)["results"]
    inst = next(i for i in found if generator.tenant_profile(i["id"], PERSONAS["alex"], "cerner").binary_notes)
    return connect_institution(client, inst["id"])["connection_id"]


def test_notes_after_a_token_expires_still_get_their_text_and_the_new_token_is_saved(client):
    sim.faults.rotate_refresh = True
    cid = _with_notes_by_link(client)
    with db() as c:
        c.execute("UPDATE clinical_records SET narrative = NULL WHERE connection_id = ? AND category = 'notes'", (cid,))
    # Everything up to the notes runs on this token; the first Binary finds it dead.
    sim.faults.requests.clear()
    _sync(client, cid)
    needed = len([r for r in sim.faults.requests if not r.startswith("Binary/")])
    with db() as c:
        c.execute("UPDATE clinical_records SET narrative = NULL WHERE connection_id = ? AND category = 'notes'", (cid,))
    token = _credentials(cid)["refresh_token"]
    sim.faults.expire_access_after = needed
    res = _sync(client, cid)
    assert res["notes_fetched"] > 0 and res["status"] == "success", res
    assert _credentials(cid)["refresh_token"] != token


def test_a_token_dying_during_the_notes_keeps_the_records_and_says_so(client):
    cid = _with_notes_by_link(client)
    with db() as c:
        c.execute("UPDATE clinical_records SET narrative = NULL WHERE connection_id = ? AND category = 'notes'", (cid,))
    sim.faults.requests.clear()
    _sync(client, cid)
    needed = len([r for r in sim.faults.requests if not r.startswith("Binary/")])
    with db() as c:
        c.execute("UPDATE clinical_records SET narrative = NULL WHERE connection_id = ? AND category = 'notes'", (cid,))
    sim.faults.expire_access_after = needed
    sim.faults.token.append({"status": 400, "body": {"error": "invalid_grant"}, "times": 99})
    res = _sync(client, cid)
    assert res["status"] == "partial" and res["notes_fetched"] == 0 and res["notes_pending"] > 0, res
    assert any("Clinical notes" in w for w in res["warnings"])
    assert _stored(cid) > 0


def test_tokens_from_a_sign_in_during_the_sync_arent_overwritten(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    during = _credentials(cid)
    fresh = {**during, "access_token": "from-the-new-sign-in", "refresh_token": "new-refresh"}
    connections.update(cid, credentials=fresh)
    assert not connections.save_renewed(cid, during, {**during, "access_token": "old-sync-renewed"})
    assert _credentials(cid)["access_token"] == "from-the-new-sign-in"
    assert connections.save_renewed(cid, fresh, {**fresh, "access_token": "renewed"})
    assert _credentials(cid)["access_token"] == "renewed"


def test_an_old_syncs_failure_doesnt_flag_a_source_just_signed_in_again(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    conn = connections.get(cid, include_credentials=True)
    run_id = connections.start_run(cid, "manual")
    connections.update(cid, credentials={**conn["credentials"], "access_token": "from-the-new-sign-in", "refresh_token": "new"})
    res = asyncio.run(sync_svc._stopped(run_id, conn, smart.SmartError("The source rejected our access token.", "auth"), time.time()))
    assert res["kind"] == "superseded"
    assert _status(cid) == "active"


# ---------------------------------------------------------------------------
# Patient reads (F035)
# ---------------------------------------------------------------------------

def test_declining_demographics_still_brings_the_rest(client, monkeypatch):
    from app import directory
    monkeypatch.setattr(directory, "USCDI_SCOPES", directory.USCDI_SCOPES.replace(" patient/Patient.read", ""))
    cid = connect_institution(client, BANNER)["connection_id"]
    conn = client.get(f"/api/connections/{cid}").json()
    assert conn["last_sync_status"] == "success" and conn["record_count"] > 0
    assert conn["last_run"]["stats"]["queries"]["Patient"] == "not-granted"


def test_a_403_on_patient_is_treated_as_declined(client):
    sim.faults.answers.append({"type": "Patient", "read": True, "status": 403})
    cid = connect_institution(client, BANNER)["connection_id"]
    conn = client.get(f"/api/connections/{cid}").json()
    assert conn["last_sync_status"] == "success" and conn["record_count"] > 0
    assert client.get("/api/summary").json()["identities"] == []


def test_a_server_failing_on_patient_gets_the_rest_but_may_not_remove_anything(client, faults_and_no_waiting):
    cid = connect_institution(client, BANNER)["connection_id"]
    lab, key = _one_lab(client, cid)
    sim.faults.hidden.add(key)
    _sync(client, cid)
    sim.faults.answers.append({"type": "Patient", "read": True, "status": 503})
    res = _sync(client, cid)
    assert res["status"] == "partial" and res["removed"] == 0, res
    assert any("Name and birth date" in w for w in res["warnings"])
    assert client.get(f"/api/records/{lab['id']}").status_code == 200
    assert client.get("/api/summary").json()["identities"]      # the identity already here is kept


def test_a_patient_id_that_no_longer_resolves_stays_a_hard_stop(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    before = _stored(cid)
    sim.faults.answers.append({"type": "Patient", "read": True, "status": 404})
    res = _sync(client, cid)
    assert res["status"] == "error" and res["kind"] == "error"
    assert _stored(cid) == before
    sim.faults.answers.clear()
    sim.faults.answers.append({"type": "Patient", "read": True, "status": 200, "body": {"resourceType": "Bundle"}})
    assert _sync(client, cid)["status"] == "error"


def test_a_whole_outage_is_a_failure_not_a_partial_success(client, faults_and_no_waiting):
    cid = connect_institution(client, BANNER)["connection_id"]
    before = _stored(cid)
    from app.simulator.server import SUPPORTED_TYPES
    for rtype in SUPPORTED_TYPES:
        sim.faults.answers.append({"type": rtype, "status": 503})
        sim.faults.answers.append({"type": rtype, "read": True, "status": 503})
    sim.faults.requests.clear()
    res = _sync(client, cid)
    assert res["status"] == "error" and res["kind"] == "network", res
    assert _stored(cid) == before and _status(cid) == "active"
    # It stopped asking once the server was clearly down.
    assert len(sim.faults.requests) < 30


# ---------------------------------------------------------------------------
# Retries, Retry-After and keeping what arrived (F034)
# ---------------------------------------------------------------------------

def test_a_throttled_request_waits_as_told_and_carries_on(client, faults_and_no_waiting):
    sim.faults.extra_labs = 250
    cid = connect_institution(client, BANNER)["connection_id"]
    faults_and_no_waiting.clear()
    sim.faults.answers.append({"type": "Observation", "category": "laboratory", "offset": 200, "status": 429, "retry_after": "7", "times": 1})
    res = _sync(client, cid)
    assert res["status"] == "success" and res["queries"]["Observation?category=laboratory"] == "ok", res
    assert faults_and_no_waiting == [7.0]


def test_retry_after_can_be_a_date(client, faults_and_no_waiting):
    cid = connect_institution(client, BANNER)["connection_id"]
    faults_and_no_waiting.clear()
    sim.faults.answers.append({"type": "Goal", "status": 503, "retry_after": formatdate(time.time() + 20, usegmt=True), "times": 1})
    assert _sync(client, cid)["status"] == "success"
    assert 15 < faults_and_no_waiting[0] <= 20


def test_a_long_retry_after_ends_the_sync_and_comes_back_then(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    before = _stored(cid)
    sim.faults.answers.append({"type": "Observation", "category": "laboratory", "status": 429, "retry_after": "3600", "times": 1})
    sim.faults.requests.clear()
    res = _sync(client, cid)
    assert res["status"] == "partial" and res["removed"] == 0, res
    assert connections.get(cid)["next_retry_at"] > time.time() + 3000
    assert not [r for r in sim.faults.requests if r.startswith("Encounter")]     # it didn't carry on regardless
    assert _stored(cid) == before


def test_a_failed_page_keeps_earlier_pages_and_the_next_sync_carries_on_from_it(client):
    sim.faults.extra_labs = 350
    sim.faults.answers.append({"type": "Observation", "category": "laboratory", "offset": 200, "status": 500, "times": 1})
    cid = connect_institution(client, BANNER)["connection_id"]
    run = client.get(f"/api/connections/{cid}").json()["last_run"]
    assert run["status"] == "partial" and run["stats"]["queries"]["Observation?category=laboratory"] == "error:500"
    assert 200 <= len(_labs(client)) < 350
    state = connections.query_state(cid)["Observation?category=laboratory"]
    assert "_offset=200" in state["next_url"]
    assert connections.get(cid)["next_retry_at"]

    sim.faults.requests.clear()
    res = _sync(client, cid)
    assert res["queries"]["Observation?category=laboratory"] == "ok" and res["status"] == "success"
    labs = [r for r in sim.faults.requests if "category=laboratory" in r]
    assert "_offset=200" in labs[1] and "_offset" not in labs[0]        # the newest page, then the saved place
    assert len(_labs(client)) >= 350
    assert connections.query_state(cid)["Observation?category=laboratory"]["next_url"] is None


def test_a_sweep_that_took_two_syncs_cant_remove_anything(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    lab, key = _one_lab(client, cid)
    with db() as c:
        c.execute("UPDATE clinical_records SET missing_since = 1 WHERE id = ?", (lab["id"],))
    sim.faults.hidden.add(key)
    with db() as c:     # as if the last sync stopped on a later page
        c.execute("INSERT OR REPLACE INTO sync_query_state(connection_id, query, status, next_url, updated_at) VALUES (?, ?, 'error:500', ?, 1)",
                  (cid, "Observation?category=laboratory", f"{connections.get(cid)['fhir_base_url']}/Observation?patient={connections.get(cid)['patient_ref']}&category=laboratory&_count=100&_offset=100"))
    assert _sync(client, cid)["removed"] == 0
    assert client.get(f"/api/records/{lab['id']}").status_code == 200


def test_an_expired_saved_place_starts_the_query_over(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    base = connections.get(cid)["fhir_base_url"]
    with db() as c:
        c.execute("INSERT OR REPLACE INTO sync_query_state(connection_id, query, status, next_url, updated_at) VALUES (?, ?, 'paused', ?, 1)",
                  (cid, "Immunization", f"{base}/Immunization?expired=1"))
    sim.faults.answers.append({"type": "Immunization", "status": 410, "offset": 0, "times": 1})
    sim.faults.answers.append({"type": "Immunization", "status": 410, "offset": 0, "times": 0})
    res = _sync(client, cid)
    assert res["status"] == "success", res
    assert res["queries"]["Immunization"] == "ok"


def test_a_saved_place_on_another_server_is_ignored(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    fhir = smart.FhirClient("https://fhir.example.org/r4", {"access_token": "t"}, "p")
    out = smart.FetchResult()
    runs = fhir._plan({"Immunization": {"status": "paused", "next_url": "https://elsewhere.example.net/next"}}, out)
    assert all(q.next_url is None for q in runs) and cid


# ---------------------------------------------------------------------------
# Long histories (F036)
# ---------------------------------------------------------------------------

def test_a_long_history_arrives_whole_with_no_page_limit(client):
    sim.faults.extra_labs = 3000
    sim.faults.oldest_first = True
    cid = connect_institution(client, BANNER)["connection_id"]
    run = client.get(f"/api/connections/{cid}").json()["last_run"]
    assert run["status"] == "success"
    assert len(_labs(client)) > 3000


def test_newest_results_come_first_where_the_server_can_do_that(client):
    sim.faults.extra_labs = 150
    sim.faults.oldest_first = True
    cid = connect_institution(client, BANNER)["connection_id"]
    assert [r for r in sim.faults.requests if "category=laboratory" in r and "_sort=-date" in r]
    assert connections.query_state(cid)["Observation?category=laboratory"]["no_sort"] == 0


def test_a_server_refusing_the_sort_is_asked_plainly_and_that_is_remembered(client):
    sim.faults.reject_sort = True
    cid = connect_institution(client, BANNER)["connection_id"]
    run = client.get(f"/api/connections/{cid}").json()["last_run"]
    assert run["status"] == "success" and run["stats"]["queries"]["Observation?category=laboratory"] == "ok"
    assert connections.query_state(cid)["Observation?category=laboratory"]["no_sort"] == 1
    sim.faults.requests.clear()
    _sync(client, cid)
    assert not [r for r in sim.faults.requests if "_sort" in r and "category=laboratory" in r]


def test_a_failure_with_a_sort_doesnt_get_blamed_on_the_sort(client):
    sim.faults.answers.append({"type": "Encounter", "status": 500})
    cid = connect_institution(client, BANNER)["connection_id"]
    assert connections.query_state(cid)["Encounter"]["no_sort"] == 0


def test_running_out_of_time_saves_the_place_and_the_rest_comes_at_the_next_sync(client, monkeypatch):
    sim.faults.extra_labs = 400
    monkeypatch.setattr(smart, "MAX_RESOURCES_PER_SYNC", 150)
    cid = connect_institution(client, BANNER)["connection_id"]
    run = client.get(f"/api/connections/{cid}").json()["last_run"]
    assert run["status"] == "partial" and run["stats"]["more_to_fetch"]
    assert any("still downloading" in w for w in run["stats"]["warnings"])
    conn = connections.get(cid)
    assert conn["next_retry_at"] and conn["next_retry_at"] - time.time() < 3600
    assert any(s["status"] == "paused" for s in connections.query_state(cid).values())

    monkeypatch.setattr(smart, "MAX_RESOURCES_PER_SYNC", 100_000)
    res = _sync(client, cid)
    assert res["status"] == "success", res
    assert len(_labs(client)) > 400


def test_queries_the_last_sync_didnt_reach_go_first(client):
    fhir = smart.FhirClient("https://fhir.example.org/r4", {"access_token": "t"}, "p")
    runs = fhir._plan({"Goal": {"status": "paused"}, "Device": {"status": "ok"}}, smart.FetchResult())
    assert runs[0].label == "Goal"


def test_query_status_is_visible_per_query(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    sim.faults.answers.append({"type": "Goal", "status": 403})
    _sync(client, cid)
    conn = client.get(f"/api/connections/{cid}").json()
    stats = conn["last_run"]["stats"]
    assert stats["queries"]["Goal"] == "forbidden" and stats["queries"]["Observation?category=laboratory"] == "ok"
    assert stats["counts"]["Observation?category=laboratory"] > 0


# ---------------------------------------------------------------------------
# Note bodies (F129)
# ---------------------------------------------------------------------------

def test_the_note_limit_doesnt_blank_bodies_already_stored(client, monkeypatch):
    cid = _with_notes_by_link(client)
    notes = client.get("/api/records", params={"category": "notes", "limit": 500, "dedupe": "false"}).json()["items"]
    with_text = [n for n in notes if n["narrative"]]
    assert with_text
    monkeypatch.setattr(sync_svc, "MAX_NOTE_FETCHES", 0)
    res = _sync(client, cid)
    assert res["notes_fetched"] == 0
    after = client.get("/api/records", params={"category": "notes", "limit": 500, "dedupe": "false"}).json()["items"]
    assert {n["id"]: n["narrative"] for n in after} == {n["id"]: n["narrative"] for n in notes}


def test_note_text_past_the_limit_comes_with_a_later_sync_newest_first(client, monkeypatch):
    cid = _with_notes_by_link(client)
    with db() as c:
        c.execute("UPDATE clinical_records SET narrative = NULL WHERE connection_id = ? AND category = 'notes'", (cid,))
    rows = [n for n in client.get("/api/records", params={"category": "notes", "limit": 500, "dedupe": "false"}).json()["items"] if not n["narrative"]]
    assert len(rows) >= 2
    monkeypatch.setattr(sync_svc, "MAX_NOTE_FETCHES", 1)
    res = _sync(client, cid)
    assert res["notes_fetched"] == 1 and res["notes_pending"] == len(rows) - 1 and res["more_to_fetch"]
    now = {n["id"]: n for n in client.get("/api/records", params={"category": "notes", "limit": 500, "dedupe": "false"}).json()["items"]}
    newest = max(rows, key=lambda r: r["effective_at"] or "")
    assert now[newest["id"]]["narrative"]


# ---------------------------------------------------------------------------
# A fake server, for what the simulator can't do: transports failing
# ---------------------------------------------------------------------------

BASE_URL = "https://fake.example/fhir"
TOKEN_URL = "https://fake.example/oauth/token"


class Fake:
    def __init__(self):
        self.calls = []
        self.handler = None
        self.resources = {}

    def __call__(self, request):
        self.calls.append(str(request.url))
        if self.handler:
            answer = self.handler(request)
            if answer is not None:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        if request.url.path.startswith("/fhir/Patient/"):
            return httpx.Response(200, json={"resourceType": "Patient", "id": "p1", "name": [{"family": "Doe", "given": ["Jo"]}], "birthDate": "1980-01-01"})
        rtype = request.url.path.split("/")[2]
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": [{"resource": r} for r in self.resources.get(rtype, [])]})


@pytest.fixture
def fake(monkeypatch):
    server = Fake()
    monkeypatch.setattr(smart, "http_client", lambda simulated=False, timeout=30.0: httpx.AsyncClient(transport=httpx.MockTransport(server), timeout=timeout))
    return server


def _fake_source(client, **creds):
    profile = client.get("/api/connections").json()["profile_id"]
    return connections.create(profile, "ehr", "cerner", "Fake Hospital", mode="production", institution_id="x", fhir_base_url=BASE_URL,
                              patient_ref="p1", credentials={"access_token": "t0", "refresh_token": "r0", "token_endpoint": TOKEN_URL, "client_id": "cid", **creds})["id"]


def _run(cid):
    return asyncio.run(sync_svc.sync_connection(cid, "manual"))


def test_a_timeout_on_a_search_is_retried_then_kept_out_of_the_answer(client, fake, faults_and_no_waiting):
    cid = _fake_source(client)
    fake.resources["Condition"] = [{"resourceType": "Condition", "id": "c1", "code": {"text": "Asthma"},
                                    "category": [{"coding": [{"code": "problem-list-item"}]}]}]

    def handler(request):
        if request.url.path.endswith("/Condition") and "category" not in str(request.url):
            return httpx.ReadTimeout("slow")
    fake.handler = handler
    res = _run(cid)
    assert res["status"] == "partial" and res["queries"]["Condition"] == "error:network", res
    assert res["inserted"] == 1 and len(faults_and_no_waiting) == smart.RETRY_ATTEMPTS - 1
    assert len([c for c in fake.calls if c.endswith("/Condition") or "Condition?" in c and "category" not in c]) == smart.RETRY_ATTEMPTS


def test_a_timeout_reading_the_patient_keeps_the_rest_and_has_words(client, fake):
    cid = _fake_source(client)
    fake.handler = lambda r: httpx.ReadTimeout("") if "/Patient/" in str(r.url) else None
    fake.resources["Goal"] = [{"resourceType": "Goal", "id": "g1", "description": {"text": "Walk"}, "lifecycleStatus": "active"}]
    res = _run(cid)
    assert res["status"] == "partial" and res["inserted"] == 1, res


def test_nothing_reachable_at_all_is_temporary_and_in_words(client, fake):
    cid = _fake_source(client)
    fake.handler = lambda r: httpx.ConnectError("[Errno 8] nodename nor servname provided")
    res = _run(cid)
    conn = connections.get(cid)
    assert res["kind"] == "network" and conn["status"] == "active" and "Errno" not in conn["last_error"]
    assert "try again" in conn["last_error"]


def test_a_mid_paging_timeout_keeps_the_pages_and_saves_the_place(client, fake):
    cid = _fake_source(client)
    lab = {"resourceType": "Observation", "status": "final", "category": [{"coding": [{"code": "laboratory"}]}], "code": {"text": "Glucose"},
           "valueQuantity": {"value": 90, "unit": "mg/dL"}, "effectiveDateTime": "2024-01-01"}

    def handler(request):
        if "category=laboratory" not in str(request.url):
            return None
        if "page=2" in str(request.url):
            return httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"resourceType": "Bundle", "link": [{"relation": "next", "url": f"{BASE_URL}/Observation?category=laboratory&page=2"}],
                                         "entry": [{"resource": {**lab, "id": "l1"}}]})
    fake.handler = handler
    res = _run(cid)
    assert res["queries"]["Observation?category=laboratory"] == "error:network" and res["inserted"] == 1
    assert "page=2" in connections.query_state(cid)["Observation?category=laboratory"]["next_url"]


def test_relative_paging_links_are_followed(client, fake):
    fhir = smart.FhirClient(BASE_URL, {"access_token": "t"}, "p1")

    def handler(request):
        if "page=2" in str(request.url):
            return httpx.Response(200, json={"resourceType": "Bundle", "entry": [{"resource": {"resourceType": "Goal", "id": "2"}}]})
        return httpx.Response(200, json={"resourceType": "Bundle", "link": [{"relation": "next", "url": "/fhir/Goal?page=2"}],
                                         "entry": [{"resource": {"resourceType": "Goal", "id": "1"}}, "not a dict", {"resource": ["nor", "this"]}]})
    fake.handler = handler

    async def go():
        async with smart.http_client() as http:
            return await fhir.search_all(http, "Goal", {})
    found, status = asyncio.run(go())
    assert status == "ok" and [r["id"] for r in found] == ["1", "2"]


def test_a_next_link_going_round_in_circles_stops(client, fake):
    fhir = smart.FhirClient(BASE_URL, {"access_token": "t"}, "p1")
    fake.handler = lambda r: httpx.Response(200, json={"resourceType": "Bundle", "link": [{"relation": "next", "url": f"{BASE_URL}/Goal?page=2"}],
                                                       "entry": [{"resource": {"resourceType": "Goal", "id": "1"}}]})

    async def go():
        async with smart.http_client() as http:
            return await fhir.search_all(http, "Goal", {})
    _, status = asyncio.run(go())
    assert status == "truncated" and len(fake.calls) == 2


def test_binary_keeps_its_own_accept_header_and_renews_on_401(client, fake):
    seen = []

    def handler(request):
        if "/oauth/token" in str(request.url):
            return httpx.Response(200, json={"access_token": "t1", "refresh_token": "r1", "expires_in": 3600})
        seen.append((request.headers["authorization"], request.headers["accept"]))
        if request.headers["authorization"] == "Bearer t0":
            return httpx.Response(401, json={})
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="the note")
    fake.handler = handler
    saves = []

    async def on_refresh(creds):
        saves.append(creds["access_token"])
    fhir = smart.FhirClient(BASE_URL, {"access_token": "t0", "refresh_token": "r0", "token_endpoint": TOKEN_URL}, "p1", on_refresh=on_refresh)

    async def go():
        async with smart.http_client() as http:
            return await fhir.read_binary_text(http, f"{BASE_URL}/Binary/1")
    assert asyncio.run(go()) == ("text/plain", b"the note")
    assert saves == ["t1"] and seen[0][1] == "application/fhir+json, text/*"


def test_a_token_refused_straight_after_renewing_stops_without_a_storm(client, fake):
    calls = {"token": 0}

    def handler(request):
        if "/oauth/token" in str(request.url):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": f"t{calls['token']}", "refresh_token": "r"})
        return httpx.Response(401, json={})
    fake.handler = handler
    cid = _fake_source(client)
    res = _run(cid)
    assert res["kind"] == "auth" and calls["token"] == 1


def test_default_ports_and_scheme_downgrades_when_comparing_servers():
    assert smart._same_server("https://a.example:443/x", "https://a.example/y")
    assert smart._same_server("https://A.EXAMPLE/x", "https://a.example/y")
    assert not smart._same_server("http://a.example/x", "https://a.example/y")
    assert not smart._same_server("https://a.example:8443/x", "https://a.example/y")


def test_the_due_list_honours_a_retry_time(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    now = time.time()
    with db() as c:
        c.execute("UPDATE connections SET last_sync_at = ?, next_retry_at = ? WHERE id = ?", (now - 90000, now + 500, cid))
    assert cid not in {c["id"] for c in sync_svc.due_connections(24, 4)}     # told when to come back, even past the interval
    with db() as c:
        c.execute("UPDATE connections SET last_sync_at = ?, next_retry_at = ? WHERE id = ?", (now - 60, now - 5, cid))
    assert cid in {c["id"] for c in sync_svc.due_connections(24, 4)}         # and sooner than the interval


def test_wearable_vendor_trouble_is_temporary_and_respects_retry_after(client, monkeypatch):
    from app.connectors import oura
    cid = client.post("/api/connections/wearable", json={"provider": "oura", "mode": "simulated"}).json()["connection_id"]
    connections.update(cid, mode="live", credentials={"access_token": "t", "refresh_token": "r"})
    with db() as c:
        c.execute("UPDATE connections SET mode = 'live' WHERE id = ?", (cid,))

    async def down(*_):
        response = httpx.Response(503, headers={"Retry-After": "900"}, request=httpx.Request("GET", "https://api.ouraring.com/x"))
        raise httpx.HTTPStatusError("boom", request=response.request, response=response)
    monkeypatch.setattr(oura, "fetch_live", down)
    res = _sync(client, cid)
    conn = connections.get(cid)
    assert res["kind"] == "network" and "try again" in conn["last_error"] and conn["status"] == "active"
    assert conn["next_retry_at"] >= time.time() + 800


def test_sync_now_answers_while_a_long_sync_carries_on(client, monkeypatch):
    from app.api import connections as api
    cid = connect_institution(client, BANNER)["connection_id"]
    monkeypatch.setattr(api, "SYNC_NOW_WAIT", 0.0)
    real = sync_svc.sync_connection

    async def slow(*args):
        await asyncio.sleep(0.2)
        return await real(*args)
    monkeypatch.setattr(sync_svc, "sync_connection", slow)
    assert _sync(client, cid)["status"] == "running"


def test_read_helper_stays_available():
    with read() as c:
        assert c.execute("SELECT COUNT(*) FROM clinical_records_removed").fetchone()[0] == 0
