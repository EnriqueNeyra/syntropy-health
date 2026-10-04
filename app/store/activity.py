"""Workouts and health events (symptoms, cycle tracking, notifications, mindful sessions, ECG, mood)."""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from app.core.db import db, read, dumps, row_to_dict, rows_to_dicts
from app.store.biometrics import _tz as local_tz, id_held_elsewhere, parse_ts, source_kind, to_utc_iso

WORKOUT_FIELDS = (
    "activity_type", "name", "duration_s", "active_energy_kcal", "total_energy_kcal", "distance_m", "step_count",
    "avg_hr", "max_hr", "min_hr", "elevation_ascent_m", "elevation_descent_m", "indoor", "temperature_c",
    "humidity_pct", "source_name", "device_name",
)

EVENT_CATEGORIES = {
    "symptoms": "Symptoms", "cycle": "Cycle tracking", "events": "Health notifications", "mindfulness": "Mindfulness",
    "ecg": "ECG", "stateOfMind": "State of mind", "activity": "Activity", "sleep": "Sleep", "medication": "Medication",
    "notes": "Notes", "other": "Other",
}


def stable_id(*parts: Any) -> str:
    """Deterministic id for items that arrive without one (e.g. HealthKit export JSON)."""
    return "hx_" + hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:24]


def _since(days: Optional[int]) -> Optional[str]:
    if not days:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window(days: Optional[int], start: Optional[str], end: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """The UTC bounds for a list: an explicit start/end (dates or times) wins over "the last N days". Plain dates are
    whole days in the instance's time zone."""
    def bound(value: str, clock: str) -> str:
        if len(value) == 10:
            return datetime.fromisoformat(f"{value}T{clock}").replace(tzinfo=local_tz()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return to_utc_iso(value)
    return (bound(start, "00:00:00") if start else _since(days)), (bound(end, "23:59:59") if end else None)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def humidity_pct(value: Any) -> Optional[float]:
    """Workout humidity as 0–100. Apple's Workout app stores 65% as "6500 %", which iPhone app versions before the fix
    sent as 6500."""
    if value is None:
        return None
    return value / 100 if value > 100 else value


def insert_workouts(profile_id: str, connection_id: Optional[str], workouts: Iterable[dict[str, Any]]) -> int:
    """Stores workouts; idempotent on id and on (start, end, source) so re-sent workouts are kept once."""
    now = time.time()
    inserted = 0
    with db() as conn:
        for w in workouts:
            try:
                start, end = to_utc_iso(w["start"]), to_utc_iso(w.get("end") or w["start"])
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            source = w.get("source_name") or "Unknown"
            indoor = w.get("indoor")
            values = {k: w.get(k) for k in WORKOUT_FIELDS}
            values["indoor"] = None if indoor is None else int(bool(indoor))
            values["humidity_pct"] = humidity_pct(values["humidity_pct"])
            values["source_name"] = source
            values["name"] = (w.get("name") or "Workout")[:120]
            insert = lambda workout_id: conn.execute(
                f"""INSERT OR IGNORE INTO workouts(id, profile_id, connection_id, start_date, end_date,
                        {", ".join(WORKOUT_FIELDS)}, metadata_json, heart_rate_json, route_json, created_at)
                    SELECT ?, ?, ?, ?, ?, {", ".join("?" * len(WORKOUT_FIELDS))}, ?, ?, ?, ?
                    WHERE NOT EXISTS (SELECT 1 FROM workouts WHERE profile_id = ? AND start_date = ? AND end_date = ?
                                      AND source_name = ?)""",
                (workout_id, profile_id, connection_id, start, end, *[values[k] for k in WORKOUT_FIELDS],
                 dumps(w.get("metadata") or None), dumps(w.get("heart_rate") or None), dumps(w.get("route") or None), now,
                 profile_id, start, end, source),
            )
            cur = insert(w["id"])
            if not cur.rowcount and (own := id_held_elsewhere(conn, "workouts", w["id"], profile_id)):
                cur = insert(own)
            inserted += cur.rowcount
    return inserted


def insert_events(profile_id: str, connection_id: Optional[str], events: Iterable[dict[str, Any]]) -> int:
    now = time.time()
    inserted = 0
    with db() as conn:
        for e in events:
            try:
                start, end = to_utc_iso(e["start"]), to_utc_iso(e.get("end") or e["start"])
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            source = e.get("source_name") or "Unknown"
            insert = lambda event_id: conn.execute(
                """INSERT OR IGNORE INTO health_events(id, profile_id, connection_id, event_type, category, name,
                       start_date, end_date, value, value_label, source_name, metadata_json, created_at)
                   SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                   WHERE NOT EXISTS (SELECT 1 FROM health_events WHERE profile_id = ? AND event_type = ?
                                     AND start_date = ? AND end_date = ? AND source_name = ?)""",
                (event_id, profile_id, connection_id, e["event_type"][:80], (e.get("category") or "other")[:40],
                 (e.get("name") or e["event_type"])[:120], start, end, e.get("value"), e.get("value_label"), source,
                 dumps(e.get("metadata") or None), now,
                 profile_id, e["event_type"][:80], start, end, source),
            )
            cur = insert(e["id"])
            if not cur.rowcount and (own := id_held_elsewhere(conn, "health_events", e["id"], profile_id)):
                cur = insert(own)
            inserted += cur.rowcount
    return inserted



# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def list_workouts(profile_id: str, days: Optional[int] = 90, limit: int = 200, dedupe: bool = True,
                  start: Optional[str] = None, end: Optional[str] = None, name: Optional[str] = None) -> list[dict[str, Any]]:
    q = ["SELECT id, activity_type, name, start_date, end_date, duration_s, active_energy_kcal, total_energy_kcal,",
         "distance_m, step_count, avg_hr, max_hr, min_hr, elevation_ascent_m, indoor, source_name,",
         "route_json IS NOT NULL AS has_route, heart_rate_json IS NOT NULL AS has_heart_rate",
         "FROM workouts WHERE profile_id = ?"]
    params: list[Any] = [profile_id]
    lo, hi = _window(days, start, end)
    if lo:
        q.append("AND start_date >= ?")
        params.append(lo)
    if hi:
        q.append("AND start_date <= ?")
        params.append(hi)
    if name:
        q.append("AND name = ?")
        params.append(name)
    q.append("ORDER BY start_date DESC LIMIT ?")
    params.append(limit * 2 if dedupe else limit)   # headroom for duplicates folded away below
    with read() as conn:
        rows = rows_to_dicts(conn.execute(" ".join(q), params).fetchall())
    for r in rows:
        r["has_route"] = bool(r["has_route"])
        r["has_heart_rate"] = bool(r["has_heart_rate"])
        r["indoor"] = None if r["indoor"] is None else bool(r["indoor"])
    return (dedupe_workouts(rows) if dedupe else rows)[:limit]


# A Watch run that WHOOP also auto-detected is one session: fold workouts from different sources that overlap by at
# least this share of the shorter one.
WORKOUT_OVERLAP_MIN = 0.5
_KIND_ORDER = {"watch": 0, "ring": 1, "phone": 2, "other": 3}


def _richness(w: dict[str, Any]) -> tuple:
    simulated = "(simulated)" in (w.get("source_name") or "").lower()
    return (simulated, not w.get("has_route"), not w.get("has_heart_rate"), w.get("avg_hr") is None,
            _KIND_ORDER[source_kind(w.get("source_name"))], -(w.get("duration_s") or 0))


def dedupe_workouts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keeps the richest record of each session (route, heart rate, preferred device) and lists the others under
    ``also_recorded_by``. Input and output are newest first."""
    spans = []
    for w in rows:
        try:
            s, e = parse_ts(w["start_date"]).timestamp(), parse_ts(w["end_date"]).timestamp()
        except (ValueError, TypeError):
            s = e = None
        spans.append((s, e))
    groups: list[list[int]] = []
    for i, (s, e) in enumerate(spans):
        match = None
        if s is not None:
            for g in groups[-20:]:   # newest first, so a duplicate is always among the most recent groups
                j = g[0]
                gs, ge = spans[j]
                if gs is None or rows[j]["source_name"] == rows[i]["source_name"]:
                    continue
                shorter = max(1.0, min(e - s, ge - gs))
                if (min(e, ge) - max(s, gs)) / shorter >= WORKOUT_OVERLAP_MIN:
                    match = g
                    break
        if match is None:
            groups.append([i])
        else:
            match.append(i)
    out = []
    for g in groups:
        members = sorted((rows[i] for i in g), key=_richness)
        primary = dict(members[0])
        primary["also_recorded_by"] = [{"id": m["id"], "source_name": m["source_name"], "name": m["name"]} for m in members[1:]]
        out.append(primary)
    return out


def get_workout(profile_id: str, workout_id: str) -> Optional[dict[str, Any]]:
    with read() as conn:
        row = conn.execute("SELECT * FROM workouts WHERE profile_id = ? AND id = ?", (profile_id, workout_id)).fetchone()
    w = row_to_dict(row, ["metadata_json", "heart_rate_json", "route_json"])
    if w:
        w["indoor"] = None if w["indoor"] is None else bool(w["indoor"])
        w["route"] = w.get("route") or []
        w["heart_rate"] = w.get("heart_rate") or []
    return w


def workout_summary(profile_id: str, days: Optional[int] = 30, start: Optional[str] = None, end: Optional[str] = None,
                    name: Optional[str] = None) -> dict[str, Any]:
    by_name: dict[str, dict[str, Any]] = {}
    # One entry per session, so totals aren't doubled.
    for w in list_workouts(profile_id, days, limit=100_000, start=start, end=end, name=name):
        t = by_name.setdefault(w["name"], {"name": w["name"], "count": 0, "duration_s": 0.0, "energy_kcal": 0.0, "distance_m": 0.0})
        t["count"] += 1
        t["duration_s"] += w["duration_s"] or 0
        t["energy_kcal"] += w["active_energy_kcal"] if w["active_energy_kcal"] is not None else (w["total_energy_kcal"] or 0)
        t["distance_m"] += w["distance_m"] or 0
    rows = sorted(by_name.values(), key=lambda t: -t["count"])
    return {
        "days": days,
        "count": sum(r["count"] for r in rows),
        "duration_s": sum(r["duration_s"] or 0 for r in rows),
        "energy_kcal": sum(r["energy_kcal"] or 0 for r in rows),
        "distance_m": sum(r["distance_m"] or 0 for r in rows),
        "by_type": rows,
    }


# Hourly bookkeeping that would bury symptoms, ECGs and alerts in an unfiltered list.
ROUTINE_EVENT_TYPES = ("apple_stand_hour",)

# Two views of the same events. The Journal is how the person feels: check-ins and mood, symptoms, notes, doses and
# cycle tracking, whether logged here or in Apple Health. Signals are what devices measured or warned about (ECGs,
# heart, hearing and steadiness notifications), shown with the other measurements in Trends.
JOURNAL_CATEGORIES = ("stateOfMind", "symptoms", "notes", "medication", "cycle", "other")
NOT_JOURNAL_TYPES = ("apple_stand_hour", "handwashing_event", "toothbrushing_event")
SIGNAL_CATEGORIES = ("ecg", "events")


def _view_filter(view: Optional[str]) -> tuple[str, list[Any]]:
    if view == "journal":
        return (f"AND category IN ({','.join('?' * len(JOURNAL_CATEGORIES))}) "
                f"AND event_type NOT IN ({','.join('?' * len(NOT_JOURNAL_TYPES))})", [*JOURNAL_CATEGORIES, *NOT_JOURNAL_TYPES])
    if view == "signals":
        return f"AND category IN ({','.join('?' * len(SIGNAL_CATEGORIES))})", list(SIGNAL_CATEGORIES)
    return "", []


def list_events(profile_id: str, days: Optional[int] = 90, event_type: Optional[str] = None,
                category: Optional[str] = None, limit: int = 500, hide_routine: bool = False,
                start: Optional[str] = None, end: Optional[str] = None, search: Optional[str] = None,
                manual: Optional[bool] = None, view: Optional[str] = None) -> list[dict[str, Any]]:
    q = ["SELECT id, event_type, category, name, start_date, end_date, value, value_label, source_name, metadata_json,",
         "note, manual FROM health_events WHERE profile_id = ?"]
    params: list[Any] = [profile_id]
    lo, hi = _window(days, start, end)
    if lo:
        q.append("AND start_date >= ?")
        params.append(lo)
    if hi:
        q.append("AND start_date <= ?")
        params.append(hi)
    if manual is not None:
        q.append("AND manual = ?")
        params.append(int(manual))
    if search:
        q.append("AND (name LIKE ? OR IFNULL(value_label, '') LIKE ? OR IFNULL(note, '') LIKE ?)")
        params.extend([f"%{search}%"] * 3)
    if event_type:
        q.append("AND event_type = ?")
        params.append(event_type)
    if category:
        q.append("AND category = ?")
        params.append(category)
    clause, extra = _view_filter(view)
    if clause:
        q.append(clause)
        params.extend(extra)
    if hide_routine and not category and not event_type:
        q.append(f"AND event_type NOT IN ({','.join('?' * len(ROUTINE_EVENT_TYPES))})")
        params.extend(ROUTINE_EVENT_TYPES)
    q.append("ORDER BY start_date DESC LIMIT ?")
    params.append(limit)
    with read() as conn:
        rows = rows_to_dicts(conn.execute(" ".join(q), params).fetchall(), ["metadata_json"])
    for r in rows:
        r["manual"] = bool(r["manual"])
    return rows


def event_summary(profile_id: str, days: Optional[int] = 90, start: Optional[str] = None,
                  end: Optional[str] = None, view: Optional[str] = None) -> list[dict[str, Any]]:
    lo, hi = _window(days, start, end)
    clause, extra = _view_filter(view)
    with read() as conn:
        return rows_to_dicts(conn.execute(
            f"""SELECT event_type, category, name, COUNT(*) AS count, MAX(start_date) AS latest
               FROM health_events WHERE profile_id = ? AND start_date >= ? AND start_date <= ? {clause}
               GROUP BY event_type, category, name ORDER BY count DESC""",
            (profile_id, lo or "", hi or "9999", *extra)).fetchall())


# ---------------------------------------------------------------------------
# Journal entries typed in by the person
# ---------------------------------------------------------------------------

JOURNAL_SOURCE = "Journal"
_ENTRY_FIELDS = ("event_type", "category", "name", "value", "value_label", "note")
# An entry may also carry ``details`` (a check-in's energy and stress, 1-5), kept as its metadata.


def create_entry(profile_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    start = to_utc_iso(entry["start"])
    end = to_utc_iso(entry["end"]) if entry.get("end") else start
    entry_id = "je_" + hashlib.sha256(f"{profile_id}|{time.time_ns()}|{entry['name']}".encode()).hexdigest()[:24]
    with db() as conn:
        conn.execute(
            """INSERT INTO health_events(id, profile_id, connection_id, event_type, category, name, start_date, end_date,
                   value, value_label, source_name, metadata_json, note, manual, created_at)
               VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (entry_id, profile_id, entry["event_type"], entry["category"], entry["name"], start, end, entry.get("value"),
             entry.get("value_label"), JOURNAL_SOURCE, dumps(entry["details"]) if entry.get("details") else None,
             entry.get("note"), time.time()))
    return get_event(profile_id, entry_id)  # type: ignore[return-value]


def get_event(profile_id: str, event_id: str) -> Optional[dict[str, Any]]:
    with read() as conn:
        row = conn.execute("""SELECT id, event_type, category, name, start_date, end_date, value, value_label, source_name,
                                     metadata_json, note, manual FROM health_events WHERE profile_id = ? AND id = ?""",
                           (profile_id, event_id)).fetchone()
    e = row_to_dict(row, ["metadata_json"])
    if e:
        e["manual"] = bool(e["manual"])
    return e


def update_entry(profile_id: str, event_id: str, entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Changes an entry typed in here; entries from devices are left as they arrived. None if there's no such entry."""
    sets, params = [], []
    for k in _ENTRY_FIELDS:
        if k in entry:
            sets.append(f"{k} = ?")
            params.append(entry[k])
    if "details" in entry:
        sets.append("metadata_json = ?")
        params.append(dumps(entry["details"]) if entry["details"] else None)
    if entry.get("start"):
        start = to_utc_iso(entry["start"])
        sets += ["start_date = ?", "end_date = ?"]
        params += [start, to_utc_iso(entry["end"]) if entry.get("end") else start]
    with db() as conn:
        if sets:
            cur = conn.execute(f"UPDATE health_events SET {', '.join(sets)} WHERE profile_id = ? AND id = ? AND manual = 1",
                               (*params, profile_id, event_id))
            if not cur.rowcount:
                return None
    e = get_event(profile_id, event_id)
    return e if e and e["manual"] else None


def delete_entry(profile_id: str, event_id: str) -> bool:
    with db() as conn:
        return conn.execute("DELETE FROM health_events WHERE profile_id = ? AND id = ? AND manual = 1",
                            (profile_id, event_id)).rowcount > 0


def counts(profile_id: str) -> dict[str, int]:
    with read() as conn:
        w = conn.execute("SELECT COUNT(*) FROM workouts WHERE profile_id = ?", (profile_id,)).fetchone()[0]
        e = conn.execute("SELECT COUNT(*) FROM health_events WHERE profile_id = ?", (profile_id,)).fetchone()[0]
    return {"workouts": w, "events": e}
