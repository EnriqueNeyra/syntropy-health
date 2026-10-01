"""Common lab tests with LOINC codes and the names people use for them (HbA1c, LDL, TSH...)."""

from __future__ import annotations

import json
import re
from functools import lru_cache

from app.core import config


@lru_cache(maxsize=1)
def common_labs() -> list[dict]:
    return json.loads((config.APP_DIR / "data" / "common_labs.json").read_text())


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


# Method and specimen notes lab reports append to a test name ("VITAMIN D,25-OH,TOTAL,IA"), which don't change the test.
NOISE = {"total", "ia", "serum", "plasma", "blood", "lcmsms", "calc", "direct"}


def _match(key: str) -> list[str]:
    return [t["loinc"] for t in common_labs() if key and key in {_key(n) for n in (t["name"], *t.get("aliases", []))}]


def loinc_codes(name: str) -> list[str]:
    """LOINC codes of the common tests this name or abbreviation refers to ("LDL" is both the calculated and the
    directly measured test), most specific first."""
    if found := _match(_key(name)):
        return found
    parts = [p for p in re.sub(r"\(([^)]*)\)", r",\1", name).split(",") if p.strip()]
    while len(parts) > 1 and _key(parts[-1]) in NOISE:
        parts.pop()
        if found := _match(_key(",".join(parts))):
            return found
    return []
