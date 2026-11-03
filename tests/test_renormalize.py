"""Stored records read again by today's normalizer: corrected once, safely, and nothing the raw resource can't reproduce is lost."""

import json

import pytest

from app.connectors import fhir_normalize as fn
from app.core import data_fixes, db
from app.core.db import dumps
from app.services import renormalize, sync
from app.store import connections, records

RXNORM = fn.RXNORM


@pytest.fixture
def prof(client):
    return client.get("/api/profiles").json()["profiles"][0]["id"]


def rows(prof=None):
    with db.read() as conn:
        return {r["resource_id"]: dict(r) for r in conn.execute("SELECT * FROM clinical_records")}


def run(cursor=None):
    """All of it, returning the totals."""
    totals = cursor
    while True:
        with db.db() as conn:
            nxt = renormalize.step(conn, totals, 1000)
        if nxt is None:
            return totals or {"changed": 0, "removed": 0}
        totals = nxt


def stale(rid, **fields):
    """A row as an older version wrote it."""
    with db.db() as conn:
        for k, v in fields.items():
            conn.execute(f"UPDATE clinical_records SET {k} = ? WHERE resource_id = ?", (v, rid))


def glucose(rid="g", **kw):
    return {"resourceType": "Observation", "id": rid, "status": "final", "category": [{"coding": [{"code": "laboratory"}]}],
            "code": {"coding": [{"system": fn.LOINC, "code": "2345-7", "display": "Glucose"}]}, "effectiveDateTime": "2025-01-01T08:30:00-05:00", **kw}


MED = {"resourceType": "Medication", "id": "m1", "code": {"coding": [{"system": RXNORM, "code": "314076", "display": "Lisinopril 10 MG"}]}}
BY_REFERENCE = {"resourceType": "MedicationRequest", "id": "r1", "status": "active", "intent": "order", "authoredOn": "2024-01-01",
                "medicationReference": {"reference": "Medication/m1"}, "dosageInstruction": [{"text": "daily"}]}


def test_reading_unchanged_records_again_changes_nothing(prof, client):
    """The test that would have caught a plain replay stripping every referenced medication's RxNorm code."""
    from tests.conftest import connect_institution
    connect_institution(client, "epic-stanford-health-care")
    panel = {"resourceType": "DiagnosticReport", "id": "rep", "status": "final", "category": [{"coding": [{"code": "LAB"}]}], "code": {"text": "Panel"},
             "effectiveDateTime": "2025-01-01", "result": [{"reference": "https://x.example/fhir/Observation/g"}]}
    sync.import_fhir_resources(prof, "File", [MED, BY_REFERENCE, glucose(valueQuantity={"value": 5, "unit": "mmol/L"}), panel,
                                              {"resourceType": "Patient", "id": "p", "gender": "female", "name": [{"text": "Alex Rivera"}]}])
    before = rows()
    assert before["r1"]["code"] == "314076" and json.loads(before["g"]["details_json"])["panel"] == "Panel"
    totals = run()
    assert totals["changed"] == 0 and totals["removed"] == 0
    assert rows() == before


def test_wrong_values_from_an_old_version_are_corrected(prof):
    sync.import_fhir_resources(prof, "File", [glucose(code={"coding": [{"system": fn.LOINC, "code": "2157-6", "display": "CK"}]},
                                                      valueQuantity={"value": 1250.4, "unit": "U/L"},
                                                      referenceRange=[{"low": {"value": 1000.4}, "high": {"value": 2000.4}}])])
    first_seen = rows()["g"]["first_seen_at"]
    stale("g", value_text="125 U/L", ref_text="1–2", dedup_key="obs|2157-6|2025-01-01T08:30|125 U/L")
    assert run()["changed"] == 1
    row = rows()["g"]
    assert row["value_text"] == "1250 U/L" and row["ref_text"] == "1000–2000"
    assert row["dedup_key"] == "obs|2157-6|2025-01-01T13:30|1250 u/l"
    assert row["first_seen_at"] == first_seen
    assert run()["changed"] == 0


def test_a_converted_result_and_its_range_are_corrected_together(prof):
    sync.import_fhir_resources(prof, "File", [glucose(valueQuantity={"value": 2.2, "unit": "mmol/L", "comparator": "<"},
                                                      referenceRange=[{"low": {"value": 3.9}, "high": {"value": 5.5}}])])
    stale("g", value_text="39.6 mg/dL", ref_text="3.9–5.5", interpretation="low")
    run()
    row = rows()["g"]
    details = json.loads(row["details_json"])
    assert row["value_text"] == "<2.2 mmol/L" and row["ref_text"] == "3.9–5.5" and row["interpretation"] == "low"
    assert details["comparator"] == "<" and details["converted_value"] == "<39.6 mg/dL"


def test_a_medication_keeps_the_name_and_code_only_the_lost_resource_had(prof):
    sync.import_fhir_resources(prof, "File", [MED, BY_REFERENCE])
    stale("r1", status="draft")
    assert run()["changed"] == 1
    row = rows()["r1"]
    assert (row["title"], row["code"], row["code_system"], row["dedup_key"]) == ("Lisinopril 10 MG", "314076", RXNORM, "med|314076")
    assert row["status"] == "active" and json.loads(row["details_json"])["rxnorm"] == "314076"


def test_nameless_medications_merged_under_one_key_come_apart(prof):
    a = {"resourceType": "MedicationRequest", "id": "a", "status": "active", "medicationReference": {"reference": "Medication/x"}}
    b = {"resourceType": "MedicationRequest", "id": "b", "status": "active", "medicationReference": {"reference": "Medication/y"}}
    sync.import_fhir_resources(prof, "File", [a, b])
    stale("a", title="Medication", dedup_key="med|medication")
    stale("b", title="Medication", dedup_key="med|medication")
    run()
    assert rows()["a"]["dedup_key"] is None and rows()["a"]["title"] == fn.UNNAMED_MEDICATION
    assert len(records.list_records(prof, "medications")["items"]) == 2


def test_a_contained_medication_gets_its_real_name(prof):
    a = {"resourceType": "MedicationRequest", "id": "a", "status": "active", "medicationReference": {"reference": "#c"},
         "contained": [{"resourceType": "Medication", "id": "c", "code": {"text": "Metformin 500 MG"}}]}
    sync.import_fhir_resources(prof, "File", [a])
    stale("a", title="Medication", dedup_key="med|medication")
    run()
    assert rows()["a"]["title"] == "Metformin 500 MG"


def test_what_came_from_outside_the_resource_is_kept(prof):
    conn = connections.create(prof, "import", "manual", "Entered by you")
    note = {"resourceType": "DocumentReference", "id": "n", "status": "current", "date": "2024-01-01", "type": {"text": "Note"},
            "content": [{"attachment": {"contentType": "text/plain", "url": "Binary/1"}}]}
    _, recs = fn.normalize_resources([note, glucose(valueQuantity={"value": 90, "unit": "mg/dL"})])
    recs[0]["narrative"] = "Fetched from Binary"
    recs[0]["details"]["body_fingerprint"] = "fp"
    recs[1]["details"]["panel"] = "Metabolic panel"
    records.upsert_records(prof, conn["id"], "Quest Diagnostics", recs)
    stale("n", status="wrong")
    stale("g", status="wrong")
    assert run()["changed"] == 2
    after = rows()
    assert after["n"]["narrative"] == "Fetched from Binary" and json.loads(after["n"]["details_json"])["body_fingerprint"] == "fp"
    assert json.loads(after["g"]["details_json"])["panel"] == "Metabolic panel"
    assert after["g"]["source_name"] == "Quest Diagnostics"


def test_a_stored_rtf_body_becomes_text(prof):
    note = {"resourceType": "DocumentReference", "id": "n", "status": "current", "date": "2024-01-01", "type": {"text": "Note"},
            "content": [{"attachment": {"contentType": "text/rtf", "url": "Binary/1"}}]}
    sync.import_fhir_resources(prof, "File", [note])
    stale("n", narrative=r"{\rtf1\ansi{\fonttbl{\f0 Arial;}}\f0 Hello\par World}")
    run()
    assert rows()["n"]["narrative"] == "Hello\nWorld"


def test_only_a_row_the_source_retracted_is_removed(prof):
    note = {"resourceType": "DocumentReference", "id": "n", "status": "current", "docStatus": "entered-in-error", "date": "2024-01-01",
            "type": {"text": "Note"}, "content": []}
    conn = connections.create(prof, "import", "fhir_file", "File")
    records.upsert_records(prof, conn["id"], "File", [fn._base(note, "notes", "Note", status="current"),
                                                      fn._base({"resourceType": "Observation", "id": "empty"}, "labs", "Nothing here"),
                                                      fn._base({"resourceType": "Goal", "id": "bad", "lifecycleStatus": "cancelled"}, "goals", "Cancelled goal")])
    stale("empty", value_text="kept")
    totals = run()
    after = rows()
    assert "n" not in after and totals["removed"] == 1
    assert after["empty"]["value_text"] == "kept"     # the normalizer drops it, but the source never said it was a mistake
    assert "bad" in after


def test_a_resource_that_breaks_the_normalizer_stays_as_it_is(prof, monkeypatch):
    sync.import_fhir_resources(prof, "File", [glucose(valueQuantity={"value": 90, "unit": "mg/dL"})])
    stale("g", value_text="kept")
    with monkeypatch.context() as patch:
        patch.setattr(fn, "normalize_one", lambda *a: 1 / 0)
        assert run()["changed"] == 0
    assert rows()["g"]["value_text"] == "kept"


def test_rows_with_no_or_unreadable_raw_are_left_alone(prof):
    conn = connections.create(prof, "import", "fhir_file", "File")
    records.upsert_records(prof, conn["id"], "File", [fn._base({"resourceType": "Observation", "id": "a"}, "labs", "A")])
    with db.db() as c:
        c.execute("UPDATE clinical_records SET raw_json = NULL WHERE resource_id = 'a'")
    records.upsert_records(prof, conn["id"], "File", [fn._base({"resourceType": "Observation", "id": "b"}, "labs", "B")])
    with db.db() as c:
        c.execute("UPDATE clinical_records SET raw_json = 'not json' WHERE resource_id = 'b'")
    assert run()["changed"] == 0 and set(rows()) == {"a", "b"}


def test_it_resumes_and_finishes_through_the_ledger(prof, monkeypatch):
    sync.import_fhir_resources(prof, "File", [glucose(rid=f"g{i}", valueQuantity={"value": i, "unit": "mg/dL"}) for i in range(5)])
    for i in range(5):
        stale(f"g{i}", value_text="wrong")
    with db.db() as conn:
        first = renormalize.step(conn, None, 2)
    assert first["changed"] == 2 and rows()["g0"]["value_text"] == "0 mg/dL" and rows()["g4"]["value_text"] == "wrong"
    with db.db() as conn:
        conn.execute("INSERT INTO data_fixes(name, state, attempts, started_at, updated_at, cursor_json) VALUES (?, 'running', 1, 0, 0, ?) "
                     "ON CONFLICT(name) DO UPDATE SET state = 'running', finished_at = NULL, cursor_json = excluded.cursor_json",
                     (renormalize.NAME, dumps(first)))
    with monkeypatch.context() as patch:
        patch.setattr(data_fixes, "_fixes", {renormalize.NAME: renormalize.step})
        data_fixes.run_pending()
    assert all(r["value_text"] != "wrong" for r in rows().values())
    state = next(s for s in data_fixes.status() if s["name"] == renormalize.NAME)
    assert state["state"] == "done"
    with db.read() as conn:
        logged = conn.execute("SELECT detail FROM audit_log WHERE action = 'records.renormalized'").fetchone()
    assert json.loads(logged["detail"]) == {"changed": 5, "removed": 0}


def test_ranges_for_old_rows_choose_by_the_person_on_the_connection(prof):
    male = {"appliesTo": [{"text": "Male"}], "low": {"value": 13.5}, "high": {"value": 17.5}}
    female = {"appliesTo": [{"text": "Female"}], "low": {"value": 12.0}, "high": {"value": 15.5}}
    res = glucose(code={"coding": [{"system": fn.LOINC, "code": "718-7", "display": "Hgb"}]}, valueQuantity={"value": 12.5, "unit": "g/dL"},
                  referenceRange=[male, female])
    sync.import_fhir_resources(prof, "File", [res, {"resourceType": "Patient", "id": "p", "gender": "female", "name": [{"text": "A"}]}])
    stale("g", interpretation="low", ref_text="13.5–17.5")
    run()
    assert rows()["g"]["interpretation"] == "normal" and rows()["g"]["ref_text"] == "12–15.5"
