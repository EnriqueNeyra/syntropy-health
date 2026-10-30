"""
The calculations everything else stands on, at exact values, so that breaking one fails a test. To regenerate the
golden snapshot after a deliberate change, run with UPDATE_INSIGHTS_GOLDEN=1 and read the diff.
"""

from __future__ import annotations

import json
import os
import random
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.connectors import units
from app.services import health_limits, insight_stats, insights, sync
from app.simulator.generator import build_chart
from app.simulator.personas import PERSONAS
from app.store import activity, biometrics, goals, profiles, sleep

GOLDEN = Path(__file__).parent / "fixtures" / "insights_golden.json"
LOS_ANGELES = ZoneInfo("America/Los_Angeles")


# ---------------------------------------------------------------------------
# Time: which day a reading belongs to
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("given,utc", [
    ("2024-03-01T23:30:00-08:00", "2024-03-02T07:30:00Z"),
    ("2024-03-01T23:30:00+05:30", "2024-03-01T18:00:00Z"),
    ("2024-03-01T07:30:00Z", "2024-03-01T07:30:00Z"),
    ("2024-05-15 07:30:00 -0700", "2024-05-15T14:30:00Z"),     # the HealthKit export style
    ("2024-03-01T07:30:00", "2024-03-01T07:30:00Z"),           # no offset is taken as UTC
    ("2024-03-01", "2024-03-01T12:00:00Z"),                    # a bare date is its middle
])
def test_timestamps_become_utc_without_losing_their_offset(given, utc):
    assert biometrics.to_utc_iso(given) == utc


@pytest.mark.parametrize("given,day", [
    ("2024-03-01T23:30:00-08:00", "2024-03-01"),      # late evening stays that evening
    ("2024-03-02T07:30:00Z", "2024-03-01"),           # the same moment, written in UTC
    ("2024-03-10T09:59:00Z", "2024-03-10"),           # 01:59 PST, just before the clocks change
    ("2024-03-10T10:00:00Z", "2024-03-10"),           # 03:00 PDT, just after
    ("2024-03-11T06:59:00Z", "2024-03-10"),           # 23:59 PDT
    ("2024-03-11T07:00:00Z", "2024-03-11"),           # midnight PDT: an hour earlier in UTC than the day before
    ("2024-11-03T07:30:00Z", "2024-11-03"),           # 00:30 PDT, the morning they go back
    ("2024-11-04T07:59:00Z", "2024-11-03"),           # 23:59 PST: that day has 25 hours
])
def test_the_local_day_follows_the_time_zone_across_daylight_saving(given, day):
    assert biometrics.local_day(given, LOS_ANGELES) == day


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code,value,unit,expected", [
    ("29463-7", 180, "[lb_av]", (81.647, "kg")), ("29463-7", 180, "lbs", (81.647, "kg")), ("29463-7", 180, "LB", (81.647, "kg")),
    ("3141-9", 80, "kg", (80.0, "kg")), ("29463-7", 80000, "g", (80.0, "kg")), ("29463-7", 16, "[oz_av]", (0.454, "kg")),
    ("8302-2", 70, "[in_i]", (177.8, "cm")), ("8306-3", 1.8, "m", (180.0, "cm")), ("8302-2", 6, "[ft_i]", (182.88, "cm")),
    ("8302-2", 1800, "mm", (180.0, "cm")),
    ("8310-5", 98.6, "[degF]", (37.0, "°C")), ("8331-1", 100.4, "°F", (38.0, "°C")), ("8310-5", 37.1, "Cel", (37.1, "°C")),
    ("2345-7", 5.5, "mmol/L", (99.1, "mg/dL")), ("2339-0", 5.5, "mmol/L", (99.1, "mg/dL")), ("2345-7", 99, "mg/dL", (99, "mg/dL")),
    ("2093-3", 5, "mmol/L", (193.4, "mg/dL")), ("13457-7", 2.5, "mmol/L", (96.7, "mg/dL")), ("2085-9", 1.2, "mmol/L", (46.4, "mg/dL")),
    ("2571-8", 1.5, "mmol/L", (132.9, "mg/dL")),
    ("2160-0", 88.42, "umol/L", (1.0, "mg/dL")), ("2160-0", 88.42, "µmol/L", (1.0, "mg/dL")), ("2160-0", 1.1, "mg/dL", (1.1, "mg/dL")),
    ("718-7", 135, "g/L", (13.5, "g/dL")), ("718-7", 13.5, "g/dL", (13.5, "g/dL")),
])
def test_every_unit_conversion(code, value, unit, expected):
    got = units.normalize(code, value, unit)
    assert got[0] == pytest.approx(expected[0], abs=1e-6) and got[1] == expected[1]


@pytest.mark.parametrize("code,value,unit,expected", [
    (None, 5, "mg/dL", (5, "mg/dL")), ("unknown", 5, " x ", (5, "x")), ("unknown", 5, None, (5, None)),
    ("2345-7", None, "mg/dL", (None, "mg/dL")),
    # Not a known unit for the test, so it's left as reported rather than guessed at.
    ("2345-7", 5, "furlongs", (5, "furlongs")), ("2160-0", 5, "mmol/L", (5, "mmol/L")), ("29463-7", 5, "stone", (5, "stone")),
])
def test_values_that_cannot_be_converted_stay_as_reported(code, value, unit, expected):
    assert units.normalize(code, value, unit) == expected


@pytest.mark.parametrize("given,shown", [("[lb_av]", "lb"), ("Cel", "°C"), ("mm[Hg]", "mmHg"), ("{score}", None), ("{beats}/min", "/min"),
                                         ("mg/dL", "mg/dL"), (None, None), ("m[IU]/L", "mIU/L")])
def test_units_are_shown_plainly(given, shown):
    assert units.pretty_unit(given) == shown


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def test_robust_center_ignores_a_wild_value_and_handles_identical_ones():
    assert insight_stats.robust_center([1, 2, 3, 4, 500]) == (3, pytest.approx(1.4826))
    assert insight_stats.robust_center([5, 5, 5, 5, 6])[1] == pytest.approx(0.4)


def test_effective_sample_size():
    assert insight_stats.effective_n(60, 0.0) == 60
    assert insight_stats.effective_n(60, 0.5) == pytest.approx(20)
    assert insight_stats.effective_n(7, 0.95) == 1


def test_correlated_values_read_as_correlated():
    rng = random.Random(1)
    assert insight_stats.lag1([rng.gauss(0, 1) for _ in range(300)]) < 0.2
    x, out = 0.0, []
    for _ in range(300):
        x = 0.8 * x + rng.gauss(0, 1)
        out.append(x)
    assert insight_stats.lag1(out) == pytest.approx(0.8, abs=0.1)


@pytest.mark.parametrize("t,df,p", [(0, 10, 1.0), (2.228, 10, 0.05), (2.086, 20, 0.05), (2.845, 20, 0.01), (1.96, 1000, 0.05)])
def test_t_two_sided_against_published_tables(t, df, p):
    assert insight_stats.t_two_sided(t, df) == pytest.approx(p, abs=0.004)


def test_false_discovery_control_by_hand():
    # Benjamini and Hochberg's rule, step by step: the largest p under q * rank / m, and everything smaller.
    assert insight_stats.false_discoveries_removed([0.01, 0.02, 0.03, 0.5], 0.05) == [True, True, True, False]
    assert insight_stats.false_discoveries_removed([0.04, 0.5], 0.05) == [False, False]
    assert insight_stats.false_discoveries_removed([0.04, 0.03], 0.05) == [True, True]
    assert insight_stats.false_discoveries_removed([], 0.05) == []


def test_permutation_is_repeatable_and_tells_real_from_chance():
    rng = random.Random(2)
    flags = [i % 3 == 0 for i in range(60)]
    real = [rng.gauss(0, 1) + (3 if f else 0) for f in flags]
    assert insight_stats.group_gap_p(real, flags, "k") < 0.01
    assert insight_stats.group_gap_p(real, flags, "k") == insight_stats.group_gap_p(real, flags, "k")
    assert insight_stats.group_gap_p(real, [True] * 60, "k") == 1.0


def test_theil_sen_ignores_one_odd_result():
    slope, tau = insight_stats.theil_sen([0, 1, 2, 3, 4], [0, 1, 200, 3, 4])
    assert slope == pytest.approx(1.0) and tau == pytest.approx(0.6)
    assert insight_stats.theil_sen([0, 1, 2], [5, 4, 3]) == (pytest.approx(-1.0), pytest.approx(-1.0))


def test_association_removes_a_shared_trend():
    rng = random.Random(3)
    ts = list(range(60))
    assert insight_stats.association(ts, [t + rng.gauss(0, 2) for t in ts], [t + rng.gauss(0, 2) for t in ts])["clear"] is False
    xs = [rng.gauss(0, 1) for _ in ts]
    linked = insight_stats.association(ts, xs, [x + rng.gauss(0, 0.3) for x in xs])
    assert linked["clear"] is True and linked["r"] > 0.8 and linked["low"] < linked["r"] < linked["high"]
    flipped = insight_stats.association(ts, xs, [-x for x in xs])
    assert flipped["clear"] is True and flipped["r"] < -0.99
    assert insight_stats.association(ts, xs, [1.0] * 60) is None


# ---------------------------------------------------------------------------
# Tone, one row at a time
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("metric,usual,now,better,tone,mention", [
    ("resting_heart_rate", 66, 60, "down", "good", False),
    ("resting_heart_rate", 60, 66, "down", "attention", False),
    ("resting_heart_rate", 55, 47, "down", "info", False),            # past the expected band, not yet a concern
    ("resting_heart_rate", 55, 40, "down", "attention", True),
    ("resting_heart_rate", 80, 112, "down", "attention", True),
    ("resting_heart_rate", 110, 95, "down", "good", False),           # heading back, though not there yet
    ("respiratory_rate", 20, 13, "down", "info", False),              # large, so it isn't called good news
    ("respiratory_rate", 15, 8, "down", "attention", True),
    ("oxygen_saturation", 94, 97, "up", "good", False),
    ("oxygen_saturation", 97, 93, "up", "attention", True),
    ("sleep_duration", 6.5, 7.5, "up", "good", False),
    ("sleep_duration", 8.5, 9.5, "up", "info", False),
    ("sleep_duration", 8, 11, "up", "attention", True),
    ("sleep_duration", 8, 5, "up", "attention", True),
    ("hrv_rmssd", 40, 44, "up", "good", False),
    ("hrv_rmssd", 40, 60, "up", "info", False),
    ("hrv_rmssd", 40, 20, "up", "attention", False),
    ("body_mass", 80, 76, None, "info", False),
])
def test_tone_for_a_move(metric, usual, now, better, tone, mention):
    assert health_limits.tone_for_move(metric, usual, now, better) == (tone, mention)


# ---------------------------------------------------------------------------
# Generators at their thresholds
# ---------------------------------------------------------------------------

def _prof():
    return profiles.default_profile()["id"]


def _noon(days_ago=0):
    return datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)


def _daily(pid, metric, values, source="Apple Watch", unit="x"):
    n = len(values)
    biometrics.insert_samples(pid, None, [
        {"id": f"{metric}-{source}-{i}", "metric_type": metric, "value": v, "unit": unit, "source_name": source,
         "start_date": _noon(n - 1 - i).isoformat(), "end_date": (_noon(n - 1 - i) + timedelta(minutes=1)).isoformat()}
        for i, v in enumerate(values) if v is not None])


def _noisy(mean, sd, n, seed=7):
    rng = random.Random(seed)
    return [rng.gauss(mean, sd) for _ in range(n)]


@pytest.mark.parametrize("metric,step,raises", [("resting_heart_rate", 2.9, False), ("resting_heart_rate", 3.6, True),
                                                ("respiratory_rate", 0.9, False), ("respiratory_rate", 1.3, True)])
def test_smallest_change_worth_mentioning(client, metric, step, raises):
    pid = _prof()
    _daily(pid, metric, _noisy(50, 0.1, 60) + _noisy(50 + step, 0.1, 7))
    assert bool(insights._changes(pid)) is raises


def test_a_change_needs_most_of_the_week_and_to_hold_a_second_day(client):
    pid = _prof()
    _daily(pid, "resting_heart_rate", _noisy(55, 0.3, 60) + [55, 55, 70, 70, 70, 70, 70], unit="bpm")
    assert len(insights._changes(pid)) == 1
    pid2 = profiles.create_profile("Sam")["id"]
    _daily(pid2, "resting_heart_rate", _noisy(55, 0.3, 60) + [55, 55, 55, 70, 70, 70, 70], unit="bpm")
    assert insights._changes(pid2) == []


def test_too_few_days_on_either_side_say_nothing(client):
    pid = _prof()
    _daily(pid, "resting_heart_rate", _noisy(55, 0.3, 19) + [70] * 7, unit="bpm")
    assert insights._changes(pid) == []
    pid2 = profiles.create_profile("Sam")["id"]
    _daily(pid2, "resting_heart_rate", _noisy(55, 0.3, 60) + [None] * 4 + [70] * 3, unit="bpm")
    assert insights._changes(pid2) == []


def test_a_stale_measure_says_nothing(client):
    pid = _prof()
    _daily(pid, "resting_heart_rate", _noisy(55, 0.3, 60) + [70] * 7 + [None] * 4, unit="bpm")
    assert insights._changes(pid) == []


def test_sleep_notes_at_their_edges(client):
    pid = _prof()
    day = _noon().replace(hour=7)
    biometrics.insert_samples(pid, None, [
        {"id": f"n{i}", "metric_type": "sleep_analysis_asleep", "value": h * 3600, "unit": "s", "source_name": "Apple Watch",
         "start_date": (day - timedelta(days=i, hours=h)).isoformat(), "end_date": (day - timedelta(days=i)).isoformat()}
        for i, h in enumerate([5.9, 5.9, 5.9, 6.0, 7, 7, 7])])
    notes = insights._sleep_notes(pid)
    assert [n["title"] for n in notes if n["id"].startswith("short")] == ["3 short nights this week"]
    assert sleep.nights(pid, 14)["stats"]["count"] == 7


@pytest.mark.parametrize("met_days,tone", [(7, "good"), (6, "good"), (5, "info"), (3, "info"), (2, "attention"), (0, "attention")])
def test_goal_tone_at_its_edges(client, met_days, tone):
    pid = _prof()
    _daily(pid, "resting_heart_rate", [50] * met_days + [80] * (7 - met_days), unit="bpm")
    goals.set_goal(pid, "resting_heart_rate", 60, "max")
    note = insights._goal_notes(pid)[0]
    assert note["tone"] == tone and note["data"]["met"] == met_days and note["data"]["days"] == 7


def test_a_goal_with_few_days_says_nothing(client):
    pid = _prof()
    _daily(pid, "resting_heart_rate", [50] * 4, unit="bpm")
    goals.set_goal(pid, "resting_heart_rate", 60, "max")
    assert insights._goal_notes(pid) == []


def test_order_is_urgent_then_attention_then_the_rest(client, monkeypatch):
    def fake(kind, tone, urgent=False):
        return lambda _p: [{"id": kind, "kind": kind, "tone": tone, "data": {}, **({"urgent": True} if urgent else {})}]
    with monkeypatch.context() as patch:
        patch.setattr(insights, "urgent", fake("urgent", "attention", True))
        patch.setattr(insights, "_goal_notes", fake("goal", "good"))
        patch.setattr(insights, "_links", fake("link", "info"))
        patch.setattr(insights, "_lab_notes", fake("lab", "attention"))
        patch.setattr(insights, "_sleep_notes", fake("sleep", "attention"))
        patch.setattr(insights, "_changes", fake("change", "good"))
        patch.setattr(insights, "_training_notes", fake("training", "info"))
        patch.setattr(insights, "_medication_notes", fake("medication", "info"))
        assert [i["kind"] for i in insights.insights("p")] == ["urgent", "lab", "sleep", "training", "medication", "link", "change", "goal"]
        assert [i["kind"] for i in insights.insights("p", 2)] == ["urgent", "lab"]


# ---------------------------------------------------------------------------
# Golden snapshots
# ---------------------------------------------------------------------------

def _plain(text):
    """Dates move with the day the test runs; everything else in a sentence must not."""
    text = re.sub(r"[A-Z][a-z]{2} \d{1,2}, \d{4}", "<date>", text)
    return re.sub(r"\b[A-Z][a-z]{2} \d{4}\b", "<month>", text)


def _keep(items):
    return [{"kind": i["kind"], "tone": i["tone"], "title": i["title"], "text": _plain(i["text"])} for i in items]


def _simulator_snapshot(monkeypatch):
    out = {}
    frozen = date(2026, 6, 1)
    with monkeypatch.context() as patch:
        patch.setattr(insights, "_today", lambda: frozen)
        for key in sorted(PERSONAS):
            pid = profiles.create_profile(f"Golden {key}")["id"]
            sync.import_fhir_resources(pid, f"Imported · {key}", build_chart(key, "golden", "Golden Clinic", "epic", today=frozen))
            out[key] = {"labs": _keep(insights._lab_notes(pid)),
                        "markers": [{"t": m["t"], "label": m["label"]} for m in insights.markers(pid) if m["kind"] == "medications"]}
    return out


def _wearable_snapshot():
    """A person with fixed numbers and a bit of everything, through the whole of insights()."""
    pid = profiles.create_profile("Golden wearables")["id"]
    _daily(pid, "resting_heart_rate", _noisy(58, 1.5, 60, 11) + _noisy(66, 1.5, 7, 12), unit="bpm")
    _daily(pid, "hrv_rmssd", _noisy(42, 4, 60, 13) + _noisy(43, 4, 7, 14), unit="ms")
    _daily(pid, "oxygen_saturation", _noisy(96.8, 0.4, 67, 15), unit="%")
    _daily(pid, "step_count", [int(v) for v in _noisy(8000, 1500, 67, 16)], unit="count")
    _daily(pid, "walking_heart_rate_average", _noisy(99, 1.5, 50, 17), unit="bpm", source="Apple Watch")
    goals.set_goal(pid, "step_count", 7000)
    rng = random.Random(18)
    hours = [rng.gauss(7.2, 0.7) for _ in range(70)]
    biometrics.insert_samples(pid, None, [
        {"id": f"sl{i}", "metric_type": "sleep_analysis_asleep", "value": h * 3600, "unit": "s", "source_name": "Apple Watch",
         "start_date": (_noon(69 - i).replace(hour=7) - timedelta(hours=h)).isoformat(), "end_date": _noon(69 - i).replace(hour=7).isoformat()}
        for i, h in enumerate(hours)])
    activity.insert_events(pid, None, [
        {"id": f"c{i}", "event_type": "check_in", "category": "stateOfMind", "name": "Check-in", "value": max(-1, min(1, (h - 7.2) * 0.5 + rng.gauss(0, 0.15))),
         "start": _noon(69 - i).isoformat(), "end": _noon(69 - i).isoformat(), "source_name": "Syntropy"} for i, h in enumerate(hours)])
    return _keep(insights.insights(pid, 40))


def test_golden_snapshot(client, monkeypatch, caplog):
    got = {"simulator": _simulator_snapshot(monkeypatch), "wearables": _wearable_snapshot()}
    assert not [r for r in caplog.records if r.name == "syntropy.insights"], "A generator failed"
    if os.environ.get("UPDATE_INSIGHTS_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    assert got == json.loads(GOLDEN.read_text(encoding="utf-8"))
