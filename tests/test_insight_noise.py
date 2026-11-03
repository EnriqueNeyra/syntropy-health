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
from app.store import biometrics

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
    monkeypatch.setattr(insights, "_tz", lambda profile_id=None: UTC)
    return state


def _person(rng, rho, days=130):
    return {m: _points([mean + sd * z for z in _ar1(days, rho, rng)]) for m, (mean, sd) in PERSON.items()}


# ---------------------------------------------------------------------------
# Changes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rho,limit", [(0.0, 0.05), (0.3, 0.05), (0.5, 0.05), (0.7, 0.05), (0.85, 0.05), (0.95, 0.07)])
def test_noise_rarely_raises_a_change_across_all_ten_measures(world, rho, limit):
    rng = random.Random(int(rho * 100) + 11)
    runs, alarms = 300, 0
    for _ in range(runs):
        world["series"] = _person(rng, rho)
        alarms += bool(insights._changes("p"))
    assert alarms / runs < limit, f"{alarms} of {runs} runs raised a change at rho {rho}"


def test_a_level_that_wanders_is_not_often_taken_for_a_slide(world):
    # A wandering level does end up somewhere else, so this can't be near zero, but it's half what fitting a line alone gives.
    rng = random.Random(3)
    runs, alarms = 300, 0
    for _ in range(runs):
        world["series"] = {m: _points([mean + sd * 0.25 * z for z in _walk(130, rng)]) for m, (mean, sd) in PERSON.items()}
        alarms += bool(insights._changes("p"))
    assert alarms / runs < 0.25, f"{alarms} of {runs}"


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


def _slide(start, end, sd, span, every=1, source="Apple Watch", rho=0.0, flat=0):
    """From ``start`` to ``end`` over the last ``span`` days, after ``flat`` days at ``start``."""
    def make(rng):
        z = _ar1(span + flat + 1, rho, rng)
        return [{"day": _day(i), "value": (end + (start - end) * i / span if i <= span else start) + sd * z[i], "source": source}
                for i in range(span + flat, 0, -every)]
    return make


@pytest.mark.parametrize("metric,make,found_at_least", [
    ("body_mass", _slide(80, 70, 0.4, 60, source="Scale"), 0.95),
    ("body_mass", _slide(80, 72, 0.4, 90, source="Scale"), 0.95),
    ("body_mass", _slide(80, 72, 0.4, 140, source="Scale"), 0.95),
    ("body_mass", _slide(80, 72, 0.4, 140, 7, source="Scale"), 0.85),
    ("body_mass", _slide(80, 74, 0.4, 40, flat=100, source="Scale"), 0.95),
    ("vo2_max", _slide(45, 39, 0.3, 140, 7), 0.85),
    ("resting_heart_rate", _slide(58, 70, 1.5, 67), 0.95),
    ("resting_heart_rate", _slide(58, 70, 1.5, 67, rho=0.5), 0.7),
    ("resting_heart_rate", _slide(58, 68, 1.5, 30, flat=40), 0.9),
    ("oxygen_saturation", _slide(97, 93, 0.5, 67), 0.9),
    ("sleep_duration", _slide(7.5, 5.5, 0.4, 67), 0.8),
    ("step_count", _slide(9000, 4000, 800, 67), 0.85),
])
def test_a_slow_slide_is_found_though_no_single_week_stands_out(world, metric, make, found_at_least):
    rng = random.Random(1)
    runs, hits = 100, 0
    for _ in range(runs):
        world["series"] = {metric: make(rng)}
        hits += any(i.get("metric") == metric for i in insights._changes("p"))
    assert hits / runs >= found_at_least, f"found {hits} of {runs}"


def test_a_slow_slide_is_described_as_going_the_right_way(world):
    world["series"] = {"body_mass": _slide(80, 70, 0.4, 60, source="Scale")(random.Random(2))}
    found = insights._changes("p")[0]
    assert found["title"] == "Weight is down" and found["data"]["recent"] < 73 < found["data"]["usual"]


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
