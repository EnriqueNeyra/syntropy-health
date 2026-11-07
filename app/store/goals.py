"""
Goals: a daily target for a wearable metric ("at least 7.5 h of sleep", "at least 8,000 steps", "resting heart rate
at most 60"). They belong to the person, so everyone who looks after them sees the same ones. Values are in the
metric's stored unit (hours, steps, kg, bpm...); the apps convert for display.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

from app.core import settings
from app.core.db import read
from app.store import biometrics

MAX_GOALS = 20
# Offered when someone sets a goal for these, and which way it usually runs.
SUGGESTED = {
    "sleep_duration": (7.5, "min"), "step_count": (8000, "min"), "apple_exercise_time": (30, "min"),
    "active_energy": (500, "min"), "resting_heart_rate": (None, "max"), "apple_stand_time": (720, "min"),
    "time_in_daylight": (30, "min"), "dietary_water": (2000, "min"), "flights_climbed": (10, "min"),
    "body_mass": (None, "max"), "hrv_rmssd": (None, "min"), "hrv_sdnn": (None, "min"),
}


def _key(profile_id: str) -> str:
    return f"goals.{profile_id}"


def get(profile_id: str) -> dict[str, dict[str, Any]]:
    return settings.get(_key(profile_id)) or {}


def set_goal(profile_id: str, metric: str, target: Optional[float], direction: str = "min") -> dict[str, dict[str, Any]]:
    """Sets (or with ``target`` None, removes) one goal. Returns them all."""
    if metric not in biometrics.METRICS:
        raise ValueError("Unknown metric.")
    if direction not in ("min", "max"):
        raise ValueError("Direction must be min (at least) or max (at most).")
    goals = dict(get(profile_id))
    if target is None:
        goals.pop(metric, None)
    else:
        if not (target > 0) or target > 1e7:
            raise ValueError("The goal must be a positive number.")
        if metric not in goals and len(goals) >= MAX_GOALS:
            raise ValueError(f"Up to {MAX_GOALS} goals.")
        goals[metric] = {"target": float(target), "direction": direction}
    settings.set(_key(profile_id), goals)
    return goals


def met(goal: dict[str, Any], value: Optional[float]) -> Optional[bool]:
    if value is None:
        return None
    return value >= goal["target"] if goal["direction"] == "min" else value <= goal["target"]


def _first_day(profile_id: str, metric: str) -> Optional[str]:
    with read() as conn:
        row = conn.execute("SELECT MIN(day) FROM biometric_daily WHERE profile_id = ? AND metric_type = ? "
                           "AND day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'", (profile_id, metric)).fetchone()
    return row[0] if row else None


def progress(profile_id: str, days: int = 7) -> list[dict[str, Any]]:
    """Each goal over the last ``days`` days: ``days`` is how many of them count, ``met`` how many met it, ``recorded`` how many
    have a figure, and ``streak`` how many in a row up to now met it. Every complete day counts, recorded or not: a day the
    watch stayed on the charger isn't a day the goal was met, and "5 of the last 5" for a week with two days missing would
    say otherwise. Days before the first figure ever recorded for the metric don't count. Today counts only once its running
    total has met an "at least" goal, or once it has a figure that isn't a running total: steps can still get there, but
    sugar so far says nothing about an "at most"."""
    out = []
    today = datetime.now(biometrics._tz(profile_id)).date()
    for metric, goal in get(profile_id).items():
        series = biometrics.daily_series(profile_id, metric, days=days)
        pts = series["points"]
        counted = {p["day"]: p for p in pts if not (p["day"] == today.isoformat() and p.get("partial")
                                                    and (goal["direction"] != "min" or not met(goal, p["value"])))}
        first = _first_day(profile_id, metric)
        calendar: list[str] = []
        if first:
            day = max(date.fromisoformat(series["start"]), date.fromisoformat(min(first, today.isoformat())))
            while day < today:
                calendar.append(day.isoformat())
                day += timedelta(days=1)
            if today.isoformat() in counted:
                calendar.append(today.isoformat())
        hits = sum(1 for d in calendar if d in counted and met(goal, counted[d]["value"]))
        streak = 0
        for d in reversed(calendar):
            if d not in counted or not met(goal, counted[d]["value"]):
                break
            streak += 1
        label, unit, _, _ = biometrics.METRICS[metric]
        out.append({"metric": metric, "label": label, "unit": unit, **goal, "days": len(calendar), "met": hits,
                    "recorded": sum(1 for d in calendar if d in counted), "streak": streak, "latest": pts[-1] if pts else None,
                    "history": [{"day": p["day"], "value": p["value"], "met": met(goal, p["value"]),
                                 **({"partial": True} if p.get("partial") else {})} for p in pts]})
    return out
