"""What leaves is complete and correct: a person's whole record, valid FHIR, and importing it back loses nothing and
overwrites nothing."""

from __future__ import annotations

import base64
import csv
import io
import json
import time
import zipfile

import pytest

from app.core import db
from app.services import exports, imports, sync
from app.store import activity, biometrics, chats, connections, records
from tests.conftest import connect_institution


def _pid(client) -> str:
    return client.get("/api/profiles").json()["profiles"][0]["id"]


def _other(client, name="Copy") -> str:
    return client.post("/api/profiles", json={"name": name, "relationship": "other"}).json()["id"]


def _import(client, bundle: dict, pid: str, name: str = "export.json") -> dict:
    r = client.post("/api/imports/fhir", files={"file": (name, json.dumps(bundle), "application/json")}, data={"profile_id": pid})
    assert r.status_code == 200, r.text
    return r.json()


def _bundle(*resources: dict, **kw) -> dict:
    return {"resourceType": "Bundle", "type": "collection", "entry": [{"resource": r} for r in resources], **kw}


def _rows(pid: str) -> list[dict]:
    return records.list_records(pid, dedupe=False, limit=10**6)["items"]


def _csv(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


# ---------------------------------------------------------------------------
# F111: the journal CSV
# ---------------------------------------------------------------------------

def test_the_journal_csv_keeps_what_people_wrote(client):
    entry = {"event_type": "headache", "category": "symptoms", "name": "Headache", "start": "2024-01-02T03:04:05Z",
             "note": "=JOURNALNOTEZEBRA private", "details": {"energy": 3}}
    assert client.post("/api/journal", json=entry).status_code == 200
    text = client.get("/api/export/csv", params={"kind": "events"}).text
    rows = _csv(text)
    assert rows[0]["note"] == "'=JOURNALNOTEZEBRA private"       # not a spreadsheet formula
    assert rows[0]["entered_by_you"] == "yes" and json.loads(rows[0]["details"])["energy"] == 3
    # What read these by position still finds the same columns.
    assert text.splitlines()[0].startswith("start,end,category,type,name,value,label,source,")


# ---------------------------------------------------------------------------
# F114: note text
# ---------------------------------------------------------------------------

def _linked_note(pid: str, text: str, content_type: str = "text/html") -> None:
    conn = connections.create(pid, "ehr", "epic", "Hospital", fhir_base_url="https://hospital.example/fhir")
    raw = {"resourceType": "DocumentReference", "id": "doc1", "status": "current", "type": {"text": "Visit note"},
           "date": "2024-05-01T00:00:00Z", "subject": {"reference": "Patient/p1"},
           "content": [{"attachment": {"contentType": content_type, "url": "https://hospital.example/Binary/abc", "size": 99, "title": "Note"}}]}
    records.upsert_records(pid, conn["id"], "Hospital", [{
        "resource_type": "DocumentReference", "resource_id": "doc1", "category": "notes", "title": "Visit note", "raw": raw, "narrative": text}])


def test_note_text_fetched_from_a_link_is_in_the_fhir_export(client):
    pid = _pid(client)
    _linked_note(pid, "Line one of NOTEZEBRA.\nLine two.")
    bundle = client.get("/api/export/fhir").json()
    doc = next(e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "DocumentReference")
    att = doc["content"][0]["attachment"]
    assert base64.b64decode(att["data"]).decode() == "Line one of NOTEZEBRA.\nLine two."
    assert att["url"] == "https://hospital.example/Binary/abc" and att["contentType"].startswith("text/plain") and "size" not in att
    # Into another instance, the text comes along.
    other = _other(client)
    _import(client, bundle, other)
    assert "NOTEZEBRA" in _rows(other)[0]["narrative"]


def test_a_report_with_a_conclusion_is_left_as_it_came(client):
    res = {"resourceType": "DiagnosticReport", "id": "r", "conclusion": "All fine",
           "presentedForm": [{"url": "https://h/Binary/1", "contentType": "text/plain"}]}
    assert exports._with_note_text(res, "All fine") is res
    linked = {"resourceType": "DiagnosticReport", "id": "r", "presentedForm": [{"url": "https://h/Binary/1", "contentType": "application/pdf"}]}
    assert exports._with_note_text(linked, "text") is linked     # a PDF stays a link


def test_the_csv_no_longer_cuts_notes_short(client):
    pid = _pid(client)
    long_note = "x" * 4500 + "THEEND"
    _linked_note(pid, long_note)
    rows = _csv(client.get("/api/export/csv", params={"kind": "records"}).text)
    assert rows[0]["narrative"] == long_note


# ---------------------------------------------------------------------------
# F113: medications
# ---------------------------------------------------------------------------

MED_BUNDLE = _bundle(
    {"resourceType": "Patient", "id": "p1", "name": [{"family": "Rivera", "given": ["Alex"]}], "birthDate": "1981-04-12"},
    {"resourceType": "Medication", "id": "m1", "code": {"coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "314076", "display": "Lisinopril ZEBRAMED 10 mg"}], "text": "Lisinopril ZEBRAMED 10 mg"}},
    {"resourceType": "MedicationRequest", "id": "mr1", "status": "active", "intent": "order", "subject": {"reference": "Patient/p1"},
     "medicationReference": {"reference": "Medication/m1"}, "authoredOn": "2024-01-01"},
)


def test_medication_names_and_codes_survive_an_export_and_import(client):
    pid = _other(client, "Source")
    _import(client, MED_BUNDLE, pid)
    before = _rows(pid)[0]
    assert before["title"] == "Lisinopril ZEBRAMED 10 mg" and before["code"] == "314076"
    bundle = client.get("/api/export/fhir", params={"profile": pid}).json()
    assert any(e["resource"]["resourceType"] == "Medication" for e in bundle["entry"])
    target = _other(client, "Target")
    _import(client, bundle, target)
    after = _rows(target)[0]
    assert after["title"] == before["title"] and after["code"] == before["code"] and after["code_system"] == before["code_system"]


def test_a_contained_medication_is_read(client):
    pid = _other(client)
    _import(client, _bundle({"resourceType": "MedicationRequest", "id": "mr", "status": "active", "intent": "order",
                             "subject": {"reference": "Patient/x"}, "medicationReference": {"reference": "#med"},
                             "contained": [{"resourceType": "Medication", "id": "med", "code": {"text": "Contained name"}}]}), pid)
    assert _rows(pid)[0]["title"] == "Contained name"


def test_medications_stored_before_this_are_rebuilt_from_the_record(client):
    """Existing rows have no Medication saved: the export makes one from the name and code on the record."""
    pid = _pid(client)
    conn = connections.create(pid, "ehr", "epic", "Hospital")
    raw = {"resourceType": "MedicationRequest", "id": "mr1", "status": "active", "intent": "order", "subject": {"reference": "Patient/p"},
           "medicationReference": {"reference": "https://h/fhir/Medication/m9"}}
    records.upsert_records(pid, conn["id"], "Hospital", [{"resource_type": "MedicationRequest", "resource_id": "mr1", "category": "medications",
                           "title": "Oldname 5 mg", "code": "123", "code_system": "http://www.nlm.nih.gov/research/umls/rxnorm", "raw": raw}])
    bundle = client.get("/api/export/fhir").json()
    med = next(e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Medication")
    assert med["id"] == "m9" and med["code"]["text"] == "Oldname 5 mg" and med["code"]["coding"][0]["code"] == "123"
    other = _other(client)
    _import(client, bundle, other)
    assert _rows(other)[0]["title"] == "Oldname 5 mg" and _rows(other)[0]["code"] == "123"


def test_a_sync_keeps_the_medications_it_brings(client):
    connect_institution(client, "epic-stanford-health-care")
    with db.read() as conn:
        kept = conn.execute("SELECT resource_type, COUNT(*) FROM fhir_references GROUP BY 1").fetchall()
    # The simulator sends medication names inline or by reference: whichever it does, nothing named is lost.
    pid = _pid(client)
    bundle = client.get("/api/export/fhir").json()
    other = _other(client)
    _import(client, bundle, other)
    assert kept is not None
    assert {r["title"] for r in _rows(pid) if r["category"] == "medications"} == {r["title"] for r in _rows(other) if r["category"] == "medications"}


def test_every_category_comes_through_a_round_trip_unchanged(client):
    connect_institution(client, "epic-stanford-health-care")
    pid = _pid(client)
    bundle = client.get("/api/export/fhir").json()
    other = _other(client)
    result = _import(client, bundle, other)
    assert result["inserted"] == len(_rows(pid)) and result["kept_both"] == 0

    def key(r: dict) -> tuple:
        return (r["category"], r["resource_type"], r["resource_id"], r["title"], r["code"], r["value_num"], r["value_text"],
                r["unit"], r["effective_at"], r["source_name"])
    assert sorted(map(key, _rows(pid)), key=str) == sorted(map(key, _rows(other)), key=str)


# ---------------------------------------------------------------------------
# F115: importing back
# ---------------------------------------------------------------------------

def _two_hospitals(client) -> str:
    pid = _other(client, "Two")
    for name, value, display in (("first.json", 101, "Glucose"), ("second.json", 13, "Hemoglobin")):
        _import(client, _bundle({"resourceType": "Patient", "id": name[0], "name": [{"text": f"Alex {name}"}]},
                                {"resourceType": "Observation", "id": "123", "status": "final", "code": {"text": display},
                                 "category": [{"coding": [{"code": "laboratory"}]}], "effectiveDateTime": "2024-01-01",
                                 "valueQuantity": {"value": value, "unit": "x"}}), pid, name)
    return pid


def test_two_hospitals_with_the_same_id_both_keep_their_results(client):
    pid = _two_hospitals(client)
    assert sorted(r["value_num"] for r in _rows(pid)) == [13, 101]
    bundle = client.get("/api/export/fhir", params={"profile": pid}).json()
    urls = [e["fullUrl"] for e in bundle["entry"]]
    assert all(urls) and len(urls) == len(set(urls))
    target = _other(client)
    result = _import(client, bundle, target)
    assert result["inserted"] == 2 and result["updated"] == 0 and len(result["sources"]) == 2
    rows = _rows(target)
    assert sorted(r["value_num"] for r in rows) == [13, 101]
    # Where each came from, and each identity, survive.
    assert {r["source_name"] for r in rows} == {"Imported · first.json", "Imported · second.json"} or len({r["source_name"] for r in rows}) == 2
    assert len(client.get("/api/summary", params={"profile": target}).json()["identities"]) == 2


def test_importing_the_same_export_again_adds_nothing(client):
    pid = _two_hospitals(client)
    bundle = client.get("/api/export/fhir", params={"profile": pid}).json()
    target = _other(client)
    _import(client, bundle, target)
    sources_before = [c["id"] for c in connections.list_for_profile(target, "import")]
    again = _import(client, bundle, target)
    assert again["inserted"] == 0 and again["updated"] == 0 and again["unchanged"] == 2 and again["kept_both"] == 0
    assert len(_rows(target)) == 2
    assert [c["id"] for c in connections.list_for_profile(target, "import")] == sources_before


def test_importing_the_same_file_twice_adds_no_second_source(client):
    pid = _other(client)
    first = _import(client, MED_BUNDLE, pid)
    second = _import(client, MED_BUNDLE, pid)
    assert first["connection_id"] == second["connection_id"] and second["inserted"] == 0 and len(_rows(pid)) == 1


def test_a_file_imported_before_this_version_is_recognised_by_name(client):
    pid = _other(client)
    old = connections.create(pid, "import", "fhir_file", "Imported · export.json", metadata={"imported_at": 1})
    assert _import(client, MED_BUNDLE, pid)["connection_id"] == old["id"]


def test_a_changed_record_is_kept_beside_the_old_one_not_written_over_it(client):
    pid = _other(client)
    make = lambda v: _bundle({"resourceType": "Observation", "id": "o1", "status": "final", "code": {"text": "Glucose"},  # noqa: E731
                              "category": [{"coding": [{"code": "laboratory"}]}], "effectiveDateTime": "2024-01-01",
                              "valueQuantity": {"value": v, "unit": "mg/dL"}})
    _import(client, make(90), pid)
    changed = _import(client, make(95), pid)
    assert changed["kept_both"] == 1 and changed["updated"] == 0
    assert sorted(r["value_num"] for r in _rows(pid)) == [90, 95]
    ids = [r["resource_id"] for r in _rows(pid)]
    assert all(exports._fhir_id(i) == i for i in ids)
    # Importing the changed one again finds it.
    again = _import(client, make(95), pid)
    assert again["kept_both"] == 0 and again["unchanged"] == 1 and len(_rows(pid)) == 2


def test_duplicates_inside_one_file_are_both_kept(client):
    pid = _other(client)
    a = {"resourceType": "Condition", "id": "c", "code": {"text": "One"}, "subject": {"reference": "Patient/p"}}
    b = {**a, "code": {"text": "Two"}}
    result = _import(client, _bundle(a, b), pid)
    assert result["kept_both"] == 1
    assert {r["title"] for r in _rows(pid)} == {"One", "Two"}


def test_a_file_naming_two_people_is_not_stored_as_one(client):
    pid = _other(client)
    bundle = _bundle({"resourceType": "Patient", "id": "a", "name": [{"family": "One", "given": ["Ann"]}], "birthDate": "1980-01-01"},
                     {"resourceType": "Patient", "id": "b", "name": [{"family": "Two", "given": ["Bob"]}], "birthDate": "1990-01-01"},
                     {"resourceType": "Condition", "id": "c1", "code": {"text": "Ann's"}, "subject": {"reference": "Patient/a"}},
                     {"resourceType": "Condition", "id": "c2", "code": {"text": "Bob's"}, "subject": {"reference": "Patient/b"}})
    result = _import(client, bundle, pid)
    assert len(result["sources"]) == 2 and result["records"] == 2
    summary = client.get("/api/summary", params={"profile": pid}).json()
    assert len(summary["identities"]) == 2 and len(summary["identity_conflict"]) == 2


# ---------------------------------------------------------------------------
# F251: valid FHIR
# ---------------------------------------------------------------------------

def _manual_lab(client, pid=None):
    assert client.post("/api/labs", json={"profile_id": pid, "collected": "2024-01-02", "results": [
        {"test": "Glucose", "value": 5.0, "unit": "mg/dL"}]}).status_code == 200


def test_a_lab_typed_in_has_a_valid_id_from_now_on(client):
    _manual_lab(client)
    with db.read() as conn:
        rid = conn.execute("SELECT resource_id FROM clinical_records").fetchone()[0]
    assert rid.startswith("man-") and exports._ID_BAD.search(rid) is None


def test_one_saved_with_the_old_id_is_exported_with_a_valid_one(client):
    pid = _pid(client)
    conn = connections.create(pid, "import", "manual", "Entered by you")
    records.upsert_records(pid, conn["id"], "Entered by you", [{
        "resource_type": "Observation", "resource_id": "man_75023b28", "category": "labs", "title": "Glucose",
        "raw": {"resourceType": "Observation", "id": "man_75023b28", "status": "final", "code": {"text": "Glucose"}}}])
    entry = next(e for e in client.get("/api/export/fhir").json()["entry"] if e["resource"]["resourceType"] == "Observation")
    rid = entry["resource"]["id"]
    assert rid.startswith("man-75023b28-") and exports._ID_BAD.search(rid) is None and entry["fullUrl"].endswith(f"/Observation/{rid}")


def _all_ids_are_valid(bundle: dict) -> None:
    for entry in bundle["entry"]:
        assert entry["fullUrl"]
        res = entry["resource"]
        if res.get("id"):
            assert exports._ID_BAD.search(res["id"]) is None and len(res["id"]) <= 64, res["id"]


def test_entries_have_full_urls_that_references_resolve_against(client):
    connect_institution(client, "epic-stanford-health-care")
    _manual_lab(client)
    bundle = client.get("/api/export/fhir").json()
    _all_ids_are_valid(bundle)
    urls = {e["fullUrl"] for e in bundle["entry"]}
    assert len(urls) == len(bundle["entry"])
    entry = next(e for e in bundle["entry"] if e["resource"]["resourceType"] == "Observation" and e["resource"].get("subject"))
    base = entry["fullUrl"].rsplit("/", 2)[0]
    assert f"{base}/{entry['resource']['subject']['reference'].split('/')[-2]}/{entry['resource']['subject']['reference'].split('/')[-1]}" in urls


@pytest.fixture
def fhir_models():
    pytest.importorskip("fhir.resources.R4B.bundle")
    from fhir.resources.R4B.bundle import Bundle
    return Bundle


def test_the_export_is_valid_fhir(client, fhir_models):
    connect_institution(client, "epic-stanford-health-care")
    _manual_lab(client)
    fhir_models.model_validate(client.get("/api/export/fhir").json())


def test_the_patient_summary_is_valid_fhir(client, fhir_models):
    connect_institution(client, "epic-stanford-health-care")
    _manual_lab(client)
    fhir_models.model_validate(client.get("/api/export/ips").json())


def test_a_sparse_patient_summary_is_valid_fhir_too(client, fhir_models):
    fhir_models.model_validate(client.get("/api/export/ips").json())


# ---------------------------------------------------------------------------
# The International Patient Summary
# ---------------------------------------------------------------------------

def _sections(document: dict) -> dict[str, dict]:
    composition = document["entry"][0]["resource"]
    return {s["code"]["coding"][0]["code"]: s for s in composition["section"]}


def test_a_patient_summary_is_a_document_with_the_required_sections(client):
    connect_institution(client, "epic-stanford-health-care")
    r = client.get("/api/export/ips")
    assert r.status_code == 200 and "summary" in r.headers["content-disposition"]
    document = r.json()
    assert document["resourceType"] == "Bundle" and document["type"] == "document" and document["identifier"] and document["timestamp"]
    composition = document["entry"][0]["resource"]
    assert composition["resourceType"] == "Composition" and composition["type"]["coding"][0]["code"] == "60591-5"
    sections = _sections(document)
    assert {"10160-0", "48765-2", "11450-4"} <= set(sections)
    assert sections["10160-0"]["entry"] and sections["11450-4"]["entry"]
    # Everything it refers to is inside it.
    urls = {e["fullUrl"] for e in document["entry"]}
    assert len(urls) == len(document["entry"]) and all(u.startswith("urn:uuid:") for u in urls)

    def walk(value):
        if isinstance(value, dict):
            ref = value.get("reference")
            if isinstance(ref, str) and not ref.startswith("#"):
                assert ref in urls, ref
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(document)
    types = [e["resource"]["resourceType"] for e in document["entry"]]
    assert types[1] == "Patient" and "Device" in types
    _all_ids_are_valid(document)


def test_a_summary_with_nothing_on_record_still_says_so(client):
    client.patch(f"/api/profiles/{_pid(client)}", json={"birth_date": "1981-04-12", "name": "Alex <b>Rivera</b>"})
    document = client.get("/api/export/ips").json()
    sections = _sections(document)
    assert set(sections) == {"10160-0", "48765-2", "11450-4"}
    for section in sections.values():
        assert "entry" not in section and section["emptyReason"]["coding"][0]["code"] == "unavailable"
        assert section["text"]["div"].startswith("<div xmlns=")
    patient = document["entry"][1]["resource"]
    assert patient["birthDate"] == "1981-04-12" and patient["name"][0]["text"] == "Alex <b>Rivera</b>"
    assert "<b>" not in document["entry"][0]["resource"]["text"]["div"]


def test_a_summary_carries_only_current_problems_and_medications(client):
    pid = _other(client)
    _import(client, _bundle(
        {"resourceType": "Condition", "id": "on", "code": {"text": "Current thing"}, "subject": {"reference": "Patient/p"},
         "clinicalStatus": {"coding": [{"code": "active"}]}},
        {"resourceType": "Condition", "id": "off", "code": {"text": "Old thing"}, "subject": {"reference": "Patient/p"},
         "clinicalStatus": {"coding": [{"code": "resolved"}]}},
        {"resourceType": "MedicationRequest", "id": "stopped", "status": "stopped", "intent": "order", "subject": {"reference": "Patient/p"},
         "medicationCodeableConcept": {"text": "Stopped drug"}}), pid)
    text = json.dumps(_sections(client.get("/api/export/ips", params={"profile": pid}).json()))
    assert "Current thing" in text and "Old thing" not in text and "Stopped drug" not in text


def test_a_summary_warns_when_the_records_name_two_people(client):
    pid = _other(client)
    _import(client, _bundle({"resourceType": "Patient", "id": "a", "name": [{"family": "One", "given": ["Ann"]}], "birthDate": "1980-01-01"},
                            {"resourceType": "Patient", "id": "b", "name": [{"family": "Two", "given": ["Bob"]}], "birthDate": "1990-01-01"}), pid)
    assert "more than one person" in client.get("/api/export/ips", params={"profile": pid}).json()["entry"][0]["resource"]["text"]["div"]


def test_a_summary_names_medications_by_reference(client):
    pid = _other(client)
    _import(client, MED_BUNDLE, pid)
    document = client.get("/api/export/ips", params={"profile": pid}).json()
    med = next(e for e in document["entry"] if e["resource"]["resourceType"] == "Medication")
    request = next(e["resource"] for e in document["entry"] if e["resource"]["resourceType"] == "MedicationRequest")
    assert request["medicationReference"]["reference"] == med["fullUrl"] and med["resource"]["code"]["text"].startswith("Lisinopril")


# ---------------------------------------------------------------------------
# F112: everything about a person
# ---------------------------------------------------------------------------

def _rich_person(client) -> str:
    pid = _other(client, "Person Zebra")
    source = connections.create(pid, "import", "apple_health_export", "Watch")
    biometrics.insert_samples(pid, source["id"], [{"id": f"s{i}", "metric_type": "step_count", "value": 10 + i, "unit": "count",
                              "start_date": f"2024-02-01T00:00:{i:02d}Z", "end_date": f"2024-02-01T00:00:{i:02d}Z",
                              "source_name": "Watch", "metadata": {"x": i}} for i in range(5)])
    activity.insert_workouts(pid, source["id"], [{"id": "w1", "start": "2024-02-01T01:00:00Z", "end": "2024-02-01T02:00:00Z", "name": "Run",
                             "source_name": "Watch", "step_count": 500, "route": [{"t": "2024-02-01T01:00:00Z", "lat": 1.5, "lon": 2.5}],
                             "heart_rate": [{"t": "2024-02-01T01:00:00Z", "avg": 120}]}])
    assert client.post("/api/journal", params={"profile": pid}, json={"event_type": "headache", "category": "symptoms", "name": "Headache",
                       "start": "2024-02-02T03:04:05Z", "note": "NOTEZEBRA"}).status_code == 200
    assert client.put("/api/goals", params={"profile": pid}, json={"metric": "step_count", "target": 9123, "direction": "min"}).status_code == 200
    principal_account = client.get("/api/status").json()["account"]["id"]
    chats.save_chat(pid, principal_account, "chat_1", "Title Zebra", [{"role": "user", "content": "CHATZEBRA"}])
    chats.save_chat(pid, "acc_someone_else", "chat_2", "Theirs", [{"role": "user", "content": "PRIVATEOTHER"}])
    _import(client, MED_BUNDLE, pid)
    return pid


def _archive(client, pid: str) -> zipfile.ZipFile:
    r = client.get("/api/export/archive", params={"profile": pid})
    assert r.status_code == 200, r.text
    assert "archive" in r.headers["content-disposition"]
    return zipfile.ZipFile(io.BytesIO(r.content))


def _ndjson(z: zipfile.ZipFile, name: str) -> list[dict]:
    return [json.loads(line) for line in z.read(name).decode().splitlines()]


def test_an_archive_holds_everything_about_a_person(client):
    pid = _rich_person(client)
    z = _archive(client, pid)
    assert {"manifest.json", "README.txt", "fhir.json", "summary-ips.json", "records.csv", "daily.csv", "workouts.csv", "events.csv",
            "samples.ndjson", "workouts.ndjson", "journal.ndjson", "goals.json", "chats.ndjson"} == set(z.namelist())
    manifest = json.loads(z.read("manifest.json"))
    assert manifest["format"] == exports.ARCHIVE_FORMAT and manifest["schema_version"] == db.newest_schema()
    assert manifest["counts"]["samples"] == 5 and manifest["counts"]["workouts"] == 1 and manifest["person"]["name"] == "Person Zebra"
    assert "credentials" not in z.read("manifest.json").decode()
    assert len(_ndjson(z, "samples.ndjson")) == 5 and _ndjson(z, "samples.ndjson")[0]["metadata"]
    workout = _ndjson(z, "workouts.ndjson")[0]
    assert workout["route"][0]["lat"] == 1.5 and workout["heart_rate"][0]["avg"] == 120 and workout["step_count"] == 500
    assert _ndjson(z, "journal.ndjson")[0]["note"] == "NOTEZEBRA"
    assert json.loads(z.read("goals.json"))["step_count"]["target"] == 9123
    assert b"CHATZEBRA" in z.read("chats.ndjson") and b"PRIVATEOTHER" not in z.read("chats.ndjson")
    assert b"Lisinopril" in z.read("fhir.json") and b"Lisinopril" in z.read("summary-ips.json")
    assert not [f for f in db.config.data_dir().iterdir() if f.name.startswith(".backup-work")]


def test_a_view_only_person_can_not_take_the_archive(client):
    from tests.test_accounts import _join, _ids
    alex = _pid(client)
    pid = _other(client, "Maria")
    member, _ = _join(client, pid)
    assert member.get("/api/export/archive", params={"profile": alex}).status_code == 404
    assert member.get("/api/export/archive", params={"profile": pid}).status_code == 200
    # Shared to view: can read it, but not take the whole of it.
    view_only = client.post("/api/profiles", json={"name": "Sam", "relationship": "other"}).json()["id"]
    account = member.get("/api/status").json()["account"]["id"]
    assert client.put(f"/api/profiles/{view_only}/access", json={"account_id": account, "level": "view"}).status_code == 200
    assert "Sam" in _ids(member)
    assert member.get("/api/export/archive", params={"profile": view_only}).status_code == 403


def test_workout_csv_has_the_columns_it_was_missing(client):
    pid = _rich_person(client)
    row = _csv(client.get("/api/export/csv", params={"kind": "workouts", "profile": pid}).text)[0]
    assert row["steps"] == "500.0" and "min_hr" in row and "device" in row and "temperature_c" in row and row["id"] == "w1"


def _wait(client, job_id):
    for _ in range(200):
        job = imports.get_job(job_id)
        if job["finished_at"]:
            return job
        time.sleep(0.05)
    raise AssertionError("import didn't finish")


def _bring_in(client, raw: bytes, pid: str) -> dict:
    r = client.post("/api/imports/archive", files={"file": ("archive.zip", raw, "application/zip")}, data={"profile_id": pid})
    assert r.status_code == 200, r.text
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "completed", job
    return job["result"]


def test_an_archive_imports_elsewhere_and_again_without_doubling(client):
    pid = _rich_person(client)
    raw = client.get("/api/export/archive", params={"profile": pid}).content
    target = _other(client, "Moved")
    first = _bring_in(client, raw, target)
    assert first["samples"] == 5 and first["workouts"] == 1
    assert first["journal"] == 1 and first["goals"] == 1 and first["chats"] == 1 and first["clinical"]["inserted"] == 1
    # Samples and workouts with these ids belong to the first person, so these get their own: nothing is stolen.
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM biometric_samples WHERE profile_id = ?", (pid,)).fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM biometric_samples WHERE profile_id = ?", (target,)).fetchone()[0] == 5
        moved = conn.execute("SELECT route_json, heart_rate_json FROM workouts WHERE profile_id = ?", (target,)).fetchone()
        note = conn.execute("SELECT note, manual FROM health_events WHERE profile_id = ?", (target,)).fetchone()
    assert json.loads(moved[0])[0]["lat"] == 1.5 and json.loads(moved[1])[0]["avg"] == 120
    assert note[0] == "NOTEZEBRA" and note[1] == 1
    sources = {c["display_name"] for c in connections.list_for_profile(target, "import")}
    assert "Watch" in sources
    second = _bring_in(client, raw, target)
    assert second["samples"] == 0 and second["workouts"] == 0 and second["journal"] == 0 and second["goals"] == 0
    assert second["goals_kept"] == 1 and second["chats_kept"] == 1 and second["clinical"]["inserted"] == 0
    assert sources == {c["display_name"] for c in connections.list_for_profile(target, "import")}


def test_an_archive_import_never_overwrites_a_goal_here(client):
    pid = _rich_person(client)
    raw = client.get("/api/export/archive", params={"profile": pid}).content
    target = _other(client)
    client.put("/api/goals", params={"profile": target}, json={"metric": "step_count", "target": 5000, "direction": "min"})
    assert _bring_in(client, raw, target)["goals_kept"] == 1
    assert client.get("/api/goals", params={"profile": target}).json()["goals"]["step_count"]["target"] == 5000


def test_something_that_is_not_an_archive_is_refused_in_words(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "hi")
    for raw in (b"not a zip", buf.getvalue()):
        r = client.post("/api/imports/archive", files={"file": ("x.zip", raw, "application/zip")})
        job = _wait(client, r.json()["job_id"])
        assert job["status"] == "error" and "isn't an archive" in job["error"]
    assert not list(imports.upload_dir().iterdir())


# ---------------------------------------------------------------------------
# Nothing is lost on the way through
# ---------------------------------------------------------------------------

def test_two_medications_taken_together_both_come_through_an_archive(client):
    """The journal's time goes to the minute and every medication has one type, but each entry has its own id."""
    pid = _pid(client)
    for name in ("Ibuprofen", "Metformin"):
        entry = {"event_type": "medication", "category": "medication", "name": name, "start": "2024-03-05T08:00:00Z", "value_label": "200 mg"}
        assert client.post("/api/journal", json=entry).status_code == 200
    raw = client.get("/api/export/archive", params={"profile": pid}).content
    target = _other(client)
    assert _bring_in(client, raw, target)["journal"] == 2
    assert _bring_in(client, raw, target)["journal"] == 0
    with db.read() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM health_events WHERE profile_id = ?", (target,))]
    assert sorted(names) == ["Ibuprofen", "Metformin"]


def test_a_device_sending_the_same_event_under_a_new_id_still_adds_nothing(client):
    pid = _pid(client)
    source = connections.create(pid, "device", "ios", "Phone")["id"]
    for event_id in ("one", "two"):
        activity.insert_events(pid, source, [{"id": event_id, "event_type": "stand", "start": "2024-02-01T00:00:00Z", "source_name": "Phone"}])
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM health_events WHERE profile_id = ?", (pid,)).fetchone()[0] == 1


def _one_person_file(who: str, name: str, value: float = 90, birth: str = "1980-01-01") -> dict:
    return _bundle({"resourceType": "Patient", "id": who, "name": [{"text": name}], "birthDate": birth},
                   {"resourceType": "Observation", "id": f"o-{who}", "status": "final", "code": {"text": "Glucose"},
                    "subject": {"reference": f"Patient/{who}"}, "effectiveDateTime": "2024-01-01",
                    "valueQuantity": {"value": value, "unit": "mg/dL"}})


def _sources(pid: str) -> list[dict]:
    return connections.list_for_profile(pid, "import")


def test_another_persons_file_with_the_same_name_keeps_both_people(client):
    pid = _pid(client)
    _import(client, _one_person_file("alice", "Alice Adams"), pid)
    _import(client, _one_person_file("bobby", "Bobby Brown", birth="1990-02-02"), pid)
    assert len(_sources(pid)) == 2
    with db.read() as conn:
        names = {r[0] for r in conn.execute("SELECT full_name FROM patient_identities WHERE profile_id = ?", (pid,))}
    assert names == {"Alice Adams", "Bobby Brown"}
    assert len(records.summary(pid)["identity_conflict"]) == 2
    assert any("Bobby Brown" in c["display_name"] for c in _sources(pid))
    # Either one coming back finds its own source.
    for who, name, birth in (("alice", "Alice Adams", "1980-01-01"), ("bobby", "Bobby Brown", "1990-02-02")):
        again = _import(client, _one_person_file(who, name, birth=birth), pid)
        assert again["inserted"] == 0 and again["kept_both"] == 0
    assert len(_sources(pid)) == 2


def test_a_newer_file_for_the_same_person_stays_in_one_source(client):
    pid = _pid(client)
    _import(client, _one_person_file("alice", "Alice Adams"), pid)
    newer = _one_person_file("alice", "Alice B. Adams")
    newer["entry"].append({"resource": {"resourceType": "Observation", "id": "o2", "status": "final", "code": {"text": "Sodium"},
                                        "subject": {"reference": "Patient/alice"}, "effectiveDateTime": "2024-02-01",
                                        "valueQuantity": {"value": 140, "unit": "mmol/L"}}})
    assert _import(client, newer, pid)["inserted"] == 1
    assert len(_sources(pid)) == 1


def test_one_id_used_for_two_people_is_not_one_source(client):
    pid = _pid(client)
    _import(client, _one_person_file("1", "Alice Adams"), pid)
    _import(client, _one_person_file("1", "Bobby Brown", birth="1990-02-02"), pid)
    assert len(_sources(pid)) == 2 and len(records.summary(pid)["identity_conflict"]) == 2


def test_the_same_person_from_another_hospital_keeps_both_identities(client):
    pid = _pid(client)
    _import(client, _one_person_file("h1-77", "Alice Adams"), pid)
    _import(client, _one_person_file("h2-12", "Alice Adams"), pid)
    assert len(_sources(pid)) == 2
    assert records.summary(pid)["identity_conflict"] == []


def test_a_source_from_before_origins_keeps_its_person_too(client):
    pid = _pid(client)
    _import(client, _one_person_file("alice", "Alice Adams"), pid)
    old = _sources(pid)[0]
    with db.db() as conn:
        conn.execute("UPDATE connections SET metadata_json = NULL WHERE id = ?", (old["id"],))
    _import(client, _one_person_file("bobby", "Bobby Brown", birth="1990-02-02"), pid)
    assert len(_sources(pid)) == 2 and len(records.summary(pid)["identity_conflict"]) == 2
    assert _import(client, _one_person_file("alice", "Alice Adams"), pid)["inserted"] == 0
    assert len(_sources(pid)) == 2


def _unnamed_lab(rid: str, subject: str | None = None) -> dict:
    res = {"resourceType": "Observation", "id": rid, "status": "final", "code": {"text": "Glucose"}, "effectiveDateTime": "2024-01-01",
           "valueQuantity": {"value": 90, "unit": "mg/dL"}}
    return {**res, "subject": {"reference": f"Patient/{subject}"}} if subject else res


def test_files_that_name_nobody_share_a_source_only_if_they_share_a_record(client):
    pid = _pid(client)
    _import(client, _bundle(_unnamed_lab("a")), pid)
    assert _import(client, _bundle(_unnamed_lab("a"), _unnamed_lab("b")), pid)["inserted"] == 1
    assert len(_sources(pid)) == 1
    other = _bundle(_unnamed_lab("c"))
    assert _import(client, other, pid)["inserted"] == 1
    assert len(_sources(pid)) == 2
    assert _import(client, other, pid)["inserted"] == 0
    assert len(_sources(pid)) == 2


def test_files_with_only_a_subject_are_kept_apart_by_it(client):
    pid = _pid(client)
    _import(client, _bundle(_unnamed_lab("a", "alice")), pid)
    _import(client, _bundle(_unnamed_lab("b", "alice")), pid)
    assert len(_sources(pid)) == 1
    _import(client, _bundle(_unnamed_lab("c", "bobby")), pid)
    assert len(_sources(pid)) == 2
    assert _import(client, _bundle(_unnamed_lab("c", "bobby")), pid)["inserted"] == 0
    assert len(_sources(pid)) == 2


def test_a_second_apple_health_upload_stays_in_its_source(client):
    """Not a file's name but the one place Apple's records go, and those files name no patient."""
    pid = _pid(client)
    for rid in ("a", "b"):
        sync.import_fhir_resources(pid, "Apple Health Records", [_unnamed_lab(rid, rid)], provider="apple_health_records")
    assert len(_sources(pid)) == 1


def test_a_medication_shared_by_two_people_in_a_file_names_both_prescriptions(client):
    pid = _pid(client)
    med = {"resourceType": "Medication", "id": "m1", "code": {"text": "Lisinopril 10 mg", "coding": [
        {"system": "http://www.nlm.nih.gov/research/umls/rxnorm", "code": "314076"}]}}

    def request(rid, who):
        return {"resourceType": "MedicationRequest", "id": rid, "status": "active", "intent": "order",
                "subject": {"reference": f"Patient/{who}"}, "medicationReference": {"reference": "Medication/m1"}, "authoredOn": "2024-01-01"}
    loose = {"resourceType": "Observation", "id": "loose", "status": "final", "code": {"text": "No patient named"},
             "valueQuantity": {"value": 1, "unit": "mg"}}
    result = _import(client, _bundle({"resourceType": "Patient", "id": "a", "name": [{"text": "A A"}]},
                                     {"resourceType": "Patient", "id": "b", "name": [{"text": "B B"}]},
                                     med, request("r1", "a"), request("r2", "b"), loose), pid)
    items = records.list_records(pid, "medications", dedupe=False)["items"]
    assert {(i["resource_id"], i["title"], i["code"]) for i in items} == {("r1", "Lisinopril 10 mg", "314076"), ("r2", "Lisinopril 10 mg", "314076")}
    assert result["records"] == 3 and len(result["sources"]) == 2     # the record with no patient isn't copied to both
    # Each person's part of the export stands on its own too.
    other = _other(client)
    _import(client, client.get("/api/export/fhir", params={"profile": pid}).json(), other)
    assert {i["title"] for i in records.list_records(other, "medications", dedupe=False)["items"]} == {"Lisinopril 10 mg"}


@pytest.mark.parametrize("bad_id", ["e" + "A1b2C3d4" * 11, "med_one"])
def test_an_id_changed_for_the_export_is_changed_where_it_is_pointed_to(client, bad_id):
    pid = _pid(client)
    who = "p" + "Z" * 80
    bundle = _bundle({"resourceType": "Patient", "id": who, "name": [{"text": "A A"}]},
                     {"resourceType": "Medication", "id": bad_id, "code": {"text": "Lisinopril 10 mg"}},
                     {"resourceType": "MedicationRequest", "id": "r1", "status": "active", "intent": "order", "subject": {"reference": f"Patient/{who}"},
                      "medicationReference": {"reference": f"https://hospital.example/fhir/Medication/{bad_id}"}, "authoredOn": "2024-01-01"},
                     {"resourceType": "Observation", "id": "lab_1", "status": "final", "code": {"text": "Glucose"}, "subject": {"reference": f"Patient/{who}"},
                      "valueQuantity": {"value": 90, "unit": "mg/dL"}},
                     {"resourceType": "DiagnosticReport", "id": "rep", "status": "final", "code": {"text": "Panel"}, "subject": {"reference": f"Patient/{who}"},
                      "category": [{"coding": [{"code": "LAB"}]}], "result": [{"reference": "Observation/lab_1"}]})
    _import(client, bundle, pid)
    out = client.get("/api/export/fhir").json()
    _all_ids_are_valid(out)
    by_type = {e["resource"]["resourceType"]: e["resource"] for e in out["entry"]}
    med_id = by_type["Medication"]["id"]
    assert by_type["MedicationRequest"]["medicationReference"]["reference"].endswith(f"/Medication/{med_id}")
    assert by_type["MedicationRequest"]["subject"]["reference"] == f"Patient/{by_type['Patient']['id']}"
    assert by_type["DiagnosticReport"]["result"][0]["reference"] == f"Observation/{by_type['Observation']['id']}"
    target = _other(client)
    _import(client, out, target)
    assert records.list_records(target, "medications", dedupe=False)["items"][0]["title"] == "Lisinopril 10 mg"
    # The summary points inside itself, whatever the ids were.
    document = client.get("/api/export/ips").json()
    _all_ids_are_valid(document)
    urls = {e["fullUrl"] for e in document["entry"]}
    request = next(e["resource"] for e in document["entry"] if e["resource"]["resourceType"] == "MedicationRequest")
    assert request["medicationReference"]["reference"] in urls


def test_two_long_ids_that_begin_alike_stay_two_medications(client):
    pid = _pid(client)
    start = "x" * 70

    def pair(end, name, rid):
        return ({"resourceType": "Medication", "id": start + end, "code": {"text": name}},
                {"resourceType": "MedicationRequest", "id": rid, "status": "active", "intent": "order",
                 "medicationReference": {"reference": f"Medication/{start}{end}"}, "authoredOn": "2024-01-01"})
    _import(client, _bundle(*pair("a", "Lisinopril", "r1"), *pair("b", "Warfarin", "r2")), pid)
    target = _other(client)
    _import(client, client.get("/api/export/fhir").json(), target)
    titles = {i["resource_id"]: i["title"] for i in records.list_records(target, "medications", dedupe=False)["items"]}
    assert titles == {"r1": "Lisinopril", "r2": "Warfarin"}


def test_a_contained_medication_with_an_odd_id_is_still_found(client):
    pid = _pid(client)
    _import(client, _bundle({"resourceType": "MedicationRequest", "id": "r1", "status": "active", "intent": "order", "authoredOn": "2024-01-01",
                             "contained": [{"resourceType": "Medication", "id": "med_1", "code": {"text": "Warfarin"}}],
                             "medicationReference": {"reference": "#med_1"}}), pid)
    for out in (client.get("/api/export/fhir").json(), client.get("/api/export/ips").json()):
        request = next(e["resource"] for e in out["entry"] if e["resource"]["resourceType"] == "MedicationRequest")
        assert exports._ID_BAD.search(request["contained"][0]["id"]) is None
        assert request["medicationReference"]["reference"] == "#" + request["contained"][0]["id"]
    target = _other(client)
    _import(client, client.get("/api/export/fhir").json(), target)
    assert records.list_records(target, "medications", dedupe=False)["items"][0]["title"] == "Warfarin"


def test_the_workout_csv_fills_in_what_is_stored(client):
    pid = _pid(client)
    source = connections.create(pid, "wearable", "oura", "Ring")["id"]
    activity.insert_workouts(pid, source, [{"id": "w1", "name": "Run", "start": "2024-01-01T10:00:00Z", "end": "2024-01-01T11:00:00Z", "duration_s": 3600,
                                            "elevation_descent_m": 44.0, "temperature_c": 21.5, "humidity_pct": 55.0, "device_name": "Watch Ultra",
                                            "source_name": "Watch", "step_count": 500, "min_hr": 88}])
    row = _csv(client.get("/api/export/csv", params={"kind": "workouts"}).text)[0]
    assert (row["elevation_loss_m"], row["temperature_c"], row["humidity_pct"], row["device"]) == ("44.0", "21.5", "55.0", "Watch Ultra")
    assert (float(row["steps"]), float(row["min_hr"])) == (500, 88)


def _account() -> str:
    with db.read() as conn:
        return conn.execute("SELECT id FROM accounts LIMIT 1").fetchone()[0]


def test_an_archive_import_never_pushes_out_a_conversation_here(client):
    pid, target, account = _pid(client), _other(client), _account()
    for i in range(150):
        chats.save_chat(pid, account, f"src{i}", f"S{i}", [{"role": "user", "content": "x"}])
        chats.save_chat(target, account, f"tgt{i}", f"T{i}", [{"role": "user", "content": "y"}])
    raw = client.get("/api/export/archive", params={"profile": pid}).content
    result = _bring_in(client, raw, target)
    assert (result["chats"], result["chats_skipped"], result["chats_kept"]) == (50, 100, 0)
    left = [c["title"] for c in chats.list_chats(target, account, limit=1000)]
    assert len(left) == chats.MAX_CHATS and sum(c.startswith("T") for c in left) == 150
    # The newest are the ones that came, and a second run says the same about the rest.
    assert "S149" in left and "S0" not in left
    again = _bring_in(client, raw, target)
    assert (again["chats"], again["chats_kept"], again["chats_skipped"]) == (0, 50, 100)


def test_a_conversation_brought_in_keeps_its_dates(client):
    pid, target, account = _pid(client), _other(client), _account()
    chats.save_chat(pid, account, "old", "Old", [{"role": "user", "content": "x"}])
    with db.db() as conn:
        conn.execute("UPDATE chats SET created_at = 1000, updated_at = 2000 WHERE id = 'old'")
    raw = client.get("/api/export/archive", params={"profile": pid}).content
    assert _bring_in(client, raw, target)["chats"] == 1
    moved = chats.list_chats(target, account)[0]
    assert (moved["created_at"], moved["updated_at"]) == (1000, 2000)
