"""Findings from a review of records: what a source sends, how a sync walks it, and what's kept and shown."""

import os
import subprocess
import sys
import textwrap

import pytest

from app.connectors import fhir_normalize as fn
from app.connectors import units

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---- Range text padded with spaces

def lab(ranges, value=24, **kw):
    res = {"resourceType": "Observation", "id": "o1", "status": "final", "category": [{"coding": [{"code": "laboratory"}]}],
           "code": {"coding": [{"system": "http://loinc.org", "code": "1989-3", "display": "Vitamin D"}]},
           "effectiveDateTime": "2025-01-01T08:00:00Z", "valueQuantity": {"value": value, "unit": "ng/mL"}, "referenceRange": ranges}
    res.update(kw)
    if "valueString" in kw:
        del res["valueQuantity"]
    return fn.normalize_observation(res)


def test_range_text_padded_with_twenty_thousand_spaces_is_read_at_once():
    """Patterns that could share a run of spaces between them tried every way of doing it: 800 spaces took a second, 20,000 would
    take hours. In a child process, so that a regression fails at the time limit instead of holding the run."""
    script = textwrap.dedent("""
        import time
        from app.connectors import fhir_normalize as fn, units
        pad = " " * 20000
        texts = [pad, "a" + pad + "x", "1" + pad + "x", "1" + pad + "normalx", "1" + pad + "or", "1" + pad + "-" + pad,
                 "Normal:" + pad + "30-100" + pad + "x", "1" * 20000 + "x", "\\n" * 20000 + "x", " ," * 20000]
        res = {"resourceType": "Observation", "status": "final", "category": [{"coding": [{"code": "laboratory"}]}],
               "code": {"coding": [{"system": "http://loinc.org", "code": "1989-3"}]}, "effectiveDateTime": "2025-01-01"}
        start = time.perf_counter()
        for text in texts:
            fn.normalize_observation({**res, "valueQuantity": {"value": 24, "unit": text}, "referenceRange": [{"text": text}]})
            fn.normalize_observation({**res, "valueQuantity": {"value": 24}, "referenceRange": [{"text": text}, {"text": text + "y"}]})
            fn.normalize_observation({**res, "valueString": text})
            units.unit_key(text), units.unit_key("{1.73" * 4000), units.pretty_unit("{" + text)
        print(time.perf_counter() - start)
    """)
    done = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert float(done.stdout) < 2.0


@pytest.mark.parametrize("text, low, high", [
    ("Normal:      30   -    100", 30, 100), ("  30 \t-\t 100  ng/mL  ", 30, 100), ("Sufficient\t30-100", 30, 100),
    ("30-100     (normal)", 30, 100), ("Deficient:   <20;   Sufficient:    30-100", 30, 100), (">   OR   =   30", 30, None),
    # A lab's long note around its bands: the band is still read, and the note isn't put to a pattern whole.
    ("Vitamin D Status 25-OH Vitamin D:\nDeficiency: <20 ng/mL\nInsufficiency: 20 - 29 ng/mL\nOptimal: > or = 30 ng/mL\n"
     + "For testing on patients on D2-supplementation " * 10, 30, None),
])
def test_range_text_with_spaces_and_line_breaks_reads_as_before(text, low, high):
    r = lab([{"text": text}])
    assert (r["ref_low"], r["ref_high"]) == (low, high)


def test_a_band_longer_than_any_range_stays_unread():
    r = lab([{"text": "Normal: 30-100 " + "x" * 300}])
    assert r["ref_low"] is None and r["ref_high"] is None and r["interpretation"] is None


@pytest.mark.parametrize("unit, key", [
    ("mg / dL", "mg/dl"), ("mL/min/{1.73_m2}", "ml/min/1.73m2"), ("mL/min/1.73 m2", "ml/min/1.73m2"), ("mg/dL (calc)", "mg/dl"),
    ("{beats}/min", "/min"), ("mmol / L  (estimated)", "mmol/l"),
])
def test_unit_spellings_still_meet(unit, key):
    assert units.unit_key(unit) == key


def test_a_number_written_with_spaces_around_it_is_still_a_number():
    r = lab([], valueString="   < 5.2   ")
    assert r["value_num"] == 5.2 and r["details"]["comparator"] == "<"
