"""The visit summary and the Overview say only what the record can back up."""

import re
from pathlib import Path

import pytest

from app.connectors import fhir_normalize as fn
from app.core import db, settings
from app.services import renormalize
from app.store import connections, profiles, records

STATIC = Path(__file__).resolve().parent.parent / "app" / "static" / "js"


@pytest.fixture
def pid(client):
    return profiles.default_profile()["id"]


def _source(pid, name="Lab"):
    return connections.create(pid, "import", "manual", name)["id"]


def _lab(pid, src, rid, value, interp=None, code=None, title=None, day="2026-09-01", **extra):
    code = code or rid
    records.upsert_records(pid, src, "Lab", [{
        "resource_type": "Observation", "resource_id": rid, "category": "labs", "title": title or f"Test {rid}", "code": code,
        "code_system": "http://loinc.org", "effective_at": f"{day}T09:00:00Z", "value_num": value, "value_norm": value,
        "value_text": str(value), "interpretation": interp, "dedup_key": f"lab|{code}|{day}", **extra}])


def _obs(value, text=None, unit="mg/dL", **kw):
    res = {"resourceType": "Observation", "id": "o", "status": "final", "category": [{"coding": [{"code": "laboratory"}]}],
           "code": {"text": "Glucose"}, "effectiveDateTime": "2026-01-01", "valueQuantity": {"value": value, "unit": unit}, **kw}
    if text is not None:
        res["referenceRange"] = [{"text": text}]
    return fn.normalize_observation(res)


# ---------------------------------------------------------------------------
# F018
# ---------------------------------------------------------------------------

def test_no_labs_counts_as_no_labs(pid):
    assert records.summary(pid)["lab_status"] == {"total": 0, "flagged": 0, "in_range": 0, "not_assessed": 0}


def test_a_result_with_no_range_is_not_in_range(pid):
    src = _source(pid)
    _lab(pid, src, "k", 7.2)
    _lab(pid, src, "g", 90, "normal")
    _lab(pid, src, "t", 9, "high")
    assert records.summary(pid)["lab_status"] == {"total": 3, "flagged": 1, "in_range": 1, "not_assessed": 1}


def test_counts_cover_more_than_the_results_listed(pid):
    src = _source(pid)
    for i in range(20):
        _lab(pid, src, f"c{i}", 5, "normal")
    assert records.summary(pid)["lab_status"]["in_range"] == 20


def test_a_lab_that_says_normal_without_a_range_is_in_range(pid):
    _lab(pid, _source(pid), "n", 1, "normal")
    assert records.summary(pid)["lab_status"]["in_range"] == 1


@pytest.mark.parametrize("value,text,expected", [
    (410, "70-99 mg/dL", "high"), (50, "70–99", "low"), (80, "70 - 99 mg/dL", "normal"), (6.1, "<5.7", "high"), (5.7, "<5.7", "high"),
    (5.0, "<5.7 mg/dL", "normal"), (5.7, "<=5.7", "normal"), (45, ">60", "low"), (60, ">60", "low"), (75, ">=60", "normal"), (60, "≥60", "normal"),
    (410, "NON-REACTIVE", None), (410, "See comment", None), (410, "Adults: 70-99", None), (410, "70-99 mmol/L", None), (410, "99-70", None),
    (410, "", None), (410, "70-99 fasting", None), (410, "<5.7 if diabetic", None),
])
def test_simple_written_ranges_are_compared(value, text, expected):
    assert _obs(value, text)["interpretation"] == expected


def test_a_written_range_is_shown_as_written_and_stored_as_numbers():
    rec = _obs(410, "70-99 mg/dL")
    assert rec["ref_text"].startswith("70–99") and (rec["ref_low"], rec["ref_high"]) == (70, 99)


def test_a_words_only_result_stays_not_assessed():
    res = {"resourceType": "Observation", "id": "h", "status": "final", "category": [{"coding": [{"code": "laboratory"}]}],
           "code": {"text": "HIV antibody"}, "effectiveDateTime": "2026-01-01", "valueString": "Reactive", "referenceRange": [{"text": "NON-REACTIVE"}]}
    assert fn.normalize_observation(res)["interpretation"] is None


def test_stored_written_ranges_are_compared_once(pid):
    res = {"resourceType": "Observation", "id": "g", "status": "final", "category": [{"coding": [{"code": "laboratory"}]}],
           "code": {"text": "Glucose"}, "effectiveDateTime": "2026-01-01", "valueQuantity": {"value": 410, "unit": "mg/dL"}, "referenceRange": [{"text": "70-99 mg/dL"}]}
    src = _source(pid)
    rec = fn.normalize_observation(res)
    rec.update(resource_type="Observation", resource_id="g", raw=res, interpretation=None, ref_low=None, ref_high=None)
    records.upsert_records(pid, src, "Lab", [rec])
    with db.read() as conn:
        assert conn.execute("SELECT interpretation FROM clinical_records").fetchone()[0] is None
    cursor = None
    while True:
        with db.db() as conn:
            cursor = renormalize.written_ranges_step(conn, cursor, 100)
        if cursor is None:
            break
    with db.read() as conn:
        row = conn.execute("SELECT interpretation, ref_high FROM clinical_records").fetchone()
    assert row["interpretation"] == "high" and row["ref_high"] == 99


def test_the_all_in_range_sentences_are_gone():
    for path in ("views/report.js", "views/overview.js"):
        text = (STATIC / path).read_text(encoding="utf-8")
        assert "within reference ranges" not in text and "lab results are in range" not in text


def test_ai_apps_get_the_counts(pid):
    from app import mcp_server
    _lab(pid, _source(pid), "k", 7.2)
    assert mcp_server.t_health_summary({})["lab_status"]["not_assessed"] == 1


# ---------------------------------------------------------------------------
# F125
# ---------------------------------------------------------------------------

def test_the_report_states_how_old_each_source_is_and_what_it_could_not_fetch(client, pid):
    src = _source(pid, "Sutter")
    _lab(pid, src, "a", 1, "normal", day="2026-01-05")
    _lab(pid, src, "b", 1, "normal", day="2026-03-05")
    run = connections.start_run(src, "manual")
    connections.finish_run(run, src, "partial", {"warnings": ["Medications: this source didn't allow them to be read."]})
    status = next(s for s in client.get("/api/report", params={"profile": pid}).json()["source_status"] if s["name"] == "Sutter")
    assert status["newest_record_at"].startswith("2026-03-05") and status["last_sync_status"] == "partial" and status["last_sync_at"]
    assert status["not_fetched"] == ["Medications: this source didn't allow them to be read."]


def test_a_later_clean_sync_clears_what_could_not_be_fetched(client, pid):
    src = _source(pid, "Sutter")
    connections.finish_run(connections.start_run(src, "manual"), src, "partial", {"warnings": ["Medications: refused."]})
    with db.db() as conn:
        conn.execute("UPDATE sync_runs SET started_at = 1")
    connections.finish_run(connections.start_run(src, "manual"), src, "success", {"warnings": []})
    assert connections.source_status(pid)[0]["not_fetched"] == []


def test_connections_list_carries_the_last_warnings_for_alerts(client, pid):
    src = _source(pid, "Sutter")
    connections.finish_run(connections.start_run(src, "manual"), src, "partial", {"warnings": ["Labs: refused."]})
    found = client.get("/api/connections", params={"profile": pid}).json()["connections"]
    assert next(c for c in found if c["id"] == src)["last_warnings"] == ["Labs: refused."]


def test_stale_sources_are_flagged_by_kind_in_alerts():
    text = (STATIC / "alerts.js").read_text(encoding="utf-8")
    assert "ehr: 7, wearable: 3" in text and "synced in part" in text


# ---------------------------------------------------------------------------
# F072, F122, F308, F127
# ---------------------------------------------------------------------------

def _report(client, pid):
    return client.get("/api/report", params={"profile": pid}).json()


def test_nothing_is_cut_without_saying_how_much(client, pid):
    src = _source(pid)
    for i in range(45):
        _lab(pid, src, f"t{i}", 5, "normal")
    report = _report(client, pid)
    assert len(report["recent_labs"]) == 40 and report["labs_more"] == 5
    assert report["lab_status"]["total"] == 45
    assert len(records.summary(pid)["recent_labs"]) == 12


def test_a_lab_carries_its_previous_result(client, pid):
    src = _source(pid)
    _lab(pid, src, "a1", 7.0, "high", code="4548-4", title="A1c", day="2026-09-01")
    _lab(pid, src, "a2", 8.0, "high", code="4548-4", title="A1c", day="2026-03-01")
    latest = _report(client, pid)["recent_labs"][0]
    assert latest["value_text"] == "7.0" and latest["previous"]["value_text"] == "8.0" and latest["previous"]["effective_at"].startswith("2026-03-01")


def test_more_than_a_hundred_vaccines_are_all_there(client, pid):
    src = _source(pid)
    records.upsert_records(pid, src, "Clinic", [{"resource_type": "Immunization", "resource_id": f"v{i}", "category": "immunizations",
                                                 "title": f"Vaccine {i}", "status": "completed", "effective_at": "2026-01-01", "dedup_key": f"v{i}"} for i in range(120)])
    assert len(_report(client, pid)["immunizations"]) == 120


def test_device_scores_are_not_on_the_summary(client, pid):
    from tests.test_insight_findings import _daily
    _daily(pid, "strain_score", [10, 11, 12], unit="score")
    _daily(pid, "resting_heart_rate", [60, 61, 62], unit="bpm")
    assert [b["metric"] for b in _report(client, pid)["biometrics"]] == ["resting_heart_rate"]


def test_home_readings_give_averages_counts_ranges_and_source(client, pid):
    from tests.test_insight_findings import _daily
    _daily(pid, "blood_pressure_systolic", [120] * 17 + [130] * 10, unit="mmHg", source="Omron")
    _daily(pid, "blood_pressure_diastolic", [80] * 27, unit="mmHg", source="Omron")
    _daily(pid, "blood_glucose", [100, 120], unit="mg/dL", source="Dexcom")
    home = {m["metric"]: m for m in _report(client, pid)["home_readings"]}
    systolic = home["blood_pressure_systolic"]
    assert systolic["d7"]["count"] in (7, 8) and systolic["d7"]["avg"] == 130 and systolic["d30"]["count"] == 27
    assert (systolic["d30"]["low"], systolic["d30"]["high"]) == (120, 130) and systolic["sources"] == ["Omron"]
    assert home["blood_glucose"]["d30"]["avg"] == 110 and home["blood_glucose"]["unit"] == "mg/dL"


def test_home_readings_leave_out_simulated_and_doubled_ones(client, pid):
    from tests.test_insight_findings import _daily
    _daily(pid, "blood_glucose", [100], unit="mg/dL", source="Demo (simulated)")
    assert _report(client, pid)["home_readings"] == []
    _daily(pid, "blood_glucose", [100], unit="mg/dL", source="A")
    _daily(pid, "blood_glucose", [100], unit="mg/dL", source="B")
    assert _report(client, pid)["home_readings"][0]["d30"]["count"] == 1


def test_report_includes_procedures_imaging_smoking_and_journal(client, pid):
    src = _source(pid)
    records.upsert_records(pid, src, "Clinic", [
        {"resource_type": "Procedure", "resource_id": "p", "category": "procedures", "title": "Total knee replacement", "effective_at": "2024-01-01", "dedup_key": "p"},
        {"resource_type": "DiagnosticReport", "resource_id": "e", "category": "reports", "title": "Echo", "effective_at": "2025-01-01", "narrative": "EF 55-60%", "details": {"kind": "imaging"}, "dedup_key": "e"},
        {"resource_type": "DiagnosticReport", "resource_id": "l", "category": "reports", "title": "Lipid panel", "effective_at": "2025-01-01", "details": {"kind": "lab"}, "dedup_key": "l"},
        {"resource_type": "Observation", "resource_id": "s", "category": "observations", "title": "Smoking status", "code": "72166-2", "value_text": "Former smoker", "effective_at": "2025-06-01", "dedup_key": "s"}])
    from app.store import activity
    from tests.test_insight_findings import _noon
    activity.insert_events(pid, None, [{"id": f"h{i}", "event_type": "headache", "category": "symptoms", "name": "Headache", "start": _noon(i + 1).isoformat(),
                                        "end": _noon(i + 1).isoformat(), "source_name": "Syntropy"} for i in range(3)])
    report = _report(client, pid)
    assert [p["title"] for p in report["procedures"]] == ["Total knee replacement"]
    assert [p["title"] for p in report["imaging"]] == ["Echo"]
    assert report["smoking"]["value_text"] == "Former smoker"
    assert report["journal"][0]["name"] == "Headache" and report["journal"][0]["count"] == 3


def test_choices_and_questions_are_remembered_per_person_and_go_with_them(client, pid):
    assert client.get("/api/report/preferences", params={"profile": pid}).json() == {"sections": {}, "questions": []}
    res = client.put("/api/report/preferences", json={"profile_id": pid, "sections": {"insurance": True, "BAD KEY": True}, "questions": ["  Why this dose?  ", " "]})
    assert res.status_code == 200 and res.json() == {"sections": {"insurance": True}, "questions": ["Why this dose?"]}
    client.put("/api/report/preferences", json={"profile_id": pid, "sections": {"insurance": False}})
    assert client.get("/api/report/preferences", params={"profile": pid}).json() == {"sections": {"insurance": False}, "questions": ["Why this dose?"]}
    from app.core import settings
    with db.db() as conn:
        profiles._person_settings(conn, pid)
    assert settings.get(f"report.preferences.{pid}") is None


def test_insurance_starts_off_and_the_summary_stops_promising_one_page():
    text = (STATIC / "views/report.js").read_text(encoding="utf-8")
    assert '{ id: "insurance", title: "Insurance", off: true }' in text and "one-page" not in text
    assert "one-page" not in (STATIC / "views/records.js").read_text(encoding="utf-8")
    assert "one-page" not in (STATIC / "views/settings.js").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# F121
# ---------------------------------------------------------------------------

def _rec(pid, src, rid, category, title, day="2026-10-28", **extra):
    records.upsert_records(pid, src, "Clinic", [{"resource_type": extra.pop("resource_type", "X"), "resource_id": rid, "category": category, "title": title,
                                                 "effective_at": day, "dedup_key": extra.pop("dedup_key", f"{category}|{rid}"), **extra}])


def _new(client, pid):
    return client.get("/api/new-records", params={"profile": pid}).json()


def _age_connection(src):
    with db.db() as conn:
        conn.execute("UPDATE connections SET created_at = 1 WHERE id = ?", (src,))


def test_new_records_are_listed_with_counts_titles_and_dates(client, pid):
    from datetime import date
    today = date.today().isoformat()
    src = _source(pid, "Stanford")
    _age_connection(src)
    assert _new(client, pid)["groups"] == []
    _rec(pid, src, "e", "encounters", "Emergency visit", day=today)
    _rec(pid, src, "c", "conditions", "Atrial fibrillation", day=today)
    _rec(pid, src, "m", "medications", "Warfarin 5 MG", day=today)
    _rec(pid, src, "n", "notes", "Discharge summary", day=today)
    found = {g["key"]: g for g in _new(client, pid)["groups"]}
    assert set(found) == {"encounters", "conditions", "medications", "notes"}
    assert found["medications"]["count"] == 1 and found["medications"]["items"][0]["title"] == "Warfarin 5 MG" and found["medications"]["items"][0]["effective_at"] == today


def test_a_first_import_is_one_line(client, pid):
    assert _new(client, pid)["imported"] == []
    src = _source(pid, "Banner")
    for i in range(30):
        _rec(pid, src, f"l{i}", "labs", f"Test {i}", day="2010-01-01")
    found = _new(client, pid)
    assert found["imported"] == [{"source": "Clinic", "count": 30}] and found["groups"] == []


def test_a_first_import_counts_what_it_brought_including_what_another_source_also_sent(client, pid):
    old = _source(pid, "Earlier")
    _rec(pid, old, "shared", "labs", "Glucose", day="2010-01-01", dedup_key="labs|shared")
    with db.db() as conn:
        conn.execute("UPDATE clinical_records SET first_seen_at = 1")
        conn.execute("UPDATE connections SET created_at = 1")
    _new(client, pid)
    src = _source(pid, "Banner")
    _rec(pid, src, "shared", "labs", "Glucose", day="2010-01-01", dedup_key="labs|shared")
    _rec(pid, src, "fresh", "labs", "Sodium", day="2010-01-01")
    assert _new(client, pid)["imported"] == [{"source": "Clinic", "count": 2}]


def test_old_history_arriving_later_is_not_news(client, pid):
    src = _source(pid)
    _age_connection(src)
    _new(client, pid)
    _rec(pid, src, "old", "labs", "Old", day="2010-01-01")
    found = _new(client, pid)
    assert found["older"] == 1 and found["groups"] == []


def test_what_another_source_already_sent_is_not_new(client, pid):
    src = _source(pid)
    _rec(pid, src, "m1", "medications", "Metformin", dedup_key="med|1")
    other = _source(pid, "Other")
    _age_connection(other)
    _new(client, pid)
    _rec(pid, other, "m2", "medications", "Metformin", dedup_key="med|1")
    assert _new(client, pid)["groups"] == []


def test_a_medication_marked_stopped_is_listed(client, pid):
    src = _source(pid)
    _age_connection(src)
    _rec(pid, src, "m", "medications", "Lisinopril", status="active")
    _new(client, pid)
    _rec(pid, src, "m", "medications", "Lisinopril", status="stopped")
    assert [g["key"] for g in _new(client, pid)["groups"]] == ["medications_stopped"]


def test_marking_as_seen_clears_it_and_the_mark_goes_with_the_person(client, pid):
    src = _source(pid)
    _age_connection(src)
    from datetime import date
    _new(client, pid)
    _rec(pid, src, "e", "encounters", "Visit", day=date.today().isoformat())
    assert _new(client, pid)["groups"]
    assert client.post(f"/api/new-records/seen?profile={pid}").status_code == 200
    assert _new(client, pid)["groups"] == []
    from app.core import settings
    with db.db() as conn:
        profiles._person_settings(conn, pid)
    assert settings.get(f"records.seen.{pid}.") is None


# ---------------------------------------------------------------------------
# F126
# ---------------------------------------------------------------------------

def _linked(pid):
    src = _source(pid)
    enc = {"reference": "https://x.example/fhir/Encounter/enc1"}
    _rec(pid, src, "enc1", "encounters", "Nephrology follow-up", resource_type="Encounter", raw={"resourceType": "Encounter", "id": "enc1"})
    _rec(pid, src, "n", "notes", "Progress note", resource_type="DocumentReference", raw={"resourceType": "DocumentReference", "context": {"encounter": [enc]}})
    _rec(pid, src, "l1", "labs", "Creatinine", resource_type="Observation", code="2160-0", code_system="http://loinc.org", value_text="1.2", day="2026-10-15",
         raw={"resourceType": "Observation", "encounter": {"reference": "Encounter/enc1"}}, details={"panel": "Metabolic panel"})
    _rec(pid, src, "l0", "labs", "Creatinine", resource_type="Observation", code="2160-0", code_system="http://loinc.org", value_text="1.0", day="2026-01-15",
         raw={"resourceType": "Observation"})
    _rec(pid, src, "x", "labs", "Other visit", resource_type="Observation", raw={"resourceType": "Observation", "encounter": {"reference": "Encounter/enc11"}})
    return src


def _get(client, pid, resource_id):
    with db.read() as conn:
        rid = conn.execute("SELECT id FROM clinical_records WHERE resource_id = ?", (resource_id,)).fetchone()[0]
    return client.get(f"/api/records/{rid}").json()


def test_a_visit_lists_its_notes_and_results(client, pid):
    _linked(pid)
    visit = _get(client, pid, "enc1")
    assert [r["title"] for r in visit["visit_records"]["notes"]] == ["Progress note"]
    assert [r["title"] for r in visit["visit_records"]["labs"]] == ["Creatinine"]
    assert visit["visit_records"]["labs"][0]["details"]["panel"] == "Metabolic panel"


def test_a_note_and_a_result_link_back_to_their_visit(client, pid):
    _linked(pid)
    assert _get(client, pid, "n")["visit"]["title"] == "Nephrology follow-up"
    assert _get(client, pid, "l1")["visit"]["title"] == "Nephrology follow-up"


def test_a_result_shows_the_one_before_it(client, pid):
    _linked(pid)
    latest = _get(client, pid, "l1")
    assert latest["previous"]["value_text"] == "1.0" and "previous" not in _get(client, pid, "l0")


# ---------------------------------------------------------------------------
# F272
# ---------------------------------------------------------------------------

def _reports(pid):
    src = _source(pid)
    _rec(pid, src, "l", "reports", "Lipid panel", details={"kind": "lab"})
    _rec(pid, src, "i", "reports", "Echocardiogram", details={"kind": "imaging"})
    _rec(pid, src, "p", "procedures", "Colonoscopy")
    _rec(pid, src, "n", "notes", "Progress note", details={"type": "Progress", "authors": ["Dr Alvarez"]})
    _rec(pid, src, "n2", "notes", "Discharge", details={"type": "Discharge", "authors": ["Dr Chen"]})
    return src


def test_timeline_keeps_lab_panels_out_of_imaging(client, pid):
    _reports(pid)
    titles = lambda cats: {i["title"] for i in client.get("/api/timeline", params={"profile": pid, "categories": cats}).json()["items"]}  # noqa: E731
    assert titles("imaging") == {"Echocardiogram"}
    assert titles("procedures") == {"Colonoscopy"}
    assert titles("lab_reports") == {"Lipid panel"}
    assert titles("reports") == {"Lipid panel", "Echocardiogram"}


def test_records_narrow_by_kind_type_author_and_date(client, pid):
    src = _reports(pid)
    titles = lambda **kw: {i["title"] for i in client.get("/api/records", params={"profile": pid, **kw}).json()["items"]}  # noqa: E731
    assert titles(category="reports", kind="imaging") == {"Echocardiogram"}
    assert titles(category="notes", note_type="Discharge") == {"Discharge"}
    assert titles(category="notes", author="Dr Alvarez") == {"Progress note"}
    assert titles(category="notes", connection=src, until="2020-01-01") == set()
    filters = client.get("/api/record-filters", params={"profile": pid, "category": "notes"}).json()
    assert filters["note_types"] == ["Discharge", "Progress"] and filters["authors"] == ["Dr Alvarez", "Dr Chen"]
    assert client.get("/api/record-filters", params={"profile": pid, "category": "reports"}).json()["kinds"]


# ---------------------------------------------------------------------------
# F123
# ---------------------------------------------------------------------------

def test_visit_prep_lists_what_changed_since_a_date(client, pid):
    src = _source(pid)
    _lab(pid, src, "a1", 7.0, "high", code="4548-4", title="A1c", day="2026-09-01")
    _lab(pid, src, "a2", 7.5, "high", code="4548-4", title="A1c", day="2026-08-01")
    _lab(pid, src, "a3", 8.0, "high", code="4548-4", title="A1c", day="2026-01-01")
    _lab(pid, src, "old", 5, "normal", code="1", title="Old", day="2025-01-01")
    _rec(pid, src, "v", "encounters", "Cardiology", day="2026-07-01", details={"clinicians": ["Dr Alvarez"]})
    _rec(pid, src, "v0", "encounters", "Early", day="2025-07-01")
    _rec(pid, src, "m", "medications", "Warfarin", day="2026-07-02", status="active", resource_type="MedicationRequest", dedup_key="med|1", details={"intent": "order"})
    _rec(pid, src, "m0", "medications", "Old drug", day="2024-07-02", status="active", resource_type="MedicationRequest", dedup_key="med|2", details={"intent": "order"})
    _rec(pid, src, "s", "medications", "Aspirin", day="2026-07-05", status="stopped", resource_type="MedicationRequest", dedup_key="med|3", details={"intent": "order"})
    _rec(pid, src, "c", "conditions", "Atrial fibrillation", day="2026-07-03")
    _rec(pid, src, "n", "notes", "Note", day="2026-07-03")
    d = client.get("/api/visit-prep", params={"profile": pid, "since": "2026-06-01"}).json()
    assert [r["title"] for r in d["results"]] == ["A1c"]
    result = d["results"][0]
    assert result["value_text"] == "7.0" and result["count"] == 2 and result["before"]["value_text"] == "8.0"
    assert [v["title"] for v in d["visits"]] == ["Cardiology"] and d["visits"][0]["clinician"] == "Dr Alvarez"
    assert "Warfarin" in {m["title"] for m in d["medications_started"]} and "Old drug" not in {m["title"] for m in d["medications_started"]}
    assert [m["title"] for m in d["medications_stopped"]] == ["Aspirin"]
    assert d["diagnoses"][0]["title"] == "Atrial fibrillation" and d["notes"][0]["title"] == "Note"


def test_visit_prep_is_the_same_each_time_and_asks_for_a_real_date(client, pid):
    _lab(pid, _source(pid), "a1", 7.0, "high", day="2026-09-01")
    args = {"profile": pid, "since": "2026-06-01"}
    assert client.get("/api/visit-prep", params=args).json() == client.get("/api/visit-prep", params=args).json()
    assert client.get("/api/visit-prep", params={"profile": pid, "since": "soon"}).status_code == 422


# ---------------------------------------------------------------------------
# F128
# ---------------------------------------------------------------------------

def test_before_and_after_gives_values_and_counts_on_each_side(client, pid):
    src = _source(pid)
    for i, (day, v) in enumerate([("2025-01-01", 8.0), ("2025-06-01", 7.8), ("2026-01-01", 7.0), ("2026-02-01", 7.2)]):
        _lab(pid, src, f"a{i}", v, "high", code="4548-4", title="A1c", day=day)
    _lab(pid, src, "same", 7.5, "high", code="4548-4", title="A1c", day="2025-09-01")
    d = client.get("/api/observations/before-after", params={"profile": pid, "code": "4548-4", "start": "2025-09-01", "category": "labs"}).json()
    line = d["lines"][0]
    assert line["before"]["count"] == 2 and line["before"]["avg"] == 7.9 and line["after"]["count"] == 2 and line["after"]["avg"] == 7.1
    assert (line["after"]["low"], line["after"]["high"]) == (7.0, 7.2)
    near = client.get("/api/observations/before-after", params={"profile": pid, "code": "4548-4", "start": "2025-09-01", "category": "labs", "window": 180}).json()
    assert near["lines"][0]["before"]["count"] == 1 and near["lines"][0]["after"]["count"] == 2


def test_before_and_after_works_for_blood_pressure_and_leaves_limits_out(client, pid):
    src = _source(pid)
    for i, (day, s) in enumerate([("2025-01-01", 150), ("2026-01-01", 130)]):
        _rec(pid, src, f"bp{i}", "vitals", "Blood pressure", day=day, code="85354-6", code_system="http://loinc.org", value_text=f"{s}/80 mmHg",
             details={"components": [{"code": "8480-6", "name": "Systolic", "value": s, "unit": "mmHg"}, {"code": "8462-4", "name": "Diastolic", "value": 80, "unit": "mmHg"}]})
    d = client.get("/api/observations/before-after", params={"profile": pid, "code": "85354-6", "start": "2025-06-01", "category": "vitals"}).json()
    assert {x["name"]: (x["before"]["avg"], x["after"]["avg"]) for x in d["lines"]} == {"Systolic": (150, 130), "Diastolic": (80, 80)}
    _lab(pid, src, "t1", 5, "normal", code="X1", title="T", day="2025-01-01", details={"comparator": "<"})
    d = client.get("/api/observations/before-after", params={"profile": pid, "code": "X1", "start": "2025-06-01", "category": "labs"}).json()
    assert d["lines"][0]["before"]["count"] == 0 and d["lines"][0]["limits_left_out"] == 1


def test_before_and_after_never_claims_a_cause():
    text = (STATIC / "views/trends.js").read_text(encoding="utf-8").lower()
    start = text.index("function beforeaftercard")
    words = text[start:text.index("\n}\n", start)]
    for word in ("improved", "worked", "lowered", "because", "effect"):
        assert word not in words, word


# ---------------------------------------------------------------------------
# F303
# ---------------------------------------------------------------------------

def test_a_source_says_what_it_brought_in(client, pid):
    src = _source(pid)
    _rec(pid, src, "c", "conditions", "A")
    _lab(pid, src, "l", 1, "normal")
    _lab(pid, src, "l2", 1, "normal", code="z")
    counts = client.get(f"/api/connections/{src}").json()["record_counts"]
    assert counts == [{"category": "conditions", "label": "Conditions", "count": 1}, {"category": "labs", "label": "Lab results", "count": 2}]


# ---------------------------------------------------------------------------
# F270
# ---------------------------------------------------------------------------

def test_vaccines_are_not_cut_on_the_summary_and_doses_are_grouped():
    text = (STATIC / "views/report.js").read_text(encoding="utf-8")
    assert "slice(0, 15)" not in text and "vaccineRows(r.immunizations)" in text
    assert "ageText(dob)" in text
    assert "may be due" not in text.lower()


# ---------------------------------------------------------------------------
# F269
# ---------------------------------------------------------------------------

def test_people_at_a_glance_covers_everyone_this_account_can_see(client, pid):
    child = client.post("/api/profiles", json={"name": "Riley", "relationship": "child"}).json()["id"]
    src = _source(child)
    _age_connection(src)
    client.get("/api/new-records", params={"profile": child})
    from datetime import date
    _rec(child, src, "e", "encounters", "Visit", day=date.today().isoformat())
    _lab(child, src, "k", 7, "high", day=date.today().isoformat())
    with db.db() as conn:
        conn.execute("UPDATE connections SET status = 'needs_reauth' WHERE id = ?", (src,))
    people = {p["name"]: p for p in client.get("/api/profiles/at-a-glance").json()["people"]}
    assert people["Riley"]["flagged_labs"] == 1 and people["Riley"]["new_records"] == 2 and people["Riley"]["sources_need_signing_in"] == 1
    assert people["Alex"]["new_records"] is None and people["Alex"]["flagged_labs"] == 0


# ---------------------------------------------------------------------------
# F271
# ---------------------------------------------------------------------------

def _headaches(pid, days):
    from app.store import activity
    from tests.test_insight_findings import _noon
    activity.insert_events(pid, None, [{"id": f"h{d}", "event_type": "headache", "category": "symptoms", "name": "Headache", "start": _noon(d).isoformat(),
                                        "end": _noon(d).isoformat(), "source_name": "Syntropy"} for d in days])


def test_something_logged_can_be_compared_with_a_measure(client, pid):
    from tests.test_insight_findings import _daily
    _headaches(pid, [2, 5, 8, 11])
    _daily(pid, "sleep_duration", [7.0] * 20, unit="h")
    measures = client.get("/api/insights/comparable", params={"profile": pid}).json()["measures"]
    assert any(m["key"] == "logged:Headache" and m["group"] == "logged" for m in measures)
    res = client.get("/api/insights/compare", params={"profile": pid, "a": "sleep_duration", "b": "logged:Headache", "days": 30}).json()
    ww = res["with_without"]
    assert ww["item"] == "Headache" and ww["days_with"] == 4 and ww["avg_with"] == 7 and ww["days_without"] >= 1


def test_logged_once_or_twice_is_not_offered(client, pid):
    _headaches(pid, [2, 5])
    measures = client.get("/api/insights/comparable", params={"profile": pid}).json()["measures"]
    assert not any(m["key"].startswith("logged:") for m in measures)


def test_before_and_after_a_date_for_a_measure(client, pid):
    from datetime import date, timedelta
    from tests.test_insight_findings import _daily
    _daily(pid, "resting_heart_rate", [70] * 10 + [60] * 10, unit="bpm")
    pivot = (date.today() - timedelta(days=10)).isoformat()
    res = client.get("/api/insights/before-after", params={"profile": pid, "measure": "resting_heart_rate", "date": pivot, "window": 30}).json()
    assert res["before"]["avg"] == 70 and res["before"]["days"] >= 8
    assert res["after"]["avg"] == 60 and res["after"]["days"] >= 8
    assert client.get("/api/insights/before-after", params={"profile": pid, "measure": "resting_heart_rate", "date": "soon"}).status_code == 400


def test_comparison_wording_stays_with_counts():
    text = (STATIC / "views/trend-tools.js").read_text(encoding="utf-8")
    start = text.index("function withWithout")
    words = text[start:text.index("function describe", start)].lower()
    for word in ("because", "caused", "triggers", "effect", "improved"):
        assert word not in words


# ---------------------------------------------------------------------------
# What a reviewer found: statements these pages made that weren't true
# ---------------------------------------------------------------------------

def test_the_visit_summarys_page_footer_goes_when_the_page_does():
    text = (STATIC / "views/report.js").read_text(encoding="utf-8")
    assert re.search(r"return \(\) => page\.remove\(\);\s*}\s*$", text), "the footer with a person's name has to leave with the page, or it prints on whatever is printed next"


@pytest.mark.parametrize("status", ["preparation", "on-hold", "stopped", "in-progress", "unknown"])
def test_a_procedure_not_marked_completed_stays_out_of_past_procedures(client, pid, status):
    src = _source(pid)
    _rec(pid, src, "done", "procedures", "Appendectomy", day="2026-02-01", status="completed")
    _rec(pid, src, "none", "procedures", "Mole removal", day="2026-02-02")
    _rec(pid, src, "p", "procedures", "Total knee replacement", day="2026-09-01", status=status)
    report = client.get("/api/report", params={"profile": pid}).json()
    assert {p["title"] for p in report["procedures"]} == {"Appendectomy", "Mole removal"}
    assert [(p["title"], p["status"]) for p in report["procedures_unfinished"]] == [("Total knee replacement", status)]
    d = client.get("/api/visit-prep", params={"profile": pid, "since": "2026-01-01"}).json()
    assert {p["title"] for p in d["procedures"]} == {"Appendectomy", "Mole removal"}
    assert [(p["title"], p["status"]) for p in d["procedures_unfinished"]] == [("Total knee replacement", status)]


def test_visit_prep_keeps_visits_that_havent_happened_apart(client, pid):
    from datetime import date, timedelta
    src = _source(pid)
    ahead = (date.today() + timedelta(days=40)).isoformat()
    _rec(pid, src, "gone", "encounters", "Last visit", day=(date.today() - timedelta(days=3)).isoformat(), status="finished")
    _rec(pid, src, "booked", "encounters", "Booked", day=ahead, status="booked")
    _rec(pid, src, "plan", "encounters", "Planned", day=ahead, status="planned")
    _rec(pid, src, "missed", "encounters", "Planned and past", day=(date.today() - timedelta(days=5)).isoformat(), status="planned")
    d = client.get("/api/visit-prep", params={"profile": pid, "since": "2020-01-01"}).json()
    assert [v["title"] for v in d["visits"]] == ["Last visit"]
    assert {v["title"] for v in d["upcoming"]} == {"Booked", "Planned"}


def test_visit_prep_leaves_out_no_kind_of_dated_record(client, pid):
    src = _source(pid)
    _rec(pid, src, "al", "allergies", "Penicillin", day="2026-09-01", resource_type="AllergyIntolerance")
    _rec(pid, src, "im", "immunizations", "Tdap", day="2026-09-01", resource_type="Immunization", status="completed")
    _rec(pid, src, "vi", "vitals", "Weight", day="2026-09-01", code="29463-7", value_num=80, value_text="80", unit="kg")
    _rec(pid, src, "vi0", "vitals", "Weight", day="2025-09-01", code="29463-7", value_num=78, value_text="78", unit="kg")
    _rec(pid, src, "pa", "reports", "Pathology: biopsy", day="2026-09-01", details={"kind": "other"})
    _rec(pid, src, "ct", "reports", "CT chest", day="2026-09-01", details={"kind": "imaging"})
    _rec(pid, src, "cp", "care_plans", "Plan", day="2026-09-01")
    _rec(pid, src, "dv", "devices", "Pacemaker", day="2026-09-01")
    d = client.get("/api/visit-prep", params={"profile": pid, "since": "2026-01-01"}).json()
    assert [r["title"] for r in d["allergies"]] == ["Penicillin"]
    assert [r["title"] for r in d["immunizations"]] == ["Tdap"]
    assert [r["title"] for r in d["vitals"]] == ["Weight"] and d["vitals"][0]["before"]["value_text"] == "78"
    assert [r["title"] for r in d["reports"]] == ["Pathology: biopsy"] and [r["title"] for r in d["imaging"]] == ["CT chest"]
    assert {(r["title"], r["category_label"]) for r in d["other"]} == {("Plan", "Care plans"), ("Pacemaker", "Devices")}
    assert all(d["counts"][k] for k in ("allergies", "immunizations", "vitals", "reports", "other"))


def test_visit_prep_says_how_many_records_have_no_date(client, pid):
    src = _source(pid)
    records.upsert_records(pid, src, "Clinic", [{"resource_type": "AllergyIntolerance", "resource_id": "a", "category": "allergies", "title": "Latex", "dedup_key": "a"}])
    d = client.get("/api/visit-prep", params={"profile": pid, "since": "2026-01-01"}).json()
    assert d["undated"] == [{"label": "Allergies", "count": 1}] and d["counts"]["allergies"] == 0
    text = (STATIC / "views/prepare.js").read_text(encoding="utf-8")
    assert "Everything on record dated" not in text and "Nothing on record is dated" not in text


def test_a_medication_already_stopped_isnt_newly_stopped_when_something_else_about_it_changes(client, pid):
    src = _source(pid)
    _age_connection(src)
    _rec(pid, src, "w", "medications", "Warfarin 5 MG", day="2019-01-01", status="completed", raw={"meta": {"lastUpdated": "a"}})
    _new(client, pid)
    _rec(pid, src, "w", "medications", "Warfarin 5 MG", day="2019-01-01", status="completed", raw={"meta": {"lastUpdated": "b"}})
    assert _new(client, pid)["groups"] == []
    # One ended status to another says nothing new either. It counts again only if it was restarted and then stopped.
    _rec(pid, src, "w", "medications", "Warfarin 5 MG", day="2019-01-01", status="stopped")
    assert _new(client, pid)["groups"] == []
    _rec(pid, src, "w", "medications", "Warfarin 5 MG", day="2019-01-01", status="active")
    assert _new(client, pid)["groups"] == []
    _rec(pid, src, "w", "medications", "Warfarin 5 MG", day="2019-01-01", status="stopped")
    assert [g["key"] for g in _new(client, pid)["groups"]] == ["medications_stopped"]


def test_a_stopped_medication_counts_only_after_the_look_it_happened_after_and_leaves_with_the_person(client, pid):
    src = _source(pid)
    _age_connection(src)
    _rec(pid, src, "m", "medications", "Lisinopril", status="active")
    _rec(pid, src, "m", "medications", "Lisinopril", status="stopped")
    _new(client, pid)
    assert _new(client, pid)["groups"] == []
    profile = client.post("/api/profiles", json={"name": "Riley", "relationship": "child"}).json()["id"]
    other = _source(profile)
    _rec(profile, other, "m", "medications", "Lisinopril", status="active")
    _rec(profile, other, "m", "medications", "Lisinopril", status="stopped")
    assert settings.get(f"records.stopped.{profile}")
    client.delete(f"/api/profiles/{profile}")
    assert settings.get(f"records.stopped.{profile}") is None


def test_your_people_starts_counting_for_someone_never_opened_and_doesnt_claim_nothing_new(client, pid):
    from datetime import date
    glance = lambda: next(p for p in client.get("/api/profiles/at-a-glance").json()["people"] if p["id"] == pid)  # noqa: E731
    src = _source(pid)
    _age_connection(src)
    first = glance()
    assert first["new_records"] is None and first["new_since"] is None
    _lab(pid, src, "k", 5, "normal", day=date.today().isoformat())
    second = glance()
    assert second["new_records"] == 1 and second["new_since"]
    assert '"Nothing new"' not in (STATIC / "views/people.js").read_text(encoding="utf-8")
    assert "(p.new_records || 0)" in (STATIC / "app.js").read_text(encoding="utf-8")


def test_a_partial_syncs_alert_depends_on_what_it_says_not_when_it_ran():
    text = (STATIC / "alerts.js").read_text(encoding="utf-8")
    assert ":partial:${Math.floor(" not in text
    assert ":partial:${textKey(c.last_warnings" in text


def test_imaging_text_says_when_it_continues():
    text = (STATIC / "views/report.js").read_text(encoding="utf-8")
    assert '(p.narrative || "—").slice(0, 300)' not in text
    assert "conclusion(p)" in text and "continues" in text
