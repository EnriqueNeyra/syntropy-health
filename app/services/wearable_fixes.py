"""
Corrections to wearable data already stored, once per database, in the background (see app/core/data_fixes.py). They run in
this order, and the daily numbers are worked out again last, from what the others left.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from app.core import data_fixes
from app.services.sync import WEARABLE_INITIAL_DAYS
from app.store import biometrics, sample_counts
from app.store.biometrics import FLAG_UNIT, SLEEP_MAP, _stored_day, device_kind, reading_flag
from app.store.metric_registry import METRICS, to_canonical


def _fix_samples(conn: sqlite3.Connection, cursor: Any, size: int) -> Any:
    """Readings stored under the wrong unit are converted, ones that can't be true are flagged, and each source's kind of device
    is noted from what its rows say. A unit the paths didn't convert stayed on the row as sent, so that's how to find them."""
    rows = conn.execute(
        "SELECT rowid, id, profile_id, metric_type, value, unit, start_date, end_date, source_name, device_name, metadata_json, flag "
        "FROM biometric_samples WHERE rowid > ? ORDER BY rowid LIMIT ?", (cursor or 0, size)).fetchall()
    seen: dict[str, dict[str, tuple[str, Optional[str], bool]]] = {}
    now = datetime.now(timezone.utc)
    for r in rows:
        metric, start, end = r["metric_type"], r["start_date"], r["end_date"]
        value, unit, understood = to_canonical(metric, r["value"], r["unit"])
        flag = r["flag"] or (FLAG_UNIT if not understood and r["id"][:3] in ("ah-", "hx_") else None)
        flag = flag or reading_flag(metric, value, start, end, _stored_day(r["metadata_json"], start), now)
        if value != r["value"] or flag != r["flag"]:
            conn.execute("UPDATE biometric_samples SET value = ?, unit = ?, flag = ? WHERE rowid = ?", (value, unit, flag, r["rowid"]))
        if r["profile_id"] and r["source_name"]:
            try:
                meta = json.loads(r["metadata_json"]) if r["metadata_json"] and "device_type" in r["metadata_json"] else {}
            except ValueError:
                meta = {}
            seen.setdefault(r["profile_id"], {})[r["source_name"]] = (
                device_kind(r["source_name"], r["device_name"], meta), r["device_name"],
                bool(meta.get("device_type") or biometrics._kind_from_words(r["device_name"])))
    for profile_id, sources in seen.items():
        biometrics._note_sources(conn, profile_id, sources, time.time())
    return rows[-1]["rowid"] if rows else None


def _fix_json_repeats(conn: sqlite3.Connection, cursor: Any, size: int) -> Any:
    """Before re-sent totals replaced each other, each new value of a growing total was a row of its own and they were added
    up. Only the two shapes that can't be two real readings are collapsed: a night's aggregated sleep, and a summed metric's row at
    local midnight. Only that export route writes hx_ ids, whatever the connection it came in on says about itself. Other rows at
    one moment can be real entries (two foods logged together)."""
    rows = conn.execute(
        "SELECT rowid, id, profile_id, metric_type, start_date, source_name, metadata_json FROM biometric_samples "
        "WHERE id > ? AND id < 'hx`' ORDER BY id LIMIT ?", (cursor or "hx_", size)).fetchall()
    for r in rows:
        metric, source, start = r["metric_type"], r["source_name"], r["start_date"]
        day = _stored_day(r["metadata_json"], start)
        if metric in SLEEP_MAP and day:
            lo = (date.fromisoformat(day) - timedelta(days=4)).isoformat()
            hi = (date.fromisoformat(day) + timedelta(days=5)).isoformat()
            same = [s for s in conn.execute(
                "SELECT rowid, value, created_at, metadata_json, start_date FROM biometric_samples WHERE profile_id = ? AND metric_type = ? "
                "AND start_date >= ? AND start_date < ? AND source_name = ? AND substr(id, 1, 3) = 'hx_'",
                (r["profile_id"], metric, lo, hi, source)).fetchall() if _stored_day(s["metadata_json"], s["start_date"]) == day]
        elif METRICS.get(metric, ("", "", ""))[2] == "sum":
            tz = biometrics._tz(r["profile_id"])
            at = biometrics.parse_ts(start).astimezone(tz)
            if at.hour or at.minute:
                continue
            same = conn.execute(
                "SELECT rowid, value, created_at FROM biometric_samples WHERE profile_id = ? AND metric_type = ? AND start_date = ? "
                "AND source_name = ? AND substr(id, 1, 3) = 'hx_'", (r["profile_id"], metric, start, source)).fetchall()
        else:
            continue
        if len({s["value"] for s in same}) > 1:
            newest = max(s["created_at"] for s in same)     # a whole send shares one created_at
            for s in same:
                if s["created_at"] < newest:
                    conn.execute("DELETE FROM biometric_samples WHERE rowid = ?", (s["rowid"],))
    if not rows:
        sample_counts.forget()
        return None
    return rows[-1]["id"]


def _fix_whoop_dates(conn: sqlite3.Connection, cursor: Any, size: int) -> Any:
    """WHOOP recovery and strain were dated by the start of the cycle, the evening before the day they're about. A recovery's cycle
    starts when its sleep does, so the sleep stored beside it says when that morning was. Without one a closed cycle goes to its
    middle, and fetching it again (below) puts right what's left."""
    rows = conn.execute(
        "SELECT rowid, id, profile_id, start_date, end_date, source_name FROM biometric_samples "
        "WHERE id > ? AND id < 'whoop-s' ORDER BY id LIMIT ?", (cursor or "whoop-c", size)).fetchall()
    for r in rows:
        if not r["id"].startswith(("whoop-rec-", "whoop-cyc-")):
            continue
        night = conn.execute(
            "SELECT end_date, tz_offset_min FROM biometric_samples WHERE profile_id = ? AND metric_type = 'sleep_total_duration' "
            "AND start_date = ? AND source_name = ?", (r["profile_id"], r["start_date"], r["source_name"])).fetchone()
        if night:
            at = night["end_date"]
        elif r["end_date"] > r["start_date"]:
            s, e = biometrics.parse_ts(r["start_date"]), biometrics.parse_ts(r["end_date"])
            at = (s + (e - s) / 2).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            continue
        conn.execute("UPDATE biometric_samples SET start_date = ?, end_date = ? WHERE rowid = ?", (at, at, r["rowid"]))
    return rows[-1]["id"] if rows else None


def _fix_fetch_again(conn: sqlite3.Connection, cursor: Any, size: int) -> Any:
    """Corrections used to be thrown away, so every day ever synced from a cloud wearable is frozen at its first value. Each live
    connection fetches its whole stored span again, a stretch per sync (see sync._sync_wearable). What a provider no longer holds
    stays as it was."""
    rows = conn.execute("SELECT id, metadata_json FROM connections WHERE kind = 'wearable' AND mode = 'live' AND id > ? ORDER BY id LIMIT 20",
                        (cursor or "",)).fetchall()
    for r in rows:
        try:
            meta = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
        except ValueError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        first = conn.execute("SELECT MIN(start_date) FROM biometric_samples WHERE connection_id = ?", (r["id"],)).fetchone()[0]
        floor = (datetime.now(timezone.utc).date() - timedelta(days=WEARABLE_INITIAL_DAYS)).isoformat()
        start = min(first[:10], floor) if first else floor
        meta["refetch_from"] = min(start, meta["refetch_from"]) if isinstance(meta.get("refetch_from"), str) else start
        conn.execute("UPDATE connections SET metadata_json = ? WHERE id = ?", (json.dumps(meta), r["id"]))
    return rows[-1]["id"] if rows else None


def _fix_rollups(conn: sqlite3.Connection, cursor: Any, size: int) -> Any:
    """Everyone's daily numbers, under the rules of this DAILY_VERSION, a month at a time."""
    if cursor is None:
        biometrics.queue_all_rebuilds(conn)
        return 1
    return biometrics.rebuild_step(conn, cursor, size)


data_fixes.register("wearables-samples-units-flags-sources", _fix_samples)
data_fixes.register("wearables-json-repeated-totals", _fix_json_repeats)
data_fixes.register("wearables-whoop-recovery-dates", _fix_whoop_dates)
data_fixes.register("wearables-fetch-history-again", _fix_fetch_again)
data_fixes.register(f"wearables-daily-rollups-v{biometrics.DAILY_VERSION}", _fix_rollups)
