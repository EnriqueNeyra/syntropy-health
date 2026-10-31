"""The visit summary and the Overview say only what the record can back up."""

from pathlib import Path

import pytest

from app.connectors import fhir_normalize as fn
from app.core import db
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
