"""What a person says about their record: beside what the source says, never over it, and surviving a re-sync."""

from __future__ import annotations

import csv
import io
import json
import time
import zipfile
from pathlib import Path

import pytest

import app
from app.core.db import db
from app.mcp_server import t_health_summary
from app.services import exports, sync
from app.store import connections, profiles, record_files, record_overrides, records


def _bundle(status="active", when="2025-05-01"):
    return [
        {"resourceType": "Patient", "id": "p", "name": [{"text": "Alex Rivera"}], "birthDate": "1980-01-01"},
        {"resourceType": "MedicationRequest", "id": "m1", "status": status, "intent": "order", "authoredOn": when,
         "medicationCodeableConcept": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "197361", "display": "Amlodipine"}], "text": "Amlodipine"}},
        {"resourceType": "Condition", "id": "c1", "clinicalStatus": {"coding": [{"code": "active"}]}, "code": {"text": "Gout"}},
        {"resourceType": "AllergyIntolerance", "id": "a1", "clinicalStatus": {"coding": [{"code": "active"}]}, "code": {"text": "Latex"}},
    ]


@pytest.fixture()
def pid(client):
    profile = profiles.list_profiles()[0]["id"]
    sync.import_fhir_resources(profile, "Clinic", _bundle(when=time.strftime("%Y-%m-%d")))
    return profile


def _one(pid, category):
    return records.list_records(pid, category)["items"][0]


def test_not_taking_moves_a_medication_and_the_source_keeps_saying_what_it_said(client, pid):
    med = _one(pid, "medications")
    raw_before = records.get_record(med["id"])["raw"]
    res = client.put(f"/api/records/{med['id']}/override", json={"state": "not_taking", "note": "Stopped in March"})
    assert res.status_code == 200, res.text
    s = records.summary(pid)
    assert not s["active_medications"]
    assert s["medications_corrected"][0]["override"]["text"] == "Not taking: Stopped in March"
    assert s["medications_corrected"][0]["status"] == "active"
    assert records.get_record(med["id"])["raw"] == raw_before


def test_it_survives_a_re_sync_and_covers_another_source(client, pid):
    med = _one(pid, "medications")
    client.put(f"/api/records/{med['id']}/override", json={"state": "not_taking"})
    sync.import_fhir_resources(pid, "Clinic", _bundle(when=time.strftime("%Y-%m-%d")))
    other = [dict(r, id=f"x{r['id']}") for r in _bundle(when=time.strftime("%Y-%m-%d"))[1:]]
    sync.import_fhir_resources(pid, "Other hospital", other)
    assert records.summary(pid)["medications_corrected"]
    assert not records.summary(pid)["active_medications"]


def test_a_newer_prescription_is_not_filed_away_on_old_words(client, pid):
    med = _one(pid, "medications")
    client.put(f"/api/records/{med['id']}/override", json={"state": "not_taking"})
    with_date = dict(_one(pid, "medications"), effective_at="2999-01-01")
    tier, reason = records.medication_tier(with_date)
    assert tier != "corrected" and "marked not taking" in reason


def test_taking_differently_stays_current_with_the_persons_words(client, pid):
    med = _one(pid, "medications")
    client.put(f"/api/records/{med['id']}/override", json={"state": "taking_differently", "note": "Half a tablet"})
    s = records.summary(pid)
    assert s["active_medications"][0]["override"]["text"] == "Taking differently: Half a tablet"
    assert not s["medications_corrected"]


def test_wrong_takes_a_condition_and_an_allergy_out_of_the_current_lists_and_leaves_them_visible(client, pid):
    for category in ("conditions", "allergies"):
        assert client.put(f"/api/records/{_one(pid, category)['id']}/override", json={"state": "wrong", "note": "Not mine"}).status_code == 200
    s = records.summary(pid)
    assert not s["active_conditions"] and not s["allergies"]
    assert s["conditions_corrected"][0]["title"] == "Gout" and s["allergies_corrected"][0]["title"] == "Latex"
    assert records.list_records(pid, "conditions")["total"] == 1


def test_only_medications_take_the_medication_choices(client, pid):
    res = client.put(f"/api/records/{_one(pid, 'conditions')['id']}/override", json={"state": "not_taking"})
    assert res.status_code == 400
    assert client.put(f"/api/records/{_one(pid, 'medications')['id']}/override", json={}).status_code == 400
    assert client.put(f"/api/records/{_one(pid, 'medications')['id']}/override", json={"state": "made_up"}).status_code == 400


def test_removing_it_puts_things_back(client, pid):
    med = _one(pid, "medications")
    client.put(f"/api/records/{med['id']}/override", json={"state": "wrong"})
    assert client.delete(f"/api/records/{med['id']}/override").status_code == 200
    assert records.summary(pid)["active_medications"]


def test_someone_who_can_only_view_cant_change_it(client, pid, monkeypatch):
    from app.api import records as api_records

    seen = []
    monkeypatch.setattr(api_records, "check_access", lambda profile, level=None: seen.append(level))
    client.put(f"/api/records/{_one(pid, 'medications')['id']}/override", json={"state": "wrong"})
    assert seen == ["manage"]


def test_ai_apps_and_the_assistant_see_the_persons_words_labelled(client, pid):
    client.put(f"/api/records/{_one(pid, 'medications')['id']}/override", json={"state": "not_taking", "note": "Stopped in March"})
    client.put(f"/api/records/{_one(pid, 'allergies')['id']}/override", json={"note": "A rash, years ago"})
    s = t_health_summary({"profile_id": pid})
    assert s["corrected_by_the_person"][0]["person_says"] == "Not taking: Stopped in March"
    assert not s["active_medications"]
    assert s["allergies"][0]["person_says"] == "A rash, years ago"
    from app.services import ai
    context = ai.summary_context(pid, 50_000)
    assert "corrected_by_the_person" in context and "Stopped in March" in context


def test_exports_show_both(client, pid):
    client.put(f"/api/records/{_one(pid, 'medications')['id']}/override", json={"state": "not_taking", "note": "Stopped"})
    rows = list(csv.DictReader(io.StringIO(exports.csv_bytes("records", pid).decode("utf-8-sig"))))
    med = next(r for r in rows if r["category"] == "medications")
    assert med["patient_reported_correction"] == "Not taking: Stopped" and med["status"] == "active"
    bundle = json.loads(b"".join(exports.bundle_stream(pid)))
    res = next(e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "MedicationRequest")
    assert res["status"] == "active" and "Patient-reported: Not taking: Stopped" in res["note"][-1]["text"]
    ips = exports.patient_summary(profiles.get_profile(pid))
    assert "Patient-reported: Not taking: Stopped" in json.dumps(ips)


def test_an_archive_carries_them_to_another_copy(client, pid):
    client.put(f"/api/records/{_one(pid, 'medications')['id']}/override", json={"state": "not_taking"})
    path = exports.build_archive(profiles.get_profile(pid), None)
    with zipfile.ZipFile(path) as z:
        assert json.loads(z.read("corrections.json"))[0]["state"] == "not_taking"
    other = profiles.create_profile("Other", "other")["id"]
    assert exports.import_archive(other, None, path)["corrections"] == 1
    assert record_overrides.for_profile(other)


def test_deleting_the_person_or_their_data_deletes_what_they_said(client, pid):
    client.put(f"/api/records/{_one(pid, 'medications')['id']}/override", json={"state": "wrong", "note": "PRIVATE"})
    profiles.delete_all_data(pid)
    assert not record_overrides.for_profile(pid)
    other = profiles.create_profile("Other", "other")["id"]
    sync.import_fhir_resources(other, "Clinic", _bundle())
    client.put(f"/api/records/{_one(other, 'medications')['id']}/override", json={"state": "wrong"})
    profiles.delete_profile(other)
    assert not record_overrides.for_profile(other)


@pytest.mark.parametrize("kind, category, section", [("medication", "medications", "active_medications"), ("supplement", "medications", "active_medications"),
                                                     ("allergy", "allergies", "allergies")])
def test_what_the_person_adds_reaches_the_summary_labelled_patient_reported(client, pid, kind, category, section):
    res = client.post("/api/records/self-reported", json={"profile_id": pid, "kind": kind, "name": "Fish oil", "how": "One a day", "note": "From the pharmacy"})
    assert res.status_code == 200, res.text
    rec = records.get_record(res.json()["record_id"])
    assert rec["source_name"] == "Patient-reported" and rec["category"] == category
    assert any(r["title"] == "Fish oil" and r["source_name"] == "Patient-reported" for r in records.summary(pid)[section])
    assert client.get(f"/api/records/{rec['id']}").json()["editable"] is True
    assert client.delete(f"/api/labs/{rec['id']}").status_code == 200
    assert connections.get(rec["connection_id"])["provider"] == "manual"


def test_what_an_export_adds_comes_back_out_when_the_file_returns(client, pid):
    client.put(f"/api/records/{_one(pid, 'medications')['id']}/override", json={"state": "not_taking", "note": "Stopped"})
    bundle = json.loads(b"".join(exports.bundle_stream(pid)))
    exported = next(e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "MedicationRequest")
    assert exported["note"]
    clean, origin, _ = sync._take_source_tag(exported)
    assert origin and "note" not in clean and clean == records.get_record(_one(pid, "medications")["id"])["raw"]


def test_insights_leave_out_what_the_person_says_isnt_theirs(client, pid):
    from app.services import insights
    med = _one(pid, "medications")
    assert any(m["name"] == "Amlodipine" for m in insights.medication_timeline(pid))
    client.put(f"/api/records/{med['id']}/override", json={"state": "wrong"})
    assert not insights.medication_timeline(pid)
    res = sync.import_fhir_resources(pid, "Labs", [{"resourceType": "Observation", "id": "l1", "status": "final", "category": [{"coding": [{"code": "laboratory"}]}],
                                                    "code": {"coding": [{"system": "http://loinc.org", "code": "2345-7"}], "text": "Glucose"}, "effectiveDateTime": "2025-05-01",
                                                    "valueQuantity": {"value": 200, "unit": "mg/dL"}, "interpretation": [{"coding": [{"code": "H"}]}]}])
    assert res["inserted"] == 1
    assert records.flagged_labs(pid)
    client.put(f"/api/records/{_one(pid, 'labs')['id']}/override", json={"state": "wrong"})
    assert not records.flagged_labs(pid)
    assert records.summary(pid)["flagged_labs"][0]["override"]["words"] == "Wrong or not this person"


def _other_on_record(profile, when):
    with db() as conn:
        rec = conn.execute("SELECT * FROM clinical_records WHERE profile_id = ? AND category = 'medications' LIMIT 1", (profile,)).fetchone()
        conn.execute("UPDATE clinical_records SET category = 'other', title = 'A kind the app has no heading for', effective_at = ? WHERE id = ?", (when, rec["id"]))
        return rec["id"]


def test_prepare_for_a_visit_lists_what_the_person_said_is_wrong_apart_from_the_facts(client, pid):
    day = time.strftime("%Y-%m-%d")
    allergy, condition = _one(pid, "allergies"), _one(pid, "conditions")
    with db() as conn:
        conn.execute("UPDATE clinical_records SET effective_at = ? WHERE id IN (?, ?)", (day, allergy["id"], condition["id"]))
    client.put(f"/api/records/{allergy['id']}/override", json={"state": "wrong"})
    client.put(f"/api/records/{condition['id']}/override", json={"note": "Only the left foot"})
    prep = client.get("/api/visit-prep", params={"profile": pid, "since": day}).json()
    assert not prep["allergies"] and [a["title"] for a in prep["said_wrong"]] == [allergy["title"]]
    assert prep["diagnoses"][0]["override"]["text"] == "Only the left foot"


def test_the_other_category_reaches_prepare_and_the_timeline(client, pid):
    day = time.strftime("%Y-%m-%d")
    rid = _other_on_record(pid, day)
    prep = client.get("/api/visit-prep", params={"profile": pid, "since": day}).json()
    assert [r["id"] for r in prep["other"]] == [rid]
    for params in ({}, {"categories": "other"}):
        assert rid in [i["id"] for i in client.get("/api/timeline", params={"profile": pid, **params}).json()["items"]]


def test_records_and_the_timeline_narrow_to_corrections_and_documents(client, pid):
    condition = _one(pid, "conditions")
    assert client.get("/api/record-filters", params={"profile": pid}).json()["has"] == []
    client.put(f"/api/records/{condition['id']}/override", json={"note": "Only the left foot"})
    assert client.get("/api/record-filters", params={"profile": pid}).json()["has"] == ["corrected"]
    assert [r["id"] for r in client.get("/api/records", params={"profile": pid, "has": "corrected"}).json()["items"]] == [condition["id"]]
    with db() as conn:
        conn.execute("UPDATE clinical_records SET effective_at = '2025-01-01' WHERE id = ?", (condition["id"],))
    shown = client.get("/api/timeline", params={"profile": pid, "categories": "corrected"}).json()["items"]
    assert [r["id"] for r in shown] == [condition["id"]] and shown[0]["override"]["text"] == "Only the left foot"
    allergy = _one(pid, "allergies")
    record_files.save(pid, allergy["connection_id"], allergy["resource_type"], allergy["resource_id"], "letter.pdf", "application/pdf", b"%PDF-1.4 a letter")
    assert "documents" in client.get("/api/record-filters", params={"profile": pid}).json()["has"]
    assert [r["id"] for r in client.get("/api/records", params={"profile": pid, "has": "documents"}).json()["items"]] == [allergy["id"]]


def test_an_entry_marked_wrong_isnt_new_and_a_document_on_a_known_record_is(client, pid):
    mark = time.time() - 60
    with db() as conn:
        conn.execute("UPDATE clinical_records SET first_seen_at = ? WHERE profile_id = ?", (time.time() - 3600, pid))
        conn.execute("UPDATE connections SET created_at = ? WHERE profile_id = ?", (time.time() - 3600, pid))
    allergy, condition = _one(pid, "allergies"), _one(pid, "conditions")
    with db() as conn:
        conn.execute("UPDATE clinical_records SET first_seen_at = ? WHERE id IN (?, ?)", (time.time(), allergy["id"], condition["id"]))
    client.put(f"/api/records/{allergy['id']}/override", json={"state": "wrong"})
    groups = {g["key"]: g for g in records.new_since(pid, mark)["groups"]}
    assert "allergies" not in groups and groups["conditions"]["count"] == 1
    med = _one(pid, "medications")
    record_files.save(pid, med["connection_id"], med["resource_type"], med["resource_id"], "letter.pdf", "application/pdf", b"%PDF-1.4 a letter")
    groups = {g["key"]: g for g in records.new_since(pid, mark)["groups"]}
    assert [i["id"] for i in groups["documents"]["items"]] == [med["id"]]


def test_people_who_can_only_view_dont_get_controls_that_would_be_refused():
    css = (Path(app.__file__).parent / "static/css/app.css").read_text()
    hidden = css.split(":root[data-view-only] :is(")[1].split(")")[0]
    for control in ("[data-correction-form]", '[data-action="add-own"]', "#prep-form", "[data-remove-question]", ".report-choices"):
        assert control in hidden
