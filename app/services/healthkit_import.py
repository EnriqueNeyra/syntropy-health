"""
HealthKit export JSON import.

Accepts the ``{"data": {"metrics": [...], "workouts": [...], ...}}`` layout produced by the
Syntropy iPhone app's JSON export and by other HealthKit export apps' REST automations,
and stores it as samples, workouts and events. Metric names map to the same identifiers the
iPhone app uses, so data arriving through both paths de-duplicates.

A row that's a group of readings (an hour's or a day's steps) grows as the day goes on and is sent again and again, so a new
value replaces the last rather than adding to it. The iPhone app says which it's sending in the ``automation-aggregation``
header. With no header, a row with no end date is taken to be a group, as Health Auto Export's are; rows with the same date in
one request are then different readings, and are added together. A summed row at midnight alone on its date is then either a
whole day or the first hour of one, which only what else arrives for that day can settle (see ``biometrics._rollup``). A day's
total sent again from another time zone replaces the first rather than adding to it, though it starts at another moment. A plain
sample keeps its own identity, value included, so two entries at one moment both count. Aggregated sleep belongs to its day,
wherever its start and end move. Grouped by the week, month or year, totals aren't any day's and are left out.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.connectors.apple_health import convert_unit
from app.store import activity, biometrics
from app.store.metric_registry import METRICS

# Export metric name -> Syntropy metric_type
METRIC_NAMES: dict[str, str] = {
    "active_energy": "active_energy",
    "active_energy_burned": "active_energy",
    "apple_exercise_time": "apple_exercise_time",
    "apple_move_time": "apple_move_time",
    "apple_sleeping_wrist_temperature": "sleeping_wrist_temperature",
    "apple_stand_time": "apple_stand_time",
    "apple_walking_steadiness": "walking_steadiness",
    "atrial_fibrillation_burden": "atrial_fibrillation_burden",
    "basal_body_temperature": "basal_body_temperature",
    "basal_energy_burned": "basal_energy",
    "biotin": "dietary_biotin",
    "blood_alcohol_content": "blood_alcohol_content",
    "blood_glucose": "blood_glucose",
    "blood_oxygen": "oxygen_saturation",
    "blood_oxygen_saturation": "oxygen_saturation",
    "blood_pressure_diastolic": "blood_pressure_diastolic",
    "blood_pressure_systolic": "blood_pressure_systolic",
    "body_fat_percentage": "body_fat_percentage",
    "body_mass_index": "body_mass_index",
    "body_temperature": "body_temperature",
    "caffeine": "dietary_caffeine",
    "calcium": "dietary_calcium",
    "carbohydrates": "dietary_carbohydrates",
    "cardio_recovery": "heart_rate_recovery_one_minute",
    "chloride": "dietary_chloride",
    "cholesterol": "dietary_cholesterol",
    "chromium": "dietary_chromium",
    "copper": "dietary_copper",
    "cycling_cadence": "cycling_cadence",
    "cycling_distance": "distance_cycling",
    "cycling_functional_threshold_power": "cycling_functional_threshold_power",
    "cycling_power": "cycling_power",
    "cycling_speed": "cycling_speed",
    "dietary_energy": "dietary_energy",
    "dietary_sugar": "dietary_sugar",
    "dietary_water": "dietary_water",
    "distance_cross_country_skiing": "distance_cross_country_skiing",
    "distance_downhill_snow_sports": "distance_downhill_snow_sports",
    "distance_paddle_sports": "distance_paddle_sports",
    "distance_rowing": "distance_rowing",
    "distance_skating_sports": "distance_skating_sports",
    "electrodermal_activity": "electrodermal_activity",
    "environmental_audio_exposure": "environmental_audio_exposure",
    "environmental_sound_reduction": "environmental_sound_reduction",
    "fiber": "dietary_fiber",
    "flights_climbed": "flights_climbed",
    "folate": "dietary_folate",
    "forced_expiratory_volume_1": "forced_expiratory_volume_1",
    "forced_vital_capacity": "forced_vital_capacity",
    "headphone_audio_exposure": "headphone_audio_exposure",
    "heart_rate": "heart_rate",
    "heart_rate_recovery": "heart_rate_recovery_one_minute",
    "heart_rate_variability": "hrv_sdnn",
    "height": "height",
    "hrv": "hrv_sdnn",
    "inhaler_usage": "inhaler_usage",
    "insulin_delivery": "insulin_delivery",
    "iodine": "dietary_iodine",
    "iron": "dietary_iron",
    "lean_body_mass": "lean_body_mass",
    "magnesium": "dietary_magnesium",
    "manganese": "dietary_manganese",
    "molybdenum": "dietary_molybdenum",
    "monounsaturated_fat": "dietary_fat_monounsaturated",
    "niacin": "dietary_niacin",
    "number_of_alcoholic_beverages": "number_of_alcoholic_beverages",
    "number_of_times_fallen": "number_of_times_fallen",
    "pantothenic_acid": "dietary_pantothenic_acid",
    "peak_expiratory_flow_rate": "peak_expiratory_flow_rate",
    "peripheral_perfusion_index": "peripheral_perfusion_index",
    "phosphorus": "dietary_phosphorus",
    "physical_effort": "physical_effort",
    "polyunsaturated_fat": "dietary_fat_polyunsaturated",
    "potassium": "dietary_potassium",
    "protein": "dietary_protein",
    "push_count": "push_count",
    "respiratory_rate": "respiratory_rate",
    "resting_heart_rate": "resting_heart_rate",
    "riboflavin": "dietary_riboflavin",
    "running_ground_contact_time": "running_ground_contact_time",
    "running_power": "running_power",
    "running_speed": "running_speed",
    "running_stride_length": "running_stride_length",
    "running_vertical_oscillation": "running_vertical_oscillation",
    "saturated_fat": "dietary_fat_saturated",
    "selenium": "dietary_selenium",
    "six_minute_walking_test_distance": "six_minute_walk_distance",
    "sleeping_breathing_disturbances": "sleeping_breathing_disturbances",
    "sleeping_wrist_temperature": "sleeping_wrist_temperature",
    "sodium": "dietary_sodium",
    "stair_speed_down": "stair_descent_speed",
    "stair_speed_up": "stair_ascent_speed",
    "step_count": "step_count",
    "swimming_distance": "distance_swimming",
    "swimming_stroke_count": "swimming_stroke_count",
    "thiamin": "dietary_thiamin",
    "time_in_daylight": "time_in_daylight",
    "total_fat": "dietary_fat_total",
    "underwater_depth": "underwater_depth",
    "uv_exposure": "uv_exposure",
    "vitamin_a": "dietary_vitamin_a",
    "vitamin_b12": "dietary_vitamin_b12",
    "vitamin_b6": "dietary_vitamin_b6",
    "vitamin_c": "dietary_vitamin_c",
    "vitamin_d": "dietary_vitamin_d",
    "vitamin_e": "dietary_vitamin_e",
    "vitamin_k": "dietary_vitamin_k",
    "vo2_max": "vo2_max",
    "waist_circumference": "waist_circumference",
    "walking_asymmetry_percentage": "walking_asymmetry",
    "walking_double_support_percentage": "walking_double_support",
    "walking_heart_rate_average": "walking_heart_rate_average",
    "walking_running_distance": "distance_walking_running",
    "walking_speed": "walking_speed",
    "walking_step_length": "walking_step_length",
    "water_temperature": "water_temperature",
    "weight": "body_mass",
    "weight_body_mass": "body_mass",
    "wheelchair_distance": "distance_wheelchair",
    "zinc": "dietary_zinc",
}

# Category type display name (lower-case) -> (event_type, category)
CATEGORY_NAMES: dict[str, tuple[str, str]] = {
    "sleep": ("sleep_analysis", "sleep"),
    "mindful minutes": ("mindful_session", "mindfulness"),
    "high heart rate notification": ("high_heart_rate_event", "events"),
    "low heart rate notification": ("low_heart_rate_event", "events"),
    "irregular rhythm notification": ("irregular_heart_rhythm_event", "events"),
    "low cardio fitness notification": ("low_cardio_fitness_event", "events"),
    "walking steadiness notification": ("walking_steadiness_event", "events"),
    "loud environment notification": ("environmental_audio_exposure_event", "events"),
    "headphone audio notification": ("headphone_audio_exposure_event", "events"),
    "breathing disturbances notification": ("sleep_apnea_event", "events"),
    "stand hours": ("apple_stand_hour", "activity"),
    "handwashing": ("handwashing_event", "other"),
    "toothbrushing": ("toothbrushing_event", "other"),
    "menstrual flow": ("menstrual_flow", "cycle"),
    "spotting": ("intermenstrual_bleeding", "cycle"),
    "ovulation test": ("ovulation_test_result", "cycle"),
    "cervical mucus quality": ("cervical_mucus_quality", "cycle"),
    "sexual activity": ("sexual_activity", "cycle"),
    "contraceptives": ("contraceptive", "cycle"),
    "pregnancy": ("pregnancy", "cycle"),
    "lactation": ("lactation", "cycle"),
    "pregnancy test": ("pregnancy_test_result", "cycle"),
    "progesterone test": ("progesterone_test_result", "cycle"),
    "persistent spotting": ("persistent_intermenstrual_bleeding", "cycle"),
    "prolonged periods": ("prolonged_menstrual_periods", "cycle"),
    "irregular cycles": ("irregular_menstrual_cycles", "cycle"),
    "infrequent periods": ("infrequent_menstrual_cycles", "cycle"),
    "bleeding during pregnancy": ("bleeding_during_pregnancy", "cycle"),
    "bleeding after pregnancy": ("bleeding_after_pregnancy", "cycle"),
    "abdominal cramps": ("abdominal_cramps", "symptoms"),
    "acne": ("acne", "symptoms"),
    "appetite changes": ("appetite_changes", "symptoms"),
    "bladder incontinence": ("bladder_incontinence", "symptoms"),
    "bloating": ("bloating", "symptoms"),
    "breast pain": ("breast_pain", "symptoms"),
    "chest tightness or pain": ("chest_tightness_or_pain", "symptoms"),
    "chills": ("chills", "symptoms"),
    "constipation": ("constipation", "symptoms"),
    "coughing": ("coughing", "symptoms"),
    "diarrhea": ("diarrhea", "symptoms"),
    "dizziness": ("dizziness", "symptoms"),
    "dry skin": ("dry_skin", "symptoms"),
    "fainting": ("fainting", "symptoms"),
    "fatigue": ("fatigue", "symptoms"),
    "fever": ("fever", "symptoms"),
    "body and muscle ache": ("generalized_body_ache", "symptoms"),
    "hair loss": ("hair_loss", "symptoms"),
    "headache": ("headache", "symptoms"),
    "heartburn": ("heartburn", "symptoms"),
    "hot flashes": ("hot_flashes", "symptoms"),
    "loss of smell": ("loss_of_smell", "symptoms"),
    "loss of taste": ("loss_of_taste", "symptoms"),
    "lower back pain": ("lower_back_pain", "symptoms"),
    "memory lapse": ("memory_lapse", "symptoms"),
    "mood changes": ("mood_changes", "symptoms"),
    "nausea": ("nausea", "symptoms"),
    "night sweats": ("night_sweats", "symptoms"),
    "pelvic pain": ("pelvic_pain", "symptoms"),
    "rapid, pounding, or fluttering heartbeat": ("rapid_pounding_or_fluttering_heartbeat", "symptoms"),
    "runny nose": ("runny_nose", "symptoms"),
    "shortness of breath": ("shortness_of_breath", "symptoms"),
    "sinus congestion": ("sinus_congestion", "symptoms"),
    "skipped heartbeat": ("skipped_heartbeat", "symptoms"),
    "sleep changes": ("sleep_changes", "symptoms"),
    "sore throat": ("sore_throat", "symptoms"),
    "vaginal dryness": ("vaginal_dryness", "symptoms"),
    "vomiting": ("vomiting", "symptoms"),
    "wheezing": ("wheezing", "symptoms"),
}

SLEEP_STAGES = {
    "in bed": "sleep_analysis_inbed", "inbed": "sleep_analysis_inbed", "asleep": "sleep_analysis_unspecified",
    "unspecified": "sleep_analysis_unspecified", "awake": "sleep_analysis_awake", "core": "sleep_analysis_core",
    "deep": "sleep_analysis_deep", "rem": "sleep_analysis_rem",
}

_TEMP_UNITS = {"degF", "°F", "F"}
# What the automation-aggregation header says when rows are groups of readings.
GROUPED_GRAINS = {"minutes", "hours", "days", "weeks", "months", "years"}
# Groups longer than a day. A week's steps are no day's, and dated by the week's first day they'd show as one huge Monday, so
# totals (and aggregated nights, and mindful minutes) at these grains are left out. An average or a latest value stays: it
# describes that stretch, and stands for it on its first day.
LONGER_THAN_A_DAY = {"weeks", "months", "years"}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "unknown"


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        f = None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None
    return f if f is not None and math.isfinite(f) else None


def _rows(v: Any) -> list[dict[str, Any]]:
    """The dict items of a list; anything else in the payload is ignored."""
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _ts(v: Any):
    """Parsed timestamp, or None when missing, unreadable or past either end of the calendar in UTC (the row then goes alone)."""
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        parsed = biometrics.parse_ts(v)
        parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None
    return parsed


def _after(start: datetime, seconds: float) -> Optional[datetime]:
    """The time a length after ``start``, or None when a length that long can't be one."""
    try:
        return start + timedelta(seconds=seconds)
    except OverflowError:
        return None


def _qty(obj: Any) -> tuple[Optional[float], str]:
    """Reads ``{"qty": 1, "units": "km"}`` or a bare number."""
    if isinstance(obj, dict):
        return _num(obj.get("qty")), str(obj.get("units") or "")
    return _num(obj), ""


def _text(v: Any) -> Optional[str]:
    """A name or label as text. A number reads fine as one; a list or an object isn't a name at all."""
    if isinstance(v, str):
        return v
    return str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _sample_unit(metric: str, value: float, units: str) -> str:
    """The unit to store a row under. These rows' percentages are 0–100, unlike the fractions the phones send."""
    if metric in biometrics.FRACTION_METRICS and (units == "%" or value > 1.0):
        return "percent"
    return units


def _settle_groups(samples: list[dict[str, Any]], grain: str) -> list[dict[str, Any]]:
    """Rows of a group sharing a date in one request add up (or the later one stands, for what isn't a total). A summed row is
    the whole day's when the automation says so. With nothing said, one at midnight and alone on its date is the whole day's
    only as long as no other hour of that day turns up."""
    settled: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for s in samples:
        if not s.get("replaces") or (s.get("metadata") or {}).get("day"):     # a night is one row; there's nothing to add up
            out.append(s)
        elif s["id"] in settled:
            if METRICS.get(s["metric_type"], ("", "", ""))[2] == "sum":
                settled[s["id"]]["value"] += s["value"]
            else:
                settled[s["id"]]["value"] = s["value"]
        else:
            settled[s["id"]] = s
            out.append(s)
    on_date: dict[tuple[str, str, str], int] = {}
    for s in settled.values():
        key = (s["metric_type"], s["source_name"], str(s["start_date"])[:10])
        on_date[key] = on_date.get(key, 0) + 1
    for s in settled.values():
        if METRICS.get(s["metric_type"], ("", "", ""))[2] != "sum":
            continue
        at = biometrics.parse_ts(s["start_date"])
        alone = on_date[(s["metric_type"], s["source_name"], str(s["start_date"])[:10])] == 1
        if grain == "days":
            # The day it was counted for, where it was counted. Sent again after a flight, the same day starts at another moment
            # and so has another id; the day is what makes the new figure replace the old one rather than add to it.
            s["metadata"] = {**(s.get("metadata") or {}), "daily_total": True, "day": at.date().isoformat()}
        elif not grain and alone and not at.hour and not at.minute:
            s["metadata"] = {**(s.get("metadata") or {}), "daily_total_if_alone": True}
    return out


def convert(payload: dict[str, Any], grain: Optional[str] = None) -> dict[str, list[dict[str, Any]]]:
    grain = (grain or "").strip().lower()
    too_long = grain in LONGER_THAN_A_DAY
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        data = {}
    samples: list[dict[str, Any]] = []
    workouts: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    deleted_ids: list[str] = []

    def is_group(row: dict[str, Any]) -> bool:
        return (grain in GROUPED_GRAINS or (not grain and not row.get("endDate"))) and _ts(row.get("date")) is not None

    def event(event_type: str, category: str, name: str, start: str, end: Optional[str], source: Any,
              value: Optional[float] = None, label: Any = None, metadata: Optional[dict] = None) -> None:
        source, label, name = _text(source), _text(label), _text(name) or event_type
        events.append({
            "id": activity.stable_id("ev", event_type, start, end, source, label), "event_type": event_type,
            "category": category, "name": name, "start": start, "end": end or start, "value": value,
            "value_label": label, "source_name": source, "metadata": metadata or None,
        })

    for m in _rows(data.get("metrics")):
        name = str(m.get("name") or "")
        units = str(m.get("units") or "")
        rows = _rows(m.get("data"))
        if name == "sleep_analysis":
            _sleep(rows, samples, deleted_ids, nights=not too_long)
            continue
        if name == "blood_pressure":
            for r in rows:
                for field, metric in (("systolic", "blood_pressure_systolic"), ("diastolic", "blood_pressure_diastolic")):
                    v = _num(r.get(field))
                    if v is not None and r.get("date"):
                        samples.append(_sample(metric, v, "mmHg", r["date"], r.get("endDate"), r.get("source"), group=is_group(r)))
            continue
        if name == "mindful_minutes":
            for r in rows if not too_long else ():
                v = _num(r.get("qty"))
                start = _ts(r.get("date"))
                end = _after(start, v * 60) if start is not None and v is not None else None
                if end is not None:
                    event("mindful_session", "mindfulness", "Mindful Minutes", start.isoformat(), end.isoformat(), r.get("source"),
                          value=v, label=f"{v:g} min")
            continue
        metric = METRIC_NAMES.get(name) or slug(name)
        summed = METRICS.get(metric, ("", "", ""))[2] == "sum"
        for r in rows:
            if "start" in r and "date" not in r:
                # Category-style rows (e.g. stand hours) exported inside "metrics".
                ev_type, category = CATEGORY_NAMES.get(str(r.get("name") or "").lower(), (metric, "other"))
                event(ev_type, category, r.get("name") or name, r["start"], r.get("end"), r.get("source"),
                      value=_num(r.get("qty")), label=r.get("value"))
                continue
            if not r.get("date") or (too_long and summed):
                continue
            if name == "heart_rate" and "Avg" in r:
                v = _num(r.get("Avg"))
                meta = {"min": r.get("Min"), "max": r.get("Max")}
            else:
                v = _num(r.get("qty"))
                meta = None
            if v is None:
                continue
            samples.append(_sample(metric, v, _sample_unit(metric, v, units), r["date"], r.get("endDate"), r.get("source"), meta,
                                   group=is_group(r)))

    for w in _rows(data.get("workouts")):
        converted = _workout(w)
        if converted:
            workouts.append(converted)

    for key, category in (("symptoms", "symptoms"), ("cycleTracking", "cycle"), ("heartRateNotifications", "events")):
        for r in _rows(data.get(key)):
            if not r.get("start"):
                continue
            name = str(r.get("name") or key)
            ev_type, cat = CATEGORY_NAMES.get(name.lower(), (slug(name), category))
            label = r.get("severity") or r.get("value")
            event(ev_type, cat, name, r["start"], r.get("end"), r.get("source"), label=str(label) if label is not None else None,
                  metadata=r.get("metadata") if isinstance(r.get("metadata"), dict) else None)

    for r in _rows(data.get("ecg")):
        if r.get("start"):
            meta = {k: r.get(k) for k in ("numberOfVoltageMeasurements", "samplingFrequency", "symptomsStatus") if r.get(k) is not None}
            event("ecg", "ecg", "ECG", r["start"], r.get("end"), r.get("source"), value=_num(r.get("averageHeartRate")),
                  label=r.get("classification"), metadata=meta)

    for r in _rows(data.get("stateOfMind")):
        if r.get("start"):
            meta = {"kind": r.get("kind"), "labels": r.get("labels"), "associations": r.get("associations")}
            event("state_of_mind", "stateOfMind", "Daily Mood" if r.get("kind") == "dailyMood" else "Emotion", r["start"],
                  r.get("end"), r.get("source"), value=_num(r.get("valence")), label=r.get("valenceClassification"), metadata=meta)

    return {"samples": _settle_groups(samples, grain), "workouts": workouts, "events": events, "deleted_ids": deleted_ids}


def _sample(metric: str, value: float, unit: str, start: str, end: Optional[str], source: Any,
            metadata: Optional[dict] = None, group: bool = False) -> dict[str, Any]:
    source = _text(source) or "HealthKit export"
    sample = {
        "id": activity.stable_id("s", metric, start, end, source, value), "metric_type": metric, "value": value,
        "unit": unit, "start_date": start, "end_date": end or start, "source_name": source, "strict_units": True,
        "metadata": {k: v for k, v in (metadata or {}).items() if v is not None} or None,
    }
    if group:
        sample["id"] = activity.stable_id("a", metric, biometrics.to_utc_iso(start), source)
        sample["replaces"] = True
        # A group is dated by the clock where it was counted, so its offset is what says which day it is.
        if (offset := biometrics.offset_of(start)) is not None:
            sample["tz_offset_min"] = offset
    return sample


def _sleep(rows: list[dict[str, Any]], samples: list[dict[str, Any]], deleted_ids: list[str], nights: bool = True) -> None:
    """Stage rows as they are; aggregated nights (``nights``) only when each row is one night."""
    for r in rows:
        source = _text(r.get("source")) or "HealthKit export"
        if r.get("startDate") and r.get("value"):
            stage = SLEEP_STAGES.get(str(r["value"]).lower())
            if not stage:
                continue
            start, end = _ts(r["startDate"]), _ts(r.get("endDate") or r["startDate"])
            if start is None or end is None:
                continue
            samples.append(_sample(stage, (end - start).total_seconds(), "s", r["startDate"], r.get("endDate"), source))
            continue
        dated = _ts(r.get("date"))
        if dated is None or not nights:
            continue
        # Aggregated night: hours per stage, attributed to the day it's dated (the wake-up day).
        day = dated.date().isoformat() if len(str(r["date"])) > 10 else str(r["date"])
        start = r.get("sleepStart") or r.get("inBedStart") or r["date"]
        end = r.get("sleepEnd") or r.get("inBedEnd") or r["date"]
        total = _num(r.get("totalSleep"))
        if total is None:
            total = sum(_num(r.get(k)) or 0 for k in ("asleep", "core", "deep", "rem"))
        for metric, hours in (("sleep_total_duration", total), ("sleep_deep", _num(r.get("deep"))),
                              ("sleep_rem", _num(r.get("rem"))), ("sleep_light", _num(r.get("core"))),
                              ("sleep_awake", _num(r.get("awake")))):
            sample_id = activity.stable_id("n", metric, day, source)
            if hours:
                samples.append({**_sample(metric, hours * 3600, "s", start, end, source, {"day": day}), "id": sample_id, "replaces": True})
            elif hours == 0:
                deleted_ids.append(sample_id)      # a stage that's been revised down to nothing doesn't keep its old figure


def _workout(w: dict[str, Any]) -> Optional[dict[str, Any]]:
    start, end = w.get("start"), w.get("end")
    if not start:
        return None
    name = str(w.get("name") or "Workout")
    source = _text(w.get("source")) or "HealthKit export"

    def measure(*keys: str, metric: str) -> Optional[float]:
        for k in keys:
            v, units = _qty(w.get(k))
            if v is not None:
                return convert_unit(metric, v, units or ("km" if metric.startswith("distance") else ""))[0]
        return None

    duration = _num(w.get("duration"))
    started, ended = _ts(start), _ts(end)
    if started is None:
        return None
    if duration is None and ended is not None:
        duration = (ended - started).total_seconds()
    if ended is None:
        # Keep the workout even if its end time is missing or unreadable.
        finished = _after(started, duration or 0)
        if finished is None:
            duration, finished = None, started
        end = finished.isoformat()
    hr = w.get("heartRate") if isinstance(w.get("heartRate"), dict) else {}
    avg_hr = _qty(hr.get("avg"))[0] if hr else _qty(w.get("avgHeartRate"))[0]
    max_hr = _qty(hr.get("max"))[0] if hr else _qty(w.get("maxHeartRate"))[0]
    min_hr = _qty(hr.get("min"))[0] if hr else None
    temp, temp_units = _qty(w.get("temperature"))
    if temp is not None and temp_units in _TEMP_UNITS:
        temp = (temp - 32) * 5 / 9
    humidity = _qty(w.get("humidity"))[0]
    location = str(w.get("location") or "").lower()

    heart_rate = []
    for p in _rows(w.get("heartRateData")):
        if p.get("date"):
            avg = _num(p.get("Avg")) or _num(p.get("qty"))
            if avg is not None:
                heart_rate.append({"t": p["date"], "min": _num(p.get("Min")) or avg, "avg": avg, "max": _num(p.get("Max")) or avg})
    route = []
    for p in _rows(w.get("route")):
        lat, lon = _num(p.get("latitude") or p.get("lat")), _num(p.get("longitude") or p.get("lon"))
        if lat is not None and lon is not None:
            route.append({"t": p.get("timestamp") or p.get("t"), "lat": lat, "lon": lon,
                          "alt": _num(p.get("altitude") or p.get("alt")), "speed": _num(p.get("speed"))})

    return {
        "id": str(w.get("id") or activity.stable_id("w", name, start, end, source)),
        "activity_type": None, "name": name, "start": start, "end": end or start, "duration_s": duration,
        "active_energy_kcal": measure("activeEnergyBurned", "activeEnergy", metric="active_energy"),
        "total_energy_kcal": measure("totalEnergy", metric="active_energy"),
        "distance_m": measure("distance", metric="distance_walking_running"),
        "step_count": _qty(w.get("stepCount"))[0],
        "avg_hr": avg_hr, "max_hr": max_hr, "min_hr": min_hr,
        "elevation_ascent_m": measure("elevationUp", metric="six_minute_walk_distance"),
        "elevation_descent_m": measure("elevationDown", metric="six_minute_walk_distance"),
        "indoor": True if location == "indoor" else False if location == "outdoor" else None,
        "temperature_c": temp, "humidity_pct": humidity, "source_name": source,
        "metadata": w.get("metadata") if isinstance(w.get("metadata"), dict) else None,
        "heart_rate": heart_rate, "route": route,
    }
