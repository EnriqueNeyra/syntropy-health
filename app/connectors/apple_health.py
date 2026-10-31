"""
Apple Health export importer.

Streams ``export.xml`` (from Health app → profile → Export All Health Data) with
``iterparse`` so multi-gigabyte exports import in constant memory. Records are
mapped to the same metric identifiers the iOS companion app uses, so data from
both paths de-duplicates. If the export contains ``clinical-records/*.json`` (Health
Records), those FHIR resources are returned for import as well.

The offset in an export's dates isn't kept. It's reported to be the phone's zone when the file was made rather than where the
reading was taken, which would pin years of history to the wrong place. A record's own HKTimeZone is used when it has one.
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

from app.core import private_files
from xml.etree.ElementTree import iterparse
from zoneinfo import ZoneInfo

from app.store.metric_registry import QUANTITY_TYPES, to_canonical

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


def convert_unit(metric: str, value: float, unit: str) -> tuple[float, str]:
    """``value`` in the metric's own unit, for a workout's figures. A unit that isn't recognised stays as it is."""
    converted, label, _ = to_canonical(metric, value, unit)
    return converted, label


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


def parse_gpx(data: bytes, room: Optional[int] = None) -> list[dict[str, Any]]:
    """Up to ``room`` points. Only the point being read is kept while reading, so what the file holds besides doesn't add up."""
    room = MAX_ROUTE_POINTS if room is None else room
    points: list[dict[str, Any]] = []
    within: list[Any] = []
    current = None
    parts = 0
    try:
        for event, el in iterparse(io.BytesIO(data), events=("start", "end")):
            if event == "start":
                within.append(el)
                if len(within) > MAX_XML_DEPTH:
                    break
                if current is None and el.tag.endswith("trkpt"):
                    current = el
                continue
            within.pop()
            if current is not None and el is not current:
                parts += 1
                if parts > MAX_POINT_PARTS:
                    break
                continue
            if el is current:
                current, parts = None, 0
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
                    if len(points) >= room:
                        break
                el.clear()
            if within:
                within[-1].remove(el)
    except Exception:  # noqa: BLE001 - a damaged route file shouldn't fail the import
        return points
    return points


def _elevation_m(value: Optional[float], unit: str) -> Optional[float]:
    if value is None:
        return None
    return convert_unit("six_minute_walk_distance", value, unit)[0]


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
            if len(route) >= MAX_ROUTE_POINTS:
                break
            data = read_file(path)
            if data:
                route += parse_gpx(data, MAX_ROUTE_POINTS - len(route))
    indoor = meta.get("HKIndoorWorkout")
    return {
        "id": _sample_id("workout", source, start, end),
        "activity_type": None, "name": workout_name(a.get("workoutActivityType", "")), "start": start, "end": end,
        "duration_s": duration, "active_energy_kcal": active, "total_energy_kcal": (active + basal) if active and basal else None,
        "distance_m": distance, "step_count": stat("StepCount", "sum"),
        "avg_hr": stat("HeartRate", "average"), "max_hr": stat("HeartRate", "maximum"), "min_hr": stat("HeartRate", "minimum"),
        "elevation_ascent_m": _elevation_m(ascent, ascent_unit),
        "elevation_descent_m": _elevation_m(descent, descent_unit),
        "indoor": None if indoor is None else indoor == "1", "temperature_c": temp, "humidity_pct": humidity,
        "source_name": source, "device_name": None,
        "metadata": {k.removeprefix("HK"): v for k, v in meta.items()} or None, "heart_rate": [], "route": route,
    }


# Control characters XML 1.0 doesn't allow, which Apple's exports have been seen to carry. On UTF-8 they can be taken out byte
# by byte, since the bytes of a longer character are all 0x80 or above.
_NOT_XML = bytes(b for b in range(0x20) if b not in (0x09, 0x0A, 0x0D))
_DOCTYPE_SEARCH = 4 * 1024 * 1024


class CleanStream:
    """Reads an export.xml around two known Apple faults: the document type block, which iOS 16 exports wrote malformed, and
    stray control characters. What else goes wrong in a file ends the read where it happens (see imports)."""

    def __init__(self, raw: Any):
        self._raw = raw
        self._started = False

    def _start(self) -> bytes:
        head = self._raw.read(64 * 1024)
        at = head.find(b"<!DOCTYPE")
        if at < 0:
            return head
        # The block's end can't be trusted when the block is what's broken, so it ends where the document starts.
        while (root := head.find(b"<HealthData", at)) < 0 and len(head) < _DOCTYPE_SEARCH:
            more = self._raw.read(64 * 1024)
            if not more:
                break
            head += more
        return head[:at] + head[root:] if root >= 0 else head

    def read(self, size: int = -1) -> bytes:
        chunk = self._raw.read(size) if self._started else self._start()
        self._started = True
        return chunk.translate(None, _NOT_XML)


def _record_offset(elem: Any, start: str) -> Optional[int]:
    for child in elem:
        if child.tag == "MetadataEntry" and child.get("key") == "HKTimeZone":
            try:
                delta = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(ZoneInfo(child.get("value", ""))).utcoffset()
            except Exception:  # noqa: BLE001 - someone else's text, not necessarily a zone
                return None
            return int(delta.total_seconds() // 60) if delta is not None else None
    return None


def iter_items(xml_stream: Any, read_file: Optional[Callable[[str], Optional[bytes]]] = None,
               tally: Optional[dict[str, dict[str, int]]] = None) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yields ("sample" | "workout" | "event", item) from export.xml in constant memory. ``tally`` counts the records of each
    type imported and left aside. What's held at any moment is one of its top-level elements, so what that can be is limited;
    past that, TooBig (what came before it has been yielded)."""
    stream = _CountedRead(xml_stream)

    def count(which: str, record_type: str) -> None:
        if tally is not None:
            tally[which][record_type] = tally[which].get(record_type, 0) + 1

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
            count("imported", rtype)
            sample = {"id": _sample_id(rtype, source, start, end, a.get("value", "")), "metric_type": metric,
                      "hk_identifier": rtype, "value": raw, "unit": a.get("unit", ""), "start_date": start, "end_date": end,
                      "source_name": source, "device_name": a.get("device") and _device_label(a["device"]) or source, "strict_units": True}
            offset = _record_offset(elem, start)
            if offset is not None:
                sample["tz_offset_min"] = offset
            yield "sample", sample
        elif rtype == "HKCategoryTypeIdentifierSleepAnalysis" and a.get("value") in SLEEP_VALUES:
            s = datetime.fromisoformat(start.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end.replace("Z", "+00:00"))
            count("imported", rtype)
            yield "sample", {"id": _sample_id(rtype, source, start, end, a["value"]), "metric_type": SLEEP_VALUES[a["value"]],
                             "hk_identifier": rtype, "value": (e - s).total_seconds(), "unit": "seconds",
                             "start_date": start, "end_date": end, "source_name": source, "device_name": source}
        elif rtype in CATEGORY_TYPES:
            event_type, category, name = CATEGORY_TYPES[rtype]
            label = category_label(a.get("value", ""))
            count("imported", rtype)
            yield "event", {"id": _sample_id(rtype, source, start, end, a.get("value", "")), "event_type": event_type,
                            "category": category, "name": name, "start": start, "end": end, "value": None,
                            "value_label": label, "source_name": source, "metadata": None}
        else:
            count("skipped", rtype or "unknown")
        elem.clear()


_DEVICE_NAME = re.compile(r"\bname:([^,>]+)")


def _device_label(device: str) -> Optional[str]:
    """The record's device attribute reads like "<<HKDevice: 0x...>, name:Apple Watch, manufacturer:Apple Inc., ...>"."""
    match = _DEVICE_NAME.search(device)
    return match.group(1).strip() if match else None


# What an export's files may be. Real ones are far below these; a booby-trapped "export" isn't. The sizes are what each file
# says it holds, which reading can't go past. Deflate shrinks these formats 5 to 30 times, and a file claiming far more
# is built to fill memory.
MAX_CLINICAL_FILE = 50 * 1024 * 1024
MAX_CLINICAL_TOTAL = 256 * 1024 * 1024        # all kept in memory until they're imported
MAX_CLINICAL_PARTS = 5_000_000                # the same, in what memory goes on: tens of thousands of real records
MAX_ROUTE_FILE = 100 * 1024 * 1024
MAX_ROUTE_POINTS = 500_000                    # for one workout: nearly six days, second by second
MAX_POINT_PARTS = 100
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
        self._file = private_files.open_read(path)
        self.zip = zipfile.ZipFile(self._file) if zipfile.is_zipfile(self._file) else None
        self.skipped: list[str] = []

    def close(self) -> None:
        if self.zip:
            self.zip.close()
        self._file.close()

    def xml_opener(self) -> Optional[Callable[[], io.BufferedIOBase]]:
        """Read as a stream, so its size doesn't matter to memory; a lie about its size can still only fill the disk."""
        if not self.zip:
            with private_files.open_read(self.path) as fh:
                head = fh.read(512).lstrip(b"\xef\xbb\xbf \t\r\n")
            if head.startswith(b"PK"):
                raise ValueError("This zip file is incomplete. Copy it again, or export from the Health app again.")
            if not head.startswith(b"<"):
                raise ValueError("This isn't an Apple Health export. It should be the export.zip the Health app makes, or the export.xml inside it.")
            return lambda: private_files.open_read(self.path)  # type: ignore[return-value]
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
        total = parts = 0
        for info in self.zip.infolist():
            name = info.filename
            if "/clinical-records/" not in name or not name.endswith(".json"):
                continue
            noted = name.rsplit("/", 1)[-1][:100]
            if info.file_size > MAX_CLINICAL_FILE or total + info.file_size > MAX_CLINICAL_TOTAL or _implausible(info):
                self.skipped.append(noted)
                continue
            try:
                raw = self.zip.read(info)
                held = raw.count(b",") + raw.count(b"{") + raw.count(b"[")
                if parts + held > MAX_CLINICAL_PARTS:
                    self.skipped.append(noted)
                    continue
                res = json.loads(raw)
            except (ValueError, RecursionError, zipfile.BadZipFile):
                continue
            total += info.file_size
            parts += held
            if isinstance(res, dict) and res.get("resourceType"):
                out.append(res)
        return out

    def route_reader(self) -> Optional[Callable[[str], Optional[bytes]]]:
        """Reads files referenced from export.xml (e.g. '/workout-routes/route_x.gpx') out of the zip."""
        if not self.zip:
            return None
        infos = self.zip.infolist()
        read_already: set[str] = set()

        def read(ref: str) -> Optional[bytes]:
            ref = ref.lstrip("/")
            info = next((i for i in infos if i.filename == ref or i.filename.endswith("/" + ref)), None)
            # A route is one workout's: read for every mention, one small file could fill any amount of memory and disk.
            if not info or info.filename in read_already:
                return None
            read_already.add(info.filename)
            if info.file_size > MAX_ROUTE_FILE or _implausible(info):
                self.skipped.append(info.filename.rsplit("/", 1)[-1][:100])
                return None
            return self.zip.read(info)  # type: ignore[union-attr]
        return read
