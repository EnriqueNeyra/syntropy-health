"""
Unit normalization (UCUM).

Observations for the same LOINC code arrive in different units depending on the
source (lb vs kg, °F vs °C, mmol/L vs mg/dL). Each record keeps its original value
and unit; ``value_norm``/``unit_norm`` hold the value in the canonical unit for the
analyte when the conversion is certain, so trends and comparisons across institutions
are apples to apples. A unit with no known factor for that analyte stays as it was, and
``unit_key`` keeps it apart from the rest of the series rather than on the same axis.
"""

from __future__ import annotations

import re
from typing import Callable, Optional, Union

UCUM = "http://unitsofmeasure.org"

# Spellings of one unit, and units that mean the same number: ng/mL and ug/L, K/uL and 10*9/L.
_SAME_UNIT = {
    "10*3/ul": ("10^3/ul", "x10e3/ul", "x10^3/ul", "x10*3/ul", "x10(3)/ul", "10(3)/ul", "10e3/ul", "×10³/ul", "k/ul", "thou/ul", "thousand/ul",
                "10*3/mm3", "k/mm3", "thou/mm3", "k/cumm", "thou/cumm", "k/cmm", "10*9/l", "10^9/l", "x10e9/l", "x10^9/l", "x10*9/l", "10e9/l", "×10⁹/l", "giga/l"),
    "10*6/ul": ("10^6/ul", "x10e6/ul", "x10^6/ul", "x10*6/ul", "x10(6)/ul", "10(6)/ul", "10e6/ul", "×10⁶/ul", "m/ul", "mil/ul", "mill/ul", "million/ul",
                "10*6/mm3", "m/mm3", "mil/mm3", "m/cumm", "mil/cumm", "m/cmm", "10*12/l", "10^12/l", "x10e12/l", "x10^12/l", "x10*12/l", "10e12/l", "×10¹²/l"),
    "ng/ml": ("ug/l",),
    "pg/ml": ("ng/l",),
    "miu/l": ("uiu/ml", "u[iu]/ml", "m[iu]/l", "mu/l", "uu/ml", "mciu/ml"),
    "u/l": ("iu/l", "[iu]/l", "miu/ml"),
    "mm/h": ("mm/hr", "mm/hour"),
    "mmhg": ("mm[hg]", "mm hg"),
    "%": ("percent", "pct"),
    "kg": ("kilogram", "kilograms", "kgs"),
    "g": ("gram", "grams"),
    "lb": ("[lb_av]", "lbs", "pound", "pounds"),
    "oz": ("[oz_av]", "ounce", "ounces"),
    "cm": ("centimeter", "centimeters", "centimetre", "centimetres"),
    "m": ("meter", "meters", "metre", "metres"),
    "mm": ("millimeter", "millimeters"),
    "in": ("[in_i]", "[in_us]", "inch", "inches"),
    "ft": ("[ft_i]", "foot", "feet"),
    "°c": ("cel", "degc", "deg c", "degrees c", "degree c", "c", "celsius", "degrees celsius"),
    "°f": ("[degf]", "degf", "deg f", "degrees f", "degree f", "f", "fahrenheit", "degrees fahrenheit"),
    "/min": ("bpm", "beats/min", "beats/minute", "breaths/min", "breaths/minute", "1/min"),
}
_SPELLINGS = {spelled: key for key, spellings in _SAME_UNIT.items() for spelled in spellings}


def unit_key(unit: Optional[str]) -> str:
    """One spelling for each unit, for telling whether two results can share an axis. "" for no unit at all."""
    if not unit:
        return ""
    u = re.sub(r"\{\s*(1\.73[^}]*)\}", r"\1", unit)     # the one annotation that's part of the unit
    u = re.sub(r"\{[^}]*\}", "", u).strip()
    u = re.sub(r"\s*\((?:calc|calculated|est|estimated)\)$", "", u, flags=re.I)
    u = u.lower().replace("µ", "u").replace("μ", "u").replace("mcg", "ug").replace("mcmol", "umol").replace("mcl", "ul").strip()
    u = re.sub(r"\s*/\s*", "/", u)
    u = re.sub(r"^ml/min(?:/|\s+per\s+)1\.73.*$", "ml/min/1.73m2", u)
    return _SPELLINGS.get(u, u)


Convert = Union[float, Callable[[float], float]]


def _fahrenheit(v: float) -> float:
    return (v - 32) * 5 / 9


def _ifcc_to_ngsp(v: float) -> float:
    """HbA1c in mmol/mol (IFCC) to % (NGSP): the master equation, an offset as well as a ratio."""
    return 0.0915 * v + 2.15


# Analyte: canonical unit, decimals a converted value is rounded to, and what one of each unit is in the canonical unit.
# Molar factors belong to one analyte; a unit that isn't listed for it is never converted.
_ANALYTES: dict[str, tuple[str, int, dict[str, Convert]]] = {
    "weight": ("kg", 3, {"kg": 1.0, "g": 0.001, "lb": 0.45359237, "oz": 0.028349523}),
    "height": ("cm", 2, {"cm": 1.0, "m": 100.0, "mm": 0.1, "in": 2.54, "ft": 30.48}),
    "temperature": ("°C", 2, {"°c": 1.0, "°f": _fahrenheit}),
    "glucose": ("mg/dL", 1, {"mg/dl": 1.0, "mmol/l": 18.016, "g/l": 100.0}),
    "cholesterol": ("mg/dL", 1, {"mg/dl": 1.0, "mmol/l": 38.67, "g/l": 100.0}),
    "triglycerides": ("mg/dL", 1, {"mg/dl": 1.0, "mmol/l": 88.57, "g/l": 100.0}),
    "creatinine": ("mg/dL", 3, {"mg/dl": 1.0, "umol/l": 1 / 88.42, "mg/l": 0.1}),
    "hemoglobin": ("g/dL", 2, {"g/dl": 1.0, "g/l": 0.1, "mmol/l": 1.611}),
    "a1c": ("%", 2, {"%": 1.0, "mmol/mol": _ifcc_to_ngsp}),
    "vitamin_d": ("ng/mL", 2, {"ng/ml": 1.0, "nmol/l": 1 / 2.496}),
    "b12": ("pg/mL", 1, {"pg/ml": 1.0, "pmol/l": 1.355}),
    "folate": ("ng/mL", 2, {"ng/ml": 1.0, "nmol/l": 1 / 2.266}),
    "calcium": ("mg/dL", 2, {"mg/dl": 1.0, "mmol/l": 4.008}),
    "bilirubin": ("mg/dL", 2, {"mg/dl": 1.0, "umol/l": 1 / 17.1}),
    "uric_acid": ("mg/dL", 2, {"mg/dl": 1.0, "umol/l": 1 / 59.48, "mmol/l": 16.81}),
    "urea_nitrogen": ("mg/dL", 1, {"mg/dl": 1.0, "mmol/l": 2.801}),
    "iron": ("µg/dL", 1, {"ug/dl": 1.0, "umol/l": 5.585}),
    "testosterone": ("ng/dL", 1, {"ng/dl": 1.0, "nmol/l": 28.84, "ng/ml": 100.0}),
    "cortisol": ("µg/dL", 2, {"ug/dl": 1.0, "nmol/l": 1 / 27.59}),
    "free_t4": ("ng/dL", 2, {"ng/dl": 1.0, "pmol/l": 1 / 12.87}),
    "free_t3": ("pg/mL", 2, {"pg/ml": 1.0, "pmol/l": 0.651}),
    "protein": ("g/dL", 2, {"g/dl": 1.0, "g/l": 0.1}),
    "crp": ("mg/L", 2, {"mg/l": 1.0, "mg/dl": 10.0}),
    "magnesium": ("mg/dL", 2, {"mg/dl": 1.0, "mmol/l": 2.431}),
    "phosphate": ("mg/dL", 2, {"mg/dl": 1.0, "mmol/l": 3.097}),
    "electrolyte": ("mmol/L", 1, {"mmol/l": 1.0, "meq/l": 1.0}),     # sodium, potassium, chloride, bicarbonate: one charge each
}

# LOINC -> analyte. Codes of one analyte reported by mass and by moles are both listed.
CANONICAL = {
    **dict.fromkeys(("29463-7", "3141-9"), "weight"),
    **dict.fromkeys(("8302-2", "8306-3"), "height"),
    **dict.fromkeys(("8310-5", "8331-1"), "temperature"),
    **dict.fromkeys(("2345-7", "2339-0", "1558-6", "14749-6", "15074-8"), "glucose"),
    **dict.fromkeys(("2093-3", "14647-2", "13457-7", "18262-6", "2089-1", "22748-8", "39469-2", "2085-9", "14646-4"), "cholesterol"),
    **dict.fromkeys(("2571-8", "14927-8"), "triglycerides"),
    **dict.fromkeys(("2160-0", "14682-9"), "creatinine"),
    **dict.fromkeys(("718-7", "59260-0"), "hemoglobin"),
    **dict.fromkeys(("4548-4", "17856-6", "59261-8"), "a1c"),
    **dict.fromkeys(("1989-3", "14635-7", "62292-8"), "vitamin_d"),
    **dict.fromkeys(("2132-9", "14685-2"), "b12"),
    **dict.fromkeys(("2284-8", "14732-2"), "folate"),
    **dict.fromkeys(("17861-6", "2000-8"), "calcium"),
    **dict.fromkeys(("1975-2", "14631-6", "1968-7", "14629-0"), "bilirubin"),
    **dict.fromkeys(("3084-1", "14933-6"), "uric_acid"),
    **dict.fromkeys(("3094-0", "14937-7"), "urea_nitrogen"),
    **dict.fromkeys(("2498-4", "14798-3"), "iron"),
    **dict.fromkeys(("2986-8", "14913-8"), "testosterone"),
    **dict.fromkeys(("2143-6", "14675-3"), "cortisol"),
    **dict.fromkeys(("3024-7", "14920-3"), "free_t4"),
    **dict.fromkeys(("3051-0", "14928-6"), "free_t3"),
    **dict.fromkeys(("1751-7", "61151-7", "2885-2"), "protein"),
    **dict.fromkeys(("1988-5", "30522-7"), "crp"),
    **dict.fromkeys(("19123-9", "2601-3"), "magnesium"),
    **dict.fromkeys(("2777-1", "14879-1"), "phosphate"),
    **dict.fromkeys(("2951-2", "2823-3", "2075-0", "2028-9"), "electrolyte"),
}


def quantity_unit(q: dict) -> Optional[str]:
    """The unit to convert from: a Quantity's UCUM code when it has one (its display is free text, like "pounds"), else the display."""
    code, system = q.get("code"), q.get("system")
    return code if code and (not system or system in (UCUM, "urn:oid:2.16.840.1.113883.6.8")) else q.get("unit")


def normalize(code: Optional[str], value: Optional[float], unit: Optional[str],
              *prefer: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    """Returns (value_norm, unit_norm). Falls back to the original value/unit. ``prefer``: the unit's UCUM code, tried first."""
    if value is None:
        return None, unit
    analyte = _ANALYTES.get(CANONICAL.get(code or "", ""))
    if analyte:
        canonical, decimals, table = analyte
        for spelled in (*prefer, unit):
            conv = table.get(unit_key(spelled))
            if conv is None:
                continue
            if conv == 1.0:
                return value, canonical
            return round(conv(value) if callable(conv) else value * conv, decimals), canonical
    return value, ((unit or "").strip() or None)


def converted(unit: Optional[str], unit_norm: Optional[str]) -> bool:
    """Whether a value's normalized unit really differs from the one reported, not just how it is spelled."""
    return unit_key(unit) != unit_key(unit_norm)


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
        "kg/m2": "kg/m²", "beats/minute": "bpm", "breaths/minute": "/min", "/[HPF]": "/HPF", "/[LPF]": "/LPF",
        "pounds": "lb", "pound": "lb", "lbs": "lb", "kilograms": "kg", "kilogram": "kg", "inches": "in", "inch": "in",
        "degrees F": "°F", "deg F": "°F", "fahrenheit": "°F", "degrees C": "°C", "deg C": "°C", "celsius": "°C",
    }.get(unit, _strip_annotations(unit))


def _strip_annotations(unit: str) -> Optional[str]:
    """UCUM curly-brace annotations are labels, not units: "{score}" -> None, "{beats}/min" -> "/min"."""
    if "{" not in unit:
        return unit
    return re.sub(r"\{[^}]*\}", "", unit).strip() or None
