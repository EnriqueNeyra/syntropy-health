"""
Sleep as nights rather than numbers: when each night started and ended, how it split into stages, and how regular the
schedule was. Built from the same samples and source choice as the daily sleep figures (``biometrics.daily_series``),
so a night here always matches the hours shown everywhere else.

Two shapes of data arrive. Apple Health (and anything writing to it) sends one sample per stretch of a stage; those are
grouped into nights exactly as ``biometrics._sleep_nights`` does, and the stretches are kept for a hypnogram. Oura,
WHOOP and Google send one summary per night (total, deep, REM, light, awake over the whole time in bed).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from app.core.db import read
from app.store import biometrics
from app.store.biometrics import SLEEP_SESSION_GAP_S, SLEEP_STAGE_SAMPLES, _tz, _union, local_day, parse_ts

# Raw sample type -> stage. "asleep" is sleep a device didn't break into stages.
STAGE = {
    "sleep_analysis_deep": "deep", "sleep_analysis_rem": "rem", "sleep_analysis_core": "core",
    "sleep_analysis_asleep": "asleep", "sleep_analysis_unspecified": "asleep", "sleep_analysis_awake": "awake",
    "sleep_deep": "deep", "sleep_rem": "rem", "sleep_light": "core", "sleep_awake": "awake",
    "sleep_total_duration": "total",
}
STAGES = ("deep", "core", "rem", "awake")
# Minutes after noon: a 23:30 bedtime is 690, a 01:00 one is 780, so nights that cross midnight average sensibly.
_NOON = 12 * 60


def _clock(ts: float, tz) -> int:
    t = datetime.fromtimestamp(ts, tz)
    return (t.hour * 60 + t.minute - _NOON) % 1440


def _hours(spans: list[tuple[float, float]]) -> float:
    return sum(e - s for s, e in _union(spans)) / 3600.0


def _staged_night(samples: list[tuple[float, float, str]], tz, keep_segments: bool) -> dict[str, Any]:
    by_stage: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for s, e, metric in samples:
        by_stage[STAGE[metric]].append((s, e))
    asleep = _union([sp for st, spans in by_stage.items() if st != "awake" for sp in spans])
    staged = {st: _hours(by_stage.get(st, [])) for st in ("deep", "core", "rem")}
    total = sum(e - s for s, e in asleep) / 3600.0
    bed = min(s for s, _, _ in samples)
    wake = max(e for _, e, _ in samples)
    night = {
        "bed_ts": bed, "wake_ts": wake, "asleep_h": total,
        "stages": {**staged, "awake": _hours(by_stage.get("awake", [])),
                   "unstaged": max(0.0, total - sum(staged.values()))},
    }
    if keep_segments:
        segs = sorted((s, e, STAGE[m]) for s, e, m in samples if STAGE[m] != "asleep" or not any(staged.values()))
        night["segments"] = [{"start": datetime.fromtimestamp(s, tz).isoformat(timespec="minutes"),
                              "end": datetime.fromtimestamp(e, tz).isoformat(timespec="minutes"), "stage": st}
                             for s, e, st in segs]
    return night


def _summary_night(parts: dict[str, list[tuple[float, float, float]]]) -> dict[str, Any]:
    spans = [(s, e) for rows in parts.values() for s, e, _ in rows]
    val = {st: sum(v for _, _, v in rows) / 3600.0 for st, rows in parts.items()}
    staged = {st: val.get(st, 0.0) for st in ("deep", "core", "rem")}
    total = val.get("total") or val.get("asleep") or sum(staged.values())
    return {
        "bed_ts": min(s for s, _ in spans), "wake_ts": max(e for _, e in spans), "asleep_h": total,
        "stages": {**staged, "awake": val.get("awake", 0.0), "unstaged": max(0.0, total - sum(staged.values()))},
    }


def _all_nights(profile_id: str, start: date, end: date, tz) -> dict[tuple[str, str], dict[str, Any]]:
    """(day, source) -> night, for nights ending between ``start`` and ``end``."""
    # A night can start up to a day before the morning it counts toward.
    lo_utc, hi_utc = (datetime.combine(d, time.min, tz).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                      for d in (start - timedelta(days=2), end + timedelta(days=2)))
    with read() as conn:
        rows = conn.execute(
            f"SELECT metric_type, value, start_date, end_date, source_name, metadata_json FROM biometric_samples "
            f"WHERE profile_id = ? AND metric_type IN ({','.join('?' * len(STAGE))}) AND start_date >= ? AND start_date < ?",
            (profile_id, *STAGE, lo_utc, hi_utc),
        ).fetchall()
    staged: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    summaries: dict[tuple[str, str], dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        metric, source = r["metric_type"], r["source_name"] or "Unknown"
        meta_day = None
        if r["metadata_json"] and '"day"' in r["metadata_json"]:
            try:
                meta_day = json.loads(r["metadata_json"]).get("day")
            except ValueError:
                meta_day = None
        try:
            s, e = parse_ts(r["start_date"]).timestamp(), parse_ts(r["end_date"]).timestamp()
        except (ValueError, TypeError):
            continue
        if metric in SLEEP_STAGE_SAMPLES and not meta_day:
            if e > s:
                staged[source].append((s, e, metric))
            continue
        day = meta_day or local_day(r["end_date"], tz)
        summaries[(day, source)][STAGE[metric]].append((s, e, float(r["value"])))

    out: dict[tuple[str, str], dict[str, Any]] = {}
    for (day, source), parts in summaries.items():
        if start.isoformat() <= day <= end.isoformat():
            out[(day, source)] = {"day": day, "source": source, **_summary_night(parts)}
    for source, samples in staged.items():
        groups: list[list[Any]] = []
        for sample in sorted(samples):
            if groups and sample[0] - groups[-1][0] <= SLEEP_SESSION_GAP_S:
                groups[-1][0] = max(groups[-1][0], sample[1])
                groups[-1][1].append(sample)
            else:
                groups.append([sample[1], [sample]])
        for woke, night in groups:
            day = datetime.fromtimestamp(woke, tz).date().isoformat()
            if start.isoformat() <= day <= end.isoformat():
                out[(day, source)] = {"day": day, "source": source, **_staged_night(night, tz, keep_segments=True)}
    return out


def _circular_mean(minutes: list[int]) -> Optional[float]:
    if not minutes:
        return None
    a = [m / 1440 * 2 * math.pi for m in minutes]
    ang = math.atan2(sum(math.sin(x) for x in a) / len(a), sum(math.cos(x) for x in a) / len(a))
    return (ang / (2 * math.pi) * 1440) % 1440


def _spread(minutes: list[int], mean: Optional[float]) -> Optional[float]:
    """Typical distance from the usual time, in minutes (the mean absolute deviation, which reads as "± 25 min")."""
    if mean is None or len(minutes) < 3:
        return None
    return sum(min(abs(m - mean), 1440 - abs(m - mean)) for m in minutes) / len(minutes)


def _clock_text(minutes: Optional[float]) -> Optional[str]:
    if minutes is None:
        return None
    m = int(round(minutes + _NOON)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def nights(profile_id: str, days: int = 30, source: Optional[str] = None, end: Optional[date] = None) -> dict[str, Any]:
    """Each night in the period from the source the daily sleep figures use, with bedtime, wake time and stages, plus
    averages and how regular bedtime and wake time were. The latest night keeps its stage-by-stage segments when the
    device recorded them."""
    tz = _tz()
    series = biometrics.daily_series(profile_id, "sleep_duration", days=days, source=source, end=end)
    start, last = date.fromisoformat(series["start"]), date.fromisoformat(series["end"])
    found = _all_nights(profile_id, start, last, tz)
    out = []
    for p in series["points"]:
        n = found.get((p["day"], p["source"]))
        if n is None:
            n = next((v for (d, _), v in found.items() if d == p["day"]), None)
        if n is None:
            continue
        in_bed = max(n["asleep_h"], (n["wake_ts"] - n["bed_ts"]) / 3600.0)
        row = {
            "day": p["day"], "source": p["source"], "asleep_h": round(p["value"], 2),
            "in_bed_h": round(in_bed, 2),
            "efficiency": round(min(1.0, p["value"] / in_bed), 3) if in_bed else None,
            "bed": datetime.fromtimestamp(n["bed_ts"], tz).isoformat(timespec="minutes"),
            "wake": datetime.fromtimestamp(n["wake_ts"], tz).isoformat(timespec="minutes"),
            "bed_min": _clock(n["bed_ts"], tz), "wake_min": _clock(n["wake_ts"], tz),
            "stages": {k: round(v, 2) for k, v in n["stages"].items()},
        }
        if "segments" in n:
            row["segments"] = n["segments"]
        out.append(row)
    for row in out[:-1]:
        row.pop("segments", None)    # only the latest night's hypnogram is drawn

    beds = [n["bed_min"] for n in out]
    wakes = [n["wake_min"] for n in out]
    bed_mean, wake_mean = _circular_mean(beds), _circular_mean(wakes)
    asleep = [n["asleep_h"] for n in out]
    staged_nights = [n for n in out if sum(n["stages"][s] for s in ("deep", "core", "rem")) > 0]
    avg_stage = {s: round(sum(n["stages"][s] for n in staged_nights) / len(staged_nights), 2) if staged_nights else None
                 for s in (*STAGES, "unstaged")}
    return {
        "start": series["start"], "end": series["end"], "sources": series["sources"], "nights": out,
        "stats": {
            "count": len(out),
            "asleep_h": round(sum(asleep) / len(asleep), 2) if asleep else None,
            "in_bed_h": round(sum(n["in_bed_h"] for n in out) / len(out), 2) if out else None,
            "efficiency": round(sum(n["efficiency"] or 0 for n in out) / len(out), 3) if out else None,
            "bedtime": _clock_text(bed_mean), "wake_time": _clock_text(wake_mean),
            "bedtime_spread_min": round(_spread(beds, bed_mean)) if _spread(beds, bed_mean) is not None else None,
            "wake_spread_min": round(_spread(wakes, wake_mean)) if _spread(wakes, wake_mean) is not None else None,
            "short_nights": sum(1 for h in asleep if h < 6),
            "stages": avg_stage,
        },
    }
