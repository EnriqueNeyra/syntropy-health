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
