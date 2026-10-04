"""
Training over time: weekly volume, training load, heart-rate zones, pace and splits, and personal bests. Everything is
built from workouts after duplicates are folded (``activity.list_workouts``), so a run the Watch and WHOOP both saw
counts once.

Load follows Edwards' summated heart-rate zones: minutes in each zone of maximum heart rate weighted 1 (50–60%) to
5 (90% and up). A workout with minute-by-minute heart rate uses it; one with only an average uses that for its whole
length; one without heart rate counts as zone 2. Maximum heart rate is the highest recorded in a workout in the last
year when that's plausible, otherwise 220 minus age.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from app.core.db import read
from app.store import activity, profiles
from app.store.biometrics import _tz, parse_ts

ZONES = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 9.0)]
ZONE_NAMES = ["Easy", "Light", "Moderate", "Hard", "Maximum"]
NO_HR_WEIGHT = 2
# Kinds of workout where distance and pace mean something, and the distance a best effort has to cover.
PACE_KINDS = ("run", "walk", "hik", "cycl", "bik", "swim", "row", "ski", "wheelchair")
MIN_BEST_DISTANCE_M = 1000


def _age(birth: Optional[str]) -> Optional[int]:
    try:
        b = date.fromisoformat(str(birth)[:10])
    except (TypeError, ValueError):
        return None
    t = date.today()
    return t.year - b.year - ((t.month, t.day) < (b.month, b.day))


def max_heart_rate(profile_id: str) -> dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with read() as conn:
        row = conn.execute("SELECT MAX(max_hr) FROM workouts WHERE profile_id = ? AND start_date >= ? AND max_hr < 230",
                           (profile_id, since)).fetchone()
    seen = row[0] if row else None
    age = _age((profiles.get_profile(profile_id) or {}).get("birth_date"))
    estimate = 220 - age if age else None
    if seen and seen >= 150 and (estimate is None or seen >= estimate - 15):
        return {"bpm": round(seen), "basis": "highest recorded in a workout"}
    if estimate:
        return {"bpm": estimate, "basis": "estimated from age (220 − age)"}
    return {"bpm": round(seen) if seen and seen >= 140 else 185, "basis": "estimated"}


def _zone(hr: Optional[float], max_hr: float) -> Optional[int]:
    if not hr:
        return None
    f = hr / max_hr
    for i, (lo, hi) in enumerate(ZONES):
        if lo <= f < hi:
            return i
    return None      # below 50%: not counted as training


def zone_minutes(w: dict[str, Any], max_hr: float) -> list[float]:
    """Minutes in each zone. Per-minute heart rate when the workout has it, else its average for the whole time."""
    mins = [0.0] * len(ZONES)
    series = w.get("heart_rate") or []
    if series:
        for p in series:
            z = _zone(p.get("avg"), max_hr)
            if z is not None:
                mins[z] += 1
        return mins
    z = _zone(w.get("avg_hr"), max_hr)
    if z is not None:
        mins[z] += (w.get("duration_s") or 0) / 60
    return mins


def workout_load(w: dict[str, Any], max_hr: float) -> float:
    if not w.get("avg_hr") and not w.get("heart_rate"):
        return (w.get("duration_s") or 0) / 60 * NO_HR_WEIGHT
    return sum(m * (i + 1) for i, m in enumerate(zone_minutes(w, max_hr)))


def _with_hr(profile_id: str, workouts: list[dict[str, Any]]) -> None:
    """Adds per-minute heart rate to workouts that recorded it (one query)."""
    ids = [w["id"] for w in workouts if w.get("has_heart_rate")]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        with read() as conn:
            rows = conn.execute(f"SELECT id, heart_rate_json FROM workouts WHERE profile_id = ? AND id IN ({','.join('?' * len(chunk))})",
                                (profile_id, *chunk)).fetchall()
        hr = {r["id"]: json.loads(r["heart_rate_json"]) for r in rows if r["heart_rate_json"]}
        for w in workouts:
            if w["id"] in hr:
                w["heart_rate"] = hr[w["id"]]


def is_pace_kind(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in PACE_KINDS)


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def summary(profile_id: str, weeks: int = 12, name: Optional[str] = None) -> dict[str, Any]:
    """Weekly volume and load for the last ``weeks`` weeks (this week included), this week's load against the four
    before it, and, for one kind of workout, each session's pace and heart rate and the best efforts."""
    tz = _tz()
    today = datetime.now(tz).date()
    first = _week_start(today) - timedelta(weeks=weeks - 1)
    # Enough history for the weeks shown and for the four weeks before this one (the load comparison).
    workouts = activity.list_workouts(profile_id, max((today - first).days + 1, 35), limit=100_000)
    _with_hr(profile_id, workouts)
    mhr = max_heart_rate(profile_id)
    by_week: dict[date, dict[str, Any]] = {}
    for i in range(weeks):
        ws = first + timedelta(weeks=i)
        by_week[ws] = {"week": ws.isoformat(), "minutes": 0.0, "count": 0, "distance_m": 0.0, "load": 0.0,
                       "zones": [0.0] * len(ZONES)}
    daily_load: dict[date, float] = defaultdict(float)
    for w in workouts:
        d = datetime.fromtimestamp(parse_ts(w["start_date"]).timestamp(), tz).date()
        w["_day"] = d
        load = workout_load(w, mhr["bpm"])
        w["load"] = round(load)
        daily_load[d] += load
        if name and w["name"] != name:
            continue
        ws = _week_start(d)
        if ws in by_week:
            b = by_week[ws]
            b["minutes"] += (w["duration_s"] or 0) / 60
            b["count"] += 1
            b["distance_m"] += w["distance_m"] or 0
            b["load"] += load
            for z, m in enumerate(zone_minutes(w, mhr["bpm"])):
                b["zones"][z] += m

    acute = sum(v for d, v in daily_load.items() if (today - d).days < 7)
    chronic = sum(v for d, v in daily_load.items() if 7 <= (today - d).days < 35) / 4
    ratio = acute / chronic if chronic else None
    status = ("none" if not acute and not chronic else "new" if not chronic
              else "spike" if ratio > 1.5 else "building" if ratio > 1.2 else "easing" if ratio < 0.8 else "steady")

    out: dict[str, Any] = {
        "weeks": [{**b, "minutes": round(b["minutes"]), "load": round(b["load"]), "distance_m": round(b["distance_m"]),
                   "zones": [round(z) for z in b["zones"]]} for b in by_week.values()],
        "load": {"acute": round(acute), "chronic": round(chronic), "ratio": round(ratio, 2) if ratio else None, "status": status},
        "max_hr": mhr, "zones": [{"name": n, "from": round(lo * mhr["bpm"]), "to": round(min(hi, 1.0) * mhr["bpm"])}
                                 for n, (lo, hi) in zip(ZONE_NAMES, ZONES)],
    }
    if name:
        mine = [w for w in activity.list_workouts(profile_id, None, limit=100_000, name=name)]
        out["progress"] = [_progress_point(w) for w in reversed(mine) if w.get("duration_s")]
        out["bests"] = bests(mine)
        out["pace"] = is_pace_kind(name)
    return out


def _progress_point(w: dict[str, Any]) -> dict[str, Any]:
    p = {"id": w["id"], "t": w["start_date"], "duration_s": w["duration_s"], "distance_m": w["distance_m"], "avg_hr": w["avg_hr"]}
    if w["distance_m"] and w["distance_m"] >= 200 and w["duration_s"]:
        p["pace_s_per_km"] = round(w["duration_s"] / (w["distance_m"] / 1000))
        p["speed_kmh"] = round(w["distance_m"] / 1000 / (w["duration_s"] / 3600), 2)
    return p


def bests(workouts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The longest, the farthest, the fastest over at least a kilometre, and the most energy, each with its workout."""
    def top(key, rows):
        rows = [w for w in rows if key(w)]
        return max(rows, key=key) if rows else None

    picks = [
        ("distance", "Farthest", top(lambda w: w["distance_m"] or 0, workouts)),
        ("duration", "Longest", top(lambda w: w["duration_s"] or 0, workouts)),
        ("speed", "Fastest", top(lambda w: (w["distance_m"] / w["duration_s"])
                                 if (w["distance_m"] or 0) >= MIN_BEST_DISTANCE_M and w["duration_s"] else 0, workouts)),
        ("energy", "Most energy", top(lambda w: w["active_energy_kcal"] or w["total_energy_kcal"] or 0, workouts)),
    ]
    out = []
    for kind, label, w in picks:
        if not w:
            continue
        item = {"kind": kind, "label": label, "id": w["id"], "t": w["start_date"], "duration_s": w["duration_s"],
                "distance_m": w["distance_m"], "energy_kcal": w["active_energy_kcal"] or w["total_energy_kcal"]}
        if kind == "speed":
            item["pace_s_per_km"] = round(w["duration_s"] / (w["distance_m"] / 1000))
            item["speed_kmh"] = round(w["distance_m"] / 1000 / (w["duration_s"] / 3600), 2)
        out.append(item)
    return out


def _haversine(a: dict[str, Any], b: dict[str, Any]) -> float:
    r = 6371000.0
    la1, la2 = math.radians(a["lat"]), math.radians(b["lat"])
    dla, dlo = la2 - la1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def splits(route: list[dict[str, Any]], unit_m: float = 1000.0) -> list[dict[str, Any]]:
    """Time for each full kilometre (or ``unit_m``) along a timed route, and the part left over at the end."""
    pts = [p for p in route if p.get("t")]
    if len(pts) < 2:
        return []
    out, dist, mark, mark_t = [], 0.0, unit_m, parse_ts(pts[0]["t"]).timestamp()
    prev = pts[0]
    for p in pts[1:]:
        step = _haversine(prev, p)
        t0, t1 = parse_ts(prev["t"]).timestamp(), parse_ts(p["t"]).timestamp()
        while step > 0 and dist + step >= mark:
            f = (mark - dist) / step
            at = t0 + f * (t1 - t0)
            out.append({"n": len(out) + 1, "distance_m": unit_m, "seconds": round(at - mark_t)})
            mark_t, mark = at, mark + unit_m
        dist += step
        prev = p
    rest = dist - (mark - unit_m)
    end = parse_ts(pts[-1]["t"]).timestamp()
    if rest >= unit_m * 0.1 and end > mark_t:
        out.append({"n": len(out) + 1, "distance_m": round(rest), "seconds": round(end - mark_t), "partial": True})
    return out


def detail(profile_id: str, w: dict[str, Any], split_m: float = 1000.0) -> dict[str, Any]:
    """Adds zones, load, pace and splits (per ``split_m``: a kilometre, or a mile) to a workout's details."""
    mhr = max_heart_rate(profile_id)
    w["max_hr_basis"] = mhr
    w["zones"] = [{"name": n, "minutes": round(m, 1), "from": round(lo * mhr["bpm"]), "to": round(min(hi, 1.0) * mhr["bpm"])}
                  for n, m, (lo, hi) in zip(ZONE_NAMES, zone_minutes(w, mhr["bpm"]), ZONES)]
    w["load"] = round(workout_load(w, mhr["bpm"]))
    if w.get("distance_m") and w.get("duration_s") and w["distance_m"] >= 200:
        w["pace_s_per_km"] = round(w["duration_s"] / (w["distance_m"] / 1000))
        w["speed_kmh"] = round(w["distance_m"] / 1000 / (w["duration_s"] / 3600), 2)
    w["splits"] = splits(w.get("route") or [], split_m) if is_pace_kind(w.get("name", "")) else []
    return w
