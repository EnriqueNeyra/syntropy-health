"""Bringing in a record file again: the same file, a corrected one, and someone else's that looks like it."""

import json

import pytest

from app.connectors import fhir_normalize
from app.core import data_fixes
from app.core.db import db
from app.services import imports, sync
from app.store import connections, records

PATIENT = {"resourceType": "Patient", "id": "1", "birthDate": "1980-01-01", "name": [{"family": "Doe", "given": ["Jo"]}],
           "identifier": [{"system": "urn:oid:1.2.3", "value": "MRN-1"}]}
ENCOUNTER = {"resourceType": "Encounter", "id": "e1", "status": "finished", "class": {"code": "AMB"}, "type": [{"text": "Office visit"}],
             "period": {"start": "2024-03-01T10:15:00Z"}}
NOTE = {"resourceType": "DocumentReference", "id": "d1", "status": "current", "type": {"text": "Progress note"}, "date": "2024-03-01",
        "content": [{"attachment": {"contentType": "text/plain", "data": "bm90ZSB0ZXh0"}}]}
TEAM = {"resourceType": "CareTeam", "id": "t1", "status": "active", "participant": [{"member": {"display": "Dr. Lee"}, "role": [{"text": "PCP"}]}]}
PLAN = {"resourceType": "CarePlan", "id": "p1", "status": "active", "intent": "plan", "title": "Diabetes plan", "period": {"start": "2024-01-01"}}
LAB = {"resourceType": "Observation", "id": "o1", "status": "final", "category": [{"coding": [{"code": "laboratory"}]}],
       "code": {"coding": [{"system": "http://loinc.org", "code": "2345-7", "display": "Glucose"}]}, "effectiveDateTime": "2024-03-01T08:00:00Z",
       "valueQuantity": {"value": 400, "unit": "mg/dL"}}


def bundle(*resources, **extra):
    return {"resourceType": "Bundle", "type": "collection", "entry": [{"resource": r} for r in resources], **extra}


def upload(client, data, name="records.json"):
    r = client.post("/api/imports/fhir", files={"file": (name, json.dumps(data).encode(), "application/json")})
    assert r.status_code == 200, r.text
    return r.json()


def imported(client):
    return [c for c in client.get("/api/connections").json()["connections"] if c["kind"] == "import"]


def counts(client):
    return client.get("/api/summary").json()["counts"]


def test_the_same_file_again_changes_nothing_and_says_so(client):
    data = bundle(PATIENT, ENCOUNTER, NOTE, TEAM, PLAN, LAB)
    first = upload(client, data)
    assert first["inserted"] == 5
    again = upload(client, data, "records (1).json")
    assert again["unchanged"] and again["connection_id"] == first["connection_id"]
    assert again["inserted"] == 0 and again["updated"] == 0 and again["records"] == 5
    assert len(imported(client)) == 1


def test_a_file_with_no_patient_identifier_uploaded_twice_is_still_recognised_exactly(client):
    data = bundle(ENCOUNTER, LAB)
    assert upload(client, data)["connection_id"] == upload(client, data)["connection_id"]


def test_the_same_file_for_another_person_is_theirs(client):
    data = bundle(PATIENT, LAB)
    first = upload(client, data)
    other = client.post("/api/profiles", json={"name": "Sam", "relationship": "child"}).json()["id"]
    r = client.post("/api/imports/fhir", files={"file": ("records.json", json.dumps(data).encode(), "application/json")},
                    data={"profile_id": other}).json()
    assert r["connection_id"] != first["connection_id"] and not r.get("unchanged")


def test_a_newer_file_from_the_same_place_updates_in_place(client):
    first = upload(client, bundle(PATIENT, ENCOUNTER, LAB))
    second = upload(client, bundle(PATIENT, ENCOUNTER, {**LAB, "valueQuantity": {"value": 90, "unit": "mg/dL"}, "status": "corrected"},
                                   {**ENCOUNTER, "id": "e2", "period": {"start": "2024-05-01T09:00:00Z"}}), "newer.json")
    assert second["connection_id"] == first["connection_id"] and second["matched_earlier"]
    assert second["inserted"] == 1 and second["updated"] == 1
    assert len(imported(client)) == 1
    labs = client.get("/api/records", params={"category": "labs", "dedupe": "false"}).json()["items"]
    assert [(i["value_num"], i["status"]) for i in labs] == [(90, "corrected")]


def test_what_a_newer_file_doesnt_mention_stays(client):
    upload(client, bundle(PATIENT, ENCOUNTER, LAB))
    upload(client, bundle(PATIENT, {**ENCOUNTER, "id": "e2", "period": {"start": "2024-05-01T09:00:00Z"}}))
    assert counts(client)["labs"] == 1 and counts(client)["encounters"] == 2


def test_a_file_saying_a_result_was_wrong_takes_it_out_and_it_can_be_put_back(client):
    first = upload(client, bundle(PATIENT, LAB))
    third = upload(client, bundle(PATIENT, {**LAB, "status": "entered-in-error"}))
    assert third["removed"] == 1 and third["connection_id"] == first["connection_id"]
    assert not client.get("/api/records", params={"category": "labs"}).json()["items"]
    cid = first["connection_id"]
    item = client.get(f"/api/connections/{cid}/removed").json()["items"][0]
    assert item["reason"] == "retracted"
    assert client.post(f"/api/connections/{cid}/removed/{item['id']}/restore").status_code == 200
    assert client.get("/api/records", params={"category": "labs"}).json()["items"]


@pytest.mark.parametrize("change", [
    {"id": "1", "identifier": []},                                                         # only a bare id: "1" could be anyone
    {"identifier": [{"value": "MRN-1"}]},                                                  # a number with no issuer
    {"identifier": [{"system": "urn:oid:9.9.9", "value": "MRN-1"}]},                       # another hospital's number
    {"birthDate": "1975-05-05"},                                                           # same number, different person
])
def test_a_look_alike_file_gets_its_own_source(client, change):
    first = upload(client, bundle(PATIENT, LAB))
    second = upload(client, bundle({**PATIENT, **change}, {**LAB, "id": "o1", "valueQuantity": {"value": 111, "unit": "mg/dL"}}))
    assert second["connection_id"] != first["connection_id"]
    labs = client.get("/api/records", params={"category": "labs", "dedupe": "false"}).json()["items"]
    assert sorted(i["value_num"] for i in labs) == [111, 400]


def test_a_file_with_two_people_is_never_matched(client):
    both = bundle(PATIENT, {**PATIENT, "id": "2", "identifier": [{"system": "urn:oid:1.2.3", "value": "MRN-2"}]}, LAB)
    assert imports._source_keys(both["entry"] and [e["resource"] for e in both["entry"]], []) == []


def test_a_bundle_identifier_counts_with_the_birth_date(client):
    plain = {**PATIENT, "identifier": []}
    first = upload(client, bundle(plain, LAB, identifier={"system": "urn:ietf:rfc:3986", "value": "urn:uuid:abc"}))
    second = upload(client, bundle(plain, {**LAB, "valueQuantity": {"value": 99, "unit": "mg/dL"}},
                                   identifier={"system": "urn:ietf:rfc:3986", "value": "urn:uuid:abc"}))
    assert first["connection_id"] == second["connection_id"]


def test_the_same_visit_and_note_collapse_between_unrelated_files(client):
    upload(client, bundle({**PATIENT, "identifier": []}, ENCOUNTER, NOTE, TEAM, PLAN), "a.json")
    upload(client, bundle({**PATIENT, "identifier": []}, {**ENCOUNTER, "id": "x"}, {**NOTE, "id": "y"}, {**TEAM, "id": "z"}, {**PLAN, "id": "w"}), "b.json")
    assert len(imported(client)) == 2
    seen = counts(client)
    assert (seen["encounters"], seen["notes"], seen["care_team"], seen["care_plans"]) == (1, 1, 1, 1)


def test_two_real_visits_on_one_day_stay_two():
    a = fhir_normalize.normalize_encounter(ENCOUNTER)
    b = fhir_normalize.normalize_encounter({**ENCOUNTER, "period": {"start": "2024-03-01T14:30:00Z"}})
    assert a["dedup_key"] != b["dedup_key"]
    dated = fhir_normalize.normalize_encounter({**ENCOUNTER, "period": {"start": "2024-03-01"}})
    assert dated["dedup_key"] is None
    known = fhir_normalize.normalize_encounter({**ENCOUNTER, "identifier": [{"system": "urn:h", "value": "V1"}]})
    assert known["dedup_key"] == "enc|urn:h|V1|office visit|2024-03-01T10:15"
    bare = fhir_normalize.normalize_encounter({**ENCOUNTER, "identifier": [{"value": "V1"}]})
    assert bare["dedup_key"] == a["dedup_key"]


def test_notes_differ_by_their_text_and_ignore_their_links():
    a = fhir_normalize.normalize_document_reference(NOTE)
    other = {**NOTE, "content": [{"attachment": {"contentType": "text/plain", "data": "b3RoZXI="}}]}
    assert fhir_normalize.normalize_document_reference(other)["dedup_key"] != a["dedup_key"]
    linked = {**NOTE, "content": [{"attachment": {"contentType": "text/plain", "url": "Binary/1"}}]}
    assert fhir_normalize.normalize_document_reference(linked)["dedup_key"] is None
    master = {**linked, "masterIdentifier": {"system": "urn:h", "value": "N1"}}
    assert fhir_normalize.normalize_document_reference(master)["dedup_key"] == "doc|urn:h|N1"


def test_a_plan_with_nothing_to_tell_it_by_isnt_collapsed():
    assert fhir_normalize.normalize_care_plan({"resourceType": "CarePlan", "id": "1", "status": "active"})["dedup_key"] is None


def _apple_export(profile, resources):
    return sync.import_fhir_resources(profile, "Apple Health Records", resources, provider="apple_health_records",
                                      reuse=next((c for c in connections.list_for_profile(profile, "import")
                                                  if c["provider"] == "apple_health_records"), None))


def test_each_apple_health_export_updates_one_source(client):
    profile = client.get("/api/connections").json()["profile_id"]
    first = _apple_export(profile, [PATIENT, LAB, ENCOUNTER])
    again = _apple_export(profile, [PATIENT, LAB, ENCOUNTER])
    assert first["connection_id"] == again["connection_id"] and again["inserted"] == 0


def _old_apple_copies(profile):
    ids = []
    for i, extra in enumerate((ENCOUNTER, {**ENCOUNTER, "id": "e2", "period": {"start": "2024-06-01T09:00:00Z"}})):
        conn = connections.create(profile, "import", "apple_health_records", "Apple Health Records", mode="live")
        res = sync.import_fhir_resources(profile, "Apple Health Records", [PATIENT, LAB, extra], provider="apple_health_records", reuse=conn)
        with db() as c:
            c.execute("UPDATE connections SET created_at = ? WHERE id = ?", (1000 + i, res["connection_id"]))
        ids.append(res["connection_id"])
    return ids


def _run_fix(name, step):
    cursor = None
    for _ in range(100):
        with db() as conn:
            cursor = step(conn, cursor, 2)
        if cursor is None:
            return
    pytest.fail(f"{name} never finished")


def test_old_apple_health_copies_fold_into_the_newest(client):
    profile = client.get("/api/connections").json()["profile_id"]
    old, new = _old_apple_copies(profile)
    other = client.post("/api/profiles", json={"name": "Sam", "relationship": "child"}).json()["id"]
    theirs = sync.import_fhir_resources(other, "Apple Health Records", [PATIENT, LAB], provider="apple_health_records")
    assert records.list_records(profile, "labs", dedupe=False)["total"] == 2

    _run_fix("fold", imports._fold_apple_health_records)
    assert connections.get(old) is None and connections.get(new)
    assert connections.get(theirs["connection_id"])                 # someone else's is left alone
    assert records.list_records(profile, "labs", dedupe=False)["total"] == 1
    encounters = records.list_records(profile, "encounters", dedupe=False)["items"]
    assert {e["resource_id"] for e in encounters} == {"e1", "e2"}    # what only the older export had stays
    assert {e["id"] for e in encounters} == {records.record_id(new, "Encounter", r) for r in ("e1", "e2")}
    assert client.get(f"/api/records/{encounters[0]['id']}").status_code == 200

    before = records.list_records(profile, "encounters", dedupe=False)["total"]
    _run_fix("fold", imports._fold_apple_health_records)            # safe to run again
    assert records.list_records(profile, "encounters", dedupe=False)["total"] == before


def test_old_rows_get_their_dedup_keys(client):
    upload(client, bundle(PATIENT, ENCOUNTER, NOTE, TEAM, PLAN, LAB))
    with db() as c:
        c.execute("UPDATE clinical_records SET dedup_key = NULL WHERE category IN ('encounters','notes','care_team','care_plans')")
    _run_fix("keys", imports._fill_missing_dedup_keys)
    from app.core.db import read
    with read() as c:
        rows = c.execute("SELECT category, dedup_key FROM clinical_records").fetchall()
    assert all(r["dedup_key"] for r in rows)


def test_both_fixes_are_registered():
    assert imports._fold_apple_health_records in data_fixes._fixes.values()
    assert imports._fill_missing_dedup_keys in data_fixes._fixes.values()


# ---------------------------------------------------------------------------
# A key says it's the same record, so it has to be the same record (F039)
# ---------------------------------------------------------------------------

def _visit(i, day, **extra):
    return {"resourceType": "Encounter", "id": f"e{i}", "status": "finished", "class": {"code": "AMB"},
            "identifier": [{"system": "urn:hospital:account", "value": "HAR-1"}], "type": [{"text": "Physical therapy"}],
            "period": {"start": f"2024-01-{day:02d}T09:00:00Z"}, **extra}


def test_visits_sharing_an_account_number_all_stay_visible(client):
    profile = client.get("/api/connections").json()["profile_id"]
    upload(client, bundle(*[_visit(i, i + 1) for i in range(6)]))
    assert records.list_records(profile, "encounters", limit=50)["total"] == 6
    assert counts(client)["encounters"] == 6


def test_the_same_visit_from_a_file_and_elsewhere_is_still_one(client):
    profile = client.get("/api/connections").json()["profile_id"]
    upload(client, bundle(_visit(1, 1)), "a.json")
    upload(client, bundle({**_visit(2, 1), "status": "in-progress"}), "b.json")
    assert records.list_records(profile, "encounters", limit=50)["total"] == 1


def test_a_visit_with_an_identifier_but_no_time_isnt_collapsed():
    dated = fhir_normalize.normalize_encounter({**_visit(1, 1), "period": {"start": "2024-01-01"}})
    undated = fhir_normalize.normalize_encounter({**_visit(1, 1), "period": {}})
    assert dated["dedup_key"] is None and undated["dedup_key"] is None


def test_two_kinds_of_visit_at_one_time_on_one_account_stay_two():
    a = fhir_normalize.normalize_encounter(_visit(1, 1))
    b = fhir_normalize.normalize_encounter({**_visit(2, 1), "type": [{"text": "Occupational therapy"}]})
    assert a["dedup_key"] != b["dedup_key"]


def _plan(i, description, **extra):
    return {"resourceType": "CarePlan", "id": f"p{i}", "status": "active", "intent": "plan", "period": {"start": "2024-01-01"},
            "description": description, "category": [{"text": "Assessment and Plan"}], **extra}


def test_different_plans_with_the_same_title_and_date_both_stay_visible(client):
    profile = client.get("/api/connections").json()["profile_id"]
    upload(client, bundle(_plan(1, "Physical therapy three times a week"), _plan(2, "Stop metformin and recheck kidney function")))
    shown = records.list_records(profile, "care_plans", limit=50)
    assert shown["total"] == 2
    assert {r["narrative"] for r in shown["items"]} == {"Physical therapy three times a week", "Stop metformin and recheck kidney function"}


def test_plans_that_differ_only_in_their_activities_both_stay_visible():
    def activity(text):
        return {"resourceType": "CarePlan", "id": "p", "status": "active", "title": "Plan", "period": {"start": "2024-01-01"},
                "activity": [{"detail": {"description": text}}]}
    a = fhir_normalize.normalize_care_plan(activity("Walk daily"))
    b = fhir_normalize.normalize_care_plan(activity("Stop metformin"))
    assert a["dedup_key"] != b["dedup_key"]


def test_the_same_plan_from_two_files_is_still_one(client):
    profile = client.get("/api/connections").json()["profile_id"]
    upload(client, bundle(_plan(1, "Walk daily")), "a.json")
    upload(client, bundle(_plan(2, "Walk daily")), "b.json")
    assert records.list_records(profile, "care_plans", limit=50)["total"] == 1


def test_teams_with_the_same_people_under_different_names_stay_two():
    def team(name):
        return {"resourceType": "CareTeam", "id": name, "status": "active", "name": name,
                "participant": [{"member": {"display": "Dr. Ada"}}]}
    assert fhir_normalize.normalize_care_team(team("Diabetes"))["dedup_key"] != fhir_normalize.normalize_care_team(team("Heart"))["dedup_key"]


def test_filling_in_keys_for_old_rows_doesnt_hide_anything(client):
    profile = client.get("/api/connections").json()["profile_id"]
    upload(client, bundle(*[_visit(i, i + 1) for i in range(3)], _plan(1, "Walk daily"), _plan(2, "Stop metformin")))
    with db() as c:
        c.execute("UPDATE clinical_records SET dedup_key = NULL WHERE category IN ('encounters', 'care_plans')")
    _run_fix("keys-again", imports._fill_missing_dedup_keys)
    assert records.list_records(profile, "encounters", limit=50)["total"] == 3
    assert records.list_records(profile, "care_plans", limit=50)["total"] == 2
