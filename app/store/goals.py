"""
Goals: a daily target for a wearable metric ("at least 7.5 h of sleep", "at least 8,000 steps", "resting heart rate
at most 60"). They belong to the person, so everyone who looks after them sees the same ones. Values are in the
metric's stored unit (hours, steps, kg, bpm...); the apps convert for display.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app.core import settings
from app.store import biometrics

MAX_GOALS = 20
# Offered when someone sets a goal for these, and the direction that means "better".
SUGGESTED = {
    "sleep_duration": (7.5, "min"), "step_count": (8000, "min"), "apple_exercise_time": (30, "min"),
    "active_energy": (500, "min"), "resting_heart_rate": (60, "max"), "apple_stand_time": (720, "min"),
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


def progress(profile_id: str, days: int = 7) -> list[dict[str, Any]]:
    """Each goal with how many of the last ``days`` complete days met it (today counts only once it's met, since a
    running total like steps can still get there)."""
    out = []
    today = datetime.now(biometrics._tz()).date().isoformat()
    for metric, goal in get(profile_id).items():
        pts = biometrics.daily_series(profile_id, metric, days=days)["points"]
        counted = [p for p in pts if not (p["day"] == today and p.get("partial") and not met(goal, p["value"]))]
        hits = sum(1 for p in counted if met(goal, p["value"]))
        streak = 0
        for p in reversed(counted):
            if not met(goal, p["value"]):
                break
            streak += 1
        label, unit, _, _ = biometrics.METRICS[metric]
        out.append({"metric": metric, "label": label, "unit": unit, **goal, "days": len(counted), "met": hits,
                    "streak": streak, "latest": pts[-1] if pts else None,
                    "history": [{"day": p["day"], "value": p["value"], "met": met(goal, p["value"]),
                                 **({"partial": True} if p.get("partial") else {})} for p in pts]})
    return out
