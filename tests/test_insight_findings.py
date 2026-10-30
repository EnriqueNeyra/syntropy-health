"""What insights say about real shapes of data: the numbers, the direction, the wording and the tone."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from app import mcp_server
from app.services import ai, insights
from app.store import activity, biometrics, connections, goals, profiles, records


def _prof():
    return profiles.default_profile()["id"]


def _noon(days_ago=0):
    return datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)


def _daily(pid, metric, values, source="Apple Watch", unit="count", end_days_ago=0):
    """One sample a day, the last ``end_days_ago`` days ago. A None skips that day."""
    n = len(values)
    biometrics.insert_samples(pid, None, [
        {"id": f"{metric}-{source}-{end_days_ago}-{i}", "metric_type": metric, "value": v, "unit": unit, "source_name": source,
         "start_date": _noon(end_days_ago + n - 1 - i).isoformat(), "end_date": (_noon(end_days_ago + n - 1 - i) + timedelta(minutes=1)).isoformat()}
        for i, v in enumerate(values) if v is not None])


def _noisy(mean, sd, n, seed=7):
    rng = random.Random(seed)
    return [rng.gauss(mean, sd) for _ in range(n)]


def _source(pid, name="Lab"):
    return connections.create(pid, "import", "manual", name)["id"]


def _lab(pid, source, i, day, value, interpretation=None, code="3016-3", title="TSH", unit="mIU/L", low=None, high=None, ref_text=None):
    return records.upsert_records(pid, source, "Lab", [{
        "resource_type": "Observation", "resource_id": f"{code}-{i}", "category": "labs", "title": title, "code": code,
        "code_system": "http://loinc.org", "effective_at": f"{day}T09:00:00Z", "value_num": value, "value_norm": value,
        "value_text": f"{value} {unit}", "unit": unit, "ref_low": low, "ref_high": high, "ref_text": ref_text,
        "interpretation": interpretation, "dedup_key": f"lab|{code}|{day}"}])


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


# ---------------------------------------------------------------------------
# F019: percentages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scale", [1, 100])
def test_blood_oxygen_text_is_in_points_and_reaches_ai_correctly(client, scale):
    pid = _prof()
    _daily(pid, "oxygen_saturation", [v / 100 * scale if scale == 1 else v for v in _noisy(96.5, 0.3, 60)] + [94.5] * 7, unit="%")
    found = [i for i in insights._changes(pid) if i["metric"] == "oxygen_saturation"]
    assert len(found) == 1
    assert "94.5%" in found[0]["text"] and "96.5%" in found[0]["text"] and "9450" not in found[0]["text"]
    tool = mcp_server.t_insights({})["insights"]
    assert any("94.5%" in i["text"] for i in tool) and not any("00.0%" in i["text"] or "9650" in i["text"] for i in tool)
    assert "9450" not in ai.summary_context(pid, 20000)


def test_a_tiny_blood_oxygen_change_raises_nothing(client):
    pid = _prof()
    _daily(pid, "oxygen_saturation", [96.5 + (i % 2) * 0.01 for i in range(60)] + [96.4] * 7, unit="%")
    assert insights._changes(pid) == []


def test_body_fat_goal_text_says_the_percentage(client):
    pid = _prof()
    _daily(pid, "body_fat_percentage", [21.0, 21.5, 22.5, 21.0, 21.2, 21.9, 21.1], unit="%")
    goals.set_goal(pid, "body_fat_percentage", 22, "max")
    text = insights._goal_notes(pid)[0]["text"]
    assert "at most 22.0%" in text and "2200" not in text


# ---------------------------------------------------------------------------
# F020: a lab's direction
# ---------------------------------------------------------------------------

def _lab_titles_and_text(pid):
    notes = insights._lab_notes(pid)
    return [n["title"] for n in notes], " ".join(n["text"] for n in notes)


def test_abnormal_above_range_and_falling_is_not_called_below_or_further(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(100), 6.0, "abnormal", low=0.4, high=4, ref_text="0.4–4")
    _lab(pid, src, 2, _ago(10), 5.0, "abnormal", low=0.4, high=4, ref_text="0.4–4")
    titles, text = _lab_titles_and_text(pid)
    assert titles == ["TSH closer to the range"]
    assert "above the reference range (0.4–4)" in text and "below" not in text


def test_abnormal_above_range_and_rising_is_further(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(100), 5.0, "abnormal", low=0.4, high=4, ref_text="0.4–4")
    _lab(pid, src, 2, _ago(10), 6.0, "abnormal", low=0.4, high=4, ref_text="0.4–4")
    titles, text = _lab_titles_and_text(pid)
    assert titles == ["TSH moving further out of range"]
    assert "up from" in text and "above the reference range" in text


@pytest.mark.parametrize("low,high", [(0.4, 4), (None, None)])
def test_abnormal_with_no_side_says_no_direction(client, low, high):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(100), 3.0, "abnormal", low=low, high=high)
    _lab(pid, src, 2, _ago(10), 2.0, "abnormal", low=low, high=high)
    assert insights._lab_notes(pid) == []


def test_further_needs_the_previous_result_out_on_the_same_side(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(100), 3.0, "normal", low=0.4, high=4, ref_text="0.4–4")
    _lab(pid, src, 2, _ago(10), 6.0, "high", low=0.4, high=4, ref_text="0.4–4")
    titles, text = _lab_titles_and_text(pid)
    assert titles == ["TSH moved out of range"] and "first time" in text


def test_a_lab_note_does_not_invent_a_range(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(100), 5.0, "high")
    _lab(pid, src, 2, _ago(10), 6.0, "high")
    assert "as reported" not in _lab_titles_and_text(pid)[1]


def test_a_flagged_text_result_is_not_pinned_to_an_older_number(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(200), 5.0, "high", high=4)
    _lab(pid, src, 2, _ago(100), 6.0, "high", high=4)
    records.upsert_records(pid, src, "Lab", [{
        "resource_type": "Observation", "resource_id": "text", "category": "labs", "title": "TSH", "code": "3016-3",
        "effective_at": f"{_ago(5)}T09:00:00Z", "value_text": "Very high", "interpretation": "high", "dedup_key": "lab|text"}])
    assert insights._lab_notes(pid) == []


# ---------------------------------------------------------------------------
# F265: size of the change, slow drift, improvement
# ---------------------------------------------------------------------------

def test_a_tiny_lab_change_is_not_news(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(200), 52, "low", code="33914-3", title="eGFR", unit="mL/min", low=60)
    _lab(pid, src, 2, _ago(10), 51, "low", code="33914-3", title="eGFR", unit="mL/min", low=60)
    assert insights._lab_notes(pid) == []


def test_slow_drift_over_years_is_described_with_when_it_crossed(client):
    pid = _prof()
    src = _source(pid)
    history = [(1800, 63), (1450, 61), (1100, 59), (730, 56), (365, 52), (10, 51)]
    for i, (ago, v) in enumerate(history):
        _lab(pid, src, i, _ago(ago), v, "low" if v < 60 else "normal", code="33914-3", title="eGFR", unit="mL/min", low=60)
    notes = insights._lab_notes(pid)
    assert len(notes) == 1 and notes[0]["title"] == "eGFR has been falling" and notes[0]["tone"] == "attention"
    assert "63" in notes[0]["text"] and "51" in notes[0]["text"] and "below the reference range since" in notes[0]["text"]
    crossed = datetime.fromisoformat(_ago(1100))
    assert f"{crossed:%b} {crossed.year}" in notes[0]["text"]


def test_moving_toward_the_range_is_mentioned_in_a_good_tone(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(100), 8.0, "high", code="4548-4", title="A1c", unit="%", high=5.6)
    _lab(pid, src, 2, _ago(10), 7.0, "high", code="4548-4", title="A1c", unit="%", high=5.6)
    notes = insights._lab_notes(pid)
    assert [(n["title"], n["tone"]) for n in notes] == [("A1c closer to the range", "good")]
    assert "still above" in notes[0]["text"]


def test_back_in_range_is_mentioned(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(100), 6.4, "high", code="4548-4", title="A1c", unit="%", high=5.6)
    _lab(pid, src, 2, _ago(10), 5.4, "normal", code="4548-4", title="A1c", unit="%", high=5.6)
    notes = insights._lab_notes(pid)
    assert [(n["title"], n["tone"]) for n in notes] == [("A1c back in range", "good")]


# ---------------------------------------------------------------------------
# F266: order and age of flagged results
# ---------------------------------------------------------------------------

def test_flagged_results_put_critical_first_and_mark_old_ones(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(3650), 260, "high", code="2093-3", title="Cholesterol", unit="mg/dL", high=200)
    _lab(pid, src, 2, _ago(30), 5.2, "high", code="2823-3", title="Potassium", unit="mmol/L", high=5.0)
    _lab(pid, src, 3, _ago(60), 6.9, "critical_high", code="2951-2", title="Sodium", unit="mmol/L", high=145)
    flagged = records.flagged_labs(pid)
    assert [l["title"] for l in flagged] == ["Sodium", "Potassium", "Cholesterol"]
    assert [bool(l.get("older")) for l in flagged] == [False, False, True]
    assert records.summary(pid)["flagged_labs"][0]["title"] == "Sodium"


# ---------------------------------------------------------------------------
# F021: medications
# ---------------------------------------------------------------------------

def _med(pid, src, rid, day, title="Lisinopril 10 mg", status="active", intent="order", key="med|314076", resource="MedicationRequest"):
    records.upsert_records(pid, src, "Clinic", [{
        "resource_type": resource, "resource_id": rid, "category": "medications", "title": title, "status": status,
        "effective_at": f"{day}T09:00:00Z", "dedup_key": key, "details": {"intent": intent}}])


def test_a_renewal_is_not_a_start(client):
    pid = _prof()
    src = _source(pid, "Clinic")
    _med(pid, src, "m1", "2019-01-15")
    _med(pid, src, "m2", _ago(45))
    meds = [m for m in insights.markers(pid) if m["kind"] == "medications"]
    assert [m["t"] for m in meds] == ["2019-01-15"]
    assert meds[0]["name"] == "Lisinopril 10 mg" and meds[0]["label"] == "Ordered Lisinopril 10 mg"
    assert meds[0]["orders"] == 2 and meds[0]["last_ordered"] == _ago(45)
    # Inside a window that only holds the renewal, there's no start to mark.
    assert [m for m in insights.markers(pid, start=_ago(90)) if m["kind"] == "medications"] == []
    # The chart code reads these keys.
    assert {"t", "label", "kind", "record_id", "status"} <= set(meds[0])


@pytest.mark.parametrize("field,value", [("status", "cancelled"), ("status", "draft"), ("status", "entered-in-error"), ("status", "not-taken"),
                                         ("intent", "proposal"), ("intent", "plan"), ("intent", "option")])
def test_orders_that_never_happened_are_not_on_charts(client, field, value):
    pid = _prof()
    _med(pid, _source(pid, "Clinic"), "m1", _ago(45), title="Prednisone", key="med|8640", **{field: value})
    assert insights.markers(pid) == []


def test_a_cancelled_renewal_does_not_move_or_erase_the_real_first_order(client):
    pid = _prof()
    src = _source(pid, "Clinic")
    _med(pid, src, "m0", _ago(800), status="cancelled")
    _med(pid, src, "m1", _ago(400))
    _med(pid, src, "m2", _ago(50), status="cancelled")
    meds = insights.markers(pid)
    assert [m["t"] for m in meds] == [_ago(400)] and meds[0]["orders"] == 1


def test_a_statement_of_taking_it_says_started(client):
    pid = _prof()
    _med(pid, _source(pid, "Clinic"), "m1", _ago(100), resource="MedicationStatement")
    assert insights.markers(pid)[0]["label"] == "Started Lisinopril 10 mg"


def test_medication_note_only_follows_a_first_order_and_says_so(client):
    pid = _prof()
    src = _source(pid, "Clinic")
    _med(pid, src, "old1", "2019-01-15", title="Lisinopril 10 mg")
    _med(pid, src, "old2", _ago(45), title="Lisinopril 10 mg")
    _med(pid, src, "new", _ago(45), title="Metformin 500 mg", key="med|860975")
    _med(pid, src, "unknown", _ago(45), title="Atenolol", key="med|197381", status="unknown")
    # Sixty days around that date, with a large plain step in resting heart rate.
    rhr = _noisy(60, 0.8, 45) + _noisy(52, 0.8, 45, seed=8)
    _daily(pid, "resting_heart_rate", rhr, unit="bpm")
    notes = insights._medication_notes(pid)
    assert [n["data"]["medication"] for n in notes] == ["Metformin 500 mg"]
    assert "first order on record for Metformin 500 mg" in notes[0]["text"] and "you started" not in notes[0]["text"]


def test_medication_note_compares_one_device_with_itself(client):
    pid = _prof()
    src = _source(pid, "Clinic")
    _med(pid, src, "new", _ago(45), title="Metformin 500 mg", key="med|860975")
    _daily(pid, "resting_heart_rate", _noisy(60, 0.8, 45), source="Apple Watch", unit="bpm", end_days_ago=45)
    _daily(pid, "resting_heart_rate", _noisy(52, 0.8, 45), source="Oura Ring", unit="bpm")
    assert insights._medication_notes(pid) == []


def test_medication_statement_date_is_when_it_began_not_when_it_was_noted():
    from app.connectors import fhir_normalize
    rec = fhir_normalize.normalize_medication_statement({
        "resourceType": "MedicationStatement", "id": "s", "status": "active", "dateAsserted": "2026-01-01",
        "effectivePeriod": {"start": "2019-03-01"}, "medicationCodeableConcept": {"text": "Lisinopril"}})
    assert rec["effective_at"].startswith("2019-03-01")


# ---------------------------------------------------------------------------
# F071: urgent readings and results
# ---------------------------------------------------------------------------

URGENT_WORDS = "Talk to your care team, or call emergency services if you feel unwell."


@pytest.mark.parametrize("metric,value,title", [
    ("oxygen_saturation", 84, "Blood oxygen reading is unusually low"),
    ("blood_glucose", 42, "Blood glucose reading is unusually low"),
    ("blood_glucose", 340, "Blood glucose reading is unusually high"),
    ("body_temperature", 40.4, "Body temperature reading is unusually high"),
    ("resting_heart_rate", 142, "Resting heart rate reading is unusually high"),
])
def test_an_urgent_device_reading_comes_first_and_says_what_to_do(client, metric, value, title):
    pid = _prof()
    _daily(pid, "step_count", [5000] * 20)
    _daily(pid, metric, [value], unit="x")
    goals.set_goal(pid, "step_count", 1000)
    found = client.get("/api/insights").json()["insights"]
    assert found[0]["kind"] == "urgent" and found[0]["title"] == title and found[0]["urgent"] is True
    assert URGENT_WORDS in found[0]["text"]
    assert not any(w in found[0]["text"].lower() for w in ("hypoxemia", "hypoglyc", "fever", "tachycard", "diagnos"))
    assert client.get("/api/insights/urgent").json()["urgent"][0]["title"] == title


def test_blood_pressure_is_one_note_with_both_numbers(client):
    pid = _prof()
    _daily(pid, "blood_pressure_systolic", [198], unit="mmHg")
    _daily(pid, "blood_pressure_diastolic", [124], unit="mmHg")
    urgent = insights.urgent(pid)
    assert len(urgent) == 1 and "198/124" in urgent[0]["text"] and urgent[0]["title"] == "Blood pressure reading is unusually high"


@pytest.mark.parametrize("systolic,diastolic,fires", [(180, 120, False), (181, 80, True), (130, 121, True)])
def test_blood_pressure_limit_is_above_180_or_above_120(client, systolic, diastolic, fires):
    pid = _prof()
    _daily(pid, "blood_pressure_systolic", [systolic], unit="mmHg")
    _daily(pid, "blood_pressure_diastolic", [diastolic], unit="mmHg")
    assert bool(insights.urgent(pid)) is fires


@pytest.mark.parametrize("metric,value,fires", [
    ("oxygen_saturation", 92, False), ("oxygen_saturation", 91, True), ("resting_heart_rate", 41, False), ("resting_heart_rate", 40, True),
    ("resting_heart_rate", 130, False), ("resting_heart_rate", 131, True), ("respiratory_rate", 9, False), ("respiratory_rate", 8, True),
    ("respiratory_rate", 24, False), ("respiratory_rate", 25, True), ("body_temperature", 39.3, False), ("body_temperature", 39.4, True),
    ("body_temperature", 35.1, False), ("body_temperature", 35.0, True), ("blood_glucose", 54, False), ("blood_glucose", 53, True),
    ("blood_glucose", 300, False), ("blood_glucose", 301, True),
])
def test_each_limit_at_its_edge(client, metric, value, fires):
    pid = _prof()
    _daily(pid, metric, [value], unit="x")
    assert bool(insights.urgent(pid)) is fires


@pytest.mark.parametrize("metric,value", [("oxygen_saturation", 5), ("body_temperature", 101.3), ("blood_glucose", 5.5), ("resting_heart_rate", 400)])
def test_impossible_readings_do_not_escalate(client, metric, value):
    pid = _prof()
    _daily(pid, metric, [value], unit="x")
    assert insights.urgent(pid) == []


def test_an_old_reading_does_not_stay_urgent(client):
    pid = _prof()
    _daily(pid, "oxygen_saturation", [84, 97, 97, 97, 97], unit="%")
    assert insights.urgent(pid) == []


def test_a_reading_with_nothing_after_it_stops_being_urgent_after_a_few_days(client):
    pid = _prof()
    _daily(pid, "oxygen_saturation", [84, None, None], unit="%")
    assert len(insights.urgent(pid)) == 1
    pid2 = profiles.create_profile("Sam")["id"]
    _daily(pid2, "oxygen_saturation", [84, None, None, None], unit="%")
    assert insights.urgent(pid2) == []


def test_a_low_number_that_is_this_persons_usual_is_not_urgent(client):
    pid = _prof()
    _daily(pid, "resting_heart_rate", _noisy(39, 1, 40), unit="bpm")
    assert insights.urgent(pid) == []
    pid2 = profiles.create_profile("Sam")["id"]
    _daily(pid2, "resting_heart_rate", _noisy(58, 1, 40) + [38], unit="bpm")
    assert len(insights.urgent(pid2)) == 1


def test_simulated_data_never_escalates(client):
    pid = _prof()
    _daily(pid, "oxygen_saturation", [84], source="Oura (simulated)", unit="%")
    assert insights.urgent(pid) == []


def test_critical_lab_results_are_escalated_but_not_forever(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(5), 6.9, "critical_high", code="2823-3", title="Potassium", unit="mmol/L", low=3.5, high=5.0, ref_text="3.5–5.0")
    _lab(pid, src, 2, _ago(200), 1.9, "critical_low", code="2601-3", title="Magnesium", unit="mg/dL", low=1.7)
    urgent = insights.urgent(pid)
    assert [u["title"] for u in urgent] == ["Potassium was flagged critical by the lab"]
    assert "6.9 mmol/L" in urgent[0]["text"] and URGENT_WORDS in urgent[0]["text"]
    assert urgent[0]["data"]["code"] == "2823-3"


def test_a_critical_lab_is_not_said_twice(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(100), 5.6, "high", code="2823-3", title="Potassium", unit="mmol/L", high=5.0)
    _lab(pid, src, 2, _ago(5), 6.9, "critical_high", code="2823-3", title="Potassium", unit="mmol/L", high=5.0)
    found = insights.insights(pid)
    assert [i["kind"] for i in found] == ["urgent"]


@pytest.mark.parametrize("event_type,name,title", [
    ("irregular_heart_rhythm_event", "Irregular Rhythm Notification", "Your watch noticed an irregular rhythm"),
    ("high_heart_rate_event", "High Heart Rate Notification", "Your watch noticed a high heart rate"),
    ("low_heart_rate_event", "Low Heart Rate Notification", "Your watch noticed a low heart rate"),
])
def test_watch_notifications_are_brought_forward(client, event_type, name, title):
    pid = _prof()
    activity.insert_events(pid, None, [{"id": "e1", "event_type": event_type, "category": "events", "name": name,
                                        "start": _noon(2).isoformat(), "end": _noon(2).isoformat(), "source_name": "Apple Watch"}])
    urgent = insights.urgent(pid)
    assert [u["title"] for u in urgent] == [title]
    assert "care team" in urgent[0]["text"]


def test_an_old_watch_notification_is_not_brought_forward(client):
    pid = _prof()
    activity.insert_events(pid, None, [{"id": "e1", "event_type": "irregular_heart_rhythm_event", "category": "events",
                                        "name": "Irregular Rhythm Notification", "start": _noon(90).isoformat(),
                                        "end": _noon(90).isoformat(), "source_name": "Apple Watch"}])
    assert insights.urgent(pid) == []


# ---------------------------------------------------------------------------
# F075: a new device isn't a health change
# ---------------------------------------------------------------------------

def test_a_new_device_is_reported_as_that_not_as_a_health_change(client):
    pid = _prof()
    _daily(pid, "resting_heart_rate", _noisy(61.8, 1.2, 52), source="Apple Watch", unit="bpm", end_days_ago=8)
    _daily(pid, "resting_heart_rate", _noisy(53.9, 1.0, 8), source="Oura Ring", unit="bpm")
    _daily(pid, "hrv_rmssd", _noisy(35.8, 3, 52), source="Apple Watch", unit="ms", end_days_ago=8)
    _daily(pid, "hrv_rmssd", _noisy(64.7, 3, 8), source="Oura Ring", unit="ms")
    found = insights._changes(pid)
    assert [i["kind"] for i in found] == ["device"]
    note = found[0]
    assert note["tone"] == "info" and note["data"]["to"] == "Oura Ring" and note["data"]["from"] == "Apple Watch"
    assert "aren't comparable" in note["text"] and "Resting heart rate" in note["text"] and "HRV (RMSSD)" in note["text"]


def test_a_real_change_is_found_within_one_device_while_another_also_records(client):
    pid = _prof()
    _daily(pid, "resting_heart_rate", _noisy(55, 0.8, 60) + _noisy(63, 0.8, 7), source="Apple Watch", unit="bpm")
    # A phone app that once won some days, with quite different numbers.
    _daily(pid, "resting_heart_rate", _noisy(70, 1, 10), source="iPhone", unit="bpm", end_days_ago=30)
    found = [i for i in insights._changes(pid) if i["kind"] == "change"]
    assert len(found) == 1 and found[0]["data"]["usual"] == pytest.approx(55, abs=1)


def test_flicking_between_sources_day_to_day_is_not_a_device_change():
    points = [{"day": (datetime(2026, 1, 1) + timedelta(days=i)).date().isoformat(), "source": "Apple Watch" if i % 3 else "iPhone"}
              for i in range(60)]
    assert insights.device_changes(points) == []


def test_device_changes_give_the_day_it_happened():
    points = [{"day": (datetime(2026, 1, 1) + timedelta(days=i)).date().isoformat(), "source": "Apple Watch" if i < 40 else "Oura Ring"}
              for i in range(60)]
    assert insights.device_changes(points) == [{"day": "2026-02-10", "from": "Apple Watch", "to": "Oura Ring"}]


# ---------------------------------------------------------------------------
# F077: tone at the extremes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("metric,usual,now,unit", [
    ("resting_heart_rate", 55.0, 35.4, "bpm"), ("respiratory_rate", 14.9, 7.9, "br/min"),
])
def test_a_big_move_past_a_plausible_value_is_never_good_news(client, metric, usual, now, unit):
    pid = _prof()
    _daily(pid, metric, _noisy(usual, usual * 0.02, 60) + _noisy(now, 0.3, 7), unit=unit)
    item = next(i for i in insights._changes(pid) if i["kind"] == "change")
    assert item["tone"] == "attention"
    assert "worth mentioning to your clinician" in item["text"] and "worth mentioning to your clinician" in item["data"]["note"]


def test_twelve_hours_of_sleep_is_not_good_news(client):
    pid = _prof()
    # Hours of sleep arrive as stage samples, one a night here.
    samples = []
    for i, h in enumerate(_noisy(8.08, 0.15, 60) + _noisy(12.07, 0.1, 7)):
        wake = _noon(66 - i).replace(hour=8)
        samples.append({"id": f"s{i}", "metric_type": "sleep_analysis_asleep", "value": h * 3600, "unit": "s", "source_name": "Apple Watch",
                        "start_date": (wake - timedelta(hours=h)).isoformat(), "end_date": wake.isoformat()})
    biometrics.insert_samples(pid, None, samples)
    item = next(i for i in insights._changes(pid) if i["kind"] == "change")
    assert item["metric"] == "sleep_duration" and item["tone"] == "attention"


def test_a_moderate_healthy_move_stays_good(client):
    pid = _prof()
    _daily(pid, "resting_heart_rate", _noisy(66, 0.8, 60) + _noisy(60, 0.8, 7), unit="bpm")
    item = next(i for i in insights._changes(pid) if i["kind"] == "change")
    assert item["tone"] == "good" and "clinician" not in item["text"]


def test_a_large_move_in_a_good_direction_is_neutral(client):
    pid = _prof()
    _daily(pid, "hrv_rmssd", _noisy(30, 1.5, 60) + _noisy(48, 1.5, 7), unit="ms")
    item = next(i for i in insights._changes(pid) if i["kind"] == "change")
    assert item["tone"] == "info"


def test_change_text_uses_typical_values_and_never_claims_a_cause(client):
    pid = _prof()
    _daily(pid, "resting_heart_rate", _noisy(55, 0.8, 60) + _noisy(63, 0.8, 7), unit="bpm")
    text = insights._changes(pid)[0]["text"]
    assert "because" not in text and "was typically" in text


# ---------------------------------------------------------------------------
# F076, F338: links
# ---------------------------------------------------------------------------

def _checkins(pid, values):
    activity.insert_events(pid, None, [
        {"id": f"c{i}", "event_type": "check_in", "category": "stateOfMind", "name": "Check-in", "value": v,
         "start": _noon(len(values) - 1 - i).isoformat(), "end": _noon(len(values) - 1 - i).isoformat(), "source_name": "Syntropy"}
        for i, v in enumerate(values)])


def _sleep_daily(pid, hours):
    samples = []
    for i, h in enumerate(hours):
        wake = _noon(len(hours) - 1 - i).replace(hour=8)
        samples.append({"id": f"s{i}", "metric_type": "sleep_analysis_asleep", "value": h * 3600, "unit": "s", "source_name": "Apple Watch",
                        "start_date": (wake - timedelta(hours=h)).isoformat(), "end_date": wake.isoformat()})
    biometrics.insert_samples(pid, None, samples)


def test_a_real_sleep_and_mood_link_is_found_with_its_uncertainty_and_a_caution(client):
    pid = _prof()
    rng = random.Random(3)
    hours = [rng.gauss(7, 1) for _ in range(60)]
    _sleep_daily(pid, hours)
    _checkins(pid, [max(-1, min(1, (h - 7) * 0.5 + rng.gauss(0, 0.1))) for h in hours])
    link = next(i for i in insights._links(pid) if i["id"] == "link:sleep-mood")
    assert "give or take" in link["text"] and "coincidence" in link["text"] and "for you" not in link["text"]
    assert link["data"]["n"] >= 55 and "margin" in link["data"]


def test_too_few_days_never_become_a_link(client):
    pid = _prof()
    hours = [7 + 2 * (i % 2) for i in range(25)]
    _sleep_daily(pid, hours)
    _checkins(pid, [(h - 8) * 0.9 for h in hours])
    assert [i for i in insights._links(pid) if i["id"].startswith("link:sleep")] == []


def test_sleep_cut_never_prints_sixty_minutes(client):
    pid = _prof()
    hours = [6.5 + 1.0 * (i % 2) - 0.01 * (i % 2 == 0) for i in range(60)]
    rng = random.Random(5)
    hours = [6.99 if i % 2 else 7.0 for i in range(30)] + [5.5 + rng.random() for _ in range(15)] + [8 + rng.random() for _ in range(15)]
    rng.shuffle(hours)
    _sleep_daily(pid, hours)
    _checkins(pid, [(1 if h >= 7.0 else -1) * 0.8 for h in hours])
    links = [i for i in insights._links(pid) if i["id"] == "link:sleep-mood"]
    assert links and "60m" not in links[0]["text"]


def test_symptom_days_are_compared_with_the_days_without_it(client):
    pid = _prof()
    rng = random.Random(4)
    hours = [rng.gauss(7.5, 0.4) for _ in range(80)]
    on = set(range(0, 80, 6))
    for i in on:
        hours[i] -= 1.5
    _sleep_daily(pid, hours)
    activity.insert_events(pid, None, [
        {"id": f"h{i}", "event_type": "headache", "category": "symptoms", "name": "Headache",
         "start": _noon(79 - i).isoformat(), "end": _noon(79 - i).isoformat(), "source_name": "Syntropy"} for i in on])
    link = next(i for i in insights._links(pid) if i["id"] == "link:symptom-Headache")
    assert f"On the {len(on)} days you logged headache" in link["text"] and "less" in link["text"]
    assert "days you didn't" in link["text"] and "give or take" in link["text"] and "coincidence" in link["text"]
    # The gap is against the other days: about 90 minutes here, where all days together would give about 75.
    assert 75 <= abs(link["data"]["gap_h"]) * 60 <= 105


def test_few_symptom_days_are_not_a_pattern(client):
    pid = _prof()
    rng = random.Random(4)
    hours = [rng.gauss(7.5, 0.3) for _ in range(60)]
    on = {3, 13, 23, 33, 43}
    for i in on:
        hours[i] -= 2
    _sleep_daily(pid, hours)
    activity.insert_events(pid, None, [
        {"id": f"h{i}", "event_type": "headache", "category": "symptoms", "name": "Headache",
         "start": _noon(59 - i).isoformat(), "end": _noon(59 - i).isoformat(), "source_name": "Syntropy"} for i in on])
    assert insights._links(pid) == []


# ---------------------------------------------------------------------------
# F300, F184
# ---------------------------------------------------------------------------

def test_a_partial_day_does_not_count_toward_an_at_most_goal(client):
    pid = _prof()
    _daily(pid, "dietary_sugar", [20], unit="g")
    goals.set_goal(pid, "dietary_sugar", 50, "max")
    p = goals.progress(pid, 7)[0]
    assert p["met"] == 0 and p["streak"] == 0 and p["days"] == 0


def test_a_partial_day_counts_toward_an_at_least_goal_only_once_met(client):
    pid = _prof()
    _daily(pid, "step_count", [9000, 9500], unit="count")
    goals.set_goal(pid, "step_count", 8000, "min")
    assert goals.progress(pid, 7)[0]["met"] == 2
    goals.set_goal(pid, "step_count", 9800, "min")
    p = goals.progress(pid, 7)[0]
    assert p["met"] == 0 and p["days"] == 1


def test_hardest_days_are_the_hardest_of_the_workout_days(client, monkeypatch):
    # 40 workout days in 90: only the top quarter count as hardest, and lighter sessions aren't among them.
    start, end = insights._window(90)
    days = [(start + timedelta(days=i)).isoformat() for i in range(90)]
    load = {d: 0.0 for d in days}
    rng = random.Random(9)
    picked = rng.sample(days[:-1], 40)
    for i, d in enumerate(sorted(picked)):
        load[d] = 300.0 if i % 4 == 0 else 50.0
    hrv = {}
    for i, d in enumerate(days):
        prev = load[days[i - 1]] if i else 0
        hrv[d] = (30.0 if prev == 300.0 else 50.0) + rng.gauss(0, 1)
    pid = _prof()
    with monkeypatch.context() as patch:
        patch.setattr(insights, "workout_daily", lambda *_: {"load": load, "minutes": load})
        patch.setattr(biometrics, "daily_series", lambda _p, m, days=0, **_: {
            "points": [{"day": d, "value": v, "source": "Apple Watch"} for d, v in hrv.items()] if m == "hrv_rmssd" else []})
        candidates = insights._hard_day_links(pid)
    assert candidates and candidates[0][0]["data"]["after_hard"] == pytest.approx(30, abs=1.5)


def test_a_broken_generator_is_logged_without_health_values_and_hides_nothing(client, monkeypatch, caplog):
    pid = _prof()
    goals.set_goal(pid, "step_count", 1000)
    _daily(pid, "step_count", [5000] * 7)

    def broken(_):
        raise ValueError("potassium 6.9")
    with monkeypatch.context() as patch:
        patch.setattr(insights, "_sleep_notes", broken)
        found = insights.insights(pid)
    assert any(i["kind"] == "goal" for i in found)
    logged = [r.getMessage() for r in caplog.records if r.name == "syntropy.insights"]
    assert len(logged) == 1 and "broken" in logged[0] and "ValueError" in logged[0] and "6.9" not in logged[0]
    assert caplog.records[-1].exc_info is None


def test_wiping_a_persons_data_also_forgets_their_goals(client):
    pid = _prof()
    goals.set_goal(pid, "step_count", 1000)
    assert client.delete(f"/api/profiles/{pid}/data").status_code == 200
    assert goals.get(pid) == {}


# ---------------------------------------------------------------------------
# F185: personal normal range, hidden insights
# ---------------------------------------------------------------------------

def test_personal_range_ignores_one_wild_reading_and_is_available_to_charts(client):
    pid = _prof()
    values = _noisy(58, 1.5, 80)
    values[40] = 180
    _daily(pid, "resting_heart_rate", values, unit="bpm")
    res = client.get("/api/insights/baseline", params={"metric": "resting_heart_rate", "days": 80}).json()
    assert res["enough"] is True and res["source"] == "Apple Watch"
    assert 55 < res["low"] < 58 < res["high"] < 62 and res["median"] == pytest.approx(58, abs=1)
    assert res["n"] >= 30 and res["unit"] == "bpm"
    assert res["rolling"] and all(r["low"] <= r["median"] <= r["high"] for r in res["rolling"])
    assert res["rolling"] == sorted(res["rolling"], key=lambda r: r["day"])
    assert client.get("/api/insights/baseline", params={"metric": "nope"}).status_code == 400


def test_personal_range_says_when_there_is_too_little_to_go_on(client):
    pid = _prof()
    _daily(pid, "resting_heart_rate", _noisy(58, 1, 8), unit="bpm")
    res = client.get("/api/insights/baseline", params={"metric": "resting_heart_rate"}).json()
    assert res["enough"] is False and res["low"] is None and res["high"] is None


def test_personal_range_with_nothing_recorded(client):
    res = client.get("/api/insights/baseline", params={"metric": "resting_heart_rate"}).json()
    assert res["enough"] is False and res["source"] is None and res["rolling"] == [] and res["device_changes"] == []


def test_personal_range_comes_from_the_current_device_and_lists_the_switch(client):
    pid = _prof()
    _daily(pid, "resting_heart_rate", _noisy(62, 1, 40), source="Apple Watch", unit="bpm", end_days_ago=30)
    _daily(pid, "resting_heart_rate", _noisy(52, 1, 30), source="Oura Ring", unit="bpm")
    res = client.get("/api/insights/baseline", params={"metric": "resting_heart_rate", "days": 90}).json()
    assert res["source"] == "Oura Ring" and res["median"] == pytest.approx(52, abs=1)
    assert [c["to"] for c in res["device_changes"]] == ["Oura Ring"]


def test_change_data_carries_the_usual_range(client):
    pid = _prof()
    _daily(pid, "resting_heart_rate", _noisy(55, 1, 60) + _noisy(63, 1, 7), unit="bpm")
    data = insights._changes(pid)[0]["data"]
    assert data["low"] < data["usual"] < data["high"] < data["recent"]


def test_hiding_an_insight_follows_the_person_to_their_other_devices(client):
    pid = _prof()
    goals.set_goal(pid, "step_count", 1000)
    _daily(pid, "step_count", [5000] * 7)
    item = client.get("/api/insights").json()["insights"][0]
    assert client.post("/api/insights/hidden", json={"ids": [item["id"]]}).status_code == 200
    assert client.get("/api/insights").json()["insights"] == []
    assert client.get("/api/insights/hidden").json()["ids"] == [item["id"]]
    # AI apps still see everything.
    assert json.dumps(mcp_server.t_insights({})).count(item["title"]) == 1
