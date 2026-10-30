"""
Continuous biometrics: sample ingest, per-day rollups and trend queries.

Raw samples keep what the source said. Sending the same id again with a changed value corrects the stored reading (a
day's steps grow, a score is revised), and samples and the daily rows they feed (``biometric_daily``, one row per day /
metric / source) commit together, so a crash can't leave wrong numbers. A reading outside what's possible stays stored
with a flag, out of the daily numbers, and counted (``left_out``). Reads pick one source per day using a priority order
so totals such as steps are never double-counted when an Apple Watch, an iPhone and a ring all report them.

A reading belongs to the day where the person was: its own offset from UTC when the source gives one, otherwise that
person's home time zone (``profiles.timezone``, or the server's).

For summed metrics recorded as time intervals by several sources (steps from an Apple Watch and an iPhone), an extra
"Combined" row merges them the way Apple Health does: the preferred source counts in full and each other sample only for
the share of its interval the preferred sources did not cover. Steps taken with only the phone are kept.
"""

from __future__ import annotations

import bisect
import copy
import json
import math
import re
import threading
import time
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

from app.core import settings
from app.core.db import db, read, dumps, new_id, rows_to_dicts, write_in_batches
from app.store import sample_counts
from app.store.metric_registry import (METRICS, TIME_WEIGHT_MAX_S, TIME_WEIGHTED, in_plausible_range, to_canonical)

# Raw sample metric -> daily metric (sleep stages from HealthKit / rings).
SLEEP_MAP = {
    "sleep_analysis_deep": ["sleep_duration", "sleep_deep"],
    "sleep_analysis_rem": ["sleep_duration", "sleep_rem"],
    "sleep_analysis_core": ["sleep_duration", "sleep_core"],
    "sleep_analysis_asleep": ["sleep_duration"],
    "sleep_analysis_unspecified": ["sleep_duration"],
    "sleep_analysis_awake": ["sleep_awake"],
    "sleep_total_duration": ["sleep_duration"],
    "sleep_deep": ["sleep_deep"],
    "sleep_rem": ["sleep_rem"],
    "sleep_light": ["sleep_core"],
    "sleep_awake": ["sleep_awake"],
    "sleep_nap": ["sleep_nap"],
}

# Sleep stage samples (HealthKit style: one row per stretch of a stage, value = its length in seconds). A night is the
# run of a source's stage samples with no gap longer than this, and it counts toward the day it ends (the morning you
# wake up), as Apple Health shows it. Summaries from rings and straps already say which day they belong to.
SLEEP_STAGE_SAMPLES = {"sleep_analysis_deep", "sleep_analysis_rem", "sleep_analysis_core", "sleep_analysis_asleep",
                       "sleep_analysis_unspecified", "sleep_analysis_awake"}
SLEEP_SESSION_GAP_S = 3 * 3600
# Stage samples can move between the night and a nap, so a change to any of them can change all of these.
_STAGE_TARGETS = ["sleep_duration", "sleep_deep", "sleep_rem", "sleep_core", "sleep_awake", "sleep_nap"]
# A shorter session that isn't the day's longest and starts between these local hours is a nap, apart from the night.
# Oura's own definition of a nap is up to 3 hours. Waking for a few hours overnight doesn't make the rest of the night one.
NAP_MAX_S = 3 * 3600
NAP_HOURS = (6, 20)
# Measured over a whole night and shown on the morning it ends, like sleep.
OVERNIGHT_METRICS = {"sleeping_wrist_temperature", "sleeping_breathing_disturbances"}

FRACTION_METRICS = {"oxygen_saturation", "body_fat_percentage", "walking_asymmetry", "walking_double_support",
                    "walking_steadiness", "atrial_fibrillation_burden", "peripheral_perfusion_index",
                    "blood_alcohol_content"}

HEADLINE_METRICS = [
    "sleep_duration", "hrv_rmssd", "hrv_sdnn", "resting_heart_rate", "recovery_score", "readiness_score",
    "step_count", "strain_score", "sleep_score", "vo2_max", "active_energy", "oxygen_saturation",
    "respiratory_rate", "body_mass",
]

# Why a reading is left out of the daily numbers.
FLAG_RANGE, FLAG_UNIT, FLAG_DATE = "range", "unit", "date"
# Earlier than anything a person could have recorded; Date.distantPast (year 1) is how a phone says "no date".
EARLIEST_YEAR = 1950
_ISO = "%Y-%m-%dT%H:%M:%SZ"
OFFSET_MAX_MIN = 18 * 60
# An interval that crosses midnight is shared between the days it covers, up to this long.
SPLIT_MAX_S = 48 * 3600
# A day's label is believed only this close to the reading, and a rebuild of a day reads this far around it: up to 26
# hours between a reading's own zone and the person's home zone, plus a night that ends the next morning.
DAY_LABEL_MAX_DAYS = 3
REBUILD_MARGIN_BEFORE, REBUILD_MARGIN_AFTER = 2, 3


def _profile_zone(profile_id: str) -> Optional[str]:
    with read() as conn:
        row = conn.execute("SELECT timezone FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    return row[0] if row else None


def _tz(profile_id: Optional[str] = None) -> ZoneInfo:
    """A person's home time zone, or without one (or without a person) the server's."""
    try:
        return ZoneInfo((profile_id and _profile_zone(profile_id)) or settings.timezone_name())
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def zone_at(offset: Optional[int], tz: tzinfo) -> tzinfo:
    """Where a reading was taken: its own offset from UTC if it has one, otherwise the person's zone."""
    return tz if offset is None else timezone(timedelta(minutes=offset))


def parse_ts(value: str) -> datetime:
    v = value.strip().replace("Z", "+00:00")
    if len(v) == 10:
        v += "T12:00:00+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        # HealthKit export style: "2024-05-15 07:30:00 -0700"
        dt = datetime.strptime(v, "%Y-%m-%d %H:%M:%S %z")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_utc_iso(value: str) -> str:
    return parse_ts(value).astimezone(timezone.utc).strftime(_ISO)


def local_day(value: str, tz: Optional[tzinfo] = None, offset: Optional[int] = None) -> str:
    return parse_ts(value).astimezone(zone_at(offset, tz or _tz())).date().isoformat()


def offset_of(value: Any) -> Optional[int]:
    """Minutes from UTC written in a timestamp. A bare UTC stamp says nothing about where the person was, so it's None."""
    if not isinstance(value, str):
        return None
    try:
        delta = parse_ts(value).utcoffset()
    except (ValueError, OverflowError):
        return None
    minutes = int(delta.total_seconds() // 60) if delta else 0
    return minutes if minutes and abs(minutes) <= OFFSET_MAX_MIN else None


MERGED_SOURCE = "Combined"


def source_priority(source_name: Optional[str]) -> int:
    if source_name == MERGED_SOURCE:
        return 0
    if "(simulated)" in (source_name or "").lower():
        return 9   # demo data never outranks real measurements
    return {"watch": 1, "ring": 2, "phone": 3}.get(source_kind(source_name), 4)


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

class SourceRemoved(Exception):
    """The source was removed while its sync was running, so what it fetched is dropped."""


def id_held_elsewhere(conn: Any, table: str, item_id: str, profile_id: str) -> Optional[str]:
    """When a row's id (HealthKit's own UUID) is already stored for someone else, the id to store it under for this
    person instead; None when it's free or already theirs. A phone first paired to the wrong person and then to the
    right one sends the same samples again, which must reach the second person rather than count as duplicates."""
    row = conn.execute(f"SELECT profile_id FROM {table} WHERE id = ?", (item_id,)).fetchone()
    return f"{item_id}@{profile_id}" if row and row[0] != profile_id else None


def meta_day(meta: Any, start: Optional[str] = None) -> Optional[str]:
    """The day a reading's metadata says it belongs to. iOS forwards other apps' metadata as it finds it, so anything
    that isn't a real date near the reading is ignored."""
    value = meta.get("day") if isinstance(meta, dict) else None
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        day = date.fromisoformat(value)
        if start and abs((day - date.fromisoformat(start[:10])).days) > DAY_LABEL_MAX_DAYS:
            return None
    except ValueError:
        return None
    return value if day.year >= EARLIEST_YEAR else None


def _stored_day(metadata_json: Optional[str], start: Optional[str] = None) -> Optional[str]:
    if not metadata_json or '"day"' not in metadata_json:
        return None
    try:
        return meta_day(json.loads(metadata_json), start)
    except ValueError:
        return None


def reading_offset(s: dict[str, Any], meta: dict[str, Any], start: str) -> Optional[int]:
    """The reading's own offset: the optional per-record field, then the zone HealthKit keeps in its metadata."""
    raw = s.get("tz_offset_min")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw) if abs(raw) <= OFFSET_MAX_MIN else None
    name = meta.get("HKTimeZone") or meta.get("TimeZone")
    if not isinstance(name, str):
        return None
    try:
        delta = parse_ts(start).astimezone(ZoneInfo(name)).utcoffset()
    except Exception:  # noqa: BLE001 - another app's text, not necessarily a zone
        return None
    return int(delta.total_seconds() // 60) if delta is not None else None


def reading_flag(metric: str, value: float, start: str, end: str, day: Optional[str] = None,
                 now: Optional[datetime] = None) -> Optional[str]:
    """Why a reading can't count toward the day's numbers. A clock a little fast isn't one: the phone never sends a
    reading twice, so only what can't be true (a day ahead, before anyone wore a device) is left out."""
    now = now or datetime.now(timezone.utc)
    latest = (now + timedelta(days=1)).strftime(_ISO)
    if end < start or start[:4] < str(EARLIEST_YEAR):
        return FLAG_DATE
    if day:
        if day > (now + timedelta(days=2)).date().isoformat():
            return FLAG_DATE
    elif max(start, end) > latest:
        return FLAG_DATE
    return None if in_plausible_range(metric, value) else FLAG_RANGE


def normalize_sample(s: dict[str, Any]) -> dict[str, Any]:
    metric = s["metric_type"]
    value = float(s["value"])
    if not math.isfinite(value):
        raise ValueError(f"{metric}: value must be a finite number")
    value, unit, understood = to_canonical(metric, value, s.get("unit"))
    if metric in FRACTION_METRICS:
        # Both phone apps send fractions labelled "%", so a bare % up to 1 stays a fraction. Another client can say which
        # it means: "fraction" or "percent".
        if unit == "percent":
            unit = "%"
        elif unit == "fraction" or 0 <= value <= 1.0:
            value, unit = round(value * 100, 3), "%" if unit == "fraction" else unit
    start, end = to_utc_iso(s["start_date"]), to_utc_iso(s.get("end_date") or s["start_date"])
    # Only what an import reads from a file is held to its units: the phones' labels aren't all listed.
    flag = s.get("flag") or (FLAG_UNIT if not understood and s.get("strict_units") else None)
    return {**s, "value": value, "unit": unit, "start_date": start, "end_date": end, "flag": flag}


# Kinds of device, whole words only: "ring" isn't in "Monitoring" and "phone" isn't in "Headphones".
_WATCH_WORDS = re.compile(r"\b(watch|garmin|forerunner|fenix|polar|suunto|coros|amazfit|samsung health)\b", re.I)
_RING_WORDS = re.compile(r"\b(oura|whoop|ring|fitbit|google health|ultrahuman)\b", re.I)
_PHONE_WORDS = re.compile(r"\b(iphone|phone|pixel \d+\w*|galaxy s\d+\w*)\b", re.I)
_DEVICE_TYPES = {"watch": "watch", "ring": "ring", "band": "ring", "phone": "phone", "scale": "other", "chest_strap": "other"}


def _kind_from_words(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    for words, kind in ((_WATCH_WORDS, "watch"), (_RING_WORDS, "ring"), (_PHONE_WORDS, "phone")):
        if words.search(text):
            return kind
    return None


def device_kind(source_name: Optional[str], device_name: Optional[str] = None, meta: Optional[dict[str, Any]] = None) -> str:
    """What a source really is: what the device says about itself first (Health Connect says outright), then the
    device's name, and only then the name of the app."""
    declared = _DEVICE_TYPES.get((meta or {}).get("device_type"))
    return declared or _kind_from_words(device_name) or _kind_from_words(source_name) or "other"


def _id_path(sample_id: str) -> str:
    return "export" if sample_id.startswith("ah-") else "json" if sample_id.startswith("hx_") else "phone"


def _close(a: float, b: float) -> bool:
    """The same reading after two paths' rounding and units (kilometres to 3 decimals against unrounded metres). Between
    paths, the same source, metric and whole-second interval is all but certainly one sample."""
    return abs(a - b) <= max(0.001, 0.01 * max(abs(a), abs(b)))


def _row_days(metric: str, start: str, end: str, day: Optional[str], offset: Optional[int], tz: tzinfo) -> set[str]:
    """The days a reading can count toward."""
    if day:
        return {day}
    out = {local_day(start, tz, offset)}
    if metric in OVERNIGHT_METRICS:
        out.add(local_day(end, tz, offset))
    if metric in SLEEP_MAP:
        # A stretch before midnight belongs to the night that ends the next morning.
        woke = date.fromisoformat(local_day(end, tz, offset))
        out.update({woke.isoformat(), (woke + timedelta(days=1)).isoformat()})
    elif METRICS.get(metric, ("", "", ""))[2] == "sum" and end > start:
        out.update(piece[0] for piece in _split_by_day(start, end, 0.0, offset, tz))
    return out


def _stored_row_days(row: Any, tz: tzinfo) -> set[str]:
    if row["flag"]:
        return set()
    try:
        return _row_days(row["metric_type"], row["start_date"], row["end_date"], _stored_day(row["metadata_json"], row["start_date"]),
                         row["tz_offset_min"], tz)
    except (ValueError, OverflowError):
        return set()


_SAMPLE_COLUMNS = ("rowid, id, profile_id, connection_id, metric_type, value, unit, start_date, end_date, metadata_json, "
                   "flag, tz_offset_min")


def _prefix_end(prefix: str) -> str:
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _remove_samples(conn: Any, profile_id: str, ids: Iterable[str], prefixes: Iterable[str], affected: set[str],
                    changed: set[str], tz: tzinfo) -> int:
    """Removes readings, noting which days to work out again. A prefix is an id's family: what Oura's one sleep period or
    Health Connect's one record became. It's a range on the primary key, so ``_`` and ``%`` in an id mean nothing, and
    the id@person form from ``id_held_elsewhere`` comes along."""
    rows: list[Any] = []
    for item in ids:
        rows += conn.execute(f"SELECT {_SAMPLE_COLUMNS} FROM biometric_samples WHERE profile_id = ? AND id IN (?, ?)",
                             (profile_id, item, f"{item}@{profile_id}")).fetchall()
    for prefix in prefixes:
        rows += conn.execute(f"SELECT {_SAMPLE_COLUMNS} FROM biometric_samples WHERE profile_id = ? AND id >= ? AND id < ?",
                             (profile_id, prefix, _prefix_end(prefix))).fetchall()
    removed = set()
    for row in rows:
        if row["rowid"] in removed:
            continue
        removed.add(row["rowid"])
        affected |= _stored_row_days(row, tz)
        changed.add(row["metric_type"])
        conn.execute("DELETE FROM biometric_samples WHERE rowid = ?", (row["rowid"],))
    return len(removed)


def _supersede(conn: Any, s: dict[str, Any], profile_id: str, source: str, day: Optional[str], tz: tzinfo,
               affected: set[str], changed: set[str]) -> int:
    """A re-sent total: the rows from earlier sends of the same slot go. Only rows from the export JSON path, whose ids
    are made here, so a reading the phone sent for the same time is never touched."""
    if day:
        lo = (date.fromisoformat(day) - timedelta(days=DAY_LABEL_MAX_DAYS + 1)).isoformat()
        hi = (date.fromisoformat(day) + timedelta(days=DAY_LABEL_MAX_DAYS + 2)).isoformat()
        rows = [r for r in conn.execute(
            f"SELECT {_SAMPLE_COLUMNS} FROM biometric_samples WHERE profile_id = ? AND metric_type = ? AND start_date >= ? "
            "AND start_date < ? AND source_name = ? AND id <> ? AND substr(id, 1, 3) = 'hx_'",
            (profile_id, s["metric_type"], lo, hi, source, s["id"])).fetchall()
            if _stored_day(r["metadata_json"], r["start_date"]) == day]
    else:
        rows = conn.execute(
            f"SELECT {_SAMPLE_COLUMNS} FROM biometric_samples WHERE profile_id = ? AND metric_type = ? AND start_date = ? "
            "AND source_name = ? AND id <> ? AND substr(id, 1, 3) = 'hx_'",
            (profile_id, s["metric_type"], s["start_date"], source, s["id"])).fetchall()
    for row in rows:
        affected |= _stored_row_days(row, tz)
        conn.execute("DELETE FROM biometric_samples WHERE rowid = ?", (row["rowid"],))
    if rows:
        changed.add(s["metric_type"])
    return len(rows)


def _drop_other_day_rows(conn: Any, s: dict[str, Any], profile_id: str, connection_id: str, day: str, tz: tzinfo,
                         affected: set[str], changed: set[str]) -> None:
    """A provider's one figure for a day arriving under a new id replaces the old one rather than adding to it. Whether
    Oura's ids stay the same within a day can't be checked from here; this holds either way."""
    lo = (date.fromisoformat(day) - timedelta(days=DAY_LABEL_MAX_DAYS + 1)).isoformat()
    hi = (date.fromisoformat(day) + timedelta(days=DAY_LABEL_MAX_DAYS + 2)).isoformat()
    for row in conn.execute(
            f"SELECT {_SAMPLE_COLUMNS} FROM biometric_samples WHERE profile_id = ? AND metric_type = ? AND start_date >= ? "
            "AND start_date < ? AND connection_id = ? AND id <> ?",
            (profile_id, s["metric_type"], lo, hi, connection_id, s["id"])).fetchall():
        if _stored_day(row["metadata_json"], row["start_date"]) == day:
            affected |= _stored_row_days(row, tz)
            changed.add(s["metric_type"])
            conn.execute("DELETE FROM biometric_samples WHERE rowid = ?", (row["rowid"],))


def _same_reading(conn: Any, s: dict[str, Any], profile_id: str, source: str) -> Optional[Any]:
    """The stored row this sample repeats under another id, if any: the same one from another path (the phone and an
    export.xml import) is stored once, while two different entries one app logged at the same moment both count."""
    path = _id_path(s["id"])
    for row in conn.execute(
            "SELECT rowid, id, value, connection_id FROM biometric_samples WHERE profile_id = ? AND metric_type = ? "
            "AND start_date = ? AND end_date = ? AND source_name = ?",
            (profile_id, s["metric_type"], s["start_date"], s["end_date"], source)).fetchall():
        if row["value"] == s["value"] or (_id_path(row["id"]) != path and _close(row["value"], s["value"])):
            return row
    return None


def insert_samples(
    profile_id: str,
    connection_id: Optional[str],
    samples: Iterable[dict[str, Any]],
    *,
    batch_meta: Optional[dict[str, Any]] = None,
    deleted_ids: Iterable[str] = (),
    deleted_prefixes: Iterable[str] = (),
    must_have_source: bool = False,
) -> dict[str, Any]:
    """Stores readings and, in the same transaction, the daily rows they change. A new id is inserted, a known id with a
    changed value corrects the stored row, and anything else counts under ``duplicates``. ``deleted_*`` go first, so a
    replacement isn't taken for a repeat of what it replaces. A row that can't be read is counted under ``rejected``
    rather than failing the batch: senders only move on after a success, so a rejected batch would be retried (and
    rejected) forever."""
    now = time.time()
    received = inserted = updated = rejected = flagged = removed = 0
    moved: list[tuple[Optional[str], int]] = []
    tz = _tz(profile_id)
    orders = source_orders()
    affected: set[str] = set()
    changed: set[str] = set()
    sources_seen: dict[str, tuple[str, Optional[str], bool]] = {}
    with db() as conn:
        if must_have_source and connection_id and not conn.execute("SELECT 1 FROM connections WHERE id = ?", (connection_id,)).fetchone():
            raise SourceRemoved(connection_id)
        removed_ids = {r[0] for r in conn.execute("SELECT sample_id FROM sample_tombstones WHERE profile_id = ?", (profile_id,))}
        imports = {r[0] for r in conn.execute("SELECT id FROM connections WHERE profile_id = ? AND kind = 'import'", (profile_id,))}
        removed += _remove_samples(conn, profile_id, deleted_ids, deleted_prefixes, affected, changed, tz)
        for raw in samples:
            received += 1
            try:
                s = normalize_sample(raw)
                meta = s.get("metadata") if isinstance(s.get("metadata"), dict) else {}
                metric, start, end = s["metric_type"], s["start_date"], s["end_date"]
                day = meta_day(meta, start)
                offset = reading_offset(s, meta, start)
                flag = s["flag"] or reading_flag(metric, s["value"], start, end, day)
                days = set() if flag else _row_days(metric, start, end, day, offset, tz)
                sample_id = s["id"]
            except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                rejected += 1
                continue
            if sample_id in removed_ids:
                continue
            source = s.get("source_name") or "Unknown"
            meta_json = dumps(meta) if meta else None
            row = conn.execute(f"SELECT {_SAMPLE_COLUMNS} FROM biometric_samples WHERE id = ?", (sample_id,)).fetchone()
            if row and row["profile_id"] != profile_id:
                sample_id = f"{sample_id}@{profile_id}"
                row = conn.execute(f"SELECT {_SAMPLE_COLUMNS} FROM biometric_samples WHERE id = ?", (sample_id,)).fetchone()
            s["id"] = sample_id
            replaces = bool(s.get("replaces"))
            if flag:
                flagged += 1
            if not flag:
                # A cloud connector knows what it's connected to, whatever device a reading passed through.
                sources_seen[source] = (s.get("device_kind") or device_kind(source, s.get("device_name"), meta), s.get("device_name"),
                                        bool(s.get("device_kind") or meta.get("device_type") or _kind_from_words(s.get("device_name"))))
            if row:
                same = (abs(row["value"] - s["value"]) < 1e-9 and row["start_date"] == start and row["end_date"] == end
                        and row["flag"] == flag and (offset is None or offset == row["tz_offset_min"])
                        and (meta_json == row["metadata_json"] if replaces else meta_json is None or meta_json == row["metadata_json"]))
                if same:
                    continue
                conn.execute(
                    """UPDATE biometric_samples SET value = ?, unit = ?, start_date = ?, end_date = ?, source_name = ?,
                           hk_identifier = COALESCE(?, hk_identifier), device_id = COALESCE(?, device_id),
                           device_name = COALESCE(?, device_name), metadata_json = ?, flag = ?,
                           tz_offset_min = COALESCE(?, tz_offset_min), updated_at = ? WHERE rowid = ?""",
                    (s["value"], s.get("unit") or "", start, end, source, s.get("hk_identifier"), s.get("device_id"),
                     s.get("device_name"), meta_json if replaces else meta_json or row["metadata_json"], flag, offset, now,
                     row["rowid"]))
                updated += 1
                changed.add(metric)
                affected |= days | _stored_row_days(row, tz)
                continue
            if connection_id and s.get("one_per_day") and day:
                _drop_other_day_rows(conn, s, profile_id, connection_id, day, tz, affected, changed)
            superseded = _supersede(conn, s, profile_id, source, day, tz, affected, changed) if replaces else 0
            if not superseded and (repeat := _same_reading(conn, s, profile_id, source)):
                # An export can be imported again, but a phone never re-sends: the row now belongs to the phone, so tidying
                # up the old import doesn't take it away.
                if repeat["connection_id"] in imports and connection_id and connection_id not in imports:
                    conn.execute("UPDATE biometric_samples SET connection_id = ? WHERE rowid = ?", (connection_id, repeat["rowid"]))
                    moved.append((repeat["connection_id"], 1))
                continue
            conn.execute(
                """INSERT INTO biometric_samples(id, profile_id, connection_id, metric_type, hk_identifier, value, unit,
                       start_date, end_date, device_id, device_name, source_name, metadata_json, created_at, flag, tz_offset_min)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sample_id, profile_id, connection_id, metric, s.get("hk_identifier"), s["value"], s.get("unit") or "", start, end,
                 s.get("device_id"), s.get("device_name"), source, meta_json, now, flag, offset))
            if superseded:
                updated += 1
                removed += superseded - 1
            else:
                inserted += 1
            if not flag:
                changed.add(metric)
                affected |= days
        batch_id = None
        if batch_meta is not None:
            batch_id = batch_meta.get("batch_id") or new_id("batch")
            conn.execute(
                """INSERT OR IGNORE INTO sync_batches(batch_id, profile_id, device_id, device_name, os_version,
                       app_version, sync_trigger, sample_count, inserted, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (batch_id, profile_id, batch_meta.get("device_id") or "unknown", batch_meta.get("device_name") or "Unknown",
                 batch_meta.get("os_version"), batch_meta.get("app_version"), batch_meta.get("sync_trigger") or "manual",
                 received, inserted, now),
            )
        _note_sources(conn, profile_id, sources_seen, now)
        if affected:
            _rollup(conn, profile_id, sorted(affected), changed, tz, orders)
    sample_counts.added(profile_id, connection_id, inserted - removed)
    for was, n in moved:
        sample_counts.moved(profile_id, was, connection_id, n)
    total = sample_counts.for_profile(profile_id)
    return {
        "success": True, "batch_id": batch_id, "received": received, "inserted": inserted, "updated": updated,
        "unchanged": received - inserted - updated - rejected, "duplicates": received - inserted - updated - rejected,
        "rejected": rejected, "flagged": flagged, "removed": removed, "server_total_samples": total, "days_updated": len(affected),
    }


def _note_sources(conn: Any, profile_id: str, seen: dict[str, tuple[str, Optional[str], bool]], now: float) -> None:
    """Keeps what each source is. A batch whose readings say which device took them overrides a guess from a name."""
    for source, (kind, device, said_so) in seen.items():
        conn.execute(
            """INSERT INTO data_sources(profile_id, source_name, device_kind, device_name, updated_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(profile_id, source_name) DO UPDATE SET device_kind = excluded.device_kind,
                   device_name = COALESCE(excluded.device_name, device_name), updated_at = excluded.updated_at
               WHERE ? AND excluded.device_kind <> data_sources.device_kind""",
            (profile_id, source, kind, device, now, said_so))


def delete_sample(profile_id: str, sample_id: str) -> bool:
    """Removes one reading the person says is wrong. A sync that brings the same id back leaves it out."""
    tz = _tz(profile_id)
    orders = source_orders()
    affected: set[str] = set()
    changed: set[str] = set()
    with db() as conn:
        n = _remove_samples(conn, profile_id, [sample_id], (), affected, changed, tz)
        if not n:
            return False
        conn.execute("INSERT OR REPLACE INTO sample_tombstones(profile_id, sample_id, deleted_at) VALUES (?, ?, ?)",
                     (profile_id, sample_id, time.time()))
        if affected:
            _rollup(conn, profile_id, sorted(affected), changed, tz, orders)
        mark_daily_changed(conn, profile_id)
    sample_counts.forget()
    return True


def delete_from_phone(profile_id: str, items: Iterable[dict[str, Any]]) -> int:
    """What a phone says was deleted there, for this person only. Health Connect turns one record into suffixed samples
    (hc-id-0, hc-id-1), so the record's id takes them all. A sample first stored from an export.xml has a hash for an id
    and can't be matched from here; that row stays until the person removes it."""
    tz = _tz(profile_id)
    orders = source_orders()
    affected: set[str] = set()
    changed: set[str] = set()
    total = 0
    now = time.time()
    with db() as conn:
        for item in items:
            item_id, kind = item.get("id"), item.get("kind") or "sample"
            if not isinstance(item_id, str) or not item_id:
                continue
            if kind == "sample":
                record = item_id if item_id.startswith("hc-") else f"hc-{item_id}"
                total += _remove_samples(conn, profile_id, [item_id, record], [f"{record}-"], affected, changed, tz)
                conn.execute("INSERT OR REPLACE INTO sample_tombstones(profile_id, sample_id, deleted_at) VALUES (?, ?, ?)",
                             (profile_id, item_id, now))
            elif kind in ("workout", "event"):
                table = "workouts" if kind == "workout" else "health_events"
                total += conn.execute(f"DELETE FROM {table} WHERE profile_id = ? AND id IN (?, ?, ?)",
                                      (profile_id, item_id, f"hc-{item_id}", f"{item_id}@{profile_id}")).rowcount
        if affected:
            _rollup(conn, profile_id, sorted(affected), changed, tz, orders)
    if total:
        sample_counts.forget()
    return total


def _daily_targets(metric: str) -> list[str]:
    """The daily rollups a sample type feeds (sleep stages add up into several)."""
    if metric in SLEEP_STAGE_SAMPLES:
        return _STAGE_TARGETS
    if metric in SLEEP_MAP:
        return SLEEP_MAP[metric]
    return [metric] if metric in METRICS else []


def _day_spans(days: Iterable[str], tz: tzinfo) -> list[tuple[str, str]]:
    """UTC [start, end) spans covering each day with a margin either side (samples near midnight, in a zone other than
    the person's, nights that end the next morning), merged so no sample is read twice."""
    spans: list[list[date]] = []
    for d in sorted({date.fromisoformat(d) for d in days}):
        lo, hi = d - timedelta(days=REBUILD_MARGIN_BEFORE), d + timedelta(days=REBUILD_MARGIN_AFTER)
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], hi)
        else:
            spans.append([lo, hi])

    def utc(d: date) -> str:
        return datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc).strftime(_ISO)
    return [(utc(lo), utc(hi)) for lo, hi in spans]


def _split_by_day(start: str, end: str, value: float, offset: Optional[int], tz: tzinfo) -> list[tuple[str, str, str, float]]:
    """(day, start, end, share) for each local day an interval covers, shared by time. Steps from 23:30 to 00:30 are half
    on each day."""
    zone = zone_at(offset, tz)
    s, e = parse_ts(start).astimezone(zone), parse_ts(end).astimezone(zone)
    length = (e - s).total_seconds()
    if length <= 0 or length > SPLIT_MAX_S or s.date() == (e - timedelta(seconds=1)).date():
        return [(s.date().isoformat(), start, end, value)]
    pieces = []
    cursor = s
    while cursor < e:
        midnight = datetime.combine(cursor.date() + timedelta(days=1), datetime.min.time(), zone)
        stop = min(midnight, e)
        share = value * (stop - cursor).total_seconds() / length
        pieces.append((cursor.date().isoformat(), cursor.astimezone(timezone.utc).strftime(_ISO),
                       stop.astimezone(timezone.utc).strftime(_ISO), share))
        cursor = stop
    return pieces


def _time_mean(points: list[tuple[float, float]]) -> float:
    """Each reading counts for the time until the next one, up to TIME_WEIGHT_MAX_S."""
    points = sorted(points)
    total = weight = 0.0
    last_gap = TIME_WEIGHT_MAX_S
    for i, (t, value) in enumerate(points):
        if i + 1 < len(points):
            last_gap = min(points[i + 1][0] - t, TIME_WEIGHT_MAX_S)
        w = max(last_gap, 1.0)
        total += value * w
        weight += w
    return total / weight


def _rollup(conn: Any, profile_id: str, days: list[str], metrics: Optional[Iterable[str]], tz: tzinfo,
            orders: Optional[dict[str, list[str]]] = None) -> int:
    """Works out the given local days again on the caller's transaction. With ``metrics`` (the sample types that
    changed), only the rollups those types feed: a batch of years-old heart rate then touches heart rate, not every row
    in between."""
    targets: Optional[set[str]] = None
    if metrics is not None:
        targets = {t for m in metrics for t in _daily_targets(m)}
        if not targets:
            return 0
    where = "profile_id = ? AND flag IS NULL AND start_date >= ? AND start_date < ?"
    extra: tuple[str, ...] = ()
    if targets is not None:
        feeding = sorted(m for m in {*SLEEP_MAP, *METRICS} if targets.intersection(_daily_targets(m)))
        where += f" AND metric_type IN ({','.join('?' * len(feeding))})"
        extra = tuple(feeding)
    rows: list[Any] = []
    for lo, hi in _day_spans(days, tz):
        rows += conn.execute(
            "SELECT id, metric_type, value, start_date, end_date, source_name, metadata_json, tz_offset_min "
            f"FROM biometric_samples WHERE {where}", (profile_id, lo, hi, *extra),
        ).fetchall()

    readings: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    totals: dict[tuple[str, str, str], list[float]] = defaultdict(list)    # what a source says the whole day came to
    last_seen: dict[tuple[str, str, str], tuple[str, float]] = {}
    timed: dict[tuple[str, str, str], list[tuple[float, float]]] = defaultdict(list)
    intervals: dict[tuple[str, str], list[tuple[str, str, float, str]]] = defaultdict(list)
    stages: dict[str, list[tuple[float, float, str, Optional[int]]]] = defaultdict(list)
    repeats: dict[tuple[str, str, str, str], list[tuple[float, str]]] = defaultdict(list)
    for r in rows:
        metric, value, source = r["metric_type"], float(r["value"]), r["source_name"] or "Unknown"
        start, end, offset = r["start_date"], r["end_date"], r["tz_offset_min"]
        day = _stored_day(r["metadata_json"], start)
        if metric in SLEEP_STAGE_SAMPLES and not day:
            s, e = parse_ts(start).timestamp(), parse_ts(end).timestamp()
            if e > s:
                stages[source].append((s, e, metric, offset))
            continue
        if metric in SLEEP_MAP:
            d = day or local_day(end, tz, offset)
            for target in SLEEP_MAP[metric]:
                totals[(d, target, source)].append(value / 3600.0)
            continue
        if metric not in METRICS:
            continue
        aggregation = METRICS[metric][2]
        if aggregation == "sum":
            path, seen = _id_path(r["id"]), repeats[(metric, start, end, source)]
            # The same reading stored from two paths before they recognised each other.
            if any(p != path and _close(v, value) for v, p in seen):
                continue
            seen.append((value, path))
            if day or (r["metadata_json"] and '"daily_total":true' in r["metadata_json"]):
                totals[(day or local_day(start, tz, offset), metric, source)].append(value)
                continue
            if end > start:
                for d, piece_start, piece_end, share in _split_by_day(start, end, value, offset, tz):
                    readings[(d, metric, source)].append(share)
                    intervals[(d, metric)].append((piece_start, piece_end, share, source))
                continue
        d = day or local_day(end if metric in OVERNIGHT_METRICS else start, tz, offset)
        key = (d, metric, source)
        readings[key].append(value)
        if metric in TIME_WEIGHTED:
            timed[key].append((parse_ts(start).timestamp(), value))
        prev = last_seen.get(key)
        if prev is None or start >= prev[0]:
            last_seen[key] = (start, value)

    for (day, target, source), spans in _sleep_nights(stages, tz).items():
        # A source's own summary of the night wins over adding up its stages; both would count the night twice.
        if (day, target, source) not in totals:
            readings[(day, target, source)].extend(spans)

    wanted = set(days)
    only = f" AND metric_type IN ({','.join('?' * len(targets))})" if targets is not None else ""
    conn.execute(
        f"DELETE FROM biometric_daily WHERE profile_id = ? AND day IN ({','.join('?' * len(wanted))}){only}",
        (profile_id, *wanted, *(sorted(targets) if targets is not None else ())),
    )

    written = 0
    for key in {*readings, *totals}:
        day, metric, source = key
        if day not in wanted or (targets is not None and metric not in targets):
            continue
        values = totals.get(key) or readings[key]
        agg = METRICS[metric][2]
        if agg == "sum":
            value = sum(values)
        elif agg == "last":
            value = last_seen.get(key, (None, values[-1]))[1]
        elif timed.get(key):
            value = _time_mean(timed[key])
        else:
            value = sum(values) / len(values)
        conn.execute(
            """INSERT OR REPLACE INTO biometric_daily(profile_id, day, metric_type, source_name, value,
                   min_value, max_value, sample_count, unit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (profile_id, day, metric, source, round(value, 4), min(values), max(values), len(values), METRICS[metric][1]),
        )
        written += 1
    kinds = source_kinds(profile_id, conn)
    for (day, metric), found in intervals.items():
        if day not in wanted or (targets is not None and metric not in targets):
            continue
        items = [i for i in found if (day, metric, i[3]) not in totals]
        contributors = {i[3] for i in items}
        if len(contributors) < 2:
            continue
        merged = merge_interval_sources(items, lambda s: source_rank(s, metric, orders, kinds))
        if METRICS[metric][1] in ("steps", "flights"):
            merged = round(merged)   # whole steps, as Apple Health shows them
        conn.execute(
            """INSERT OR REPLACE INTO biometric_daily(profile_id, day, metric_type, source_name, value,
                   min_value, max_value, sample_count, unit, contributors) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (profile_id, day, metric, MERGED_SOURCE, round(merged, 4), min(i[2] for i in items),
             max(i[2] for i in items), len(items), METRICS[metric][1], dumps(sorted(contributors))),
        )
        written += 1
    mark_daily_changed(conn, profile_id)
    return written


def rebuild_daily(profile_id: str, days: Optional[list[str]] = None, metrics: Optional[Iterable[str]] = None) -> int:
    """Recomputes daily aggregates for the given local days in one transaction, or with no days, every day a month at a
    time (see ``queue_rebuild``)."""
    tz = _tz(profile_id)
    orders = source_orders()
    if not days:
        with db() as conn:
            queue_rebuild(conn, profile_id)
        run_queued_rebuilds()
        return 0
    with db() as conn:
        return _rollup(conn, profile_id, days, metrics, tz, orders)


def queue_rebuild(conn: Any, profile_id: str) -> None:
    """Says, in the caller's transaction, that all of a person's days have to be worked out again. Written before what
    makes it necessary, so a kill at any point is picked up at the next start."""
    conn.execute("""INSERT INTO daily_rebuilds(profile_id, next_day, last_day, requested_at) VALUES (?, NULL, NULL, ?)
                    ON CONFLICT(profile_id) DO UPDATE SET next_day = NULL, last_day = NULL, requested_at = excluded.requested_at""",
                 (profile_id, time.time()))


def _rebuild_range(conn: Any, profile_id: str, tz: tzinfo) -> Optional[tuple[str, str]]:
    """Every day that has rollups or could get some. Rollups outside the samples' range (a time zone ago, a deleted source)
    are in it too, so they go."""
    # What can't be a day (from before day labels were checked, or a reading dated 2105) would stretch the range for nothing.
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).strftime(_ISO)
    conn.execute("DELETE FROM biometric_daily WHERE profile_id = ? AND (day NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
                 "OR day < ? OR day > ?)", (profile_id, str(EARLIEST_YEAR), soon[:10]))
    days = conn.execute("SELECT MIN(day), MAX(day) FROM biometric_daily WHERE profile_id = ?", (profile_id,)).fetchone()
    first = conn.execute("SELECT start_date FROM biometric_samples WHERE profile_id = ? AND flag IS NULL AND start_date >= ? "
                         "ORDER BY start_date LIMIT 1", (profile_id, str(EARLIEST_YEAR))).fetchone()
    last = conn.execute("SELECT start_date FROM biometric_samples WHERE profile_id = ? AND flag IS NULL AND start_date < ? "
                        "ORDER BY start_date DESC LIMIT 1", (profile_id, soon)).fetchone()
    low = [d for d in (days[0], first and (date.fromisoformat(local_day(first[0], tz)) - timedelta(days=DAY_LABEL_MAX_DAYS + 1)).isoformat()) if d]
    high = [d for d in (days[1], last and (date.fromisoformat(local_day(last[0], tz)) + timedelta(days=DAY_LABEL_MAX_DAYS + 2)).isoformat()) if d]
    return (min(low), max(high)) if low and high else None


def rebuild_step(conn: Any, cursor: Any = None, size: int = 0) -> Any:
    """One month of the oldest queued rebuild; a step for ``write_in_batches``. A month at a time keeps memory small (one
    month's readings, not years of them) and gives the write lock back: other writes have a 15 second limit on waiting."""
    row = conn.execute("SELECT * FROM daily_rebuilds ORDER BY requested_at, profile_id LIMIT 1").fetchone()
    if not row:
        return None
    profile_id = row["profile_id"]
    tz = _tz(profile_id)
    next_day, last_day = row["next_day"], row["last_day"]
    if next_day is None:
        span = _rebuild_range(conn, profile_id, tz)
        if span is None:
            conn.execute("DELETE FROM daily_rebuilds WHERE profile_id = ?", (profile_id,))
            mark_daily_changed(conn, profile_id)
            return (cursor or 0) + 1
        next_day, last_day = span
    first = date.fromisoformat(next_day)
    end_of_month = first.replace(day=monthrange(first.year, first.month)[1])
    stop = min(end_of_month, date.fromisoformat(last_day))
    days = [(first + timedelta(days=i)).isoformat() for i in range((stop - first).days + 1)]
    _rollup(conn, profile_id, days, None, tz, source_orders())
    following = stop + timedelta(days=1)
    if following > date.fromisoformat(last_day):
        conn.execute("DELETE FROM daily_rebuilds WHERE profile_id = ?", (profile_id,))
    else:
        conn.execute("UPDATE daily_rebuilds SET next_day = ?, last_day = ? WHERE profile_id = ?",
                     (following.isoformat(), last_day, profile_id))
    return (cursor or 0) + 1


def run_queued_rebuilds() -> None:
    """Until nothing is queued. Safe beside another thread doing the same: each step reads where things stand under the
    write lock."""
    write_in_batches(rebuild_step)


def resume_rebuilds() -> None:
    """At start-up: what a kill interrupted carries on in the background, without holding up the server's start."""
    with read() as conn:
        pending = conn.execute("SELECT 1 FROM daily_rebuilds LIMIT 1").fetchone()
    if pending:
        threading.Thread(target=run_queued_rebuilds, name="daily-rebuild", daemon=True).start()


def queue_all_rebuilds(conn: Any) -> None:
    for row in conn.execute("SELECT id FROM profiles").fetchall():
        queue_rebuild(conn, row[0])


def mark_daily_changed(conn: Any, profile_id: str) -> None:
    """Raises the person's rollup revision (in the same transaction as the change): what's cached from their daily rows
    is worked out again on the next request. Called wherever biometric_daily changes."""
    conn.execute("INSERT INTO daily_revisions(profile_id, rev) VALUES (?, 1) "
                 "ON CONFLICT(profile_id) DO UPDATE SET rev = rev + 1", (profile_id,))


def daily_revision(profile_id: str) -> int:
    with read() as conn:
        row = conn.execute("SELECT rev FROM daily_revisions WHERE profile_id = ?", (profile_id,)).fetchone()
    return row[0] if row else 0


# Worked out from the daily rows and kept until they change (see mark_daily_changed), or until what else they depend on
# does (the day, the time zone, the source order): {(what, profile_id): (key, value)}. The revision lives in the database,
# so another process (the MCP server) sees a change made by this one.
_derived: dict[tuple[str, str], tuple[Any, Any]] = {}
_DERIVED_MAX = 256


def _cached(what: str, profile_id: str, key: Any, compute: Any) -> Any:
    tz = _tz(profile_id)
    full = (daily_revision(profile_id), datetime.now(tz).date().isoformat(), tz.key, dumps(source_orders()), key)
    hit = _derived.get((what, profile_id))
    if hit and hit[0] == full:
        return copy.deepcopy(hit[1])
    value = compute()
    if len(_derived) >= _DERIVED_MAX:
        _derived.clear()
    _derived[(what, profile_id)] = (full, value)
    return copy.deepcopy(value)


def _asleep_seconds(samples: list[tuple[float, float, str, Optional[int]]]) -> float:
    return sum(e - s for s, e in _union([(s, e) for s, e, m, _ in samples if not m.endswith("_awake")]))


def _is_nap(samples: list[tuple[float, float, str, Optional[int]]], tz: tzinfo) -> bool:
    first = min(samples, key=lambda x: x[0])
    started = datetime.fromtimestamp(first[0], zone_at(first[3], tz))
    return _asleep_seconds(samples) < NAP_MAX_S and NAP_HOURS[0] <= started.hour < NAP_HOURS[1]


def sleep_sessions(samples: list[tuple[float, float, str, Optional[int]]], tz: tzinfo) -> list[dict[str, Any]]:
    """One source's stage samples, as ``{day, night: samples, naps: [samples]}``. Samples are grouped into sessions (no
    gap over SLEEP_SESSION_GAP_S) and a session counts toward the local day it ends: counting each sample by its own end
    would give the stretch before midnight to the evening's date, a one-hour "night" on days with no sleep. The day's
    longest session is the night. Another counts as a nap when it's short and in the daytime, otherwise as part of the
    night (someone awake for hours at 3 AM didn't nap afterwards). The Sleep page and the daily figures both start here,
    so they can't disagree."""
    sessions: list[list[Any]] = []     # [latest end, samples]
    for sample in sorted(samples, key=lambda x: x[:3]):
        if sessions and sample[0] - sessions[-1][0] <= SLEEP_SESSION_GAP_S:
            sessions[-1][0] = max(sessions[-1][0], sample[1])
            sessions[-1][1].append(sample)
        else:
            sessions.append([sample[1], [sample]])
    by_day: dict[str, list[list[Any]]] = defaultdict(list)
    for woke, session in sessions:
        last = max(session, key=lambda x: x[1])
        by_day[datetime.fromtimestamp(woke, zone_at(last[3], tz)).date().isoformat()].append(session)
    out = []
    for day, found in by_day.items():
        main = max(found, key=_asleep_seconds)
        night, naps = list(main), []
        for session in found:
            if session is main:
                continue
            if _is_nap(session, tz):
                naps.append(session)
            else:
                night.extend(session)
        out.append({"day": day, "night": night, "naps": naps})
    return out


def _sleep_nights(stages: dict[str, list[tuple[float, float, str, Optional[int]]]], tz: tzinfo) -> dict[tuple[str, str, str], list[float]]:
    """Hours per (day, daily metric, source) from sleep stage samples. Overlapping samples from the same source (an app
    writing both "asleep" and stages) count once, as in Apple Health."""
    out: dict[tuple[str, str, str], list[float]] = {}
    for source, samples in stages.items():
        for session in sleep_sessions(samples, tz):
            by_target: dict[str, list[tuple[float, float]]] = defaultdict(list)
            for start, end, metric, _ in session["night"]:
                for target in SLEEP_MAP[metric]:
                    by_target[target].append((start, end))
            for target, spans in by_target.items():
                out.setdefault((session["day"], target, source), []).extend((e - s) / 3600.0 for s, e in _union(spans))
            for nap in session["naps"]:
                if asleep := _asleep_seconds(nap):
                    out.setdefault((session["day"], "sleep_nap", source), []).append(asleep / 3600.0)
    return out


# Bump when _rollup's rules change; stored rollups are then recomputed once, in the background (app/services/wearable_fixes.py).
# 2: combined Watch + iPhone totals; simulated sources ranked last.
# 3: sleep stages grouped into nights that count toward the day they end; overlapping stages count once; overnight
#    wrist temperature and breathing disturbances count toward the morning too.
# 4: flagged readings left out; naps apart from the night; heart rate weighted by time; intervals shared across midnight;
#    a source's daily total wins over its intraday readings; every metric the iPhone sends; a reading's own offset.
DAILY_VERSION = 4


def merge_interval_sources(items: list[tuple[str, str, float, str]], rank: Optional[Callable[[str], Any]] = None) -> float:
    """Sums interval samples from several sources without double counting overlapping time.
    Sources are taken in priority order; a sample counts for the fraction of its interval not already
    covered by a higher-priority source (the same proportional rule HealthKit statistics use)."""
    by_source: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for start, end, value, source in items:
        by_source[source].append((parse_ts(start).timestamp(), parse_ts(end).timestamp(), value))
    covered: list[tuple[float, float]] = []   # disjoint, sorted
    total = 0.0
    for source in sorted(by_source, key=lambda s: ((rank or source_priority)(s), s)):
        samples = by_source[source]
        ends = [ce for _, ce in covered]
        for s, e, v in samples:
            overlap = 0.0
            for cs, ce in covered[bisect.bisect_right(ends, s):]:   # first covered span ending after s
                if cs >= e:
                    break
                overlap += min(e, ce) - max(s, cs)
            total += v * max(0.0, 1 - overlap / (e - s))
        covered = _union(covered + [(s, e) for s, e, _ in samples])
    return total


def _union(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


# ---------------------------------------------------------------------------
# Which source wins when several report the same metric
# ---------------------------------------------------------------------------
# Sources are grouped into kinds, and each group of metrics has its own order of kinds, like Apple Health's
# per-type Data Sources list. Defaults: rings and straps are worn overnight and usually measure sleep and
# overnight vitals best; the Watch is best for activity and daytime heart rate.

SOURCE_KINDS = {"watch": "Apple Watch, Galaxy Watch, Pixel Watch, Garmin or Polar", "ring": "Oura, WHOOP, Fitbit or Google Health",
                "phone": "Phone", "other": "Other apps and devices"}

PRIORITY_GROUPS: dict[str, dict[str, Any]] = {
    "sleep": {"label": "Sleep", "hint": "Duration, stages and sleep score", "default": ["ring", "watch", "phone", "other"],
              "metrics": ["sleep_duration", "sleep_deep", "sleep_rem", "sleep_core", "sleep_awake", "sleep_nap", "sleep_score"]},
    "overnight": {"label": "Overnight vitals", "hint": "HRV, resting heart rate, breathing, blood oxygen, temperature",
                  "default": ["ring", "watch", "phone", "other"],
                  "metrics": ["hrv_sdnn", "hrv_rmssd", "resting_heart_rate", "respiratory_rate", "oxygen_saturation",
                              "body_temperature_deviation", "sleeping_wrist_temperature"]},
    "activity": {"label": "Activity and everything else", "hint": "Steps, energy, heart rate, workouts and more",
                 "default": ["watch", "ring", "phone", "other"],
                 "metrics": []},
}
_METRIC_GROUP = {m: g for g, spec in PRIORITY_GROUPS.items() for m in spec["metrics"]}

# A preferred source that recorded less than this share of the longest night is passed over for that night.
SLEEP_COVERAGE_MIN = 0.75


def priority_group(metric: str) -> str:
    return _METRIC_GROUP.get(metric, "activity")


def source_kinds(profile_id: str, conn: Any = None) -> dict[str, str]:
    """What each of a person's sources turned out to be (see ``device_kind``)."""
    if conn is not None:
        return {r[0]: r[1] for r in conn.execute("SELECT source_name, device_kind FROM data_sources WHERE profile_id = ?", (profile_id,))}
    with read() as reader:
        return source_kinds(profile_id, reader)


def source_kind(source_name: Optional[str], kinds: Optional[dict[str, str]] = None, contributors: Optional[str] = None,
                orders: Optional[list[str]] = None) -> str:
    """A source's kind. Combined takes the kind of the best source merged into it; without that on the row (one worked out
    before this was kept), the Watch's, whose rules it follows."""
    if source_name == MERGED_SOURCE:
        try:
            merged = json.loads(contributors) if contributors else None
        except ValueError:
            merged = None
        if not merged:
            return "watch"
        found = [source_kind(m, kinds) for m in merged]
        return min(found, key=(orders or list(SOURCE_KINDS)).index)
    return (kinds or {}).get(source_name or "") or device_kind(source_name)


def source_orders() -> dict[str, list[str]]:
    """The saved order per group, completed with any kinds it is missing (so a bad setting can't hide a source)."""
    saved = settings.get("biometrics.source_order") or {}
    out = {}
    for group, spec in PRIORITY_GROUPS.items():
        order = [k for k in (saved.get(group) or spec["default"]) if k in SOURCE_KINDS]
        out[group] = list(dict.fromkeys(order + [k for k in spec["default"] if k not in order]))
    return out


def set_source_orders(orders: dict[str, list[str]]) -> dict[str, list[str]]:
    clean = {}
    for group, order in orders.items():
        if group not in PRIORITY_GROUPS or not isinstance(order, list):
            raise ValueError(f"Unknown group '{group}'.")
        if sorted(order) != sorted(SOURCE_KINDS):
            raise ValueError(f"The order for {group} must list each of: {', '.join(SOURCE_KINDS)}.")
        clean[group] = order
    settings.set("biometrics.source_order", {**(settings.get("biometrics.source_order") or {}), **clean})
    return source_orders()


def source_rank(source_name: Optional[str], metric: str, orders: Optional[dict[str, list[str]]] = None,
                kinds: Optional[dict[str, str]] = None, contributors: Optional[str] = None) -> tuple[int, int]:
    if "(simulated)" in (source_name or "").lower():
        return (99, 0)   # demo data never outranks real measurements
    order = (orders or source_orders())[priority_group(metric)]
    return (order.index(source_kind(source_name, kinds, contributors, order)), 0 if source_name == MERGED_SOURCE else 1)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _pick(rows: list[dict[str, Any]], metric: str, source: Optional[str] = None,
          orders: Optional[dict[str, list[str]]] = None, kinds: Optional[dict[str, str]] = None) -> dict[str, dict[str, Any]]:
    """Chooses one source per day by the metric group's source order (ties: more samples)."""
    orders = orders or source_orders()

    def rank(r: dict[str, Any]) -> tuple[int, int]:
        return source_rank(r["source_name"], metric, orders, kinds, r.get("contributors"))
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        if source and r["source_name"] != source:
            continue
        cur = best.get(r["day"])
        if cur is None:
            best[r["day"]] = r
            continue
        a, b = rank(r), rank(cur)
        if a < b or (a == b and r["sample_count"] > cur["sample_count"]):
            best[r["day"]] = r
    return best


def _sleep_sources(nights: list[dict[str, Any]], orders: dict[str, list[str]], kinds: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Per night, the source to use for every sleep metric: the preferred one, unless it only caught part of
    the night (e.g. the Watch died at 2 AM) and another real source recorded a much longer one."""
    preferred = _pick(nights, "sleep_duration", orders=orders, kinds=kinds)
    longest: dict[str, dict[str, Any]] = {}
    for r in nights:
        if "(simulated)" in r["source_name"].lower() and "(simulated)" not in preferred[r["day"]]["source_name"].lower():
            continue
        if r["day"] not in longest or r["value"] > longest[r["day"]]["value"]:
            longest[r["day"]] = r
    chosen = {}
    for day, row in preferred.items():
        top = longest.get(day, row)
        chosen[day] = top["source_name"] if row["value"] < SLEEP_COVERAGE_MIN * top["value"] else row["source_name"]
    return chosen


def daily_series(profile_id: str, metric: str, days: int = 30, source: Optional[str] = None,
                 end: Optional[date] = None) -> dict[str, Any]:
    tz = _tz(profile_id)
    end = end or datetime.now(tz).date()
    start = end - timedelta(days=days - 1)
    orders = source_orders()
    kinds = source_kinds(profile_id)
    group = priority_group(metric)
    with read() as conn:
        rows = rows_to_dicts(conn.execute(
            "SELECT * FROM biometric_daily WHERE profile_id = ? AND metric_type = ? AND day >= ? AND day <= ? ORDER BY day",
            (profile_id, metric, start.isoformat(), end.isoformat()),
        ).fetchall())
        nights = [] if group != "sleep" or source else rows if metric == "sleep_duration" else rows_to_dicts(conn.execute(
            "SELECT * FROM biometric_daily WHERE profile_id = ? AND metric_type = 'sleep_duration' AND day >= ? AND day <= ?",
            (profile_id, start.isoformat(), end.isoformat()),
        ).fetchall())
        left_out = conn.execute("SELECT COUNT(*) FROM biometric_samples WHERE profile_id = ? AND metric_type = ? AND flag IS NOT NULL",
                                (profile_id, metric)).fetchone()[0]
    chosen = _pick(rows, metric, source, orders, kinds)
    if nights:
        # Every sleep figure for a night comes from the same source, chosen by how much of the night it covered.
        by_source = {(r["day"], r["source_name"]): r for r in rows}
        for day, src in _sleep_sources(nights, orders, kinds).items():
            if (day, src) in by_source:
                chosen[day] = by_source[(day, src)]
    label, unit, agg, group = METRICS.get(metric, (metric, "", "mean", "other"))
    today = datetime.now(tz).date().isoformat()
    points = [
        {"day": d, "value": round(r["value"], 2), "min": r["min_value"], "max": r["max_value"], "source": r["source_name"],
         **({"partial": True} if d == today and in_progress(metric) else {})}
        for d, r in sorted(chosen.items())
    ]
    # Today's steps or energy so far would pull the average down; it counts once the day is over.
    values = [p["value"] for p in points if not p.get("partial")] or [p["value"] for p in points]
    return {
        "metric": metric, "label": label, "unit": unit, "aggregation": agg, "group": group,
        "start": start.isoformat(), "end": end.isoformat(), "points": points,
        "sources": sorted({r["source_name"] for r in rows} - {MERGED_SOURCE}),   # "Best source" already uses it
        "left_out": left_out,
        "stats": {
            "count": len(values),
            "mean": round(sum(values) / len(values), 2) if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "latest": points[-1] if points else None,
        },
    }


def in_progress(metric: str) -> bool:
    """Whether today's value is a running total that keeps growing until midnight (steps, energy, distance)."""
    spec = METRICS.get(metric)
    return bool(spec and spec[2] == "sum" and spec[3] != "sleep")


def available_metrics(profile_id: str) -> list[dict[str, Any]]:
    """Every metric the person has daily values for, with how many days, the latest day and its sources."""
    return _cached("metrics", profile_id, None, lambda: _available_metrics(profile_id))


def _available_metrics(profile_id: str) -> list[dict[str, Any]]:
    today = datetime.now(_tz(profile_id)).date().isoformat()
    found: dict[str, dict[str, Any]] = {}
    with read() as conn:
        # Per source rather than joined into one text, since a source's name can hold a comma. A day that hasn't happened
        # yet doesn't get to be "latest", and text that isn't a date (rollups from before day labels were checked) isn't a day.
        for r in conn.execute(
                "SELECT metric_type, source_name, COUNT(*) AS days, MAX(day) AS latest_day FROM biometric_daily WHERE profile_id = ? "
                "AND day <= ? AND day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' GROUP BY metric_type, source_name",
                (profile_id, today)).fetchall():
            m = found.setdefault(r["metric_type"], {"days": 0, "latest_day": None, "sources": []})
            m["latest_day"] = max(m["latest_day"] or "", r["latest_day"])
            m["sources"].append(r["source_name"])
        for r in conn.execute(
                "SELECT metric_type, COUNT(DISTINCT day) FROM biometric_daily WHERE profile_id = ? AND day <= ? "
                "AND day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' GROUP BY metric_type", (profile_id, today)).fetchall():
            found[r[0]]["days"] = r[1]
    out = []
    for metric, m in found.items():
        label, unit, agg, group = METRICS.get(metric, (metric, "", "mean", "other"))
        out.append({"metric": metric, "label": label, "unit": unit, "group": group, **m})
    order = {m: i for i, m in enumerate(METRICS)}
    return sorted(out, key=lambda m: order.get(m["metric"], 999))


def with_latest_values(profile_id: str, metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adds each metric's value on its most recent day, from the source "Best source" would show."""
    latest = _cached("latest", profile_id, None, lambda: {
        m["metric"]: _latest_value(profile_id, m) for m in available_metrics(profile_id)})
    for m in metrics:
        m["latest_value"] = latest.get(m["metric"])
    return metrics


def _latest_value(profile_id: str, m: dict[str, Any]) -> Optional[float]:
    if not m.get("latest_day"):
        return None
    points = daily_series(profile_id, m["metric"], days=1, end=date.fromisoformat(m["latest_day"]))["points"]
    return points[-1]["value"] if points else None


def headline(profile_id: str, metrics: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Latest value plus 7/30-day averages and a 30-day sparkline for the key metrics, or for ``metrics`` (the ones
    chosen for the Overview) in that order."""
    chosen = tuple(metrics[:40]) if metrics else None
    return _cached(f"headline:{chosen}", profile_id, chosen, lambda: _headline(profile_id, metrics))


def _headline(profile_id: str, metrics: Optional[list[str]] = None) -> list[dict[str, Any]]:
    have = {m["metric"] for m in available_metrics(profile_id)}
    out = []
    for metric in (metrics[:40] if metrics else HEADLINE_METRICS):
        if metric not in have:
            continue
        series = daily_series(profile_id, metric, days=30)
        pts = series["points"]
        if not pts:
            continue
        # The last 7 calendar days (not the last 7 days that have data), leaving out a day still in progress.
        week = (date.fromisoformat(series["end"]) - timedelta(days=6)).isoformat()
        last7 = [p["value"] for p in pts if p["day"] >= week and not p.get("partial")]
        out.append({
            "metric": metric, "label": series["label"], "unit": series["unit"], "group": series["group"],
            "latest": pts[-1], "avg_7d": round(sum(last7) / len(last7), 2) if last7 else None,
            "avg_30d": series["stats"]["mean"], "spark": [p["value"] for p in pts],
        })
    return out


def has_samples(profile_id: str) -> bool:
    with read() as conn:
        return conn.execute("SELECT 1 FROM biometric_samples WHERE profile_id = ? LIMIT 1", (profile_id,)).fetchone() is not None


# Per-source sample totals, kept until the profile's samples change. Counting them means reading every sample.
_source_totals: dict[str, tuple[tuple, list[dict[str, Any]]]] = {}


def _samples_signature(conn: Any, profile_id: str) -> tuple:
    """Changes whenever the profile's samples do: new rows raise the highest rowid, and every insert or delete is
    followed by a rebuild of the daily rows, which raises their revision."""
    rev = conn.execute("SELECT rev FROM daily_revisions WHERE profile_id = ?", (profile_id,)).fetchone()
    return (rev[0] if rev else 0, conn.execute("SELECT MAX(rowid) FROM biometric_samples").fetchone()[0])


def left_out(profile_id: str, conn: Any = None) -> list[dict[str, Any]]:
    """Readings kept but not counted, per metric and why. The partial index only holds these, so this costs nothing."""
    if conn is None:
        with read() as reader:
            return left_out(profile_id, reader)
    return [{"metric": r["metric_type"], "label": METRICS.get(r["metric_type"], (r["metric_type"],))[0], "reason": r["flag"], "count": r["n"]}
            for r in conn.execute("SELECT metric_type, flag, COUNT(*) AS n FROM biometric_samples WHERE profile_id = ? AND flag IS NOT NULL "
                                  "GROUP BY metric_type, flag ORDER BY n DESC", (profile_id,)).fetchall()]


def overview(profile_id: str) -> dict[str, Any]:
    with read() as conn:
        signature = _samples_signature(conn, profile_id)
        cached = _source_totals.get(profile_id)
        if cached and cached[0] == signature:
            rows = cached[1]
        else:
            # One pass over the (profile, source, date) index gives the count, date span and per-source totals.
            rows = rows_to_dicts(conn.execute(
                "SELECT source_name, COUNT(*) AS samples, MIN(start_date) AS earliest, MAX(start_date) AS latest "
                "FROM biometric_samples WHERE profile_id = ? GROUP BY source_name ORDER BY samples DESC",
                (profile_id,),
            ).fetchall())
            # A reading dated 2105 is kept but doesn't get to say how far the history runs. Few readings are flagged, and only
            # the sources that have one are looked at again.
            flagged = {r[0] for r in conn.execute("SELECT DISTINCT source_name FROM biometric_samples WHERE profile_id = ? AND flag IS NOT NULL",
                                                  (profile_id,))}
            for r in rows:
                if r["source_name"] in flagged:
                    span = conn.execute("SELECT MIN(start_date), MAX(start_date) FROM biometric_samples WHERE profile_id = ? "
                                        "AND source_name = ? AND flag IS NULL", (profile_id, r["source_name"])).fetchone()
                    r["earliest"], r["latest"] = span[0], span[1]
            _source_totals[profile_id] = (signature, rows)
        last_batch = conn.execute(
            "SELECT * FROM sync_batches WHERE profile_id = ? ORDER BY synced_at DESC LIMIT 1", (profile_id,)
        ).fetchone()
        not_counted = left_out(profile_id, conn)
    sources = [{"source_name": r["source_name"], "samples": r["samples"], "latest": r["latest"]} for r in rows]
    return {
        "total_samples": sum(r["samples"] for r in rows),
        "history_span": {"earliest": min((r["earliest"] for r in rows if r["earliest"]), default=None),
                         "latest": max((r["latest"] for r in rows if r["latest"]), default=None)},
        "sources": sources,
        "left_out": not_counted,
        "last_batch": dict(last_batch) if last_batch else None,
        "headline": headline(profile_id),
    }


def recent_samples(profile_id: str, metric: Optional[str] = None, limit: int = 100, left_out_only: bool = False) -> list[dict[str, Any]]:
    q = ("SELECT id, metric_type, value, unit, start_date, end_date, device_name, source_name, flag "
         "FROM biometric_samples WHERE profile_id = ?")
    params: list[Any] = [profile_id]
    if metric:
        q += " AND metric_type = ?"
        params.append(metric)
    if left_out_only:
        q += " AND flag IS NOT NULL"
    q += " ORDER BY start_date DESC LIMIT ?"
    params.append(limit)
    with read() as conn:
        return rows_to_dicts(conn.execute(q, params).fetchall())


def _delete_batches(where: str, params: tuple[Any, ...], table: str = "biometric_samples") -> None:
    """A long delete in short transactions, so other writes get their turn."""
    def step(conn: Any, cursor: Any, size: int) -> Any:
        n = conn.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {where} LIMIT ?)", (*params, size)).rowcount
        return ((cursor or 0) + n) if n else None
    write_in_batches(step, size=5000)


def delete_for_connection(connection_id: str) -> None:
    """A source's samples, then its person's rollups. The rebuild is queued first: from a kill at any point the rollups are
    put right at the next start, whatever was or wasn't deleted, and removing the source again finishes the job."""
    with read() as conn:
        row = conn.execute("SELECT profile_id FROM connections WHERE id = ?", (connection_id,)).fetchone()
        if not row:
            row = conn.execute("SELECT profile_id FROM biometric_samples WHERE connection_id = ? LIMIT 1", (connection_id,)).fetchone()
    if not row or not row[0]:
        return
    with db() as conn:
        queue_rebuild(conn, row[0])
    _delete_batches("connection_id = ?", (connection_id,))
    sample_counts.forget()
    run_queued_rebuilds()


def delete_all_for_profile(profile_id: str) -> None:
    """Empties the biggest tables ahead of wiping someone's data, so the wipe's own transaction is short."""
    for table in ("biometric_samples", "health_events", "workouts", "biometric_daily"):
        _delete_batches("profile_id = ?", (profile_id,), table)
    with db() as conn:
        conn.execute("DELETE FROM daily_rebuilds WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM data_sources WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM sample_tombstones WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM import_jobs WHERE profile_id = ?", (profile_id,))
