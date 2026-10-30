"""
Unit normalization (UCUM).

Observations for the same LOINC code arrive in different units depending on the
source (lb vs kg, °F vs °C, mmol/L vs mg/dL). Each record keeps its original value
and unit; ``value_norm``/``unit_norm`` hold the value in the canonical unit for the
code so trends and comparisons across institutions are always apples to apples.
"""

from __future__ import annotations

import re
from typing import Optional

# LOINC -> canonical display unit
CANONICAL = {
    "29463-7": "kg", "3141-9": "kg",                       # body weight
    "8302-2": "cm", "8306-3": "cm",                        # body height
    "8310-5": "°C", "8331-1": "°C",                        # body temperature
    "2345-7": "mg/dL", "2339-0": "mg/dL", "1558-6": "mg/dL",  # glucose
    "2093-3": "mg/dL", "13457-7": "mg/dL", "18262-6": "mg/dL", "2085-9": "mg/dL",  # cholesterol
    "2571-8": "mg/dL",                                     # triglycerides
    "2160-0": "mg/dL",                                     # creatinine
    "718-7": "g/dL",                                       # hemoglobin
}

# (from unit, to unit) -> converter
_WEIGHT = {"kg": 1.0, "g": 0.001, "lb": 0.45359237, "[lb_av]": 0.45359237, "lbs": 0.45359237, "oz": 0.028349523, "[oz_av]": 0.028349523}
_LENGTH = {"cm": 1.0, "m": 100.0, "mm": 0.1, "in": 2.54, "[in_i]": 2.54, "ft": 30.48, "[ft_i]": 30.48}

# mmol/L -> mg/dL factors by analyte
_MOLAR = {
    "2345-7": 18.016, "2339-0": 18.016, "1558-6": 18.016,
    "2093-3": 38.67, "13457-7": 38.67, "18262-6": 38.67, "2085-9": 38.67,
    "2571-8": 88.57,
}


def _unit_key(unit: Optional[str]) -> str:
    return (unit or "").strip()


def normalize(code: Optional[str], value: Optional[float], unit: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    """Returns (value_norm, unit_norm). Falls back to the original value/unit."""
    if value is None:
        return None, unit
    target = CANONICAL.get(code or "")
    u = _unit_key(unit)
    ul = u.lower()
    if not target:
        return value, (u or None)
    try:
        if target == "kg" and (u in _WEIGHT or ul in _WEIGHT):
            return round(value * _WEIGHT.get(u, _WEIGHT.get(ul, 1.0)), 3), "kg"
        if target == "cm" and (u in _LENGTH or ul in _LENGTH):
            return round(value * _LENGTH.get(u, _LENGTH.get(ul, 1.0)), 2), "cm"
        if target == "°C":
            if ul in ("[degf]", "degf", "°f", "f"):
                return round((value - 32) * 5 / 9, 2), "°C"
            if ul in ("cel", "degc", "°c", "c"):
                return round(value, 2), "°C"
        if target == "mg/dL":
            if ul in ("mmol/l",) and code in _MOLAR:
                return round(value * _MOLAR[code], 1), "mg/dL"
            if code == "2160-0" and ul in ("umol/l", "µmol/l"):
                return round(value / 88.42, 3), "mg/dL"
            if ul in ("mg/dl",):
                return value, "mg/dL"
        if target == "g/dL":
            if ul == "g/l":
                return round(value / 10, 2), "g/dL"
            if ul == "g/dl":
                return value, "g/dL"
    except (TypeError, ValueError):
        pass
    return value, (u or None)


# Wearable measurements, by family: the labels that already mean the canonical unit (the phones send these), and what to
# multiply other units by. Temperature and molar glucose aren't a plain factor; see convert_family.
_CM = {"cm": 1.0, "mm": 0.1, "m": 100.0, "km": 100_000.0, "in": 2.54, "ft": 30.48, "yd": 91.44, "mi": 160_934.4}
FAMILIES: dict[str, tuple[set[str], dict[str, float]]] = {
    "metres": ({"m", "meter", "meters", "metre", "metres"},
               {"km": 1000.0, "mi": 1609.344, "ft": 0.3048, "yd": 0.9144, "cm": 0.01, "mm": 0.001, "in": 0.0254}),
    "centimetres": ({"cm"}, {k: v for k, v in _CM.items() if k != "cm"}),
    "kilograms": ({"kg"}, {"lb": 0.45359237, "lbs": 0.45359237, "g": 0.001, "st": 6.35029318, "oz": 0.028349523}),
    "energy": ({"kcal", "Cal", "Kcal", "kCal"}, {"kJ": 1 / 4.184, "kj": 1 / 4.184, "cal": 0.001, "J": 1 / 4184}),
    "speed": ({"m/s"}, {"km/hr": 1 / 3.6, "km/h": 1 / 3.6, "kph": 1 / 3.6, "mi/hr": 0.44704, "mph": 0.44704, "mi/h": 0.44704,
                        "ft/s": 0.3048}),
    "minutes": ({"min", "minutes"}, {"s": 1 / 60, "sec": 1 / 60, "seconds": 1 / 60, "hr": 60.0, "h": 60.0, "hours": 60.0}),
    "millilitres": ({"mL", "ml"}, {"L": 1000.0, "l": 1000.0, "fl_oz_us": 29.5735296, "fl oz": 29.5735296, "fl_oz_imp": 28.4130625,
                                   "cup_us": 236.588236, "cup_imp": 284.130625, "pt_us": 473.176473, "pt_imp": 568.26125}),
    "litres": ({"L", "l"}, {"mL": 0.001, "ml": 0.001}),
    "grams": ({"g"}, {"kg": 1000.0, "mg": 0.001, "mcg": 1e-6, "µg": 1e-6, "ug": 1e-6, "oz": 28.349523}),
    "milligrams": ({"mg"}, {"g": 1000.0, "mcg": 0.001, "µg": 0.001, "ug": 0.001}),
    "micrograms": ({"mcg", "µg", "ug"}, {"mg": 1000.0, "g": 1_000_000.0}),
    "rate": ({"count/min", "bpm", "/min", "br/min", "rpm", "beats/min", "breaths/min"}, {"count/s": 60.0, "Hz": 60.0}),
    "temperature": ({"degC", "°C", "C", "Cel", "celsius"}, {}),
    "glucose": ({"mg/dL", "mg/dl"}, {}),
}
_FAHRENHEIT = {"degF", "°F", "F", "[degF]", "fahrenheit"}
GLUCOSE_MG_PER_MMOL = 18.016


def is_canonical(family: str, unit: str) -> bool:
    return unit in FAMILIES[family][0]


def convert_family(family: str, value: float, unit: str) -> Optional[float]:
    """``value`` in the family's canonical unit, or None for a unit that isn't one of its own."""
    if family == "temperature" and unit in _FAHRENHEIT:
        return (value - 32) * 5 / 9
    if family == "glucose" and unit.lower().startswith("mmol"):     # HealthKit writes mmol<180.15588000005408>/L
        return value * GLUCOSE_MG_PER_MMOL
    factor = FAMILIES[family][1].get(unit)
    return None if factor is None else value * factor


def pretty_unit(unit: Optional[str]) -> Optional[str]:
    """Human-friendly rendering of common UCUM codes."""
    if not unit:
        return unit
    return {
        "[lb_av]": "lb", "[in_i]": "in", "Cel": "°C", "[degF]": "°F", "degF": "°F", "degC": "°C", "mm[Hg]": "mmHg", "/min": "/min",
        "10*3/uL": "×10³/µL", "m[IU]/L": "mIU/L", "mL/min/{1.73_m2}": "mL/min/1.73m²", "ug/dL": "µg/dL",
        "kg/m2": "kg/m²", "beats/minute": "bpm", "breaths/minute": "/min",
    }.get(unit, _strip_annotations(unit))


def _strip_annotations(unit: str) -> Optional[str]:
    """UCUM curly-brace annotations are labels, not units: "{score}" -> None, "{beats}/min" -> "/min"."""
    if "{" not in unit:
        return unit
    return re.sub(r"\{[^}]*\}", "", unit).strip() or None
