"""
Every kind of measurement the wearable paths know: how it's shown, how a day of it is worked out, which units it can
arrive in, and what range a real reading can fall in. Phones, imports and connectors all go through this one table.

Ranges are what's physically possible, deliberately wide: a real oxygen saturation in the 60s or a heart rate of 200 is
exactly what a health record must not hide. A reading outside its range is kept with a flag, left out of the daily
numbers, and counted (see ``biometrics.normalize_sample``).

``hk`` is the HealthKit name (HKQuantityTypeIdentifier…), the same list the iPhone app sends.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

from app.connectors import units


class Metric(NamedTuple):
    label: str
    unit: str
    aggregation: str        # sum, mean or last
    group: str
    family: Optional[str] = None                    # units.FAMILIES: None means the unit isn't checked
    plausible: Optional[tuple[float, float]] = None
    hk: Optional[str] = None


_HR = (20, 300)
_PCT = (0, 100)
_SCORE = (0, 100)
_TEMP = (20, 46)
_MG = (0, 100_000)
_MCG = (0, 10_000_000)
_G = (0, 5000)
_DAY_MIN = (0, 1500)
_SPEED = (0, 50)
_POWER = (0, 3000)
_DB = (0, 200)
_LONG_M = (0, 1_000_000)

REGISTRY: dict[str, Metric] = {
    "step_count": Metric("Steps", "steps", "sum", "activity", None, (0, 300_000), "StepCount"),
    "active_energy": Metric("Active energy", "kcal", "sum", "activity", "energy", (0, 30_000), "ActiveEnergyBurned"),
    "basal_energy": Metric("Resting energy", "kcal", "sum", "activity", "energy", (0, 15_000), "BasalEnergyBurned"),
    "total_energy": Metric("Total energy", "kcal", "sum", "activity", "energy", (0, 40_000)),
    "distance_walking_running": Metric("Walking + running distance", "m", "sum", "activity", "metres", _LONG_M, "DistanceWalkingRunning"),
    "flights_climbed": Metric("Flights climbed", "flights", "sum", "activity", None, (0, 2000), "FlightsClimbed"),
    "apple_exercise_time": Metric("Exercise minutes", "min", "sum", "activity", "minutes", _DAY_MIN, "AppleExerciseTime"),
    "apple_stand_time": Metric("Stand minutes", "min", "sum", "activity", "minutes", _DAY_MIN, "AppleStandTime"),
    "activity_score": Metric("Activity score", "score", "last", "activity", None, _SCORE),
    "strain_score": Metric("Strain", "score", "last", "activity", None, (0, 25)),
    "vo2_max": Metric("VO₂ max", "mL/kg·min", "last", "activity", None, (5, 120), "VO2Max"),
    "heart_rate": Metric("Heart rate", "bpm", "mean", "heart", "rate", _HR, "HeartRate"),
    "resting_heart_rate": Metric("Resting heart rate", "bpm", "mean", "heart", "rate", (20, 220), "RestingHeartRate"),
    "walking_heart_rate_average": Metric("Walking heart rate", "bpm", "mean", "heart", "rate", _HR, "WalkingHeartRateAverage"),
    "heart_rate_recovery_one_minute": Metric("Heart rate recovery", "bpm", "mean", "heart", "rate", (0, 250), "HeartRateRecoveryOneMinute"),
    "hrv_sdnn": Metric("HRV (SDNN)", "ms", "mean", "heart", None, (0, 2000), "HeartRateVariabilitySDNN"),
    "hrv_rmssd": Metric("HRV (RMSSD)", "ms", "mean", "heart", None, (0, 2000)),
    "recovery_score": Metric("Recovery", "score", "last", "readiness", None, _SCORE),
    "readiness_score": Metric("Readiness", "score", "last", "readiness", None, _SCORE),
    "sleep_duration": Metric("Sleep", "h", "sum", "sleep"),
    "sleep_deep": Metric("Deep sleep", "h", "sum", "sleep"),
    "sleep_rem": Metric("REM sleep", "h", "sum", "sleep"),
    "sleep_core": Metric("Light / core sleep", "h", "sum", "sleep"),
    "sleep_awake": Metric("Awake in bed", "h", "sum", "sleep"),
    "sleep_nap": Metric("Naps", "h", "sum", "sleep"),
    "sleep_score": Metric("Sleep score", "score", "last", "sleep", None, _SCORE),
    "oxygen_saturation": Metric("Blood oxygen", "%", "mean", "vitals", None, (30, 100), "OxygenSaturation"),
    "respiratory_rate": Metric("Respiratory rate", "br/min", "mean", "vitals", "rate", (1, 150), "RespiratoryRate"),
    "body_temperature": Metric("Body temperature", "°C", "mean", "vitals", "temperature", _TEMP, "BodyTemperature"),
    "body_temperature_deviation": Metric("Temperature deviation", "°C", "last", "vitals", None, (-15, 15)),
    "skin_temperature": Metric("Skin temperature", "°C", "last", "vitals", "temperature", (10, 46)),
    "blood_pressure_systolic": Metric("Systolic BP", "mmHg", "mean", "vitals", None, (30, 350), "BloodPressureSystolic"),
    "blood_pressure_diastolic": Metric("Diastolic BP", "mmHg", "mean", "vitals", None, (10, 250), "BloodPressureDiastolic"),
    "blood_glucose": Metric("Blood glucose", "mg/dL", "mean", "vitals", "glucose", (5, 3000), "BloodGlucose"),
    "body_mass": Metric("Weight", "kg", "last", "body", "kilograms", (0.2, 700), "BodyMass"),
    "body_mass_index": Metric("BMI", "kg/m²", "last", "body", None, (5, 200), "BodyMassIndex"),
    "body_fat_percentage": Metric("Body fat", "%", "last", "body", None, _PCT, "BodyFatPercentage"),
    "lean_body_mass": Metric("Lean body mass", "kg", "last", "body", "kilograms", (0.1, 400), "LeanBodyMass"),
    "walking_speed": Metric("Walking speed", "m/s", "mean", "mobility", "speed", (0, 15), "WalkingSpeed"),
    "walking_step_length": Metric("Step length", "cm", "mean", "mobility", "centimetres", (0, 400), "WalkingStepLength"),
    "walking_asymmetry": Metric("Walking asymmetry", "%", "mean", "mobility", None, _PCT, "WalkingAsymmetryPercentage"),
    "walking_double_support": Metric("Double support time", "%", "mean", "mobility", None, _PCT, "WalkingDoubleSupportPercentage"),
    "six_minute_walk_distance": Metric("Six-minute walk", "m", "last", "mobility", "metres", (0, 3000), "SixMinuteWalkTestDistance"),
    "stair_ascent_speed": Metric("Stair ascent speed", "m/s", "mean", "mobility", "speed", (0, 15), "StairAscentSpeed"),
    "stair_descent_speed": Metric("Stair descent speed", "m/s", "mean", "mobility", "speed", (0, 15), "StairDescentSpeed"),
    "environmental_audio_exposure": Metric("Environmental noise", "dB", "mean", "hearing", None, _DB, "EnvironmentalAudioExposure"),
    "headphone_audio_exposure": Metric("Headphone audio", "dB", "mean", "hearing", None, _DB, "HeadphoneAudioExposure"),
    "environmental_sound_reduction": Metric("Noise reduction", "dB", "mean", "hearing", None, _DB, "EnvironmentalSoundReduction"),
    "apple_move_time": Metric("Move minutes", "min", "sum", "activity", "minutes", _DAY_MIN, "AppleMoveTime"),
    "time_in_daylight": Metric("Time in daylight", "min", "sum", "activity", "minutes", _DAY_MIN, "TimeInDaylight"),
    "distance_cycling": Metric("Cycling distance", "m", "sum", "activity", "metres", _LONG_M, "DistanceCycling"),
    "distance_swimming": Metric("Swimming distance", "m", "sum", "activity", "metres", _LONG_M, "DistanceSwimming"),
    "distance_wheelchair": Metric("Wheelchair distance", "m", "sum", "activity", "metres", _LONG_M, "DistanceWheelchair"),
    "distance_downhill_snow_sports": Metric("Downhill snow sports distance", "m", "sum", "activity", "metres", _LONG_M, "DistanceDownhillSnowSports"),
    "distance_cross_country_skiing": Metric("Cross-country skiing distance", "m", "sum", "activity", "metres", _LONG_M, "DistanceCrossCountrySkiing"),
    "distance_paddle_sports": Metric("Paddle sports distance", "m", "sum", "activity", "metres", _LONG_M, "DistancePaddleSports"),
    "distance_rowing": Metric("Rowing distance", "m", "sum", "activity", "metres", _LONG_M, "DistanceRowing"),
    "distance_skating_sports": Metric("Skating distance", "m", "sum", "activity", "metres", _LONG_M, "DistanceSkatingSports"),
    "swimming_stroke_count": Metric("Swimming strokes", "strokes", "sum", "activity", None, (0, 500_000), "SwimmingStrokeCount"),
    "push_count": Metric("Wheelchair pushes", "pushes", "sum", "activity", None, (0, 500_000), "PushCount"),
    "physical_effort": Metric("Physical effort", "kcal/hr·kg", "mean", "activity", None, (0, 100), "PhysicalEffort"),
    "running_speed": Metric("Running speed", "m/s", "mean", "activity", "speed", (0, 15), "RunningSpeed"),
    "running_power": Metric("Running power", "W", "mean", "activity", None, _POWER, "RunningPower"),
    "running_stride_length": Metric("Running stride length", "m", "mean", "activity", "metres", (0, 6), "RunningStrideLength"),
    "running_ground_contact_time": Metric("Ground contact time", "ms", "mean", "activity", None, (0, 5000), "RunningGroundContactTime"),
    "running_vertical_oscillation": Metric("Vertical oscillation", "cm", "mean", "activity", "centimetres", (0, 100), "RunningVerticalOscillation"),
    "cycling_speed": Metric("Cycling speed", "m/s", "mean", "activity", "speed", _SPEED, "CyclingSpeed"),
    "cycling_power": Metric("Cycling power", "W", "mean", "activity", None, _POWER, "CyclingPower"),
    "cycling_cadence": Metric("Cycling cadence", "rpm", "mean", "activity", "rate", (0, 300), "CyclingCadence"),
    "cycling_functional_threshold_power": Metric("Cycling FTP", "W", "last", "activity", None, _POWER, "CyclingFunctionalThresholdPower"),
    "underwater_depth": Metric("Underwater depth", "m", "mean", "activity", "metres", (0, 500), "UnderwaterDepth"),
    "water_temperature": Metric("Water temperature", "°C", "mean", "activity", "temperature", (-5, 60), "WaterTemperature"),
    "atrial_fibrillation_burden": Metric("AFib history", "%", "last", "heart", None, _PCT, "AtrialFibrillationBurden"),
    "peripheral_perfusion_index": Metric("Perfusion index", "%", "mean", "heart", None, _PCT, "PeripheralPerfusionIndex"),
    "sleeping_wrist_temperature": Metric("Wrist temperature (sleep)", "°C", "last", "vitals", "temperature", (10, 46), "AppleSleepingWristTemperature"),
    "basal_body_temperature": Metric("Basal body temperature", "°C", "last", "vitals", "temperature", _TEMP, "BasalBodyTemperature"),
    "sleeping_breathing_disturbances": Metric("Breathing disturbances", "count", "last", "vitals", None, (0, 2000), "AppleSleepingBreathingDisturbances"),
    "number_of_times_fallen": Metric("Falls", "count", "sum", "vitals", None, (0, 1000), "NumberOfTimesFallen"),
    "forced_vital_capacity": Metric("Forced vital capacity", "L", "last", "vitals", "litres", (0, 15), "ForcedVitalCapacity"),
    "forced_expiratory_volume_1": Metric("FEV1", "L", "last", "vitals", "litres", (0, 15), "ForcedExpiratoryVolume1"),
    "peak_expiratory_flow_rate": Metric("Peak expiratory flow", "L/min", "last", "vitals", None, (0, 1500), "PeakExpiratoryFlowRate"),
    "inhaler_usage": Metric("Inhaler usage", "count", "sum", "vitals", None, (0, 1000), "InhalerUsage"),
    "electrodermal_activity": Metric("Electrodermal activity", "µS", "mean", "vitals", None, (0, 1000), "ElectrodermalActivity"),
    "insulin_delivery": Metric("Insulin delivery", "IU", "sum", "vitals", None, (0, 2000), "InsulinDelivery"),
    "blood_alcohol_content": Metric("Blood alcohol", "%", "last", "vitals", None, _PCT, "BloodAlcoholContent"),
    "number_of_alcoholic_beverages": Metric("Alcoholic beverages", "count", "sum", "vitals", None, (0, 500), "NumberOfAlcoholicBeverages"),
    "uv_exposure": Metric("UV index", "index", "last", "vitals", None, (0, 30), "UVExposure"),
    "height": Metric("Height", "cm", "last", "body", "centimetres", (20, 280), "Height"),
    "waist_circumference": Metric("Waist circumference", "cm", "last", "body", "centimetres", (10, 400), "WaistCircumference"),
    "walking_steadiness": Metric("Walking steadiness", "%", "last", "mobility", None, _PCT, "AppleWalkingSteadiness"),
    "dietary_energy": Metric("Dietary energy", "kcal", "sum", "nutrition", "energy", (0, 60_000), "DietaryEnergyConsumed"),
    "dietary_protein": Metric("Protein", "g", "sum", "nutrition", "grams", _G, "DietaryProtein"),
    "dietary_carbohydrates": Metric("Carbohydrates", "g", "sum", "nutrition", "grams", _G, "DietaryCarbohydrates"),
    "dietary_fat_total": Metric("Total fat", "g", "sum", "nutrition", "grams", _G, "DietaryFatTotal"),
    "dietary_fat_saturated": Metric("Saturated fat", "g", "sum", "nutrition", "grams", _G, "DietaryFatSaturated"),
    "dietary_fat_monounsaturated": Metric("Monounsaturated fat", "g", "sum", "nutrition", "grams", _G, "DietaryFatMonounsaturated"),
    "dietary_fat_polyunsaturated": Metric("Polyunsaturated fat", "g", "sum", "nutrition", "grams", _G, "DietaryFatPolyunsaturated"),
    "dietary_fiber": Metric("Fiber", "g", "sum", "nutrition", "grams", _G, "DietaryFiber"),
    "dietary_sugar": Metric("Sugar", "g", "sum", "nutrition", "grams", _G, "DietarySugar"),
    "dietary_water": Metric("Water", "mL", "sum", "nutrition", "millilitres", (0, 50_000), "DietaryWater"),
    "dietary_caffeine": Metric("Caffeine", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryCaffeine"),
    "dietary_sodium": Metric("Sodium", "mg", "sum", "nutrition", "milligrams", _MG, "DietarySodium"),
    "dietary_cholesterol": Metric("Cholesterol", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryCholesterol"),
    "dietary_calcium": Metric("Calcium", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryCalcium"),
    "dietary_iron": Metric("Iron", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryIron"),
    "dietary_potassium": Metric("Potassium", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryPotassium"),
    "dietary_magnesium": Metric("Magnesium", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryMagnesium"),
    "dietary_zinc": Metric("Zinc", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryZinc"),
    "dietary_phosphorus": Metric("Phosphorus", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryPhosphorus"),
    "dietary_vitamin_a": Metric("Vitamin A", "mcg", "sum", "nutrition", "micrograms", _MCG, "DietaryVitaminA"),
    "dietary_vitamin_b6": Metric("Vitamin B6", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryVitaminB6"),
    "dietary_vitamin_b12": Metric("Vitamin B12", "mcg", "sum", "nutrition", "micrograms", _MCG, "DietaryVitaminB12"),
    "dietary_vitamin_c": Metric("Vitamin C", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryVitaminC"),
    "dietary_vitamin_d": Metric("Vitamin D", "mcg", "sum", "nutrition", "micrograms", _MCG, "DietaryVitaminD"),
    "dietary_vitamin_e": Metric("Vitamin E", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryVitaminE"),
    "dietary_vitamin_k": Metric("Vitamin K", "mcg", "sum", "nutrition", "micrograms", _MCG, "DietaryVitaminK"),
    "dietary_folate": Metric("Folate", "mcg", "sum", "nutrition", "micrograms", _MCG, "DietaryFolate"),
    "dietary_niacin": Metric("Niacin", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryNiacin"),
    "dietary_riboflavin": Metric("Riboflavin", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryRiboflavin"),
    "dietary_thiamin": Metric("Thiamin", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryThiamin"),
    "dietary_pantothenic_acid": Metric("Pantothenic acid", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryPantothenicAcid"),
    "dietary_biotin": Metric("Biotin", "mcg", "sum", "nutrition", "micrograms", _MCG, "DietaryBiotin"),
    "dietary_selenium": Metric("Selenium", "mcg", "sum", "nutrition", "micrograms", _MCG, "DietarySelenium"),
    "dietary_iodine": Metric("Iodine", "mcg", "sum", "nutrition", "micrograms", _MCG, "DietaryIodine"),
    "dietary_copper": Metric("Copper", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryCopper"),
    "dietary_manganese": Metric("Manganese", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryManganese"),
    "dietary_chromium": Metric("Chromium", "mcg", "sum", "nutrition", "micrograms", _MCG, "DietaryChromium"),
    "dietary_molybdenum": Metric("Molybdenum", "mcg", "sum", "nutrition", "micrograms", _MCG, "DietaryMolybdenum"),
    "dietary_chloride": Metric("Chloride", "mg", "sum", "nutrition", "milligrams", _MG, "DietaryChloride"),
}

# metric -> (label, display unit, aggregation, group)
METRICS: dict[str, tuple[str, str, str, str]] = {name: (m.label, m.unit, m.aggregation, m.group) for name, m in REGISTRY.items()}

# Export identifier -> metric, for Apple Health's export.xml.
QUANTITY_TYPES: dict[str, str] = {f"HKQuantityTypeIdentifier{m.hk}": name for name, m in REGISTRY.items() if m.hk}

# Worked out by how long each reading stands for: a short run measured every second doesn't outweigh the rest of the day.
TIME_WEIGHTED = {"heart_rate"}
TIME_WEIGHT_MAX_S = 600.0

# Sleep samples are seconds whatever daily figure they feed.
SLEEP_SECONDS = (0.0, 36 * 3600.0)
SLEEP_SAMPLE_TYPES = {"sleep_analysis_deep", "sleep_analysis_rem", "sleep_analysis_core", "sleep_analysis_asleep",
                      "sleep_analysis_unspecified", "sleep_analysis_awake", "sleep_analysis_inbed", "sleep_total_duration",
                      "sleep_deep", "sleep_rem", "sleep_light", "sleep_awake", "sleep_nap"}


def in_plausible_range(metric: str, value: float) -> bool:
    limits = SLEEP_SECONDS if metric in SLEEP_SAMPLE_TYPES else REGISTRY[metric].plausible if metric in REGISTRY else None
    return limits is None or limits[0] <= value <= limits[1]


def family_of(metric: str) -> Optional[str]:
    spec = REGISTRY.get(metric)
    return spec.family if spec else None


def to_canonical(metric: str, value: float, unit: Optional[str]) -> tuple[float, str, bool]:
    """(value, unit, whether the unit was understood). A unit already canonical, or none at all, keeps its label; the
    phones' own labels ("count/min", "degC") are canonical too, so their rows pass through untouched."""
    label = (unit or "").strip()
    family = family_of(metric)
    if family is None or not label or units.is_canonical(family, label):
        return value, label, True
    converted = units.convert_family(family, value, label)
    if converted is None:
        return value, label, False
    return converted, REGISTRY[metric].unit, True
