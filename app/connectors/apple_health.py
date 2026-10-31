"""
Apple Health export importer.

Streams ``export.xml`` (from Health app → profile → Export All Health Data) with
``iterparse`` so multi-gigabyte exports import in constant memory. Records are
mapped to the same metric identifiers the iOS companion app uses, so data from
both paths de-duplicates. If the export contains ``clinical-records/*.json`` (Health
Records), those FHIR resources are returned for import as well.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from xml.etree.ElementTree import iterparse

# HealthKit identifier -> (metric_type, target unit, converter from export unit)
QUANTITY_TYPES: dict[str, str] = {
    "HKQuantityTypeIdentifierHeartRate": "heart_rate",
    "HKQuantityTypeIdentifierRestingHeartRate": "resting_heart_rate",
    "HKQuantityTypeIdentifierWalkingHeartRateAverage": "walking_heart_rate_average",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "hrv_sdnn",
    "HKQuantityTypeIdentifierHeartRateRecoveryOneMinute": "heart_rate_recovery_one_minute",
    "HKQuantityTypeIdentifierStepCount": "step_count",
    "HKQuantityTypeIdentifierDistanceWalkingRunning": "distance_walking_running",
    "HKQuantityTypeIdentifierFlightsClimbed": "flights_climbed",
    "HKQuantityTypeIdentifierActiveEnergyBurned": "active_energy",
    "HKQuantityTypeIdentifierBasalEnergyBurned": "basal_energy",
    "HKQuantityTypeIdentifierAppleExerciseTime": "apple_exercise_time",
    "HKQuantityTypeIdentifierAppleStandTime": "apple_stand_time",
    "HKQuantityTypeIdentifierVO2Max": "vo2_max",
    "HKQuantityTypeIdentifierOxygenSaturation": "oxygen_saturation",
    "HKQuantityTypeIdentifierRespiratoryRate": "respiratory_rate",
    "HKQuantityTypeIdentifierBodyTemperature": "body_temperature",
    "HKQuantityTypeIdentifierBloodPressureSystolic": "blood_pressure_systolic",
    "HKQuantityTypeIdentifierBloodPressureDiastolic": "blood_pressure_diastolic",
    "HKQuantityTypeIdentifierBloodGlucose": "blood_glucose",
    "HKQuantityTypeIdentifierBodyMass": "body_mass",
    "HKQuantityTypeIdentifierBodyMassIndex": "body_mass_index",
    "HKQuantityTypeIdentifierBodyFatPercentage": "body_fat_percentage",
    "HKQuantityTypeIdentifierLeanBodyMass": "lean_body_mass",
    "HKQuantityTypeIdentifierWalkingSpeed": "walking_speed",
    "HKQuantityTypeIdentifierWalkingStepLength": "walking_step_length",
    "HKQuantityTypeIdentifierWalkingAsymmetryPercentage": "walking_asymmetry",
    "HKQuantityTypeIdentifierWalkingDoubleSupportPercentage": "walking_double_support",
    "HKQuantityTypeIdentifierSixMinuteWalkTestDistance": "six_minute_walk_distance",
    "HKQuantityTypeIdentifierStairAscentSpeed": "stair_ascent_speed",
    "HKQuantityTypeIdentifierStairDescentSpeed": "stair_descent_speed",
    "HKQuantityTypeIdentifierEnvironmentalAudioExposure": "environmental_audio_exposure",
    "HKQuantityTypeIdentifierHeadphoneAudioExposure": "headphone_audio_exposure",
}

SLEEP_VALUES = {
    "HKCategoryValueSleepAnalysisAsleepDeep": "sleep_analysis_deep",
    "HKCategoryValueSleepAnalysisAsleepREM": "sleep_analysis_rem",
    "HKCategoryValueSleepAnalysisAsleepCore": "sleep_analysis_core",
    "HKCategoryValueSleepAnalysisAsleepUnspecified": "sleep_analysis_unspecified",
    "HKCategoryValueSleepAnalysisAsleep": "sleep_analysis_asleep",
    "HKCategoryValueSleepAnalysisAwake": "sleep_analysis_awake",
    "HKCategoryValueSleepAnalysisInBed": "sleep_analysis_inbed",
}

# HKCategoryTypeIdentifier -> (event_type, category, display name); mirrors the iPhone app's catalog.
CATEGORY_TYPES: dict[str, tuple[str, str, str]] = {
    "HKCategoryTypeIdentifierMindfulSession": ("mindful_session", "mindfulness", "Mindful Minutes"),
    "HKCategoryTypeIdentifierHighHeartRateEvent": ("high_heart_rate_event", "events", "High Heart Rate Notification"),
    "HKCategoryTypeIdentifierLowHeartRateEvent": ("low_heart_rate_event", "events", "Low Heart Rate Notification"),
    "HKCategoryTypeIdentifierIrregularHeartRhythmEvent": ("irregular_heart_rhythm_event", "events", "Irregular Rhythm Notification"),
    "HKCategoryTypeIdentifierLowCardioFitnessEvent": ("low_cardio_fitness_event", "events", "Low Cardio Fitness Notification"),
    "HKCategoryTypeIdentifierAppleWalkingSteadinessEvent": ("walking_steadiness_event", "events", "Walking Steadiness Notification"),
    "HKCategoryTypeIdentifierEnvironmentalAudioExposureEvent": ("environmental_audio_exposure_event", "events", "Loud Environment Notification"),
    "HKCategoryTypeIdentifierHeadphoneAudioExposureEvent": ("headphone_audio_exposure_event", "events", "Headphone Audio Notification"),
    "HKCategoryTypeIdentifierSleepApneaEvent": ("sleep_apnea_event", "events", "Breathing Disturbances Notification"),
    "HKCategoryTypeIdentifierAppleStandHour": ("apple_stand_hour", "activity", "Stand Hours"),
    "HKCategoryTypeIdentifierHandwashingEvent": ("handwashing_event", "other", "Handwashing"),
    "HKCategoryTypeIdentifierToothbrushingEvent": ("toothbrushing_event", "other", "Toothbrushing"),
    "HKCategoryTypeIdentifierMenstrualFlow": ("menstrual_flow", "cycle", "Menstrual Flow"),
    "HKCategoryTypeIdentifierIntermenstrualBleeding": ("intermenstrual_bleeding", "cycle", "Spotting"),
    "HKCategoryTypeIdentifierOvulationTestResult": ("ovulation_test_result", "cycle", "Ovulation Test"),
    "HKCategoryTypeIdentifierCervicalMucusQuality": ("cervical_mucus_quality", "cycle", "Cervical Mucus Quality"),
    "HKCategoryTypeIdentifierSexualActivity": ("sexual_activity", "cycle", "Sexual Activity"),
    "HKCategoryTypeIdentifierContraceptive": ("contraceptive", "cycle", "Contraceptives"),
    "HKCategoryTypeIdentifierPregnancy": ("pregnancy", "cycle", "Pregnancy"),
    "HKCategoryTypeIdentifierLactation": ("lactation", "cycle", "Lactation"),
    "HKCategoryTypeIdentifierPregnancyTestResult": ("pregnancy_test_result", "cycle", "Pregnancy Test"),
    "HKCategoryTypeIdentifierProgesteroneTestResult": ("progesterone_test_result", "cycle", "Progesterone Test"),
    "HKCategoryTypeIdentifierPersistentIntermenstrualBleeding": ("persistent_intermenstrual_bleeding", "cycle", "Persistent Spotting"),
    "HKCategoryTypeIdentifierProlongedMenstrualPeriods": ("prolonged_menstrual_periods", "cycle", "Prolonged Periods"),
    "HKCategoryTypeIdentifierIrregularMenstrualCycles": ("irregular_menstrual_cycles", "cycle", "Irregular Cycles"),
    "HKCategoryTypeIdentifierInfrequentMenstrualCycles": ("infrequent_menstrual_cycles", "cycle", "Infrequent Periods"),
    "HKCategoryTypeIdentifierBleedingDuringPregnancy": ("bleeding_during_pregnancy", "cycle", "Bleeding During Pregnancy"),
    "HKCategoryTypeIdentifierBleedingAfterPregnancy": ("bleeding_after_pregnancy", "cycle", "Bleeding After Pregnancy"),
    "HKCategoryTypeIdentifierAbdominalCramps": ("abdominal_cramps", "symptoms", "Abdominal Cramps"),
    "HKCategoryTypeIdentifierAcne": ("acne", "symptoms", "Acne"),
    "HKCategoryTypeIdentifierAppetiteChanges": ("appetite_changes", "symptoms", "Appetite Changes"),
    "HKCategoryTypeIdentifierBladderIncontinence": ("bladder_incontinence", "symptoms", "Bladder Incontinence"),
    "HKCategoryTypeIdentifierBloating": ("bloating", "symptoms", "Bloating"),
    "HKCategoryTypeIdentifierBreastPain": ("breast_pain", "symptoms", "Breast Pain"),
    "HKCategoryTypeIdentifierChestTightnessOrPain": ("chest_tightness_or_pain", "symptoms", "Chest Tightness or Pain"),
    "HKCategoryTypeIdentifierChills": ("chills", "symptoms", "Chills"),
    "HKCategoryTypeIdentifierConstipation": ("constipation", "symptoms", "Constipation"),
    "HKCategoryTypeIdentifierCoughing": ("coughing", "symptoms", "Coughing"),
    "HKCategoryTypeIdentifierDiarrhea": ("diarrhea", "symptoms", "Diarrhea"),
    "HKCategoryTypeIdentifierDizziness": ("dizziness", "symptoms", "Dizziness"),
    "HKCategoryTypeIdentifierDrySkin": ("dry_skin", "symptoms", "Dry Skin"),
    "HKCategoryTypeIdentifierFainting": ("fainting", "symptoms", "Fainting"),
    "HKCategoryTypeIdentifierFatigue": ("fatigue", "symptoms", "Fatigue"),
    "HKCategoryTypeIdentifierFever": ("fever", "symptoms", "Fever"),
    "HKCategoryTypeIdentifierGeneralizedBodyAche": ("generalized_body_ache", "symptoms", "Body and Muscle Ache"),
    "HKCategoryTypeIdentifierHairLoss": ("hair_loss", "symptoms", "Hair Loss"),
    "HKCategoryTypeIdentifierHeadache": ("headache", "symptoms", "Headache"),
    "HKCategoryTypeIdentifierHeartburn": ("heartburn", "symptoms", "Heartburn"),
    "HKCategoryTypeIdentifierHotFlashes": ("hot_flashes", "symptoms", "Hot Flashes"),
    "HKCategoryTypeIdentifierLossOfSmell": ("loss_of_smell", "symptoms", "Loss of Smell"),
    "HKCategoryTypeIdentifierLossOfTaste": ("loss_of_taste", "symptoms", "Loss of Taste"),
    "HKCategoryTypeIdentifierLowerBackPain": ("lower_back_pain", "symptoms", "Lower Back Pain"),
    "HKCategoryTypeIdentifierMemoryLapse": ("memory_lapse", "symptoms", "Memory Lapse"),
    "HKCategoryTypeIdentifierMoodChanges": ("mood_changes", "symptoms", "Mood Changes"),
    "HKCategoryTypeIdentifierNausea": ("nausea", "symptoms", "Nausea"),
    "HKCategoryTypeIdentifierNightSweats": ("night_sweats", "symptoms", "Night Sweats"),
    "HKCategoryTypeIdentifierPelvicPain": ("pelvic_pain", "symptoms", "Pelvic Pain"),
    "HKCategoryTypeIdentifierRapidPoundingOrFlutteringHeartbeat": ("rapid_pounding_or_fluttering_heartbeat", "symptoms", "Rapid, Pounding, or Fluttering Heartbeat"),
    "HKCategoryTypeIdentifierRunnyNose": ("runny_nose", "symptoms", "Runny Nose"),
    "HKCategoryTypeIdentifierShortnessOfBreath": ("shortness_of_breath", "symptoms", "Shortness of Breath"),
    "HKCategoryTypeIdentifierSinusCongestion": ("sinus_congestion", "symptoms", "Sinus Congestion"),
    "HKCategoryTypeIdentifierSkippedHeartbeat": ("skipped_heartbeat", "symptoms", "Skipped Heartbeat"),
    "HKCategoryTypeIdentifierSleepChanges": ("sleep_changes", "symptoms", "Sleep Changes"),
    "HKCategoryTypeIdentifierSoreThroat": ("sore_throat", "symptoms", "Sore Throat"),
    "HKCategoryTypeIdentifierVaginalDryness": ("vaginal_dryness", "symptoms", "Vaginal Dryness"),
    "HKCategoryTypeIdentifierVomiting": ("vomiting", "symptoms", "Vomiting"),
    "HKCategoryTypeIdentifierWheezing": ("wheezing", "symptoms", "Wheezing"),
}

_VALUE_PREFIXES = ("Severity", "MenstrualFlow", "OvulationTestResult", "CervicalMucusQuality", "AppleStandHour",
                   "Contraceptive", "PregnancyTestResult", "ProgesteroneTestResult", "AppetiteChanges", "Presence",
                   "LowCardioFitnessEvent", "AppleWalkingSteadinessEvent", "EnvironmentalAudioExposureEvent",
                   "HeadphoneAudioExposureEvent")


def category_label(value: str) -> str:
    """'HKCategoryValueSeverityModerate' -> 'Moderate'; 'HKCategoryValueNotApplicable' -> 'Occurred'."""
    v = value.removeprefix("HKCategoryValue")
    if v in ("", "NotApplicable"):
        return "Occurred"
    for prefix in _VALUE_PREFIXES:
        if v.startswith(prefix) and len(v) > len(prefix):
            v = v[len(prefix):]
            break
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", v)


def workout_name(activity_type: str) -> str:
    """'HKWorkoutActivityTypeTraditionalStrengthTraining' -> 'Traditional Strength Training'."""
    v = activity_type.removeprefix("HKWorkoutActivityType") or "Workout"
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", v)


# Export unit -> (factor, canonical unit) per metric family
_DISTANCE_M = {"m": 1.0, "km": 1000.0, "mi": 1609.344, "ft": 0.3048, "yd": 0.9144, "cm": 0.01}
_MASS_KG = {"kg": 1.0, "lb": 0.45359237, "g": 0.001, "st": 6.35029}
_ENERGY_KCAL = {"kcal": 1.0, "Cal": 1.0, "kJ": 1 / 4.184, "cal": 0.001}
_SPEED_MS = {"m/s": 1.0, "km/hr": 1 / 3.6, "mi/hr": 0.44704}
_LENGTH_CM = {"cm": 1.0, "in": 2.54, "m": 100.0}


def convert_unit(metric: str, value: float, unit: str) -> tuple[float, str]:
    if metric in ("distance_walking_running", "six_minute_walk_distance"):
        return value * _DISTANCE_M.get(unit, 1.0), "m"
    if metric in ("body_mass", "lean_body_mass"):
        return value * _MASS_KG.get(unit, 1.0), "kg"
    if metric in ("active_energy", "basal_energy"):
        return value * _ENERGY_KCAL.get(unit, 1.0), "kcal"
    if metric in ("walking_speed", "stair_ascent_speed", "stair_descent_speed"):
        return value * _SPEED_MS.get(unit, 1.0), "m/s"
    if metric == "walking_step_length":
        return value * _LENGTH_CM.get(unit, 1.0), "cm"
    if metric == "body_temperature" and unit == "degF":
        return (value - 32) * 5 / 9, "°C"
    if metric == "blood_glucose" and unit == "mmol<180.1558800000541>/L":
        return value * 18.016, "mg/dL"
    if metric in ("apple_exercise_time", "apple_stand_time") and unit == "s":
        return value / 60, "min"
    return value, unit


def _parse_date(value: str) -> str:
    dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S %z")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sample_id(*parts: str) -> str:
    return "ah-" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:24]


def _quantity_text(value: Optional[str]) -> tuple[Optional[float], str]:
    """Metadata quantities are strings such as '1234 cm' or '68.5 degF'."""
    if not value:
        return None, ""
    parts = value.strip().split(" ", 1)
    try:
        return float(parts[0]), (parts[1] if len(parts) > 1 else "")
    except ValueError:
        return None, ""


def _float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def parse_gpx(data: bytes) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    try:
        for _event, el in iterparse(io.BytesIO(data), events=("end",)):
            if el.tag.endswith("trkpt"):
                lat, lon = _float(el.get("lat")), _float(el.get("lon"))
                if lat is not None and lon is not None:
                    point: dict[str, Any] = {"lat": lat, "lon": lon, "alt": None, "t": None, "speed": None}
                    for child in el.iter():
                        tag = child.tag.rsplit("}", 1)[-1]
                        if tag == "ele":
                            point["alt"] = _float(child.text)
                        elif tag == "time":
                            point["t"] = child.text
                        elif tag == "speed":
                            point["speed"] = _float(child.text)
                    points.append(point)
                el.clear()
    except Exception:  # noqa: BLE001 - a damaged route file shouldn't fail the import
        return points
    return points


def _workout(elem: Any, start: str, end: str, source: str, read_file: Optional[Callable[[str], Optional[bytes]]]) -> dict[str, Any]:
    a = elem.attrib
    meta: dict[str, str] = {}
    stats: dict[str, dict[str, str]] = {}
    route_paths: list[str] = []
    for child in elem:
        if child.tag == "MetadataEntry":
            meta[child.get("key", "")] = child.get("value", "")
        elif child.tag == "WorkoutStatistics":
            stats[child.get("type", "")] = dict(child.attrib)
        elif child.tag == "WorkoutRoute":
            route_paths += [f.get("path", "") for f in child if f.tag == "FileReference" and f.get("path")]

    def stat(name: str, field: str, metric: Optional[str] = None) -> Optional[float]:
        st = stats.get("HKQuantityTypeIdentifier" + name)
        v = _float(st.get(field)) if st else None
        if v is not None and metric:
            v = convert_unit(metric, v, st.get("unit", ""))[0]  # type: ignore[union-attr]
        return v

    duration = _float(a.get("duration"))
    if duration is not None:
        duration *= {"min": 60, "s": 1, "hr": 3600, "h": 3600}.get(a.get("durationUnit", "min"), 60)
    distance = next((d for d in (stat(n, "sum", "distance_walking_running") for n in (
        "DistanceWalkingRunning", "DistanceCycling", "DistanceSwimming", "DistanceWheelchair", "DistanceDownhillSnowSports"))
        if d), None)
    if distance is None and _float(a.get("totalDistance")):
        distance = convert_unit("distance_walking_running", float(a["totalDistance"]), a.get("totalDistanceUnit", "km"))[0]
    active = stat("ActiveEnergyBurned", "sum", "active_energy")
    if active is None and _float(a.get("totalEnergyBurned")):
        active = convert_unit("active_energy", float(a["totalEnergyBurned"]), a.get("totalEnergyBurnedUnit", "kcal"))[0]
    basal = stat("BasalEnergyBurned", "sum", "active_energy")
    ascent, ascent_unit = _quantity_text(meta.get("HKElevationAscended"))
    descent, descent_unit = _quantity_text(meta.get("HKElevationDescended"))
    temp, temp_unit = _quantity_text(meta.get("HKWeatherTemperature"))
    humidity, _ = _quantity_text(meta.get("HKWeatherHumidity"))
    if temp is not None and temp_unit.lower() in ("degf", "°f"):
        temp = (temp - 32) * 5 / 9
    if humidity is not None and humidity > 100:
        humidity /= 100  # stored as hundredths of a percent in some exports

    route: list[dict[str, Any]] = []
    if read_file:
        for path in route_paths:
            data = read_file(path)
            if data:
                route += parse_gpx(data)
    indoor = meta.get("HKIndoorWorkout")
    return {
        "id": _sample_id("workout", source, start, end),
        "activity_type": None, "name": workout_name(a.get("workoutActivityType", "")), "start": start, "end": end,
        "duration_s": duration, "active_energy_kcal": active, "total_energy_kcal": (active + basal) if active and basal else None,
        "distance_m": distance, "step_count": stat("StepCount", "sum"),
        "avg_hr": stat("HeartRate", "average"), "max_hr": stat("HeartRate", "maximum"), "min_hr": stat("HeartRate", "minimum"),
        "elevation_ascent_m": ascent / 100 if ascent is not None and ascent_unit == "cm" else ascent,
        "elevation_descent_m": descent / 100 if descent is not None and descent_unit == "cm" else descent,
        "indoor": None if indoor is None else indoor == "1", "temperature_c": temp, "humidity_pct": humidity,
        "source_name": source, "device_name": None,
        "metadata": {k.removeprefix("HK"): v for k, v in meta.items()} or None, "heart_rate": [], "route": route,
    }


def iter_items(xml_stream: io.BufferedIOBase,
               read_file: Optional[Callable[[str], Optional[bytes]]] = None) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yields ("sample" | "workout" | "event", item) from export.xml in constant memory. What's held at any moment is one
    of its top-level elements, so what that can be is limited; past that, TooBig (what came before it has been yielded)."""
    stream = _CountedRead(xml_stream)
    root = None
    depth = parts = 0
    for event, elem in iterparse(stream, events=("start", "end")):
        if event == "start":
            if root is None:
                root = elem
            depth += 1
            if depth > MAX_XML_DEPTH:
                raise _element_too_big()
            continue
        depth -= 1
        parts += 1
        if parts > MAX_XML_PARTS:
            raise _element_too_big()
        if depth == 1:
            # Cleared elements stay attached to the root; drop them or millions of records add up to gigabytes.
            root.clear()
            stream.since_element = parts = 0
        tag = elem.tag
        if tag not in ("Record", "Workout"):
            if tag in ("ActivitySummary", "Correlation"):
                elem.clear()
            continue
        a = elem.attrib
        try:
            start, end = _parse_date(a["startDate"]), _parse_date(a.get("endDate") or a["startDate"])
        except (KeyError, ValueError):
            elem.clear()
            continue
        source = a.get("sourceName") or "Apple Health"
        if tag == "Workout":
            yield "workout", _workout(elem, start, end, source, read_file)
            elem.clear()
            continue
        rtype = a.get("type", "")
        if rtype in QUANTITY_TYPES:
            metric = QUANTITY_TYPES[rtype]
            try:
                raw = float(a.get("value", ""))
            except ValueError:
                elem.clear()
                continue
            value, unit = convert_unit(metric, raw, a.get("unit", ""))
            yield "sample", {"id": _sample_id(rtype, source, start, end, a.get("value", "")), "metric_type": metric,
                             "hk_identifier": rtype, "value": value, "unit": unit, "start_date": start, "end_date": end,
                             "source_name": source, "device_name": source}
        elif rtype == "HKCategoryTypeIdentifierSleepAnalysis" and a.get("value") in SLEEP_VALUES:
            s = datetime.fromisoformat(start.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end.replace("Z", "+00:00"))
            yield "sample", {"id": _sample_id(rtype, source, start, end, a["value"]), "metric_type": SLEEP_VALUES[a["value"]],
                             "hk_identifier": rtype, "value": (e - s).total_seconds(), "unit": "seconds",
                             "start_date": start, "end_date": end, "source_name": source, "device_name": source}
        elif rtype in CATEGORY_TYPES:
            event_type, category, name = CATEGORY_TYPES[rtype]
            label = category_label(a.get("value", ""))
            yield "event", {"id": _sample_id(rtype, source, start, end, a.get("value", "")), "event_type": event_type,
                            "category": category, "name": name, "start": start, "end": end, "value": None,
                            "value_label": label, "source_name": source, "metadata": None}
        elem.clear()


# What an export's files may be. Real ones are far below these; a booby-trapped "export" isn't. The sizes are what each file
# says it holds, which reading can't go past. Deflate shrinks these formats 5 to 30 times, and a file claiming far more
# is built to fill memory.
MAX_CLINICAL_FILE = 50 * 1024 * 1024
MAX_CLINICAL_TOTAL = 256 * 1024 * 1024        # all kept in memory until they're imported
MAX_ROUTE_FILE = 100 * 1024 * 1024
MAX_XML = 64 * 1024 ** 3
MAX_XML_ELEMENT = 64 * 1024 * 1024            # one Record or Workout, with all it holds and the text after it
MAX_XML_PARTS = 1_000_000                     # the same, in elements: a second-by-second route of 11 days
MAX_XML_DEPTH = 32                            # real exports go 4 deep
MAX_RATIO = 200
_RATIO_FROM = 1024 * 1024                     # tiny files can shrink by any amount


class TooBig(ValueError):
    pass


def _element_too_big() -> TooBig:
    return TooBig("This file doesn't look like an Apple Health export: part of its export.xml is far larger than any "
                  "health record. The import stopped there.")


class _CountedRead:
    """The parser hands nothing back while it's inside one long text or value, and holds all of it: this is what notices."""

    def __init__(self, stream: io.BufferedIOBase):
        self.stream = stream
        self.since_element = 0

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.since_element += len(data)
        if self.since_element > MAX_XML_ELEMENT:
            raise _element_too_big()
        return data


def _implausible(info: zipfile.ZipInfo) -> bool:
    return info.file_size > _RATIO_FROM and info.file_size > max(info.compress_size, 1) * MAX_RATIO


class ExportArchive:
    """An Apple Health upload (the export's zip, or its export.xml alone), open while it's imported. Close it before
    deleting the file: Windows won't delete one that's still open."""

    def __init__(self, path: Path):
        self.path = path
        self.zip = zipfile.ZipFile(path) if zipfile.is_zipfile(path) else None
        self.skipped: list[str] = []

    def close(self) -> None:
        if self.zip:
            self.zip.close()

    def xml_opener(self) -> Optional[Callable[[], io.BufferedIOBase]]:
        """Read as a stream, so its size doesn't matter to memory; a lie about its size can still only fill the disk."""
        if not self.zip:
            return lambda: open(self.path, "rb")
        info = next((i for i in self.zip.infolist() if i.filename.endswith("/export.xml") or i.filename == "export.xml"), None)
        if not info:
            return None
        if info.file_size > MAX_XML or _implausible(info):
            raise TooBig("This file doesn't look like an Apple Health export: its export.xml is built to expand far beyond "
                         "what it holds. Nothing was imported.")
        return lambda: self.zip.open(info)  # type: ignore[union-attr,return-value]

    def clinical_records(self) -> list[dict[str, Any]]:
        """The FHIR resources in the export's clinical-records folder. One too large to be real is left out and noted."""
        if not self.zip:
            return []
        out: list[dict[str, Any]] = []
        total = 0
        for info in self.zip.infolist():
            name = info.filename
            if "/clinical-records/" not in name or not name.endswith(".json"):
                continue
            total += info.file_size
            if info.file_size > MAX_CLINICAL_FILE or total > MAX_CLINICAL_TOTAL or _implausible(info):
                self.skipped.append(name.rsplit("/", 1)[-1][:100])
                total -= info.file_size
                continue
            try:
                res = json.loads(self.zip.read(info))
            except (ValueError, zipfile.BadZipFile):
                continue
            if isinstance(res, dict) and res.get("resourceType"):
                out.append(res)
        return out

    def route_reader(self) -> Optional[Callable[[str], Optional[bytes]]]:
        """Reads files referenced from export.xml (e.g. '/workout-routes/route_x.gpx') out of the zip."""
        if not self.zip:
            return None
        infos = self.zip.infolist()

        def read(ref: str) -> Optional[bytes]:
            ref = ref.lstrip("/")
            info = next((i for i in infos if i.filename == ref or i.filename.endswith("/" + ref)), None)
            if not info:
                return None
            if info.file_size > MAX_ROUTE_FILE or _implausible(info):
                self.skipped.append(info.filename.rsplit("/", 1)[-1][:100])
                return None
            return self.zip.read(info)  # type: ignore[union-attr]
        return read
