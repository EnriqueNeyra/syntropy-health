"""Training load, maximum heart rate and zones, at exact values."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.services import insights
from app.store import activity, profiles, training


def _prof():
    return profiles.default_profile()["id"]


def _now():
    return datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)


def _w(i, days_ago, minutes=60, avg_hr=None, max_hr=None, hr=None, name="Running"):
    start = _now() - timedelta(days=days_ago)
    return {"id": f"w{i}", "name": name, "start": start.isoformat(), "end": (start + timedelta(minutes=minutes)).isoformat(),
            "duration_s": minutes * 60, "avg_hr": avg_hr, "max_hr": max_hr, "source_name": "Apple Watch", "heart_rate": hr or [], "route": []}


def _load(pid):
    return training.summary(pid, 5)["load"]


# ---------------------------------------------------------------------------
# F178: the ratio and how far it can be trusted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("this_week,status", [(90, "easing"), (119, "easing"), (120, "steady"), (180, "steady"), (181, "building"),
                                              (225, "building"), (226, "spike")])
def test_load_status_bands_at_their_edges(client, this_week, status):
    pid = _prof()
    # No heart rate: two load points a minute. Four usual weeks of 150 load each, and this week's as given.
    workouts = [_w(i, 7 + 7 * i, minutes=75) for i in range(4)]
    workouts.append(_w(99, 1, minutes=this_week / 2))
    activity.insert_workouts(pid, None, workouts)
    load = _load(pid)
    assert load["acute"] == pytest.approx(this_week, abs=1) and load["chronic"] == 150
    assert load["ratio"] == pytest.approx(this_week / 150, abs=0.01) and load["status"] == status


def test_a_ten_minute_workout_is_no_baseline_for_a_jump(client):
    pid = _prof()
    activity.insert_workouts(pid, None, [_w(1, 20, minutes=10)] + [_w(i, i, minutes=60) for i in range(2, 5)])
    load = _load(pid)
    assert load["status"] == "learning" and load["ratio"] is None
    assert insights._training_notes(pid) == []


def test_twelve_days_of_history_is_not_enough(client):
    pid = _prof()
    activity.insert_workouts(pid, None, [_w(i, i * 3, minutes=30) for i in range(1, 5)] + [_w(9, 1, minutes=120)])
    assert _load(pid)["status"] == "learning"


def test_back_from_a_break_is_not_a_spike(client):
    pid = _prof()
    activity.insert_workouts(pid, None, [_w(1, 30, minutes=10)] + [_w(i, i, minutes=60) for i in range(2, 5)])
    assert _load(pid)["status"] == "learning"


def test_a_real_jump_is_worded_without_an_injury_claim(client):
    pid = _prof()
    activity.insert_workouts(pid, None, [_w(i, 7 + 7 * i, minutes=60) for i in range(4)] + [_w(9, 1, minutes=200)])
    note = insights._training_notes(pid)[0]
    assert note["title"] == "Training load jumped" and note["tone"] == "info"
    assert "much more than your recent weeks" in note["text"] and "common approach" in note["text"]
    assert "injury" not in note["text"] and "233%" in note["text"]


# ---------------------------------------------------------------------------
# F179: maximum heart rate
# ---------------------------------------------------------------------------

def test_one_glitched_reading_does_not_set_maximum_heart_rate(client):
    pid = _prof()
    profiles.update_profile(pid, birth_date=f"{date.today().year - 60}-01-01")
    activity.insert_workouts(pid, None, [_w(i, 2 + i, minutes=30, avg_hr=120, max_hr=150) for i in range(10)])
    before = _load(pid)["acute"]
    activity.insert_workouts(pid, None, [_w(50, 1, minutes=1, avg_hr=120, max_hr=226)])
    summary = training.summary(pid, 5)
    assert training.max_heart_rate(pid)["bpm"] == 150
    assert summary["load"]["acute"] == pytest.approx(before, abs=5)


def test_a_glitch_within_the_age_limit_does_not_set_it_either(client):
    pid = _prof()
    activity.insert_workouts(pid, None, [_w(i, 2 + i, minutes=30, avg_hr=120, max_hr=175) for i in range(5)] + [_w(50, 1, minutes=1, max_hr=225)])
    assert training.max_heart_rate(pid)["bpm"] == 175


def test_peaks_far_above_the_age_estimate_do_not_count_even_twice(client):
    pid = _prof()
    profiles.update_profile(pid, birth_date=f"{date.today().year - 60}-01-01")
    activity.insert_workouts(pid, None, [_w(i, 2 + i, minutes=30, max_hr=152) for i in range(5)]
                             + [_w(50, 1, minutes=1, max_hr=215), _w(51, 8, minutes=1, max_hr=214)])
    assert training.max_heart_rate(pid)["bpm"] == 152


def test_someone_can_set_their_own_maximum(client):
    pid = _prof()
    activity.insert_workouts(pid, None, [_w(i, 2 + i, minutes=30, avg_hr=120, max_hr=175) for i in range(5)])
    res = client.put("/api/biometrics/training/max-heart-rate", json={"bpm": 188})
    assert res.status_code == 200 and res.json()["bpm"] == 188
    mhr = training.max_heart_rate(pid)
    assert mhr["bpm"] == 188 and mhr["set_by_person"] is True and mhr["basis"] == "set by you"
    assert client.put("/api/biometrics/training/max-heart-rate", json={"bpm": 40}).status_code == 400
    assert client.put("/api/biometrics/training/max-heart-rate", json={"bpm": None}).json()["bpm"] == 175


def test_estimate_when_nothing_plausible_was_recorded(client):
    pid = _prof()
    profiles.update_profile(pid, birth_date=f"{date.today().year - 40}-01-01")
    assert training.max_heart_rate(pid) == {"bpm": 180, "basis": "estimated from age (220 − age)"}
    activity.insert_workouts(pid, None, [_w(i, 2 + i, minutes=30, max_hr=130) for i in range(3)])
    assert training.max_heart_rate(pid)["bpm"] == 180


# ---------------------------------------------------------------------------
# F180: zone minutes
# ---------------------------------------------------------------------------

def _series(minutes, every_s, hr):
    start = _now()
    n = int(minutes * 60 / every_s)
    return [{"t": (start + timedelta(seconds=i * every_s)).isoformat(), "avg": hr, "min": hr, "max": hr} for i in range(n)]


@pytest.mark.parametrize("minutes,every_s", [(60, 600), (10, 5), (30, 60), (45, 15)])
def test_zone_minutes_add_up_to_the_workout_whatever_the_sampling_rate(minutes, every_s):
    w = {"duration_s": minutes * 60, "heart_rate": _series(minutes, every_s, 150), "avg_hr": 150}
    mins = training.zone_minutes(w, 190)     # 150 of 190 is 79%: zone 3
    assert mins[2] == pytest.approx(minutes, abs=1e-6) and sum(mins) == pytest.approx(minutes, abs=1e-6)
    assert training.workout_load(w, 190) == pytest.approx(minutes * 3, abs=1e-6)


def test_a_long_gap_does_not_hand_all_the_time_to_one_reading():
    start = _now()
    series = [{"t": start.isoformat(), "avg": 180}, {"t": (start + timedelta(minutes=50)).isoformat(), "avg": 110},
              {"t": (start + timedelta(minutes=51)).isoformat(), "avg": 110}, {"t": (start + timedelta(minutes=52)).isoformat(), "avg": 110}]
    mins = training.zone_minutes({"duration_s": 52 * 60, "heart_rate": series}, 190)
    # Capped at five minutes against three one-minute readings, before scaling to the 52 minutes.
    assert mins[4] == pytest.approx(52 * 5 / 8, abs=0.01)


def test_points_without_usable_times_share_the_workout_evenly():
    series = [{"t": "not a time", "avg": 150}] * 6
    assert training.zone_minutes({"duration_s": 3600, "heart_rate": series}, 190)[2] == pytest.approx(60)
    assert training.zone_minutes({"heart_rate": [{"avg": 150}] * 6}, 190)[2] == 6


def test_the_phone_payload_still_counts_as_it_did():
    # One point a minute, as the iOS and Android apps send, gives the same minutes as before.
    w = {"duration_s": 1800, "heart_rate": _series(30, 60, 150)}
    assert training.zone_minutes(w, 190) == [0, 0, 30, 0, 0]


def test_below_half_of_maximum_is_not_training():
    assert training.zone_minutes({"duration_s": 600, "avg_hr": 90}, 190) == [0, 0, 0, 0, 0]
    assert training.zone_minutes({"duration_s": 600, "avg_hr": 95}, 190) == [10, 0, 0, 0, 0]
    assert training.zone_minutes({"duration_s": 600, "avg_hr": 171}, 190) == [0, 0, 0, 0, 10]
