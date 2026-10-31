"""
Insights across sources: the things only visible with everything in one place. A wearable number moving away from
the person's own normal, how sleep lines up with how they said they felt, what changed after a medication started,
a lab result moving further out of range, training load jumping, goals kept.

Everything here is descriptive: "since", "on days when", never "because". Each insight carries plain text (for Ask
and other AI apps) and the numbers behind it (so the apps can show them in the person's units). A claim only appears
when the numbers support it (insight_stats) and one device is only compared with itself. Nothing here advises or says
what a number means for someone's health: a result the lab marked critical comes first, as what the lab reported.
"""

from __future__ import annotations

import logging
import re
import statistics
import traceback
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from app.services import insight_stats, reading_bounds
from app.store import activity, biometrics, goals, records, sleep, training
from app.store.biometrics import METRICS, _tz, local_day

log = logging.getLogger("syntropy.insights")

# ---------------------------------------------------------------------------
# Daily series from any source: wearables, the Journal, workouts
# ---------------------------------------------------------------------------

JOURNAL_SERIES = {
    "journal:mood": ("Mood", "mood", "From your check-ins: −1 very unpleasant to +1 very pleasant."),
    "journal:energy": ("Energy", "1–5", "From your check-ins: 1 drained to 5 energized."),
    "journal:stress": ("Stress", "1–5", "From your check-ins: 1 calm to 5 very high."),
}
WORKOUT_SERIES = {
    "workouts:minutes": ("Workout time", "min", "Minutes of workouts each day."),
    "workouts:load": ("Training load", "load", "Heart-rate-weighted workout minutes each day."),
}
MOOD_WORDS = ["very unpleasant", "unpleasant", "neutral", "pleasant", "very pleasant"]
ENERGY_WORDS = ["drained", "low", "okay", "good", "energized"]
STRESS_WORDS = ["calm", "a little", "some", "high", "very high"]


def _soft(label: str) -> str:
    """A label inside a sentence: "Resting heart rate" → "resting heart rate", but "HRV (RMSSD)", "VO₂ max" and "SpO2"
    stay as they are."""
    return label[0].lower() + label[1:] if re.match(r"[A-Z][a-z]+(\s|$)", label) else label


def _today(profile_id: Optional[str] = None) -> date:
    return datetime.now(_tz(profile_id)).date()


def _window(days: int, end: Optional[date] = None, profile_id: Optional[str] = None) -> tuple[date, date]:
    end = end or _today(profile_id)
    return end - timedelta(days=days - 1), end


def journal_daily(profile_id: str, start: date, end: date) -> dict[str, dict[str, float]]:
    """Per day, the average mood (−1..1), energy and stress (1..5) from check-ins and mood entries."""
    tz = _tz(profile_id)
    events = activity.list_events(profile_id, None, category="stateOfMind", start=(start - timedelta(days=1)).isoformat(),
                                  end=(end + timedelta(days=1)).isoformat(), limit=50_000)
    acc: dict[str, dict[str, list[float]]] = {"mood": defaultdict(list), "energy": defaultdict(list), "stress": defaultdict(list)}
    for e in events:
        day = local_day(e["start_date"], tz)
        if not (start.isoformat() <= day <= end.isoformat()):
            continue
        if e.get("value") is not None:
            acc["mood"][day].append(float(e["value"]))
        meta = e.get("metadata") or {}
        for k in ("energy", "stress"):
            if isinstance(meta.get(k), (int, float)) and 1 <= meta[k] <= 5:
                acc[k][day].append(float(meta[k]))
    return {k: {d: sum(v) / len(v) for d, v in by.items()} for k, by in acc.items()}


def workout_daily(profile_id: str, start: date, end: date) -> dict[str, dict[str, float]]:
    tz = _tz(profile_id)
    ws = activity.list_workouts(profile_id, None, limit=100_000, start=(start - timedelta(days=1)).isoformat(),
                                end=(end + timedelta(days=1)).isoformat())
    training._with_hr(profile_id, ws)
    mhr = training.max_heart_rate(profile_id)["bpm"]
    minutes: dict[str, float] = defaultdict(float)
    load: dict[str, float] = defaultdict(float)
    for w in ws:
        day = local_day(w["start_date"], tz)
        if start.isoformat() <= day <= end.isoformat():
            minutes[day] += (w["duration_s"] or 0) / 60
            load[day] += training.workout_load(w, mhr)
    # Days without a workout are real zeros, not missing.
    d = start
    while d <= end:
        minutes.setdefault(d.isoformat(), 0.0)
        load.setdefault(d.isoformat(), 0.0)
        d += timedelta(days=1)
    return {"minutes": dict(minutes), "load": dict(load)}


def series(profile_id: str, key: str, days: int, end: Optional[date] = None) -> dict[str, Any]:
    """{label, unit, values: {day: value}} for a wearable metric, a Journal measure or workouts."""
    start, last = _window(days, end, profile_id)
    if key in JOURNAL_SERIES:
        label, unit, about = JOURNAL_SERIES[key]
        return {"key": key, "label": label, "unit": unit, "about": about,
                "values": journal_daily(profile_id, start, last)[key.split(":")[1]]}
    if key in WORKOUT_SERIES:
        label, unit, about = WORKOUT_SERIES[key]
        return {"key": key, "label": label, "unit": unit, "about": about,
                "values": workout_daily(profile_id, start, last)[key.split(":")[1]]}
    if key not in METRICS:
        raise ValueError("Unknown measure.")
    s = biometrics.daily_series(profile_id, key, days=days, end=last)
    return {"key": key, "label": s["label"], "unit": s["unit"], "about": None,
            "values": {p["day"]: p["value"] for p in s["points"] if not p.get("partial")}}


def comparable(profile_id: str) -> list[dict[str, Any]]:
    """Everything that can be compared day by day: wearable metrics, Journal measures once there are check-ins, and
    workouts once there are some."""
    out = [{"key": m["metric"], "label": m["label"], "unit": m["unit"], "group": m["group"]}
           for m in biometrics.available_metrics(profile_id) if m["days"] >= 7]
    have = journal_daily(profile_id, *_window(180, profile_id=profile_id))
    for key, (label, unit, _) in JOURNAL_SERIES.items():
        if len(have[key.split(":")[1]]) >= 5:
            out.append({"key": key, "label": label, "unit": unit, "group": "journal"})
    if activity.counts(profile_id)["workouts"]:
        out += [{"key": k, "label": label, "unit": unit, "group": "workouts"} for k, (label, unit, _) in WORKOUT_SERIES.items()]
    return out


# Fewer than this and a correlation can't be told from chance: a 30-day window has 28 or 29 once today is left out.
MIN_COMPARE_PAIRS = 28


def strength(link: Optional[dict[str, float]], n: int) -> str:
    if n < MIN_COMPARE_PAIRS:
        return "not enough days to tell"
    if not link or not link["clear"]:
        return "no clear link"
    a = abs(link["r"])
    return "weak" if a < 0.3 else "moderate" if a < 0.5 else "strong"


def compare(profile_id: str, a: str, b: str, days: int = 90, lag: int = 0) -> dict[str, Any]:
    """Pairs ``a`` on each day with ``b`` on the same day, or ``lag`` days later (negative: earlier; HRV against the
    day before's training is lag -1), how they move together, and ``b`` on ``a``'s higher and lower days."""
    lag = max(-7, min(int(lag), 7))
    sa = series(profile_id, a, days)
    sb = series(profile_id, b, days + abs(lag), end=_window(1, profile_id=profile_id)[1])
    pairs = []
    for day, x in sorted(sa["values"].items()):
        other = (date.fromisoformat(day) + timedelta(days=lag)).isoformat()
        if other in sb["values"]:
            pairs.append({"day": day, "x": round(x, 3), "y": round(sb["values"][other], 3)})
    xs, ys = [p["x"] for p in pairs], [p["y"] for p in pairs]
    link = insight_stats.association([date.fromisoformat(p["day"]).toordinal() for p in pairs], xs, ys) if len(pairs) >= 4 else None
    split = None
    if len(pairs) >= 10:
        cut = statistics.median(xs)
        hi = [p["y"] for p in pairs if p["x"] > cut]
        lo = [p["y"] for p in pairs if p["x"] <= cut]
        if len(hi) >= 3 and len(lo) >= 3:
            split = {"cut": round(cut, 3), "above": round(sum(hi) / len(hi), 3), "below": round(sum(lo) / len(lo), 3),
                     "n_above": len(hi), "n_below": len(lo)}
    meta = lambda s: {k: s[k] for k in ("key", "label", "unit", "about")}   # noqa: E731
    return {"a": meta(sa), "b": meta(sb), "lag": lag, "days": days, "pairs": pairs, "n": len(pairs),
            "r": round(link["r"], 2) if link else None, "strength": strength(link, len(pairs)), "split": split,
            "interval": {"low": round(link["low"], 2), "high": round(link["high"], 2)} if link else None}


# ---------------------------------------------------------------------------
# Events from health records, for charts
# ---------------------------------------------------------------------------

# "intended" is a statement that the person hasn't begun it yet.
NOT_TAKEN_STATUSES = {"draft", "cancelled", "entered-in-error", "not-taken", "intended"}
NOT_AN_ORDER_INTENTS = {"proposal", "plan", "option"}
# What's on record doesn't say whether these were taken, so they never support a before and after.
UNSURE_STATUSES = {"unknown", "on-hold"}
NOT_A_DIAGNOSIS_STATUSES = {"entered-in-error", "refuted"}


def _iso_day(value: Optional[str]) -> Optional[str]:
    """The date in a stored timestamp, when it has a whole one: free-text and partial dates exist."""
    try:
        return date.fromisoformat((value or "")[:10]).isoformat()
    except ValueError:
        return None


def medication_timeline(profile_id: str) -> list[dict[str, Any]]:
    """Each drug's history from every order on record. The list of records keeps only the newest order of each, which
    is right for the current dose but says nothing about when it began: a renewal's date isn't a start. Orders that
    never happened are left out first, so a cancelled one can't move or erase a real first date."""
    by_drug: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records.medication_orders(profile_id):
        day = _iso_day(r.get("effective_at"))
        if not day or (r.get("status") or "").lower() in NOT_TAKEN_STATUSES:
            continue
        details = r.get("details") or {}
        # A plan is how some systems file a medication someone reports taking at home. A plan nobody reported isn't an order.
        if (details.get("intent") or "").lower() in NOT_AN_ORDER_INTENTS and not details.get("reported"):
            continue
        by_drug[r.get("dedup_key") or r["id"]].append({**r, "day": day})
    out = []
    for rows in by_drug.values():
        rows.sort(key=lambda r: r["day"])
        oldest, newest = rows[0], rows[-1]
        sure = not {(r.get("status") or "").lower() for r in (oldest, newest)} & UNSURE_STATUSES
        first_sure = (oldest.get("status") or "").lower() not in UNSURE_STATUSES
        out.append({"name": newest["title"], "first": oldest["day"], "last_ordered": newest["day"],
                    "orders": len({r["day"] for r in rows}), "record_id": newest["id"], "status": newest.get("status"), "sure": sure,
                    # Only a statement says the person took it, and only with a status that says so. An order is proof of an order.
                    "reported_taking": (oldest.get("resource_type") == "MedicationStatement" or bool((oldest.get("details") or {}).get("reported"))) and first_sure})
    return out


def markers(profile_id: str, start: Optional[str] = None, end: Optional[str] = None) -> list[dict[str, Any]]:
    """The first order on record for each medication and conditions diagnosed, with dates, newest first."""
    out = []
    for med in medication_timeline(profile_id):
        if (start and med["first"] < start) or (end and med["first"] > end[:10]):
            continue
        verb = "Started" if med["reported_taking"] else "Ordered"
        out.append({"t": med["first"], "label": f"{verb} {med['name']}", "kind": "medications", "record_id": med["record_id"],
                    "status": med["status"], "name": med["name"], "event": verb.lower(), "last_ordered": med["last_ordered"],
                    "orders": med["orders"]})
    for r in records.list_records(profile_id, "conditions", since=start, until=end, limit=300)["items"]:
        if not r.get("effective_at") or (r.get("status") or "").lower() in NOT_A_DIAGNOSIS_STATUSES:
            continue
        out.append({"t": r["effective_at"][:10], "label": f"Diagnosed {r['title']}", "kind": "conditions", "record_id": r["id"],
                    "status": r.get("status"), "name": r["title"], "event": "diagnosed"})
    return sorted(out, key=lambda m: m["t"], reverse=True)


# ---------------------------------------------------------------------------
# One device at a time, and the person's own normal
# ---------------------------------------------------------------------------

BASELINE_DAYS = 60
MIN_BASELINE_VALUES = 14
BASELINE_SPREAD = 2.0        # how many robust standard deviations either side counts as "your usual"
DEVICE_RUN_BEFORE, DEVICE_RUN_AFTER = 5, 3
DEVICE_NOTE_DAYS = 30
WHOLE_DAY_SOURCES = ("oura", "whoop", "google health")
# A deviation from the person's own temperature is below zero about half the time. Everything else stops at zero.
SIGNED_METRICS = {"body_temperature_deviation"}


def _complete(points: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Today's running total of steps or energy isn't a day's figure yet."""
    return [p for p in points if not p.get("partial")]


def _main_source(points: Iterable[dict[str, Any]]) -> Optional[str]:
    counts = Counter(p["source"] for p in points)
    return counts.most_common(1)[0][0] if counts else None


def _merges_into(source: Optional[str]) -> bool:
    """Whether the merged row carries on from this source. It's a wearable's own numbers with the phone filling the gaps,
    so from a wearable nothing has changed. From the phone alone a wearable now sets the numbers, and accounts that send
    whole days aren't part of what's merged."""
    name = (source or "").lower()
    return biometrics.source_kind(source) != "phone" and not any(n in name for n in WHOLE_DAY_SOURCES)


def _device_name(source: str) -> str:
    return "your devices together" if source == biometrics.MERGED_SOURCE else source


def device_changes(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The days a measure moved from one device to another for good. Which device wins can flick back and forth from
    day to day when two record it, and a day or two of that isn't a change. Nor is a wearable being joined by the phone."""
    runs: list[list[Any]] = []
    for p in points:
        if runs and runs[-1][0] == p["source"]:
            runs[-1][2] += 1
        else:
            runs.append([p["source"], p["day"], 1])
    while True:
        short = [i for i, r in enumerate(runs) if r[2] < DEVICE_RUN_AFTER]
        if not short:
            break
        i = short[0]
        if i > 0:
            runs[i - 1][2] += runs[i][2]
        elif len(runs) > 1:
            runs[1][1], runs[1][2] = runs[0][1], runs[1][2] + runs[0][2]
        else:
            break
        del runs[i]
        merged = []
        for r in runs:
            if merged and merged[-1][0] == r[0]:
                merged[-1][2] += r[2]
            else:
                merged.append(r)
        runs = merged
    devices: list[list[Any]] = []
    for r in runs:
        last = devices[-1] if devices else None
        if last and last[0] == biometrics.MERGED_SOURCE and _merges_into(r[0]):
            last[0], last[2] = r[0], last[2] + r[2]
        elif last and (last[0] == r[0] or (r[0] == biometrics.MERGED_SOURCE and _merges_into(last[0]))):
            last[2] += r[2]
        else:
            devices.append(r)
    return [{"day": b[1], "from": a[0], "to": b[0]} for a, b in zip(devices, devices[1:]) if a[2] >= DEVICE_RUN_BEFORE]


def _device_names(points: list[dict[str, Any]], source: Optional[str]) -> set[Optional[str]]:
    """What one device's numbers are stored under: its own name and the merged row, which stands in for it on the days
    the phone also recorded."""
    if source == biometrics.MERGED_SOURCE:
        return {source} | {p["source"] for p in points if _merges_into(p["source"])}
    return {source, biometrics.MERGED_SOURCE} if _merges_into(source) else {source}


def _same_device(profile_id: str, metric: str, points: list[dict[str, Any]], source: Optional[str], days: int,
                 end: Optional[date] = None) -> list[dict[str, Any]]:
    """Only ``source``'s days, including those another device outranked it on."""
    names = _device_names(points, source)
    if all(p["source"] in names for p in points):
        return points
    by_day: dict[str, dict[str, Any]] = {}
    for name in sorted(names, key=lambda n: n == biometrics.MERGED_SOURCE):
        for p in _complete(biometrics.daily_series(profile_id, metric, days=days, source=name, end=end)["points"]):
            by_day[p["day"]] = p
    return [by_day[day] for day in sorted(by_day)]


def _possible(metric: str, points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Without figures no living person could have, so a glitch or a wrong unit can't read as a change in them."""
    return [p for p in points if reading_bounds.is_plausible(metric, p["value"])]


def _usual_range(metric: str, values: list[float]) -> Optional[dict[str, float]]:
    if len(values) < MIN_BASELINE_VALUES:
        return None
    mid, spread = insight_stats.robust_center(values)
    low = mid - BASELINE_SPREAD * spread
    return {"median": mid, "low": low if metric in SIGNED_METRICS else max(0.0, low), "high": mid + BASELINE_SPREAD * spread}


def personal_range(profile_id: str, metric: str, days: int = 90, source: Optional[str] = None) -> dict[str, Any]:
    """The person's own normal for a measure from one device, as a median and a band that a wild reading can't move:
    now, for each stretch of the last ``days``, and the times the device changed, since numbers before a change aren't
    comparable with those after it."""
    if metric not in METRICS:
        raise ValueError("Unknown measure.")
    label, unit, _, _ = METRICS[metric]
    today = _today(profile_id)
    span = days + BASELINE_DAYS
    points = _complete(biometrics.daily_series(profile_id, metric, days=span, source=source)["points"])
    chosen = source or _main_source(points[-RECENT_DAYS:])
    mine = _same_device(profile_id, metric, points, chosen, span)
    # The last week is left out, so a change that's under way doesn't pull "usual" along with it.
    settled = [p["value"] for p in mine if p["day"] < (today - timedelta(days=RECENT_DAYS - 1)).isoformat()][-BASELINE_DAYS:]
    now = _usual_range(metric, settled)
    rolling = []
    step = max(1, days // 60)
    for back in range(days - 1, -1, -step):
        day = (today - timedelta(days=back)).isoformat()
        since = (today - timedelta(days=back + BASELINE_DAYS)).isoformat()
        band = _usual_range(metric, [p["value"] for p in mine if since <= p["day"] < day])
        if band:
            rolling.append({"day": day, **{k: round(v, 2) for k, v in band.items()}})
    return {"metric": metric, "label": label, "unit": unit, "source": chosen, "window_days": BASELINE_DAYS, "n": len(settled),
            "enough": bool(now), "median": round(now["median"], 2) if now else None, "low": round(now["low"], 2) if now else None,
            "high": round(now["high"], 2) if now else None, "rolling": rolling, "device_changes": device_changes(points)}


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------

# What to watch for a change from the person's own normal: the direction that's better (None: neither), and the
# smallest change worth mentioning, absolute or relative.
WATCH = [
    ("resting_heart_rate", "down", 3.0, None),
    ("hrv_rmssd", "up", None, 0.12),
    ("hrv_sdnn", "up", None, 0.12),
    ("sleep_duration", "up", 0.5, None),
    ("step_count", "up", None, 0.25),
    ("respiratory_rate", "down", 1.0, None),
    ("oxygen_saturation", "up", 1.5, None),
    ("body_mass", None, 1.5, None),
    ("walking_heart_rate_average", "down", 4.0, None),
    ("vo2_max", "up", 2.0, None),
]
RECENT_DAYS = 7
MIN_RECENT, MIN_BASELINE = 4, 20
# Weighed or estimated every week or so, so a week and two months would hold too few values to say anything.
SPARSE_METRICS = {"body_mass", "vo2_max"}
SPARSE_RECENT_DAYS, SPARSE_BASELINE_DAYS = 21, 120
SPARSE_MIN_RECENT, SPARSE_MIN_BASELINE = 2, 5
STALE_DAYS, SPARSE_STALE_DAYS = 3, 14
# A change has to clear this on its own and again across everything watched, on two days running, with most of the
# days behind it. Together these keep pure noise below 5% of runs across all ten measures (tests/test_insight_noise.py).
CHANGE_ALPHA, CHANGE_FDR = 0.01, 0.05
MOST_DAYS = 0.7
# Health-record measurements worth checking before and after a medication's first order.
MED_EFFECT_METRICS = ["resting_heart_rate", "blood_pressure_systolic", "blood_glucose", "body_mass", "hrv_rmssd",
                      "sleep_duration"]
MED_ALPHA, MED_FDR = 0.01, 0.05
MED_MIN_DAYS = 14


def _fmt(metric: str, v: float) -> str:
    unit = METRICS.get(metric, ("", "", "", ""))[1]
    if unit == "h":
        h = int(v)
        m = round((v - h) * 60)
        if m == 60:
            h, m = h + 1, 0
        return f"{h}h {m:02d}m"
    # Stored as percent already: normalize_sample turns a 0-1 fraction into 0-100.
    if unit == "%":
        return f"{v:.1f}%"
    if unit in ("steps", "kcal"):
        return f"{v:,.0f} {unit}"
    return f"{v:.1f} {unit}".strip() if abs(v) < 100 else f"{v:,.0f} {unit}".strip()


def _split(points: list[dict[str, Any]], last: date, recent_days: int, base_days: int) -> tuple[list[float], list[float]]:
    cut = last - timedelta(days=recent_days - 1)
    first = cut - timedelta(days=base_days)
    recent = [p["value"] for p in points if cut.isoformat() <= p["day"] <= last.isoformat()]
    base = [p["value"] for p in points if first.isoformat() <= p["day"] < cut.isoformat()]
    return recent, base


def _matters(delta: float, usual: float, min_abs: Optional[float], min_rel: Optional[float]) -> bool:
    if min_abs is not None and abs(delta) < min_abs:
        return False
    return min_rel is None or abs(delta) >= min_rel * abs(usual)


def _changes(profile_id: str) -> list[dict[str, Any]]:
    today = _today(profile_id)
    have = {m["metric"] for m in biometrics.available_metrics(profile_id)}
    found: list[tuple[Optional[dict[str, Any]], float]] = []
    moved: dict[tuple[str, str], dict[str, Any]] = {}
    for metric, better, min_abs, min_rel in WATCH:
        if metric not in have:
            continue
        sparse = metric in SPARSE_METRICS
        recent_days, base_days = (SPARSE_RECENT_DAYS, SPARSE_BASELINE_DAYS) if sparse else (RECENT_DAYS, BASELINE_DAYS)
        min_recent, min_base = (SPARSE_MIN_RECENT, SPARSE_MIN_BASELINE) if sparse else (MIN_RECENT, MIN_BASELINE)
        span = recent_days + base_days + 1      # one more, to look again as of the day before
        pts = _complete(biometrics.daily_series(profile_id, metric, days=span)["points"])
        if not pts:
            continue
        last = date.fromisoformat(pts[-1]["day"])
        if (today - last).days > (SPARSE_STALE_DAYS if sparse else STALE_DAYS):
            continue      # nothing recent to speak of
        cut = (last - timedelta(days=recent_days - 1)).isoformat()
        source = _main_source(p for p in pts if p["day"] >= cut)
        mine = _possible(metric, _same_device(profile_id, metric, pts, source, span))
        recent, base = _split(mine, last, recent_days, base_days)
        if len(recent) < min_recent or len(base) < min_base:
            # Too little from this device to compare it with itself. If that's because it's new, say so.
            for change in device_changes(pts)[-1:]:
                if change["to"] in _device_names(pts, source) and change["day"] >= (today - timedelta(days=DEVICE_NOTE_DAYS)).isoformat():
                    note = moved.setdefault((change["from"], change["to"]), {**change, "metrics": []})
                    note["day"] = min(note["day"], change["day"])
                    note["metrics"].append(metric)
            continue
        p = insight_stats.shift_p(recent, base)
        usual, spread = insight_stats.robust_center(base)
        typical = statistics.median(recent)
        delta = typical - usual
        before = _split(mine, last - timedelta(days=1), recent_days, base_days)
        steady = (len(before[0]) >= min_recent and len(before[1]) >= min_base
                  and insight_stats.shift_p(*before) <= CHANGE_ALPHA)
        most = sum(1 for v in recent if (v > usual) == (delta > 0)) >= MOST_DAYS * len(recent)
        if p > CHANGE_ALPHA or not steady or not most or not delta or not _matters(delta, usual, min_abs, min_rel):
            found.append((None, p))
            continue
        up = delta > 0
        label = METRICS[metric][0]
        band = _usual_range(metric, base)
        found.append(({
            "id": f"change:{metric}:{last.isoformat()}", "kind": "change", "metric": metric,
            "tone": reading_bounds.tone_for_move(metric, usual, typical, better),
            "title": f"{label} is {'up' if up else 'down'}",
            "text": (f"{label} was typically {_fmt(metric, typical)} over the last {recent_days} days, "
                     f"{'up' if up else 'down'} from your usual {_fmt(metric, usual)} (the {base_days} days before)."),
            "data": {"metric": metric, "unit": METRICS[metric][1], "recent": round(typical, 3), "usual": round(usual, 3),
                     "days": recent_days, "low": round(band["low"], 3), "high": round(band["high"], 3), "source": source},
            "link": f"#/trends?metric={metric}",
        }, p))
    keep = insight_stats.false_discoveries_removed([p for _, p in found], CHANGE_FDR)
    out = [item for (item, _), kept in zip(found, keep) if item and kept]
    return out + [_device_note(change) for change in moved.values()]


def _device_note(change: dict[str, Any]) -> dict[str, Any]:
    labels = [_soft(METRICS[m][0]) for m in change["metrics"]]
    named = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + f" and {labels[-1]}"
    return {
        "id": f"device:{change['to']}:{change['day']}", "kind": "device", "tone": "info",
        "title": f"Now measured by {_device_name(change['to'])}",
        "text": (f"Since {_day(change['day'])}, the numbers for {named} come from {_device_name(change['to'])}, instead of "
                 f"{_device_name(change['from'])}. Devices measure these differently, so earlier numbers aren't comparable. "
                 "Changes will be looked for again once there are enough days from the new one."),
        "data": {"from": change["from"], "to": change["to"], "day": change["day"], "metrics": change["metrics"]},
        "link": f"#/trends?metric={change['metrics'][0]}",
    }


def _sleep_notes(profile_id: str) -> list[dict[str, Any]]:
    out = []
    n = sleep.nights(profile_id, 14)
    st = n["stats"]
    if st["count"] >= 7 and (st["bedtime_spread_min"] or 0) >= 60:
        out.append({"id": f"sleep-schedule:{n['end']}", "kind": "sleep", "tone": "info",
                    "title": "Bedtime varied",
                    "text": (f"Your bedtime varied by about ±{st['bedtime_spread_min']} minutes over the last two weeks "
                             f"(usually around {st['bedtime']})."),
                    "data": {"spread_min": st["bedtime_spread_min"], "bedtime": st["bedtime"]},
                    "link": "#/trends/sleep?metric=sleep_duration"})
    week = [x for x in n["nights"] if x["day"] >= (date.fromisoformat(n["end"]) - timedelta(days=6)).isoformat()]
    short = [x for x in week if x["asleep_h"] < 6]
    if len(short) >= 3:
        out.append({"id": f"short-nights:{n['end']}", "kind": "sleep", "tone": "info",
                    "title": f"{len(short)} nights under 6 hours this week",
                    "text": f"You slept less than 6 hours on {len(short)} of the last {len(week)} nights.",
                    "data": {"short": len(short), "nights": len(week)}, "link": "#/trends/sleep?metric=sleep_duration"})
    return out


def _five(key: str, v: float) -> float:
    """A check-in measure on its 1-5 scale (mood is stored from -1 to 1)."""
    return (v + 1) * 2 + 1 if key == "mood" else v


def _word(words: list[str], key: str, v: float) -> str:
    return words[max(0, min(len(words) - 1, round(_five(key, v)) - 1))]


def _day(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{d:%b} {d.day}, {d.year}"


# What a link needs before it's a claim at all: days with both, and days on each side.
MIN_LINK_PAIRS = 30
MIN_LINK_GROUP = 8
MIN_SYMPTOM_DAYS = 8
MIN_SYMPTOM_OTHER_DAYS = 20
LINK_ALPHA, LINK_FDR = 0.01, 0.05
LINK_PERMUTATIONS = 499
MIN_SLEEP_GAP_H = 0.5
MIN_HRV_GAP = 0.08
MAX_SYMPTOM_LINKS = 2
COINCIDENCE = "This could be coincidence, and it doesn't show that one causes the other."

Candidate = tuple[Optional[dict[str, Any]], float]


def _feeling_links(profile_id: str, days: int = 90) -> list[Candidate]:
    """How check-ins line up with sleep the night before, with how likely each is by chance alone."""
    out: list[Candidate] = []
    start, end = _window(days, profile_id=profile_id)
    feel = journal_daily(profile_id, start, end)
    if not any(len(v) >= MIN_LINK_PAIRS for v in feel.values()):
        return out
    sleep_h = {p["day"]: p["value"] for p in biometrics.daily_series(profile_id, "sleep_duration", days=days)["points"]}
    for key, words, better in (("mood", MOOD_WORDS, "up"), ("energy", ENERGY_WORDS, "up"), ("stress", STRESS_WORDS, "down")):
        pairs = [(sleep_h[d], feel[key][d]) for d in sorted(feel[key]) if d in sleep_h]
        if len(pairs) < MIN_LINK_PAIRS:
            continue
        cut = statistics.median(h for h, _ in pairs)
        longer = [h > cut for h, _ in pairs]
        values = [v for _, v in pairs]
        more = [v for v, flag in zip(values, longer) if flag]
        less = [v for v, flag in zip(values, longer) if not flag]
        if len(more) < MIN_LINK_GROUP or len(less) < MIN_LINK_GROUP:
            continue
        a, b = sum(more) / len(more), sum(less) / len(less)
        gap = a - b
        if abs(gap) < (0.25 if key == "mood" else 0.4):
            out.append((None, 1.0))
            continue
        margin = insight_stats.gap_margin(more, less)
        points = _five(key, a) - _five(key, b)
        good = (gap > 0) == (better == "up")
        label = {"mood": "Mood", "energy": "Energy", "stress": "Stress"}[key]
        out.append(({
            "id": f"link:sleep-{key}", "kind": "link", "tone": "info",
            "title": f"{label} and sleep",
            "text": (f"After nights over {_fmt('sleep_duration', cut)} you rated your {key} {_five(key, a):.1f} out of 5 on average "
                     f"({_word(words, key, a)}), against {_five(key, b):.1f} ({_word(words, key, b)}) after shorter ones, "
                     f"over {len(pairs)} days with both. That's a gap of {abs(points):.1f}, give or take "
                     f"{margin * (2 if key == 'mood' else 1):.1f}. "
                     + ("So far, longer nights have gone with feeling better. " if good
                        else "So far, longer nights haven't gone with feeling better. ")
                     + COINCIDENCE),
            "data": {"a": "sleep_duration", "b": f"journal:{key}", "cut": round(cut, 2), "above": round(a, 2),
                     "below": round(b, 2), "n": len(pairs), "margin": round(margin, 2)},
            "link": f"#/trends?metric=sleep_duration&compare=journal:{key}",
        }, insight_stats.group_gap_p(values, longer, f"sleep-{key}", LINK_PERMUTATIONS)))
    return out


def _symptom_links(profile_id: str, days: int = 120) -> list[Candidate]:
    """Symptoms logged on many days, against sleep the night before on the days without them."""
    out: list[Candidate] = []
    tz = _tz(profile_id)
    events = activity.list_events(profile_id, days, category="symptoms", limit=5000)
    by_name: dict[str, set[str]] = defaultdict(set)
    for e in events:
        by_name[e["name"]].add(local_day(e["start_date"], tz))
    if not by_name:
        return out
    sleep_h = {p["day"]: p["value"] for p in biometrics.daily_series(profile_id, "sleep_duration", days=days)["points"]}
    if len(sleep_h) < MIN_LINK_PAIRS:
        return out
    ordered = sorted(sleep_h)
    values = [sleep_h[d] for d in ordered]
    for name, days_logged in sorted(by_name.items(), key=lambda kv: -len(kv[1])):
        flags = [d in days_logged for d in ordered]
        on = [v for v, flag in zip(values, flags) if flag]
        off = [v for v, flag in zip(values, flags) if not flag]
        if len(on) < MIN_SYMPTOM_DAYS or len(off) < MIN_SYMPTOM_OTHER_DAYS:
            continue
        gap = sum(on) / len(on) - sum(off) / len(off)
        if abs(gap) < MIN_SLEEP_GAP_H:
            out.append((None, 1.0))
            continue
        margin = insight_stats.gap_margin(on, off)
        out.append(({
            "id": f"link:symptom-{name}", "kind": "link", "tone": "info", "title": f"{name} and sleep",
            "text": (f"On the {len(on)} days you logged {name.lower()}, you'd slept {round(abs(gap) * 60)} minutes "
                     f"{'less' if gap < 0 else 'more'} the night before than on days you didn't, give or take "
                     f"{round(margin * 60)} minutes. {COINCIDENCE}"),
            "data": {"symptom": name, "days": len(on), "gap_h": round(gap, 2), "margin": round(margin, 2)},
            "link": "#/journal",
        }, insight_stats.group_gap_p(values, flags, f"symptom-{name}", LINK_PERMUTATIONS)))
    return out


def _hard_day_links(profile_id: str) -> list[Candidate]:
    """The top quarter of workout days, against rest days, by the next morning's HRV."""
    for metric in ("hrv_rmssd", "hrv_sdnn"):
        hrv = {p["day"]: p["value"] for p in biometrics.daily_series(profile_id, metric, days=90)["points"]}
        if len(hrv) < MIN_LINK_PAIRS:
            continue
        start, end = _window(90, profile_id=profile_id)
        load = workout_daily(profile_id, start, end)["load"]
        trained = sorted(v for v in load.values() if v > 0)
        if not trained:
            return []
        cut = trained[int(len(trained) * 0.75)]
        days = [d for d in sorted(load) if (date.fromisoformat(d) + timedelta(days=1)).isoformat() in hrv and (load[d] >= cut or load[d] == 0)]
        values = [hrv[(date.fromisoformat(d) + timedelta(days=1)).isoformat()] for d in days]
        flags = [load[d] > 0 for d in days]
        hard = [v for v, flag in zip(values, flags) if flag]
        rest = [v for v, flag in zip(values, flags) if not flag]
        if len(hard) < MIN_LINK_GROUP or len(rest) < MIN_LINK_GROUP:
            return []
        a, b = sum(hard) / len(hard), sum(rest) / len(rest)
        if abs(a - b) < MIN_HRV_GAP * b:
            return [(None, 1.0)]
        margin = insight_stats.gap_margin(hard, rest)
        return [({"id": f"link:load-{metric}", "kind": "link", "tone": "info", "title": "Hard days and HRV",
                  "text": (f"The morning after your hardest training days your HRV averaged {a:.0f} ms, against "
                           f"{b:.0f} ms after rest days, a gap of {abs(a - b):.0f} ms give or take {margin:.0f}. {COINCIDENCE}"),
                  "data": {"after_hard": round(a, 1), "after_rest": round(b, 1), "metric": metric, "margin": round(margin, 1)},
                  "link": f"#/trends?metric={metric}&compare=workouts:load&lag=-1"},
                 insight_stats.group_gap_p(values, flags, f"load-{metric}", LINK_PERMUTATIONS))]
    return []


def _links(profile_id: str) -> list[dict[str, Any]]:
    """Patterns across sleep, how the person felt and training. They're judged together: looking at many pairs is how a
    coincidence gets found, so what counts as striking depends on how many were looked at."""
    found = _feeling_links(profile_id) + _symptom_links(profile_id) + _hard_day_links(profile_id)
    keep = insight_stats.false_discoveries_removed([p for _, p in found], LINK_FDR)
    out, symptoms = [], 0
    for (item, p), kept in sorted(zip(found, keep), key=lambda pair: pair[0][1]):
        if not item or not kept or p > LINK_ALPHA:
            continue
        if item["id"].startswith("link:symptom-"):
            symptoms += 1
            if symptoms > MAX_SYMPTOM_LINKS:
                continue
        out.append(item)
    return out


def _training_notes(profile_id: str) -> list[dict[str, Any]]:
    t = training.summary(profile_id, 5)
    load = t["load"]
    if load["status"] != "spike":
        return []
    return [{"id": f"load:{t['weeks'][-1]['week']}", "kind": "training", "tone": "info",
             "title": "Training load jumped",
             "text": (f"Your training load over the last 7 days is much more than your recent weeks, about "
                      f"{round((load['ratio'] - 1) * 100)}% above your weekly average for the four weeks before."),
             "data": load, "link": "#/workouts"}]


def _medication_notes(profile_id: str) -> list[dict[str, Any]]:
    """What differed in wearable measurements around the first order on record for a medication in the last year, from
    one device, with at least two weeks of data on each side. A renewal isn't a beginning, and the first order here may
    not be the true start either, since portals don't always keep old history; the wording says which it is."""
    today = _today(profile_id)
    have = {m["metric"] for m in biometrics.available_metrics(profile_id)}
    found: list[tuple[dict[str, Any], float]] = []
    gates = {metric: (min_abs, min_rel) for metric, _, min_abs, min_rel in WATCH}
    for med in medication_timeline(profile_id):
        started = date.fromisoformat(med["first"])
        if not 21 <= (today - started).days <= 365 or not med["sure"]:
            continue
        day = med["first"]
        end = started + timedelta(days=30)
        phrase = f"you started {med['name']}" if med["reported_taking"] else f"the first order on record for {med['name']}"
        for metric in MED_EFFECT_METRICS:
            if metric not in have:
                continue
            pts = _complete(biometrics.daily_series(profile_id, metric, days=60, end=end)["points"])
            source = _main_source(p for p in pts if p["day"] > day)
            mine = _possible(metric, _same_device(profile_id, metric, pts, source, 60, end))
            before = [p["value"] for p in mine if p["day"] < day]
            after = [p["value"] for p in mine if p["day"] > day]
            if len(before) < MED_MIN_DAYS or len(after) < MED_MIN_DAYS:
                continue
            b, a = statistics.median(before), statistics.median(after)
            min_abs, min_rel = gates.get(metric, (None, None))
            p = insight_stats.shift_p(after, before)
            if not a - b or not _matters(a - b, b, min_abs, min_rel):
                p = 1.0
            label = METRICS[metric][0]
            found.append(({
                "id": f"med:{med['record_id']}:{metric}", "kind": "medication", "tone": "info",
                "title": f"{label} since {med['name']}",
                "text": (f"In the month after {phrase} ({_day(day)}), your {_soft(label)} was typically "
                         f"{_fmt(metric, a)}, against {_fmt(metric, b)} the month before. Other things change too, "
                         "so this doesn't show an effect."),
                "data": {"metric": metric, "unit": METRICS[metric][1], "before": round(b, 3), "after": round(a, 3),
                         "started": day, "medication": med["name"], "reported_taking": med["reported_taking"], "source": source},
                "link": f"#/trends?metric={metric}",
            }, p))
    keep = insight_stats.false_discoveries_removed([p for _, p in found], MED_FDR)
    chosen = [(item, p) for (item, p), kept in zip(found, keep) if kept and p <= MED_ALPHA]
    return [item for item, _ in sorted(chosen, key=lambda pair: pair[1])][:3]


# A lab result has to move this far to be called a change: a share of the last result, or of the width of the range for
# tests with a narrow one (sodium), since ordinary variation is smaller than the range.
LAB_MIN_CHANGE, LAB_MIN_RANGE_SHARE = 0.05, 0.25
DRIFT_MIN_RESULTS, DRIFT_MIN_DAYS = 3, 365
DRIFT_MIN_TAU, DRIFT_MIN_CHANGE = 0.6, 0.1
BACK_IN_RANGE_DAYS = 180
MAX_LAB_NOTES, MAX_GOOD_LAB_NOTES = 3, 2
MAX_RECENT_NORMAL_CODES = 40


def _range_side(v: float, low: Optional[float], high: Optional[float]) -> Optional[str]:
    if high is not None and v > high:
        return "high"
    if low is not None and v < low:
        return "low"
    return None


def _flag_side(interpretation: Optional[str], v: float, low: Optional[float], high: Optional[float]) -> Optional[str]:
    """Which side of the range a result is on. The lab's own high or low wins; a bare "abnormal" says nothing about
    direction, so that comes from the value against the range, or not at all."""
    word = interpretation or ""
    if "high" in word:
        return "high"
    if "low" in word:
        return "low"
    return None if word == "normal" else _range_side(v, low, high)


def _ref_words(lab_ref: Optional[str], s: dict[str, Any]) -> str:
    low, high = s.get("ref_low"), s.get("ref_high")
    text = lab_ref or (f"{low:g}–{high:g}" if low is not None and high is not None else None)
    return f" ({text})" if text else ""


def _ordinal(iso: str) -> int:
    return date.fromisoformat(iso[:10]).toordinal()


def _span_words(days: int) -> str:
    years = round(days / 365.25)
    return f"about {years} years" if years >= 2 else f"about {round(days / 30.4)} months"


def _lab_note(lab: dict[str, Any], s: dict[str, Any]) -> Optional[dict[str, Any]]:
    """What's worth saying about a flagged result, from its history in the same test."""
    pts, low, high = s["points"], s["ref_low"], s["ref_high"]
    if len(pts) < 2 or not _iso_day(pts[-1]["t"]) or not _iso_day(pts[-2]["t"]):
        return None
    last, prev = pts[-1], pts[-2]
    if last.get("comparator") or prev.get("comparator"):
        return None     # a limit like "<5" isn't a level to have moved from
    # A newer result with only words can be what's flagged: the last number is then an older result's, not this one's.
    if (lab.get("effective_at") or "")[:10] != last["t"][:10]:
        return None
    side = _flag_side(lab.get("interpretation"), last["v"], low, high)
    if not side:
        return None
    code, unit = lab["code"], s.get("unit") or ""
    # As many decimals as the lab reports anywhere in the series: 6.0 %, not 6 %, next to 6.1 %.
    places = min(3, max(len(f"{float(p['v']):.6f}".rstrip("0").split(".")[1]) for p in pts))
    num = lambda v: f"{v:.{places}f}"  # noqa: E731
    out_word = "above" if side == "high" else "below"
    ref = _range_words(lab, s, last)
    moved = last["v"] - prev["v"]
    outward = (moved > 0) == (side == "high")
    big = abs(moved) >= (min(LAB_MIN_CHANGE * abs(prev["v"]), LAB_MIN_RANGE_SHARE * (high - low))
                         if low is not None and high is not None else LAB_MIN_CHANGE * abs(prev["v"]))
    base = {"data": {"code": code, "latest": last["v"], "previous": prev["v"], "unit": unit, "side": side},
            "link": f"#/trends?code={code}", "kind": "lab"}
    sentence = (f"{s['title']} was {num(last['v'])} {unit} on {_day(last['t'])}, {'up' if moved > 0 else 'down'} from "
                f"{num(prev['v'])} {unit} on {_day(prev['t'])}").replace("  ", " ")
    if big and not outward:
        # Closer isn't good news while the lab still calls it critical.
        if lab.get("interpretation") in records.CRITICAL:
            return {**base, "id": f"lab:{code}:{last['t'][:10]}", "tone": "attention", "title": f"{s['title']} still flagged critical",
                    "text": f"{sentence}, and still {out_word} the reference range{ref}. The lab marked this critical."}
        return {**base, "id": f"lab:{code}:{last['t'][:10]}", "tone": "good", "title": f"{s['title']} closer to the range",
                "text": f"{sentence}, and still {out_word} the reference range{ref}."}
    dated = [p for p in pts if _iso_day(p["t"]) and not p.get("comparator")]
    if len(dated) >= DRIFT_MIN_RESULTS and _ordinal(last["t"]) - _ordinal(dated[0]["t"]) >= DRIFT_MIN_DAYS:
        ts = [float(_ordinal(p["t"])) for p in dated]
        slope, tau = insight_stats.theil_sen(ts, [p["v"] for p in dated])
        span = int(ts[-1] - ts[0])
        if (slope > 0) == (side == "high") and abs(tau) >= DRIFT_MIN_TAU and abs(slope * span) >= DRIFT_MIN_CHANGE * abs(dated[0]["v"]):
            crossed = None
            for a, b in zip(dated, dated[1:]):
                if _flag_side(b.get("interpretation"), b["v"], low, high) == side and _flag_side(a.get("interpretation"), a["v"], low, high) != side:
                    crossed = b["t"]
            d = date.fromisoformat(crossed[:10]) if crossed else None
            return {**base, "id": f"lab:{code}:{last['t'][:10]}", "tone": "attention",
                    "title": f"{s['title']} has been {'rising' if side == 'high' else 'falling'}",
                    "text": (f"{s['title']} has gone from {num(dated[0]['v'])} to {num(last['v'])} {unit} over {_span_words(span)}, and "
                             + (f"has been {out_word} the reference range{ref} since {d:%b} {d.year}." if d
                                else f"is {out_word} the reference range{ref}.")).replace("  ", " ")}
    if not big or not outward:
        return None
    if _flag_side(prev.get("interpretation"), prev["v"], low, high) == side:
        return {**base, "id": f"lab:{code}:{last['t'][:10]}", "tone": "attention", "title": f"{s['title']} moving further out of range",
                "text": f"{sentence}, and {out_word} the reference range{ref}."}
    first = not any(_flag_side(p.get("interpretation"), p["v"], low, high) == side for p in pts[:-1])
    return {**base, "id": f"lab:{code}:{last['t'][:10]}", "tone": "attention", "title": f"{s['title']} moved out of range",
            "text": f"{sentence}, which is {out_word} the reference range{ref}" + (", for the first time on record." if first else ".")}


def _range_words(lab: dict[str, Any], series: dict[str, Any], last: dict[str, Any]) -> str:
    """The reference range beside a series' numbers, in their unit. The lab's own words only fit a result shown as the lab reported it."""
    if not last.get("converted"):
        return _ref_words(lab.get("ref_text"), series)
    low, high, unit = series.get("ref_low"), series.get("ref_high"), series.get("unit") or ""
    if low is not None and high is not None:
        return f" ({low:g}–{high:g} {unit})".replace(" )", ")")
    if low is not None or high is not None:
        return f" ({'≥' if low is not None else '≤'} {low if low is not None else high:g} {unit})".replace(" )", ")")
    text, reported = lab.get("ref_text"), lab.get("unit") or ""
    text = f"{text} {reported}".strip() if text and reported not in text else text
    return f" ({text})" if text else ""


def _lab_notes(profile_id: str) -> list[dict[str, Any]]:
    """Out-of-range lab results that moved meaningfully, drifted over years or came back: the numbers, and which side of
    the range they're on when the record can say."""
    found = []
    for lab in records.flagged_labs(profile_id):
        # An old result stays on the out-of-range list, marked as older. It isn't news.
        if lab.get("code") and not lab.get("older"):
            note = _lab_note(lab, records.observation_series(profile_id, lab["code"], "labs"))
            if note:
                found.append(note)
    since = (_today(profile_id) - timedelta(days=BACK_IN_RANGE_DAYS)).isoformat()
    for code in records.recently_normal_lab_codes(profile_id, since, MAX_RECENT_NORMAL_CODES):
        s = records.observation_series(profile_id, code, "labs")
        pts, low, high = s["points"], s["ref_low"], s["ref_high"]
        if len(pts) < 2 or pts[-1]["t"][:10] < since or not _iso_day(pts[-2]["t"]) or not _iso_day(pts[-1]["t"]):
            continue
        last, prev = pts[-1], pts[-2]
        if last.get("comparator") or prev.get("comparator"):
            continue        # a limit like "<5" isn't a level to have moved from
        if _flag_side(last.get("interpretation"), last["v"], low, high) or not _flag_side(prev.get("interpretation"), prev["v"], low, high):
            continue
        unit = s.get("unit") or ""
        found.append({"id": f"lab:{code}:{last['t'][:10]}", "kind": "lab", "tone": "good", "title": f"{s['title']} back in range",
                      "text": (f"{s['title']} was {last['v']:g} {unit} on {_day(last['t'])}, back within the reference range"
                               f"{_ref_words(None, s)} from {prev['v']:g} {unit} on {_day(prev['t'])}.").replace("  ", " "),
                      "data": {"code": code, "latest": last["v"], "previous": prev["v"], "unit": unit},
                      "link": f"#/trends?code={code}"})
    found.sort(key=lambda n: n["id"].rsplit(":", 1)[1], reverse=True)
    return ([n for n in found if n["tone"] != "good"][:MAX_LAB_NOTES]
            + [n for n in found if n["tone"] == "good"][:MAX_GOOD_LAB_NOTES])


def _goal_notes(profile_id: str) -> list[dict[str, Any]]:
    out = []
    for g in goals.progress(profile_id, 7):
        if g["days"] < 5:
            continue
        tone = "good" if g["met"] >= g["days"] - 1 else "info"
        out.append({
            "id": f"goal:{g['metric']}", "kind": "goal", "tone": tone,
            "title": f"{g['label']} goal: {g['met']} of {g['days']} days",
            "text": (f"You met your {_soft(g['label'])} goal ({'at least' if g['direction'] == 'min' else 'at most'} "
                     f"{_fmt(g['metric'], g['target'])}) on {g['met']} of the last {g['days']} days"
                     + (f", {g['streak']} in a row." if g["streak"] >= 3 else ".")),
            "data": {k: g[k] for k in ("metric", "unit", "target", "direction", "met", "days", "streak")},
            "link": f"#/trends?metric={g['metric']}",
        })
    return out


# ---------------------------------------------------------------------------
# What the lab marked critical
# ---------------------------------------------------------------------------

LAB_FRESH_DAYS = 30


def _when(day: str, today: date) -> str:
    ago = (today - date.fromisoformat(day)).days
    return "today" if ago == 0 else "yesterday" if ago == 1 else f"on {_day(day)}"


def _critical_labs(profile_id: str) -> list[dict[str, Any]]:
    """Recent results the lab marked critical, as the lab reported them."""
    today = _today(profile_id)
    since = (today - timedelta(days=LAB_FRESH_DAYS)).isoformat()
    out = []
    for lab in records.flagged_labs(profile_id):
        day = _iso_day(lab.get("effective_at"))
        if not (lab.get("interpretation") or "").startswith("critical") or not day or day < since:
            continue
        reference = f" (range {lab['ref_text']})" if lab.get("ref_text") else ""
        value = lab.get("value_text") or "out of range"
        out.append({
            "id": f"critical:{lab.get('code') or lab['title']}:{day}", "kind": "critical", "tone": "attention",
            "title": f"{lab['title']}: the lab marked this critical",
            "text": f"{lab['title']} was {value}{reference} {_when(day, today)}. The lab marked this critical.",
            "data": {"code": lab.get("code"), "value_text": lab.get("value_text"), "interpretation": lab["interpretation"], "day": day},
            "link": f"#/trends/labs?code={lab.get('code') or ''}",
        })
    return out


TONE_ORDER = {"attention": 0, "info": 1, "good": 2}
KIND_ORDER = {"critical": -1, "lab": 0, "change": 1, "device": 2, "training": 3, "sleep": 4, "medication": 5, "link": 6, "goal": 7}


def _guarded(generators: Iterable[Any], profile_id: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for fn in generators:
        try:
            found += fn(profile_id)
        except Exception as exc:  # noqa: BLE001 - one broken source never hides the rest
            # Where and what kind only: an exception's message can carry a person's numbers, and logs get shared.
            frame = traceback.extract_tb(exc.__traceback__)[-1]
            log.error("Insights: %s failed with %s at %s:%d", fn.__name__, type(exc).__name__, Path(frame.filename).name, frame.lineno)
    return found


def insights(profile_id: str, limit: int = 12) -> list[dict[str, Any]]:
    """What's worth knowing right now across every source, what the lab marked critical first."""
    found = _guarded((_critical_labs, _lab_notes, _changes, _training_notes, _sleep_notes, _medication_notes, _links, _goal_notes), profile_id)
    # A critical result says everything a note about the same test would.
    critical = {i["data"]["code"] for i in found if i["kind"] == "critical" and i["data"].get("code")}
    found = [i for i in found if not (i["kind"] == "lab" and i["data"].get("code") in critical)]
    found.sort(key=lambda i: (TONE_ORDER[i["tone"]], KIND_ORDER.get(i["kind"], 9)))
    return found[:limit]
