"""Common lab tests with LOINC codes and the names people use for them (HbA1c, LDL, TSH...), and which codes are one test."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Optional

from app.core import config

LOINC = "http://loinc.org"

# Codes that are one test to a person reading their history: a method's variant, or the same analyte by mass and by moles.
# Reviewed by hand against LOINC's long names. Serum and whole-blood results stay apart, and so do tests that measure something
# else: 14635-7 is vitamin D3 alone where 62292-8 is D2 + D3, and high-sensitivity CRP isn't CRP. The first code names the group.
TEST_GROUPS = (
    # Labs
    ("4548-4", "17856-6", "59261-8"),                          # hemoglobin A1c, any method, % and mmol/mol
    ("13457-7", "18262-6", "2089-1", "22748-8", "39469-2"),    # LDL cholesterol: calculated, direct, by mass and by moles
    ("2093-3", "14647-2"),                                     # total cholesterol
    ("2085-9", "14646-4"),                                     # HDL cholesterol
    ("2571-8", "14927-8"),                                     # triglycerides
    ("2345-7", "14749-6"),                                     # glucose, serum or plasma
    ("2339-0", "15074-8"),                                     # glucose, blood
    ("2160-0", "14682-9"),                                     # creatinine
    ("1989-3", "14635-7"),                                     # 25-hydroxyvitamin D3
    ("2132-9", "14685-2"),                                     # vitamin B12
    ("2284-8", "14732-2"),                                     # folate
    ("17861-6", "2000-8"),                                     # calcium
    ("1975-2", "14631-6"),                                     # bilirubin, total
    ("1968-7", "14629-0"),                                     # bilirubin, direct
    ("3084-1", "14933-6"),                                     # uric acid
    ("3094-0", "14937-7"),                                     # urea nitrogen
    ("2498-4", "14798-3"),                                     # iron
    ("2986-8", "14913-8"),                                     # testosterone
    ("2143-6", "14675-3"),                                     # cortisol
    ("3024-7", "14920-3"),                                     # free T4
    ("3051-0", "14928-6"),                                     # free T3
    ("3016-3", "11580-8"),                                     # TSH
    ("2276-4", "20567-4"),                                     # ferritin
    ("1742-6", "1743-4", "1744-2"),                            # ALT
    ("1920-8", "30239-8"),                                     # AST
    ("1751-7", "61151-7"),                                     # albumin
    ("19123-9", "2601-3"),                                     # magnesium
    ("2777-1", "14879-1"),                                     # phosphate
    ("98979-8", "62238-1", "33914-3", "77147-7", "48642-3", "48643-1", "88293-6", "88294-4", "69405-9"),  # eGFR, by any formula
    # Vitals
    ("29463-7", "3141-9"),                                     # weight
    ("8302-2", "8306-3"),                                      # height
    ("2708-6", "59408-5"),                                     # oxygen saturation
    ("85354-6", "55284-4"),                                    # blood pressure
)
_GROUP_OF = {code: group[0] for group in TEST_GROUPS for code in group}
_MEMBERS = {group[0]: group for group in TEST_GROUPS}


@lru_cache(maxsize=1)
def common_labs() -> list[dict]:
    return json.loads((config.APP_DIR / "data" / "common_labs.json").read_text())


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


# Method and specimen notes lab reports append to a test name ("VITAMIN D,25-OH,TOTAL,IA"), which don't change the test.
# "direct" and "total" do when the name is Bilirubin, which has entries of its own and so matches before anything is removed.
NOISE = {"total", "ia", "serum", "plasma", "blood", "lcmsms", "calc"}


@lru_cache(maxsize=1)
def _by_name() -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for t in common_labs():
        for name in {_key(n) for n in (t["name"], *t.get("aliases", []))}:
            index.setdefault(name, []).append(t["loinc"])
    return index


def _match(key: str) -> list[str]:
    return _by_name().get(key, [])


@lru_cache(maxsize=4096)
def _codes_for(name: str) -> tuple[str, ...]:
    if found := _match(_key(name)):
        return tuple(found)
    parts = [p for p in re.sub(r"\(([^)]*)\)", r",\1", name).split(",") if p.strip()]
    while len(parts) > 1 and _key(parts[-1]) in NOISE:
        parts.pop()
        if found := _match(_key(",".join(parts))):
            return tuple(found)
    return ()


def loinc_codes(name: str) -> list[str]:
    """LOINC codes of the common tests this name or abbreviation refers to ("LDL" is both the calculated and the
    directly measured test), most specific first."""
    return list(_codes_for(name))


def matched_test(name: str) -> Optional[dict]:
    """The common test a name was matched to, for showing that match to the person before it's saved."""
    codes = _codes_for(name)
    return next((t for t in common_labs() if t["loinc"] == codes[0]), None) if codes else None


def series_key(code: Optional[str], code_system: Optional[str], title: Optional[str]) -> str:
    """What makes results one test's history. A standard code, grouped with its equivalents; for an institution's own code or none,
    the common test its name means; otherwise that code, or that name, on its own."""
    if code in _GROUP_OF:
        return _GROUP_OF[code]
    if code and code_system == LOINC:
        return code
    codes = _codes_for(title) if title else ()
    if codes:
        return _GROUP_OF.get(codes[0], codes[0])
    return code or (title or "").lower()


def group_codes(key: str) -> tuple[str, ...]:
    """The standard codes a test's history can be filed under."""
    return _MEMBERS.get(key, (key,))
