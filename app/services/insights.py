"""
Insights across sources: the things only visible with everything in one place. A wearable number moving away from
the person's own normal, how sleep lines up with how they said they felt, what changed after a medication started,
a lab result moving further out of range, training load jumping, goals kept.

Everything here is descriptive: "since", "on days when", never "because". Each insight carries plain text (for Ask
and other AI apps) and the numbers behind it (so the apps can show them in the person's units).
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Optional

from app.store import activity, biometrics, goals, records, sleep, training
from app.store.biometrics import METRICS, _tz, local_day

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


def _window(days: int, end: Optional[date] = None) -> tuple[date, date]:
    end = end or datetime.now(_tz()).date()
    return end - timedelta(days=days - 1), end


def journal_daily(profile_id: str, start: date, end: date) -> dict[str, dict[str, float]]:
    """Per day, the average mood (−1..1), energy and stress (1..5) from check-ins and mood entries."""
    tz = _tz()
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
    tz = _tz()
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
    start, last = _window(days, end)
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
    have = journal_daily(profile_id, *_window(180))
    for key, (label, unit, _) in JOURNAL_SERIES.items():
        if len(have[key.split(":")[1]]) >= 5:
            out.append({"key": key, "label": label, "unit": unit, "group": "journal"})
    if activity.counts(profile_id)["workouts"]:
        out += [{"key": k, "label": label, "unit": unit, "group": "workouts"} for k, (label, unit, _) in WORKOUT_SERIES.items()]
    return out


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if not sx or not sy:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def strength(r: Optional[float], n: int) -> str:
    if r is None or n < 7:
        return "not enough data"
    a = abs(r)
    return "no clear link" if a < 0.1 else "weak" if a < 0.3 else "moderate" if a < 0.5 else "strong"


def compare(profile_id: str, a: str, b: str, days: int = 90, lag: int = 0) -> dict[str, Any]:
    """Pairs ``a`` on each day with ``b`` on the same day, or ``lag`` days later (negative: earlier; HRV against the
    day before's training is lag -1), the correlation, and ``b`` on ``a``'s higher and lower days."""
    lag = max(-7, min(int(lag), 7))
    sa = series(profile_id, a, days)
    sb = series(profile_id, b, days + abs(lag), end=_window(1)[1])
    pairs = []
    for day, x in sorted(sa["values"].items()):
        other = (date.fromisoformat(day) + timedelta(days=lag)).isoformat()
        if other in sb["values"]:
            pairs.append({"day": day, "x": round(x, 3), "y": round(sb["values"][other], 3)})
    xs, ys = [p["x"] for p in pairs], [p["y"] for p in pairs]
    r = _pearson(xs, ys)
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
            "r": round(r, 2) if r is not None else None, "strength": strength(r, len(pairs)), "split": split}


# ---------------------------------------------------------------------------
# Events from health records, for charts
# ---------------------------------------------------------------------------

def markers(profile_id: str, start: Optional[str] = None, end: Optional[str] = None) -> list[dict[str, Any]]:
    """Medications started and conditions diagnosed, with dates, newest first."""
    out = []
    for cat, verb in (("medications", "Started"), ("conditions", "Diagnosed")):
        for r in records.list_records(profile_id, cat, since=start, until=end, limit=300)["items"]:
            if not r.get("effective_at"):
                continue
            out.append({"t": r["effective_at"][:10], "label": f"{verb} {r['title']}", "kind": cat, "record_id": r["id"],
                        "status": r.get("status")})
    return sorted(out, key=lambda m: m["t"], reverse=True)


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
    ("oxygen_saturation", "up", 0.015, None),
    ("body_mass", None, 1.5, None),
    ("walking_heart_rate_average", "down", 4.0, None),
    ("vo2_max", "up", 2.0, None),
]
RECENT_DAYS, BASELINE_DAYS = 7, 60
# Health-record measurements worth checking before and after a medication started.
MED_EFFECT_METRICS = ["resting_heart_rate", "blood_pressure_systolic", "blood_glucose", "body_mass", "hrv_rmssd",
                      "sleep_duration"]


def _fmt(metric: str, v: float) -> str:
    unit = METRICS.get(metric, ("", "", "", ""))[1]
    if unit == "h":
        h = int(v)
        m = round((v - h) * 60)
        if m == 60:
            h, m = h + 1, 0
        return f"{h}h {m:02d}m"
    if metric in biometrics.FRACTION_METRICS:
        return f"{v * 100:.1f}%"
    if unit in ("steps", "kcal"):
        return f"{v:,.0f} {unit}"
    return f"{v:.1f} {unit}".strip() if abs(v) < 100 else f"{v:,.0f} {unit}".strip()


def _changes(profile_id: str) -> list[dict[str, Any]]:
    out = []
    have = {m["metric"] for m in biometrics.available_metrics(profile_id)}
    for metric, better, min_abs, min_rel in WATCH:
        if metric not in have:
            continue
        pts = [p for p in biometrics.daily_series(profile_id, metric, days=RECENT_DAYS + BASELINE_DAYS)["points"]
               if not p.get("partial")]
        if not pts:
            continue
        last = date.fromisoformat(pts[-1]["day"])
        if (datetime.now(_tz()).date() - last).days > 3:
            continue      # nothing recent to speak of
        cut = (last - timedelta(days=RECENT_DAYS - 1)).isoformat()
        recent = [p["value"] for p in pts if p["day"] >= cut]
        base = [p["value"] for p in pts if p["day"] < cut]
        if len(recent) < 4 or len(base) < 20:
            continue
        r_mean, b_mean = sum(recent) / len(recent), sum(base) / len(base)
        sd = statistics.pstdev(base) or 1e-9
        delta = r_mean - b_mean
        if min_abs is not None and abs(delta) < min_abs:
            continue
        if min_rel is not None and abs(delta) < min_rel * abs(b_mean):
            continue
        if abs(delta) / (sd / math.sqrt(len(recent))) < 2.5:
            continue      # within the normal day-to-day swing
        up = delta > 0
        tone = "info" if better is None else "good" if (better == "up") == up else "attention"
        label = METRICS[metric][0]
        out.append({
            "id": f"change:{metric}:{last.isoformat()}", "kind": "change", "tone": tone, "metric": metric,
            "title": f"{label} is {'up' if up else 'down'}",
            "text": (f"{label} averaged {_fmt(metric, r_mean)} over the last {RECENT_DAYS} days, "
                     f"{'up' if up else 'down'} from your usual {_fmt(metric, b_mean)} (the {BASELINE_DAYS} days before)."),
            "data": {"metric": metric, "unit": METRICS[metric][1], "recent": round(r_mean, 3), "usual": round(b_mean, 3),
                     "days": RECENT_DAYS},
            "link": f"#/trends?metric={metric}",
        })
    return out


def _sleep_notes(profile_id: str) -> list[dict[str, Any]]:
    out = []
    n = sleep.nights(profile_id, 14)
    st = n["stats"]
    if st["count"] >= 7 and (st["bedtime_spread_min"] or 0) >= 60:
        out.append({"id": f"sleep-schedule:{n['end']}", "kind": "sleep", "tone": "attention",
                    "title": "Irregular bedtime",
                    "text": (f"Your bedtime varied by about ±{st['bedtime_spread_min']} minutes over the last two weeks "
                             f"(usually around {st['bedtime']}). A steadier schedule tends to make sleep more restful."),
                    "data": {"spread_min": st["bedtime_spread_min"], "bedtime": st["bedtime"]},
                    "link": "#/trends/sleep?metric=sleep_duration"})
    week = [x for x in n["nights"] if x["day"] >= (date.fromisoformat(n["end"]) - timedelta(days=6)).isoformat()]
    short = [x for x in week if x["asleep_h"] < 6]
    if len(short) >= 3:
        out.append({"id": f"short-nights:{n['end']}", "kind": "sleep", "tone": "attention",
                    "title": f"{len(short)} short nights this week",
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


def _feeling_links(profile_id: str, days: int = 90) -> list[dict[str, Any]]:
    """How check-ins line up with sleep the night before and with training the day before."""
    out = []
    start, end = _window(days)
    feel = journal_daily(profile_id, start, end)
    if not any(len(v) >= 10 for v in feel.values()):
        return out
    sleep_h = {p["day"]: p["value"] for p in biometrics.daily_series(profile_id, "sleep_duration", days=days)["points"]}
    for key, words, better in (("mood", MOOD_WORDS, "up"), ("energy", ENERGY_WORDS, "up"), ("stress", STRESS_WORDS, "down")):
        pairs = [(sleep_h[d], v) for d, v in feel[key].items() if d in sleep_h]
        if len(pairs) < 10:
            continue
        cut = statistics.median(h for h, _ in pairs)
        more = [v for h, v in pairs if h > cut]
        less = [v for h, v in pairs if h <= cut]
        if len(more) < 4 or len(less) < 4:
            continue
        a, b = sum(more) / len(more), sum(less) / len(less)
        gap = a - b
        if abs(gap) < (0.25 if key == "mood" else 0.4):
            continue
        good = (gap > 0) == (better == "up")
        label = {"mood": "Mood", "energy": "Energy", "stress": "Stress"}[key]
        h = int(cut)
        m = round((cut - h) * 60)
        out.append({
            "id": f"link:sleep-{key}", "kind": "link", "tone": "info",
            "title": f"{label} and sleep",
            "text": (f"After nights over {h}h {m:02d}m you rated your {key} {_five(key, a):.1f} out of 5 on average "
                     f"({_word(words, key, a)}), against {_five(key, b):.1f} ({_word(words, key, b)}) after shorter ones, "
                     f"over {len(pairs)} days with both."
                     + (" More sleep lines up with feeling better for you." if good
                        else " Longer nights haven't lined up with feeling better so far.")),
            "data": {"a": "sleep_duration", "b": f"journal:{key}", "cut": round(cut, 2), "above": round(a, 2),
                     "below": round(b, 2), "n": len(pairs)},
            "link": f"#/trends?metric=sleep_duration&compare=journal:{key}",
        })
    return out


def _symptom_links(profile_id: str, days: int = 120) -> list[dict[str, Any]]:
    """Symptoms logged on several days, against sleep the night before."""
    out = []
    tz = _tz()
    events = activity.list_events(profile_id, days, category="symptoms", limit=5000)
    by_name: dict[str, set[str]] = defaultdict(set)
    for e in events:
        by_name[e["name"]].add(local_day(e["start_date"], tz))
    if not by_name:
        return out
    sleep_h = {p["day"]: p["value"] for p in biometrics.daily_series(profile_id, "sleep_duration", days=days)["points"]}
    if len(sleep_h) < 20:
        return out
    usual = sum(sleep_h.values()) / len(sleep_h)
    for name, days_logged in sorted(by_name.items(), key=lambda kv: -len(kv[1])):
        on = [sleep_h[d] for d in days_logged if d in sleep_h]
        if len(on) < 3:
            continue
        gap = sum(on) / len(on) - usual
        if abs(gap) < 0.5:
            continue
        mins = round(abs(gap) * 60)
        out.append({
            "id": f"link:symptom-{name}", "kind": "link", "tone": "info", "title": f"{name} and sleep",
            "text": (f"On the {len(on)} days you logged {name.lower()}, you'd slept {mins} minutes "
                     f"{'less' if gap < 0 else 'more'} than usual the night before."),
            "data": {"symptom": name, "days": len(on), "gap_h": round(gap, 2)},
            "link": "#/journal",
        })
        if len(out) >= 2:
            break
    return out


def _training_notes(profile_id: str) -> list[dict[str, Any]]:
    out = []
    t = training.summary(profile_id, 5)
    load = t["load"]
    if load["status"] == "spike":
        out.append({"id": f"load:{t['weeks'][-1]['week']}", "kind": "training", "tone": "attention",
                    "title": "Training load jumped",
                    "text": (f"Your training load over the last 7 days is {round((load['ratio'] - 1) * 100)}% above your "
                             "weekly average for the four weeks before. Big jumps raise the risk of injury; build up gradually."),
                    "data": load, "link": "#/workouts"})
    # Workouts and next morning's HRV.
    start, end = _window(90)
    w = workout_daily(profile_id, start, end)["load"]
    for metric in ("hrv_rmssd", "hrv_sdnn"):
        hrv = {p["day"]: p["value"] for p in biometrics.daily_series(profile_id, metric, days=90)["points"]}
        if len(hrv) < 30:
            continue
        cut = sorted(w.values())[int(len(w) * 0.75)] if w else 0
        hard = [hrv[(date.fromisoformat(d) + timedelta(days=1)).isoformat()] for d, v in w.items()
                if v > 0 and v >= cut and (date.fromisoformat(d) + timedelta(days=1)).isoformat() in hrv]
        rest = [hrv[(date.fromisoformat(d) + timedelta(days=1)).isoformat()] for d, v in w.items()
                if v == 0 and (date.fromisoformat(d) + timedelta(days=1)).isoformat() in hrv]
        if len(hard) >= 5 and len(rest) >= 5:
            a, b = sum(hard) / len(hard), sum(rest) / len(rest)
            if abs(a - b) >= 0.08 * b:
                out.append({"id": f"link:load-{metric}", "kind": "link", "tone": "info", "title": "Hard days and recovery",
                            "text": (f"The morning after your hardest training days your HRV averaged {a:.0f} ms, against "
                                     f"{b:.0f} ms after rest days."),
                            "data": {"after_hard": round(a, 1), "after_rest": round(b, 1), "metric": metric},
                            "link": f"#/trends?metric={metric}&compare=workouts:load&lag=-1"})
        break
    return out


def _medication_notes(profile_id: str) -> list[dict[str, Any]]:
    """What changed in wearable measurements since a medication started in the last year (at least three weeks of
    data on each side)."""
    out = []
    today = datetime.now(_tz()).date()
    have = {m["metric"] for m in biometrics.available_metrics(profile_id)}
    for m in markers(profile_id, start=(today - timedelta(days=365)).isoformat()):
        if m["kind"] != "medications":
            continue
        started = date.fromisoformat(m["t"])
        if (today - started).days < 21:
            continue
        name = m["label"].removeprefix("Started ")
        for metric in MED_EFFECT_METRICS:
            if metric not in have:
                continue
            pts = biometrics.daily_series(profile_id, metric, days=60, end=started + timedelta(days=30))["points"]
            before = [p["value"] for p in pts if p["day"] < m["t"] and not p.get("partial")]
            after = [p["value"] for p in pts if p["day"] > m["t"] and not p.get("partial")]
            if len(before) < 14 or len(after) < 14:
                continue
            b, a = sum(before) / len(before), sum(after) / len(after)
            sd = statistics.pstdev(before + after) or 1e-9
            if abs(a - b) < 0.5 * sd:
                continue
            label = METRICS[metric][0]
            out.append({
                "id": f"med:{m['record_id']}:{metric}", "kind": "medication", "tone": "info",
                "title": f"{label} since {name}",
                "text": (f"In the month after you started {name} ({_day(m['t'])}), your {label.lower()} averaged "
                         f"{_fmt(metric, a)}, against {_fmt(metric, b)} the month before. Other things change too, "
                         "so it's worth discussing with whoever prescribed it rather than reading it as an effect."),
                "data": {"metric": metric, "unit": METRICS[metric][1], "before": round(b, 3), "after": round(a, 3),
                         "started": m["t"], "medication": name},
                "link": f"#/trends?metric={metric}",
            })
    return out[:3]


def _lab_notes(profile_id: str) -> list[dict[str, Any]]:
    """Out-of-range lab results that moved further from the range since the previous result."""
    out = []
    for lab in records.summary(profile_id)["flagged_labs"]:
        code = lab.get("code")
        if not code:
            continue
        s = records.observation_series(profile_id, code, "labs")
        pts = s["points"]
        if len(pts) < 2:
            continue
        last, prev = pts[-1], pts[-2]
        high = "high" in (lab.get("interpretation") or "")
        worse = last["v"] > prev["v"] if high else last["v"] < prev["v"]
        if not worse or last["v"] == prev["v"]:
            continue
        unit = s.get("unit") or ""
        # As many decimals as the lab reports anywhere in the series: 6.0 %, not 6 %, next to 6.1 %.
        places = min(3, max(len(f"{float(p['v']):.6f}".rstrip("0").split(".")[1]) for p in pts))
        num = lambda v: f"{v:.{places}f}"  # noqa: E731
        out.append({
            "id": f"lab:{code}:{last['t'][:10]}", "kind": "lab", "tone": "attention",
            "title": f"{s['title']} moving further out of range",
            "text": (f"{s['title']} was {num(last['v'])} {unit} on {_day(last['t'])}, {'up' if high else 'down'} from "
                     f"{num(prev['v'])} {unit} on {_day(prev['t'])}, and {'above' if high else 'below'} the reference range "
                     f"({lab.get('ref_text') or 'as reported'})."),
            "data": {"code": code, "latest": last["v"], "previous": prev["v"], "unit": unit},
            "link": f"#/trends?code={code}",
        })
    return out[:3]


def _goal_notes(profile_id: str) -> list[dict[str, Any]]:
    out = []
    for g in goals.progress(profile_id, 7):
        if g["days"] < 5:
            continue
        tone = "good" if g["met"] >= g["days"] - 1 else "attention" if g["met"] <= g["days"] // 3 else "info"
        out.append({
            "id": f"goal:{g['metric']}", "kind": "goal", "tone": tone,
            "title": f"{g['label']} goal: {g['met']} of {g['days']} days",
            "text": (f"You met your {g['label'].lower()} goal ({'at least' if g['direction'] == 'min' else 'at most'} "
                     f"{_fmt(g['metric'], g['target'])}) on {g['met']} of the last {g['days']} days"
                     + (f", {g['streak']} in a row." if g["streak"] >= 3 else ".")),
            "data": {k: g[k] for k in ("metric", "target", "direction", "met", "days", "streak")},
            "link": f"#/trends?metric={g['metric']}",
        })
    return out


TONE_ORDER = {"attention": 0, "info": 1, "good": 2}
KIND_ORDER = {"lab": 0, "change": 1, "training": 2, "sleep": 3, "medication": 4, "link": 5, "goal": 6}


def insights(profile_id: str, limit: int = 12) -> list[dict[str, Any]]:
    """What's worth knowing right now across every source, most pressing first."""
    found: list[dict[str, Any]] = []
    for fn in (_lab_notes, _changes, _training_notes, _sleep_notes, _medication_notes, _feeling_links, _symptom_links,
               _goal_notes):
        try:
            found += fn(profile_id)
        except Exception:  # noqa: BLE001 - one broken source never hides the rest
            continue
    found.sort(key=lambda i: (TONE_ORDER[i["tone"]], KIND_ORDER.get(i["kind"], 9)))
    return found[:limit]
