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


def test_moving_toward_the_range_is_stated_without_a_verdict(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(100), 8.0, "high", code="4548-4", title="A1c", unit="%", high=5.6)
    _lab(pid, src, 2, _ago(10), 7.0, "high", code="4548-4", title="A1c", unit="%", high=5.6)
    notes = insights._lab_notes(pid)
    assert [(n["title"], n["tone"]) for n in notes] == [("A1c closer to the range", "info")]
    assert "still above" in notes[0]["text"]


def test_back_in_range_is_mentioned(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(100), 6.4, "high", code="4548-4", title="A1c", unit="%", high=5.6)
    _lab(pid, src, 2, _ago(10), 5.4, "normal", code="4548-4", title="A1c", unit="%", high=5.6)
    notes = insights._lab_notes(pid)
    assert [(n["title"], n["tone"]) for n in notes] == [("A1c back in range", "info")]


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


def test_medication_statement_date_is_when_it_began_not_when_it_was_noted():
    from app.connectors import fhir_normalize
    rec = fhir_normalize.normalize_medication_statement({
        "resourceType": "MedicationStatement", "id": "s", "status": "active", "dateAsserted": "2026-01-01",
        "effectivePeriod": {"start": "2019-03-01"}, "medicationCodeableConcept": {"text": "Lisinopril"}})
    assert rec["effective_at"].startswith("2019-03-01")


# ---------------------------------------------------------------------------
# What the lab marked critical, and nothing else brought forward
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("metric,value", [
    ("oxygen_saturation", 84), ("blood_glucose", 42), ("blood_glucose", 340), ("body_temperature", 40.4),
    ("resting_heart_rate", 142), ("blood_pressure_systolic", 198),
])
def test_a_device_reading_is_never_escalated(client, metric, value):
    pid = _prof()
    _daily(pid, metric, [value], unit="x")
    assert insights.insights(pid) == []


def test_a_critical_lab_is_what_the_lab_reported_and_not_forever(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(5), 6.9, "critical_high", code="2823-3", title="Potassium", unit="mmol/L", low=3.5, high=5.0, ref_text="3.5–5.0")
    _lab(pid, src, 2, _ago(200), 1.9, "critical_low", code="2601-3", title="Magnesium", unit="mg/dL", low=1.7)
    critical = insights._critical_labs(pid)
    assert [c["title"] for c in critical] == ["Potassium: the lab marked this critical"]
    assert critical[0]["text"].endswith("The lab marked this critical.") and "6.9 mmol/L" in critical[0]["text"] and "3.5–5.0" in critical[0]["text"]
    assert critical[0]["data"]["code"] == "2823-3" and critical[0]["tone"] == "attention"


def test_a_critical_lab_is_not_said_twice(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(100), 5.6, "high", code="2823-3", title="Potassium", unit="mmol/L", high=5.0)
    _lab(pid, src, 2, _ago(5), 6.9, "critical_high", code="2823-3", title="Potassium", unit="mmol/L", high=5.0)
    found = insights.insights(pid)
    assert [i["kind"] for i in found] == ["critical"]


def test_a_critical_lab_comes_first(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(100), 5.6, "normal", code="3016-3", title="TSH", high=6.0)
    _lab(pid, src, 2, _ago(10), 7.5, "high", code="3016-3", title="TSH", high=6.0)
    _lab(pid, src, 3, _ago(5), 6.9, "critical_high", code="2823-3", title="Potassium", unit="mmol/L", high=5.0)
    assert [i["kind"] for i in insights.insights(pid)] == ["critical", "lab"]


def test_a_watch_notification_is_not_brought_forward(client):
    pid = _prof()
    activity.insert_events(pid, None, [{"id": "e1", "event_type": "irregular_heart_rhythm_event", "category": "events",
                                        "name": "Irregular Rhythm Notification", "start": _noon(2).isoformat(),
                                        "end": _noon(2).isoformat(), "source_name": "Apple Watch"}])
    assert insights.insights(pid) == []


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
    assert "aren't comparable" in note["text"] and "resting heart rate" in note["text"] and "HRV (RMSSD)" in note["text"]


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
def test_a_big_move_is_neither_good_news_nor_an_alarm(client, metric, usual, now, unit):
    pid = _prof()
    _daily(pid, metric, _noisy(usual, usual * 0.02, 60) + _noisy(now, 0.3, 7), unit=unit)
    item = next(i for i in insights._changes(pid) if i["kind"] == "change")
    assert item["tone"] == "info" and "clinician" not in item["text"] and "note" not in item["data"]


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
    assert item["metric"] == "sleep_duration" and item["tone"] == "info"


def test_a_move_in_either_direction_is_neutral(client):
    pid = _prof()
    _daily(pid, "resting_heart_rate", _noisy(66, 0.8, 60) + _noisy(60, 0.8, 7), unit="bpm")
    item = next(i for i in insights._changes(pid) if i["kind"] == "change")
    assert item["tone"] == "info" and "clinician" not in item["text"]


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


def test_a_day_with_nothing_recorded_counts_against_a_goal_and_breaks_the_streak(client):
    pid = _prof()
    # Eight days ago to yesterday, with the fifth and second days ago missing (the watch stayed on the charger).
    _daily(pid, "step_count", [9000, 9000, 9000, None, 9000, 9000, None, 9000], end_days_ago=1)
    goals.set_goal(pid, "step_count", 8000, "min")
    p = goals.progress(pid, 7)[0]
    assert (p["days"], p["met"], p["recorded"], p["streak"]) == (6, 4, 4, 1)
    assert not insights._goal_notes(pid)            # too little recorded this week to say how it went


def test_the_goal_note_counts_the_days_of_the_week_not_the_days_with_figures(client):
    pid = _prof()
    _daily(pid, "step_count", [9000, 9000, 9000, 9000, None, 9000, 9000], end_days_ago=1)
    goals.set_goal(pid, "step_count", 8000, "min")
    note = insights._goal_notes(pid)[0]
    assert note["title"].endswith("5 of 6 days") and "on 5 of the last 6 days" in note["text"]
    assert note["data"]["streak"] == 2


def test_days_before_the_first_figure_dont_count_against_a_goal(client):
    pid = _prof()
    _daily(pid, "step_count", [9000, 9000], end_days_ago=1)
    goals.set_goal(pid, "step_count", 8000, "min")
    p = goals.progress(pid, 7)[0]
    assert (p["days"], p["met"], p["streak"]) == (2, 2, 2)


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


# ---------------------------------------------------------------------------
# F185: measures that go below zero
# ---------------------------------------------------------------------------

def test_the_usual_range_of_a_measure_that_goes_negative_holds_its_median(client):
    pid = _prof()
    _daily(pid, "body_temperature_deviation", _noisy(-0.2, 0.1, 70), source="Oura", unit="degC")
    res = client.get("/api/insights/baseline", params={"metric": "body_temperature_deviation"}).json()
    assert res["enough"] and res["low"] < res["median"] < res["high"]
    assert res["median"] == pytest.approx(-0.2, abs=0.05) and res["low"] == pytest.approx(-0.4, abs=0.1)
    assert all(day["low"] < day["median"] for day in res["rolling"])


def test_the_usual_range_of_steps_stops_at_zero(client):
    pid = _prof()
    _daily(pid, "step_count", [abs(v) for v in _noisy(3000, 3000, 70)])
    res = client.get("/api/insights/baseline", params={"metric": "step_count"}).json()
    assert res["low"] == 0 and res["median"] > 0


# ---------------------------------------------------------------------------
# F266: old results
# ---------------------------------------------------------------------------

def _cholesterol(pid, first, second):
    src = _source(pid)
    _lab(pid, src, 1, first, 190, "normal", code="2093-3", title="Cholesterol", unit="mg/dL", high=200)
    _lab(pid, src, 2, second, 260, "high", code="2093-3", title="Cholesterol", unit="mg/dL", high=200)


def test_a_result_years_old_is_not_a_note_about_what_has_changed(client):
    pid = _prof()
    _cholesterol(pid, "2015-03-01", "2016-03-01")
    assert records.flagged_labs(pid)[0]["older"] is True
    assert insights._lab_notes(pid) == []
    assert [i for i in insights.insights(pid) if i["kind"] == "lab"] == []


def test_a_result_just_inside_the_older_limit_is_still_a_note(client):
    pid = _prof()
    _cholesterol(pid, _ago(records.OLDER_RESULT_DAYS + 300), _ago(records.OLDER_RESULT_DAYS - 2))
    assert [n["title"] for n in insights._lab_notes(pid)] == ["Cholesterol moved out of range"]


def test_an_old_critical_result_with_a_lower_one_before_it_stays_quiet_too(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, "2015-03-01", 7.5, "critical_high", code="2160-0", title="Creatinine", unit="mg/dL", low=0.6, high=1.2)
    _lab(pid, src, 2, "2016-03-01", 6.0, "critical_high", code="2160-0", title="Creatinine", unit="mg/dL", low=0.6, high=1.2)
    assert insights._lab_notes(pid) == []


# ---------------------------------------------------------------------------
# F077 and F071: a critical flag and a good tone
# ---------------------------------------------------------------------------

def test_a_result_still_flagged_critical_never_gets_a_good_tone(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(100), 7.5, "critical_high", code="2160-0", title="Creatinine", unit="mg/dL", low=0.6, high=1.2)
    _lab(pid, src, 2, _ago(45), 6.0, "critical_high", code="2160-0", title="Creatinine", unit="mg/dL", low=0.6, high=1.2)
    note = next(i for i in insights.insights(pid) if i["kind"] == "lab")
    assert note["tone"] == "attention" and note["title"] == "Creatinine still flagged critical"
    assert "7.5" in note["text"] and "6.0" in note["text"] and "above the reference range" in note["text"]
    assert note["text"].endswith("The lab marked this critical.") and "closer" not in note["text"]


def test_coming_down_from_critical_to_merely_high_is_stated(client):
    pid = _prof()
    src = _source(pid)
    _lab(pid, src, 1, _ago(100), 7.5, "critical_high", code="2160-0", title="Creatinine", unit="mg/dL", low=0.6, high=1.2)
    _lab(pid, src, 2, _ago(45), 2.0, "high", code="2160-0", title="Creatinine", unit="mg/dL", low=0.6, high=1.2)
    note = next(i for i in insights.insights(pid) if i["kind"] == "lab")
    assert note["tone"] == "info" and note["title"] == "Creatinine closer to the range"


# ---------------------------------------------------------------------------
# F021: what a statement says
# ---------------------------------------------------------------------------

def _statement(pid, src, status, day=None, name=None):
    _med(pid, src, f"s-{status}-{day}", day or _ago(40), title=name or f"Drug {status}", status=status, key=f"med|{name or status}",
         resource="MedicationStatement")


@pytest.mark.parametrize("status", ["active", "completed", "stopped"])
def test_a_statement_of_taking_a_drug_says_started(client, status):
    pid = _prof()
    _statement(pid, _source(pid, "Clinic"), status)
    assert [m["label"] for m in insights.markers(pid)] == [f"Started Drug {status}"]


@pytest.mark.parametrize("status", ["unknown", "on-hold"])
def test_a_statement_with_an_unsure_status_says_ordered_at_most(client, status):
    pid = _prof()
    _statement(pid, _source(pid, "Clinic"), status)
    marker = insights.markers(pid)[0]
    assert marker["label"] == f"Ordered Drug {status}" and marker["event"] == "ordered"


def test_a_drug_not_begun_yet_is_no_event(client):
    pid = _prof()
    _statement(pid, _source(pid, "Clinic"), "intended")
    assert insights.markers(pid) == []
    assert insights.medication_timeline(pid) == []


def test_a_drug_intended_first_and_taken_later_starts_when_it_was_taken(client):
    pid = _prof()
    src = _source(pid, "Clinic")
    _statement(pid, src, "intended", _ago(90), "Metformin")
    _statement(pid, src, "active", _ago(40), "Metformin")
    marker = insights.markers(pid)[0]
    assert marker["label"] == "Started Metformin" and marker["t"] == _ago(40)


# ---------------------------------------------------------------------------
# F075: the merged row isn't a device
# ---------------------------------------------------------------------------

def test_a_watch_joined_by_a_phone_is_not_a_new_device(client):
    pid = _prof()
    _daily(pid, "step_count", _noisy(8000, 500, 70))
    _daily(pid, "step_count", [300] * 7, source="iPhone")
    biometrics.rebuild_daily(pid)
    assert biometrics.daily_series(pid, "step_count", days=3)["points"][-2]["source"] == biometrics.MERGED_SOURCE
    assert [i for i in insights._changes(pid) if i["kind"] == "device"] == []
    assert insights.personal_range(pid, "step_count")["device_changes"] == []
    assert "Combined" not in json.dumps(insights.insights(pid))


def test_a_real_change_is_found_across_the_watch_and_its_merged_days(client):
    pid = _prof()
    _daily(pid, "step_count", _noisy(9000, 500, 70), end_days_ago=7)
    _daily(pid, "step_count", _noisy(3000, 200, 7))
    _daily(pid, "step_count", [100] * 7, source="iPhone")
    biometrics.rebuild_daily(pid)
    found = [i for i in insights._changes(pid) if i.get("metric") == "step_count"]
    assert len(found) == 1 and found[0]["data"]["usual"] > 8000 and found[0]["title"] == "Steps is down"


def test_the_usual_range_carries_on_across_the_merged_days(client):
    pid = _prof()
    _daily(pid, "step_count", _noisy(8000, 500, 70))
    _daily(pid, "step_count", [300] * 7, source="iPhone")
    biometrics.rebuild_daily(pid)
    res = insights.personal_range(pid, "step_count")
    assert res["enough"] and res["median"] == pytest.approx(8000, abs=300)


def test_from_the_phone_alone_to_merged_is_a_change_in_plain_words(client):
    pid = _prof()
    _daily(pid, "step_count", _noisy(4000, 300, 70), source="iPhone", end_days_ago=5)
    _daily(pid, "step_count", _noisy(9000, 300, 5))
    _daily(pid, "step_count", [100] * 5, source="iPhone")
    biometrics.rebuild_daily(pid)
    note = next(i for i in insights._changes(pid) if i["kind"] == "device")
    assert note["title"] == "Now measured by your devices together"
    assert "Combined" not in note["text"] and "your devices together" in note["text"] and "instead of iPhone" in note["text"]
    assert "the numbers for steps come from" in note["text"]


@pytest.mark.parametrize("names,expected", [
    (["Apple Watch"] * 8 + ["Combined"] * 8, []),
    (["Combined"] * 8 + ["Apple Watch"] * 8, []),
    (["Apple Watch"] * 8 + ["Combined"] * 8 + ["Apple Watch"] * 8, []),
    (["Fitbit"] * 8 + ["Combined"] * 8, []),
    (["iPhone"] * 8 + ["Combined"] * 8, [("iPhone", "Combined")]),
    (["Combined"] * 8 + ["iPhone"] * 8, [("Combined", "iPhone")]),
    # Oura sends whole days, so it's never part of what's merged.
    (["Oura"] * 8 + ["Combined"] * 8, [("Oura", "Combined")]),
    (["Apple Watch"] * 8 + ["Combined"] * 8 + ["Oura"] * 8, [("Apple Watch", "Oura")]),
])
def test_which_moves_to_and_from_the_merged_row_are_device_changes(names, expected):
    points = [{"day": (datetime(2026, 1, 1) + timedelta(days=i)).date().isoformat(), "source": s} for i, s in enumerate(names)]
    assert [(c["from"], c["to"]) for c in insights.device_changes(points)] == expected


# ---------------------------------------------------------------------------
# F181: what AI apps are told about a comparison
# ---------------------------------------------------------------------------

def test_the_compare_tool_names_the_right_statistic_and_gives_its_interval(client):
    pid = _prof()
    xs = _noisy(40, 5, 60)
    _daily(pid, "hrv_rmssd", xs, unit="ms")
    _daily(pid, "resting_heart_rate", [100 - x for x in xs], unit="bpm")
    tool = mcp_server.t_compare({"a": "hrv_rmssd", "b": "resting_heart_rate"})
    direct = insights.compare(pid, "hrv_rmssd", "resting_heart_rate")
    assert "Pearson" not in tool["note"] and "rank correlation" in tool["note"] and "trend" in tool["note"]
    assert "interval" in tool["note"] and "pairs" in tool["note"]
    assert tool["interval"] == direct["interval"] and tool["interval"]["low"] <= tool["r"] <= tool["interval"]["high"]


def test_a_persons_own_time_zone_decides_what_today_is(client):
    from zoneinfo import ZoneInfo
    from app.core import settings
    from app.store import goals, profiles
    pid = _prof()
    settings.set("display.timezone", "Pacific/Pago_Pago")       # a day behind the zone below, whatever the time
    profiles.update_profile(pid, timezone="Pacific/Kiritimati")
    their_today = datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
    assert insights._today(pid) == their_today and insights._today() != their_today
    assert insights._window(1, profile_id=pid)[1] == their_today
    assert their_today.isoformat() in ai.system_prompt(profiles.get_profile(pid)) or f"{their_today:%B}" in ai.system_prompt(profiles.get_profile(pid))
    assert goals.progress(pid) == []


def test_a_medication_filed_as_a_plan_counts_when_someone_reported_taking_it(client):
    pid = _prof()
    src = _source(pid, "Clinic")
    records.upsert_records(pid, src, "Clinic", [
        {"resource_type": "MedicationRequest", "resource_id": "home", "category": "medications", "title": "Home drug", "status": "active",
         "effective_at": f"{_ago(40)}T09:00:00Z", "dedup_key": "med|home", "details": {"intent": "plan", "reported": True}},
        {"resource_type": "MedicationRequest", "resource_id": "idea", "category": "medications", "title": "Just a plan", "status": "active",
         "effective_at": f"{_ago(40)}T09:00:00Z", "dedup_key": "med|idea", "details": {"intent": "plan"}}])
    assert [m["label"] for m in insights.markers(pid)] == ["Started Home drug"]
