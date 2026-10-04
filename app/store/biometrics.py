"""
Continuous biometrics: idempotent sample ingest, per-day rollups and trend queries.

Raw samples are kept verbatim. After each ingest the affected local days are
re-aggregated into ``biometric_daily`` (one row per day / metric / source). Reads pick
one source per day using a priority order so totals such as steps are never
double-counted when an Apple Watch, an iPhone and a ring all report them.

For summed metrics recorded as time intervals by several sources (steps from an Apple
Watch and an iPhone), an extra "Combined" row merges them the way Apple Health does:
the preferred source counts in full and each other sample only for the share of its
interval the preferred sources did not cover. Steps taken with only the phone are kept.
"""

from __future__ import annotations

import bisect
import math
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from app.core import settings
from app.core.db import db, read, dumps, new_id, rows_to_dicts
from app.store import sample_counts

# metric -> (label, display unit, aggregation, group)
METRICS: dict[str, tuple[str, str, str, str]] = {
    "step_count": ("Steps", "steps", "sum", "activity"),
    "active_energy": ("Active energy", "kcal", "sum", "activity"),
    "basal_energy": ("Resting energy", "kcal", "sum", "activity"),
    "total_energy": ("Total energy", "kcal", "sum", "activity"),
    "distance_walking_running": ("Walking + running distance", "m", "sum", "activity"),
    "flights_climbed": ("Flights climbed", "flights", "sum", "activity"),
    "apple_exercise_time": ("Exercise minutes", "min", "sum", "activity"),
    "apple_stand_time": ("Stand minutes", "min", "sum", "activity"),
    "activity_score": ("Activity score", "score", "last", "activity"),
    "strain_score": ("Strain", "score", "last", "activity"),
    "vo2_max": ("VO₂ max", "mL/kg·min", "last", "activity"),
    "heart_rate": ("Heart rate", "bpm", "mean", "heart"),
    "resting_heart_rate": ("Resting heart rate", "bpm", "mean", "heart"),
    "walking_heart_rate_average": ("Walking heart rate", "bpm", "mean", "heart"),
    "heart_rate_recovery_one_minute": ("Heart rate recovery", "bpm", "mean", "heart"),
    "hrv_sdnn": ("HRV (SDNN)", "ms", "mean", "heart"),
    "hrv_rmssd": ("HRV (RMSSD)", "ms", "mean", "heart"),
    "recovery_score": ("Recovery", "score", "last", "readiness"),
    "readiness_score": ("Readiness", "score", "last", "readiness"),
    "sleep_duration": ("Sleep", "h", "sum", "sleep"),
    "sleep_deep": ("Deep sleep", "h", "sum", "sleep"),
    "sleep_rem": ("REM sleep", "h", "sum", "sleep"),
    "sleep_core": ("Light / core sleep", "h", "sum", "sleep"),
    "sleep_awake": ("Awake in bed", "h", "sum", "sleep"),
    "sleep_score": ("Sleep score", "score", "last", "sleep"),
    "oxygen_saturation": ("Blood oxygen", "%", "mean", "vitals"),
    "respiratory_rate": ("Respiratory rate", "br/min", "mean", "vitals"),
    "body_temperature": ("Body temperature", "°C", "mean", "vitals"),
    "body_temperature_deviation": ("Temperature deviation", "°C", "last", "vitals"),
    "blood_pressure_systolic": ("Systolic BP", "mmHg", "mean", "vitals"),
    "blood_pressure_diastolic": ("Diastolic BP", "mmHg", "mean", "vitals"),
    "blood_glucose": ("Blood glucose", "mg/dL", "mean", "vitals"),
    "body_mass": ("Weight", "kg", "last", "body"),
    "body_mass_index": ("BMI", "kg/m²", "last", "body"),
    "body_fat_percentage": ("Body fat", "%", "last", "body"),
    "lean_body_mass": ("Lean body mass", "kg", "last", "body"),
    "walking_speed": ("Walking speed", "m/s", "mean", "mobility"),
    "walking_step_length": ("Step length", "cm", "mean", "mobility"),
    "walking_asymmetry": ("Walking asymmetry", "%", "mean", "mobility"),
    "walking_double_support": ("Double support time", "%", "mean", "mobility"),
    "six_minute_walk_distance": ("Six-minute walk", "m", "last", "mobility"),
    "stair_ascent_speed": ("Stair ascent speed", "m/s", "mean", "mobility"),
    "stair_descent_speed": ("Stair descent speed", "m/s", "mean", "mobility"),
    "environmental_audio_exposure": ("Environmental noise", "dB", "mean", "hearing"),
    "headphone_audio_exposure": ("Headphone audio", "dB", "mean", "hearing"),
    "environmental_sound_reduction": ("Noise reduction", "dB", "mean", "hearing"),
    # Additional Apple Health types sent by the iPhone app
    "apple_move_time": ("Move minutes", "min", "sum", "activity"),
    "time_in_daylight": ("Time in daylight", "min", "sum", "activity"),
    "distance_cycling": ("Cycling distance", "m", "sum", "activity"),
    "distance_swimming": ("Swimming distance", "m", "sum", "activity"),
    "physical_effort": ("Physical effort", "kcal/hr·kg", "mean", "activity"),
    "running_speed": ("Running speed", "m/s", "mean", "activity"),
    "running_power": ("Running power", "W", "mean", "activity"),
    "running_stride_length": ("Running stride length", "m", "mean", "activity"),
    "running_ground_contact_time": ("Ground contact time", "ms", "mean", "activity"),
    "running_vertical_oscillation": ("Vertical oscillation", "cm", "mean", "activity"),
    "cycling_power": ("Cycling power", "W", "mean", "activity"),
    "atrial_fibrillation_burden": ("AFib history", "%", "last", "heart"),
    "sleeping_wrist_temperature": ("Wrist temperature (sleep)", "°C", "last", "vitals"),
    "basal_body_temperature": ("Basal body temperature", "°C", "last", "vitals"),
    "sleeping_breathing_disturbances": ("Breathing disturbances", "count", "last", "vitals"),
    "number_of_times_fallen": ("Falls", "count", "sum", "vitals"),
    "height": ("Height", "cm", "last", "body"),
    "waist_circumference": ("Waist circumference", "cm", "last", "body"),
    "walking_steadiness": ("Walking steadiness", "%", "last", "mobility"),
    "dietary_energy": ("Dietary energy", "kcal", "sum", "nutrition"),
    "dietary_protein": ("Protein", "g", "sum", "nutrition"),
    "dietary_carbohydrates": ("Carbohydrates", "g", "sum", "nutrition"),
    "dietary_fat_total": ("Total fat", "g", "sum", "nutrition"),
    "dietary_fiber": ("Fiber", "g", "sum", "nutrition"),
    "dietary_sugar": ("Sugar", "g", "sum", "nutrition"),
    "dietary_water": ("Water", "mL", "sum", "nutrition"),
    "dietary_caffeine": ("Caffeine", "mg", "sum", "nutrition"),
    "dietary_sodium": ("Sodium", "mg", "sum", "nutrition"),
}

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
}

# Sleep stage samples (HealthKit style: one row per stretch of a stage, value = its length in seconds). A night is the
# run of a source's stage samples with no gap longer than this, and it counts toward the day it ends (the morning you
# wake up), as Apple Health shows it. Summaries from rings and straps already say which day they belong to.
SLEEP_STAGE_SAMPLES = {"sleep_analysis_deep", "sleep_analysis_rem", "sleep_analysis_core", "sleep_analysis_asleep",
                       "sleep_analysis_unspecified", "sleep_analysis_awake"}
SLEEP_SESSION_GAP_S = 3 * 3600
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


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone_name())
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


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
    return parse_ts(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_day(value: str, tz: Optional[ZoneInfo] = None) -> str:
    return parse_ts(value).astimezone(tz or _tz()).date().isoformat()


MERGED_SOURCE = "Combined"


def source_priority(source_name: Optional[str]) -> int:
    if source_name == MERGED_SOURCE:
        return 0
    s = (source_name or "").lower()
    if "(simulated)" in s:
        return 9   # demo data never outranks real measurements
    if "watch" in s:
        return 1
    if "oura" in s or "whoop" in s or "google health" in s:
        return 2
    if "iphone" in s or "phone" in s:
        return 3
    return 4


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def id_held_elsewhere(conn: Any, table: str, item_id: str, profile_id: str) -> Optional[str]:
    """When a row's id (HealthKit's own UUID) is already stored for someone else, the id to store it under for this
    person instead; None when it's free or already theirs. A phone first paired to the wrong person and then to the
    right one sends the same samples again, which must reach the second person rather than count as duplicates."""
    row = conn.execute(f"SELECT profile_id FROM {table} WHERE id = ?", (item_id,)).fetchone()
    return f"{item_id}@{profile_id}" if row and row[0] != profile_id else None


def normalize_sample(s: dict[str, Any]) -> dict[str, Any]:
    metric = s["metric_type"]
    value = float(s["value"])
    if not math.isfinite(value):
        raise ValueError(f"{metric}: value must be a finite number")
    if metric in FRACTION_METRICS and 0 <= value <= 1.0:
        value = round(value * 100, 3)
    return {**s, "value": value, "start_date": to_utc_iso(s["start_date"]),
            "end_date": to_utc_iso(s.get("end_date") or s["start_date"])}


def insert_samples(
    profile_id: str,
    connection_id: Optional[str],
    samples: Iterable[dict[str, Any]],
    *,
    batch_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    now = time.time()
    received = inserted = rejected = 0
    tz = _tz()
    affected: set[str] = set()
    changed_metrics: set[str] = set()
    with db() as conn:
        for raw in samples:
            received += 1
            try:
                s = normalize_sample(raw)
            except (ValueError, KeyError, TypeError, AttributeError):
                # One unreadable row must not fail the batch: senders only move on after a success,
                # so a rejected batch would be retried (and rejected) forever.
                rejected += 1
                continue
            meta = s.get("metadata") or {}
            if not isinstance(meta, dict):
                meta = {}
            source = s.get("source_name") or "Unknown"
            # Idempotent on the sample id *and* on its natural key, so the same HealthKit sample
            # arriving via the companion app and via an export.xml import is stored once.
            insert = lambda sample_id: conn.execute(
                """INSERT OR IGNORE INTO biometric_samples(id, profile_id, connection_id, metric_type, hk_identifier,
                       value, unit, start_date, end_date, device_id, device_name, source_name, metadata_json, created_at)
                   SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                   WHERE NOT EXISTS (SELECT 1 FROM biometric_samples WHERE profile_id = ? AND metric_type = ?
                                     AND start_date = ? AND end_date = ? AND source_name = ? AND value = ?)""",
                (sample_id, profile_id, connection_id, s["metric_type"], s.get("hk_identifier"), s["value"],
                 s.get("unit") or "", s["start_date"], s["end_date"], s.get("device_id"), s.get("device_name"),
                 source, dumps(meta) if meta else None, now,
                 profile_id, s["metric_type"], s["start_date"], s["end_date"], source, s["value"]),
            )
            cur = insert(s["id"])
            if not cur.rowcount and (own := id_held_elsewhere(conn, "biometric_samples", s["id"], profile_id)):
                cur = insert(own)
            if cur.rowcount:
                inserted += 1
                changed_metrics.add(s["metric_type"])
                affected.add(meta.get("day") or local_day(s["start_date"], tz))
                if s["metric_type"] in OVERNIGHT_METRICS:
                    affected.add(local_day(s["end_date"], tz))
                if s["metric_type"] in SLEEP_MAP:
                    # A stretch before midnight belongs to the night that ends the next morning.
                    woke = date.fromisoformat(local_day(s["end_date"], tz))
                    affected.update({woke.isoformat(), (woke + timedelta(days=1)).isoformat()})
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
    sample_counts.added(profile_id, connection_id, inserted)
    total = sample_counts.for_profile(profile_id)
    if affected:
        rebuild_daily(profile_id, sorted(affected), changed_metrics)
    return {
        "success": True, "batch_id": batch_id, "received": received, "inserted": inserted,
        "duplicates": received - inserted - rejected, "rejected": rejected, "server_total_samples": total, "days_updated": len(affected),
    }


def _daily_targets(metric: str) -> list[str]:
    """The daily rollups a sample type feeds (sleep stages add up into several)."""
    if metric in SLEEP_MAP:
        return SLEEP_MAP[metric]
    return [metric] if metric in METRICS else []


def _day_spans(days: Iterable[str], tz: ZoneInfo) -> list[tuple[str, str]]:
    """UTC [start, end) spans covering each day with a day's margin either side (samples near midnight, nights that
    end the next morning), merged so no sample is read twice."""
    spans: list[list[date]] = []
    for d in sorted({date.fromisoformat(d) for d in days}):
        lo, hi = d - timedelta(days=1), d + timedelta(days=2)
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], hi)
        else:
            spans.append([lo, hi])

    def utc(d: date) -> str:
        return datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [(utc(lo), utc(hi)) for lo, hi in spans]


def rebuild_daily(profile_id: str, days: Optional[list[str]] = None, metrics: Optional[Iterable[str]] = None) -> int:
    """Recomputes daily aggregates for the given local days (or all days).

    With ``metrics`` (the sample types that changed), only the rollups those types feed are recomputed, reading just
    those types around just those days: a batch of years-old heart rate then touches heart rate, not every row in
    between.
    """
    tz = _tz()
    targets: Optional[set[str]] = None
    if days and metrics is not None:
        targets = {t for m in metrics for t in _daily_targets(m)}
        if not targets:
            return 0
    with db() as conn:
        if days:
            where = "profile_id = ? AND start_date >= ? AND start_date < ?"
            extra: tuple[str, ...] = ()
            if targets is not None:
                feeding = sorted(m for m in {*SLEEP_MAP, *METRICS} if targets.intersection(_daily_targets(m)))
                where += f" AND metric_type IN ({','.join('?' * len(feeding))})"
                extra = tuple(feeding)
            rows = []
            for lo, hi in _day_spans(days, tz):
                rows += conn.execute(
                    "SELECT metric_type, value, unit, start_date, end_date, source_name, metadata_json "
                    f"FROM biometric_samples WHERE {where}", (profile_id, lo, hi, *extra),
                ).fetchall()
        else:
            rows = conn.execute(
                "SELECT metric_type, value, unit, start_date, end_date, source_name, metadata_json "
                "FROM biometric_samples WHERE profile_id = ?",
                (profile_id,),
            ).fetchall()

        buckets: dict[tuple[str, str, str], list[float]] = defaultdict(list)
        last_seen: dict[tuple[str, str, str], tuple[str, float]] = {}
        intervals: dict[tuple[str, str], list[tuple[str, str, float, str]]] = defaultdict(list)
        stages: dict[str, list[tuple[float, float, str]]] = defaultdict(list)    # source -> (start, end, metric)
        import json as _json
        for r in rows:
            metric, value, source = r["metric_type"], float(r["value"]), r["source_name"] or "Unknown"
            meta_day = None
            if r["metadata_json"] and '"day"' in r["metadata_json"]:
                try:
                    meta_day = _json.loads(r["metadata_json"]).get("day")
                except ValueError:
                    meta_day = None
            if metric in SLEEP_STAGE_SAMPLES and not meta_day:
                start, end = parse_ts(r["start_date"]).timestamp(), parse_ts(r["end_date"]).timestamp()
                if end > start:
                    stages[source].append((start, end, metric))
                continue
            if metric in SLEEP_MAP:
                day = meta_day or local_day(r["end_date"], tz)
                for target in SLEEP_MAP[metric]:
                    buckets[(day, target, source)].append(value / 3600.0)
                continue
            if metric not in METRICS:
                continue
            day = meta_day or local_day(r["end_date" if metric in OVERNIGHT_METRICS else "start_date"], tz)
            key = (day, metric, source)
            buckets[key].append(value)
            if METRICS[metric][2] == "sum" and r["end_date"] > r["start_date"] and not meta_day:
                intervals[(day, metric)].append((r["start_date"], r["end_date"], value, source))
            prev = last_seen.get(key)
            if prev is None or r["start_date"] >= prev[0]:
                last_seen[key] = (r["start_date"], value)

        for (day, target, source), spans in _sleep_nights(stages, tz).items():
            buckets[(day, target, source)].extend(spans)

        wanted = set(days) if days else None
        if days:
            only = f" AND metric_type IN ({','.join('?' * len(targets))})" if targets is not None else ""
            conn.execute(
                f"DELETE FROM biometric_daily WHERE profile_id = ? AND day IN ({','.join('?' * len(wanted))}){only}",
                (profile_id, *wanted, *(sorted(targets) if targets is not None else ())),
            )
        else:
            conn.execute("DELETE FROM biometric_daily WHERE profile_id = ?", (profile_id,))

        written = 0
        for (day, metric, source), values in buckets.items():
            if (wanted is not None and day not in wanted) or (targets is not None and metric not in targets):
                continue
            agg = METRICS[metric][2]
            if agg == "sum":
                value = sum(values)
            elif agg == "last":
                value = last_seen.get((day, metric, source), (None, values[-1]))[1]
            else:
                value = sum(values) / len(values)
            conn.execute(
                """INSERT OR REPLACE INTO biometric_daily(profile_id, day, metric_type, source_name, value,
                       min_value, max_value, sample_count, unit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (profile_id, day, metric, source, round(value, 4), min(values), max(values), len(values), METRICS[metric][1]),
            )
            written += 1
        for (day, metric), items in intervals.items():
            if ((wanted is not None and day not in wanted) or (targets is not None and metric not in targets)
                    or len({i[3] for i in items}) < 2):
                continue
            merged = merge_interval_sources(items)
            if METRICS[metric][1] in ("steps", "flights"):
                merged = round(merged)   # whole steps, as Apple Health shows them
            conn.execute(
                """INSERT OR REPLACE INTO biometric_daily(profile_id, day, metric_type, source_name, value,
                       min_value, max_value, sample_count, unit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (profile_id, day, metric, MERGED_SOURCE, round(merged, 4), min(i[2] for i in items),
                 max(i[2] for i in items), len(items), METRICS[metric][1]),
            )
            written += 1
    return written


def _sleep_nights(stages: dict[str, list[tuple[float, float, str]]], tz: ZoneInfo) -> dict[tuple[str, str, str], list[float]]:
    """Hours per (day, daily metric, source) from sleep stage samples.

    Each source's samples are grouped into nights (no gap over SLEEP_SESSION_GAP_S) and a night counts toward the local
    day it ends. Counting each sample by its own end would give the stretch before midnight to the evening's date: a
    one-hour "night" on days with no sleep, and a short real one the morning after. Overlapping samples from the same
    source (an app writing both "asleep" and stages) count once, as in Apple Health.
    """
    out: dict[tuple[str, str, str], list[float]] = {}
    for source, samples in stages.items():
        nights: list[list[Any]] = []     # [latest end, samples]
        for sample in sorted(samples):
            if nights and sample[0] - nights[-1][0] <= SLEEP_SESSION_GAP_S:
                nights[-1][0] = max(nights[-1][0], sample[1])
                nights[-1][1].append(sample)
            else:
                nights.append([sample[1], [sample]])
        for woke, night in nights:
            day = datetime.fromtimestamp(woke, tz).date().isoformat()
            by_target: dict[str, list[tuple[float, float]]] = defaultdict(list)
            for start, end, metric in night:
                for target in SLEEP_MAP[metric]:
                    by_target[target].append((start, end))
            for target, spans in by_target.items():
                out.setdefault((day, target, source), []).extend((e - s) / 3600.0 for s, e in _union(spans))
    return out


# Bump when rebuild_daily's rules change; stored rollups are then recomputed once at startup.
# 2: combined Watch + iPhone totals; simulated sources ranked last.
# 3: sleep stages grouped into nights that count toward the day they end; overlapping stages count once; overnight
#    wrist temperature and breathing disturbances count toward the morning too.
DAILY_VERSION = 3


def upgrade_daily_if_needed() -> None:
    if settings.get("biometrics.daily_version") == DAILY_VERSION:
        return
    with db() as conn:
        profiles = [r[0] for r in conn.execute("SELECT DISTINCT profile_id FROM biometric_samples").fetchall()]
    for profile_id in profiles:
        rebuild_daily(profile_id)
    settings.set("biometrics.daily_version", DAILY_VERSION)


def merge_interval_sources(items: list[tuple[str, str, float, str]]) -> float:
    """Sums interval samples from several sources without double counting overlapping time.
    Sources are taken in priority order; a sample counts for the fraction of its interval not already
    covered by a higher-priority source (the same proportional rule HealthKit statistics use)."""
    by_source: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for start, end, value, source in items:
        by_source[source].append((parse_ts(start).timestamp(), parse_ts(end).timestamp(), value))
    covered: list[tuple[float, float]] = []   # disjoint, sorted
    total = 0.0
    for source in sorted(by_source, key=lambda s: (source_priority(s), s)):
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

SOURCE_KINDS = {"watch": "Apple Watch or Galaxy Watch", "ring": "Oura, WHOOP or Google (Fitbit, Pixel Watch)", "phone": "Phone",
                "other": "Other apps and devices"}

PRIORITY_GROUPS: dict[str, dict[str, Any]] = {
    "sleep": {"label": "Sleep", "hint": "Duration, stages and sleep score", "default": ["ring", "watch", "phone", "other"],
              "metrics": ["sleep_duration", "sleep_deep", "sleep_rem", "sleep_core", "sleep_awake", "sleep_score"]},
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


def source_kind(source_name: Optional[str]) -> str:
    if source_name == MERGED_SOURCE:
        return "watch"   # Watch + iPhone combined by the Watch's rules
    s = (source_name or "").lower()
    if "watch" in s:
        return "watch"
    if "oura" in s or "whoop" in s or "ring" in s or "google health" in s or "fitbit" in s:
        return "ring"      # worn overnight: Oura, WHOOP, and Fitbit / Pixel Watch (Google Health, or Health Connect)
    if "samsung health" in s:
        return "watch"     # Galaxy Watch, through Health Connect on an Android phone
    if "iphone" in s or "phone" in s:
        return "phone"
    return "other"


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


def source_rank(source_name: Optional[str], metric: str, orders: Optional[dict[str, list[str]]] = None) -> tuple[int, int]:
    if "(simulated)" in (source_name or "").lower():
        return (99, 0)   # demo data never outranks real measurements
    order = (orders or source_orders())[priority_group(metric)]
    return (order.index(source_kind(source_name)), 0 if source_name == MERGED_SOURCE else 1)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _pick(rows: list[dict[str, Any]], metric: str, source: Optional[str] = None,
          orders: Optional[dict[str, list[str]]] = None) -> dict[str, dict[str, Any]]:
    """Chooses one source per day by the metric group's source order (ties: more samples)."""
    orders = orders or source_orders()
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        if source and r["source_name"] != source:
            continue
        cur = best.get(r["day"])
        if cur is None:
            best[r["day"]] = r
            continue
        a, b = source_rank(r["source_name"], metric, orders), source_rank(cur["source_name"], metric, orders)
        if a < b or (a == b and r["sample_count"] > cur["sample_count"]):
            best[r["day"]] = r
    return best


def _sleep_sources(nights: list[dict[str, Any]], orders: dict[str, list[str]]) -> dict[str, str]:
    """Per night, the source to use for every sleep metric: the preferred one, unless it only caught part of
    the night (e.g. the Watch died at 2 AM) and another real source recorded a much longer one."""
    preferred = _pick(nights, "sleep_duration", orders=orders)
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
    end = end or datetime.now(_tz()).date()
    start = end - timedelta(days=days - 1)
    orders = source_orders()
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
    chosen = _pick(rows, metric, source, orders)
    if nights:
        # Every sleep figure for a night comes from the same source, chosen by how much of the night it covered.
        by_source = {(r["day"], r["source_name"]): r for r in rows}
        for day, src in _sleep_sources(nights, orders).items():
            if (day, src) in by_source:
                chosen[day] = by_source[(day, src)]
    label, unit, agg, group = METRICS.get(metric, (metric, "", "mean", "other"))
    today = datetime.now(_tz()).date().isoformat()
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
    with read() as conn:
        rows = conn.execute(
            "SELECT metric_type, COUNT(DISTINCT day) AS days, MAX(day) AS latest_day, GROUP_CONCAT(DISTINCT source_name) AS sources "
            "FROM biometric_daily WHERE profile_id = ? GROUP BY metric_type",
            (profile_id,),
        ).fetchall()
    out = []
    for r in rows:
        label, unit, agg, group = METRICS.get(r["metric_type"], (r["metric_type"], "", "mean", "other"))
        out.append({"metric": r["metric_type"], "label": label, "unit": unit, "group": group, "days": r["days"],
                    "latest_day": r["latest_day"], "sources": (r["sources"] or "").split(",")})
    order = {m: i for i, m in enumerate(METRICS)}
    return sorted(out, key=lambda m: order.get(m["metric"], 999))


def with_latest_values(profile_id: str, metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adds each metric's value on its most recent day, from the source "Best source" would show."""
    for m in metrics:
        m["latest_value"] = None
        if not m.get("latest_day"):
            continue
        points = daily_series(profile_id, m["metric"], days=1, end=date.fromisoformat(m["latest_day"]))["points"]
        if points:
            m["latest_value"] = points[-1]["value"]
    return metrics


def headline(profile_id: str, metrics: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Latest value plus 7/30-day averages and a 30-day sparkline for the key metrics, or for ``metrics`` (the ones
    chosen for the Overview) in that order."""
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
    followed by a rebuild of the (small) daily table, which changes its counts."""
    daily = conn.execute("SELECT COUNT(*), COALESCE(SUM(sample_count), 0) FROM biometric_daily WHERE profile_id = ?",
                         (profile_id,)).fetchone()
    return (daily[0], daily[1], conn.execute("SELECT MAX(rowid) FROM biometric_samples").fetchone()[0])


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
            _source_totals[profile_id] = (signature, rows)
        last_batch = conn.execute(
            "SELECT * FROM sync_batches WHERE profile_id = ? ORDER BY synced_at DESC LIMIT 1", (profile_id,)
        ).fetchone()
    sources = [{"source_name": r["source_name"], "samples": r["samples"], "latest": r["latest"]} for r in rows]
    return {
        "total_samples": sum(r["samples"] for r in rows),
        "history_span": {"earliest": min((r["earliest"] for r in rows), default=None),
                         "latest": max((r["latest"] for r in rows), default=None)},
        "sources": sources,
        "last_batch": dict(last_batch) if last_batch else None,
        "headline": headline(profile_id),
    }


def recent_samples(profile_id: str, metric: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
    q = ("SELECT id, metric_type, value, unit, start_date, end_date, device_name, source_name "
         "FROM biometric_samples WHERE profile_id = ?")
    params: list[Any] = [profile_id]
    if metric:
        q += " AND metric_type = ?"
        params.append(metric)
    q += " ORDER BY start_date DESC LIMIT ?"
    params.append(limit)
    with read() as conn:
        return rows_to_dicts(conn.execute(q, params).fetchall())


def delete_for_connection(connection_id: str) -> None:
    with db() as conn:
        rows = conn.execute("SELECT DISTINCT profile_id FROM biometric_samples WHERE connection_id = ?", (connection_id,)).fetchall()
        conn.execute("DELETE FROM biometric_samples WHERE connection_id = ?", (connection_id,))
    sample_counts.forget()
    for r in rows:
        if r["profile_id"]:
            rebuild_daily(r["profile_id"])
