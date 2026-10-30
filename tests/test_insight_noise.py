"""
Pure noise through the real generators: what comes out is a false alarm, and its rate is pinned here. Real changes put
through the same code must still be found, so these can't pass by saying nothing. Seeds are fixed, so a failure means the
logic moved.

For comparison, the first versions of these generators raised a change in 61% of noise runs across the ten watched
measures, a sleep link in 52% of 30-day runs, a symptom link in 38% of runs with three symptom days, and a "strong"
correlation for 39% of unrelated random walks.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import insights
from app.store import activity, biometrics, records

UTC = ZoneInfo("UTC")
TODAY = datetime.now(timezone.utc).date()
PERSON = {"resting_heart_rate": (58, 2.5), "hrv_rmssd": (45, 7), "hrv_sdnn": (55, 8), "sleep_duration": (7.3, 0.7), "step_count": (8000, 2200),
          "respiratory_rate": (14.5, 0.8), "oxygen_saturation": (96.5, 0.8), "body_mass": (75, 0.6), "walking_heart_rate_average": (95, 5),
          "vo2_max": (42, 1.2)}


def _ar1(n, rho, rng):
    x, out, s = rng.gauss(0, 1), [], (1 - rho * rho) ** 0.5
    for _ in range(n):
        x = rho * x + s * rng.gauss(0, 1)
        out.append(x)
    return out


def _walk(n, rng):
    x, out = 0.0, []
    for _ in range(n):
        x += rng.gauss(0, 1)
        out.append(x)
    return out


def _day(ago):
    return (TODAY - timedelta(days=ago)).isoformat()


def _points(values, source="Apple Watch"):
    """Oldest first, ending yesterday."""
    n = len(values)
    return [{"day": _day(n - i), "value": v, "source": source} for i, v in enumerate(values)]


@pytest.fixture
def world(monkeypatch):
    """The generators read whatever is put here, with no database, so thousands of evaluations stay quick."""
    state = {"series": {}}

    def daily_series(_profile, metric, days=30, source=None, end=None):
        last = (end or TODAY).isoformat()
        first = ((end or TODAY) - timedelta(days=days - 1)).isoformat()
        pts = [p for p in state["series"].get(metric, []) if first <= p["day"] <= last and (not source or p["source"] == source)]
        return {"points": pts}
    monkeypatch.setattr(biometrics, "daily_series", daily_series)
    monkeypatch.setattr(biometrics, "available_metrics", lambda _p: [{"metric": m} for m in state["series"]])
    monkeypatch.setattr(insights, "_tz", lambda: UTC)
    return state


def _person(rng, rho, days=130):
    return {m: _points([mean + sd * z for z in _ar1(days, rho, rng)]) for m, (mean, sd) in PERSON.items()}


# ---------------------------------------------------------------------------
# Changes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rho", [0.0, 0.3, 0.5, 0.7, 0.85])
def test_noise_rarely_raises_a_change_across_all_ten_measures(world, rho):
    rng = random.Random(int(rho * 100) + 11)
    runs, alarms = 300, 0
    for _ in range(runs):
        world["series"] = _person(rng, rho)
        alarms += bool(insights._changes("p"))
    assert alarms / runs < 0.05, f"{alarms} of {runs} runs raised a change at rho {rho}"


@pytest.mark.parametrize("rho,sds,found_at_least", [(0.0, 2.5, 0.9), (0.3, 3, 0.8), (0.5, 4, 0.75)])
def test_a_real_step_in_the_last_week_is_still_found(world, rho, sds, found_at_least):
    rng = random.Random(23)
    runs, hits = 200, 0
    mean, sd = PERSON["resting_heart_rate"]
    for _ in range(runs):
        values = [mean + sd * z for z in _ar1(68, rho, rng)]
        values[-8:] = [v + sds * sd for v in values[-8:]]
        world["series"] = {"resting_heart_rate": _points(values)}
        hits += any(i.get("metric") == "resting_heart_rate" for i in insights._changes("p"))
    assert hits / runs >= found_at_least, f"found {hits} of {runs}"


def test_a_real_change_is_found_among_nine_quiet_measures(world):
    rng = random.Random(31)
    runs, hits, others = 150, 0, 0
    mean, sd = PERSON["resting_heart_rate"]
    for _ in range(runs):
        person = _person(rng, 0.3)
        values = [mean + sd * z for z in _ar1(68, 0.3, rng)]
        values[-8:] = [v + 4 * sd for v in values[-8:]]
        person["resting_heart_rate"] = _points(values)
        world["series"] = person
        found = [i.get("metric") for i in insights._changes("p")]
        hits += "resting_heart_rate" in found
        others += any(m != "resting_heart_rate" for m in found)
    assert hits / runs >= 0.85 and others / runs < 0.08


def test_weighed_once_a_week_still_gets_looked_at(world):
    rng = random.Random(37)
    runs, hits, alarms = 200, 0, 0
    for _ in range(runs):
        weights = [{"day": _day(i), "value": 75 + rng.gauss(0, 0.4), "source": "Scale"} for i in range(140, 0, -7)]
        world["series"] = {"body_mass": weights}
        alarms += bool(insights._changes("p"))
        for p in weights:
            if p["day"] >= _day(21):
                p["value"] += 4
        hits += bool(insights._changes("p"))
    assert alarms / runs < 0.05 and hits / runs >= 0.8


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def _independent_links(monkeypatch, world, rng, days, symptom_days=0, rho=0.3):
    sleep_values = [7.3 + 0.8 * z for z in _ar1(days, rho, rng)]
    world["series"] = {"sleep_duration": _points(sleep_values)}
    feel = {k: {_day(days - i): max(-1, min(1, 0.4 * z)) if k == "mood" else 3 + 0.8 * z for i, z in enumerate(_ar1(days, rho, rng))}
            for k in ("mood", "energy", "stress")}
    monkeypatch.setattr(insights, "journal_daily", lambda *_: feel)
    events = []
    if symptom_days:
        for d in rng.sample(range(1, days), symptom_days):
            events.append({"name": "Headache", "start_date": f"{_day(d)}T12:00:00Z"})
    monkeypatch.setattr(activity, "list_events", lambda *_a, **_k: events)


@pytest.mark.parametrize("days", [30, 60, 90])
def test_sleep_and_mood_noise_rarely_becomes_a_link(world, monkeypatch, days):
    rng = random.Random(days)
    runs, alarms = 120, 0
    for _ in range(runs):
        _independent_links(monkeypatch, world, rng, days)
        alarms += bool(insights._links("p"))
    assert alarms / runs < 0.08, f"{alarms} of {runs} at {days} days"


@pytest.mark.parametrize("symptom_days", [8, 10, 20])
def test_symptom_noise_rarely_becomes_a_link(world, monkeypatch, symptom_days):
    rng = random.Random(symptom_days + 40)
    runs, alarms = 150, 0
    for _ in range(runs):
        _independent_links(monkeypatch, world, rng, 100, symptom_days)
        alarms += any(i["id"].startswith("link:symptom") for i in insights._links("p"))
    assert alarms / runs < 0.08, f"{alarms} of {runs} with {symptom_days} symptom days"


def test_a_real_link_between_sleep_and_mood_is_still_found(world, monkeypatch):
    rng = random.Random(77)
    runs, hits = 60, 0
    for _ in range(runs):
        days = 60
        z = _ar1(days, 0.3, rng)
        world["series"] = {"sleep_duration": _points([7.3 + 0.8 * v for v in z])}
        mood = {_day(days - i): max(-1, min(1, 0.35 * v + rng.gauss(0, 0.2))) for i, v in enumerate(z)}
        monkeypatch.setattr(insights, "journal_daily", lambda *_, m=mood: {"mood": m, "energy": {}, "stress": {}})
        monkeypatch.setattr(activity, "list_events", lambda *_a, **_k: [])
        hits += any(i["id"] == "link:sleep-mood" for i in insights._links("p"))
    assert hits / runs >= 0.8, f"found {hits} of {runs}"


# ---------------------------------------------------------------------------
# Medications
# ---------------------------------------------------------------------------

def test_noise_around_a_first_order_is_rarely_called_a_difference(world, monkeypatch):
    rng = random.Random(5)
    monkeypatch.setattr(records, "medication_orders", lambda _p: [
        {"id": f"m{k}", "title": f"Drug {k}", "effective_at": f"{_day(45 + 20 * k)}T09:00:00Z", "status": "active", "dedup_key": f"med|{k}",
         "details": {"intent": "order"}, "resource_type": "MedicationRequest"} for k in range(3)])
    runs, alarms = 200, 0
    for _ in range(runs):
        world["series"] = {m: _points([PERSON[m][0] + PERSON[m][1] * z for z in _ar1(160, 0.5, rng)])
                           for m in ("resting_heart_rate", "hrv_rmssd", "sleep_duration", "body_mass")}
        alarms += bool(insights._medication_notes("p"))
    assert alarms / runs < 0.08, f"{alarms} of {runs}"


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------

def _compare_strength(monkeypatch, xs, ys):
    n = len(xs)
    values = {"a": {_day(n - i): v for i, v in enumerate(xs)}, "b": {_day(n - i): v for i, v in enumerate(ys)}}
    monkeypatch.setattr(insights, "series", lambda _p, key, _d, end=None: {
        "key": key, "label": key, "unit": "", "about": None, "values": values[key]})
    return insights.compare("p", "a", "b", n + 1)


@pytest.mark.parametrize("name,make,n,named_below,strong_below", [
    ("unrelated, short", lambda n, r: [r.gauss(0, 1) for _ in range(n)], 28, 0.05, 0.02),
    ("unrelated, long", lambda n, r: [r.gauss(0, 1) for _ in range(n)], 90, 0.05, 0.005),
    ("correlated days", lambda n, r: _ar1(n, 0.7, r), 90, 0.05, 0.02),
    ("wandering", _walk, 90, 0.05, 0.03),
    ("wandering, a year", _walk, 365, 0.05, 0.03),
])
def test_unrelated_measures_are_rarely_said_to_be_linked(monkeypatch, world, name, make, n, named_below, strong_below):
    rng = random.Random(n + len(name))
    runs, named, strong = 400, 0, 0
    for _ in range(runs):
        res = _compare_strength(monkeypatch, make(n, rng), make(n, rng))
        named += res["strength"] != "no clear link"
        strong += res["strength"] == "strong"
    assert named / runs < named_below and strong / runs < strong_below, f"{name}: {named} named, {strong} strong of {runs}"


def test_few_days_never_get_a_strength(monkeypatch, world):
    rng = random.Random(1)
    for n in (5, 7, 14, 27):
        xs = [rng.gauss(0, 1) for _ in range(n)]
        assert _compare_strength(monkeypatch, xs, xs)["strength"] == "not enough days to tell"


@pytest.mark.parametrize("r,found_at_least", [(0.5, 0.8), (0.8, 0.99)])
def test_a_real_day_to_day_link_is_still_named(monkeypatch, world, r, found_at_least):
    rng = random.Random(int(r * 10))
    runs, named = 200, 0
    for _ in range(runs):
        xs = [rng.gauss(0, 1) for _ in range(60)]
        ys = [r * x + (1 - r * r) ** 0.5 * rng.gauss(0, 1) for x in xs]
        res = _compare_strength(monkeypatch, xs, ys)
        named += res["strength"] != "no clear link" and res["r"] > 0 and res["interval"]["low"] > 0
    assert named / runs >= found_at_least, f"named {named} of {runs}"
