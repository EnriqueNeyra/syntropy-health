"""The visit summary and the Overview say only what the record can back up."""

from pathlib import Path

import pytest

from app.connectors import fhir_normalize as fn
from app.core import db
from app.services import renormalize
from app.store import connections, profiles, records

STATIC = Path(__file__).resolve().parent.parent / "app" / "static" / "js"


@pytest.fixture
def pid(client):
    return profiles.default_profile()["id"]


def _source(pid, name="Lab"):
    return connections.create(pid, "import", "manual", name)["id"]


def _lab(pid, src, rid, value, interp=None, code=None, title=None, day="2026-09-01", **extra):
    code = code or rid
    records.upsert_records(pid, src, "Lab", [{
        "resource_type": "Observation", "resource_id": rid, "category": "labs", "title": title or f"Test {rid}", "code": code,
        "code_system": "http://loinc.org", "effective_at": f"{day}T09:00:00Z", "value_num": value, "value_norm": value,
        "value_text": str(value), "interpretation": interp, "dedup_key": f"lab|{code}|{day}", **extra}])


def _obs(value, text=None, unit="mg/dL", **kw):
    res = {"resourceType": "Observation", "id": "o", "status": "final", "category": [{"coding": [{"code": "laboratory"}]}],
           "code": {"text": "Glucose"}, "effectiveDateTime": "2026-01-01", "valueQuantity": {"value": value, "unit": unit}, **kw}
    if text is not None:
        res["referenceRange"] = [{"text": text}]
    return fn.normalize_observation(res)


# ---------------------------------------------------------------------------
# F018
# ---------------------------------------------------------------------------

def test_no_labs_counts_as_no_labs(pid):
    assert records.summary(pid)["lab_status"] == {"total": 0, "flagged": 0, "in_range": 0, "not_assessed": 0}


def test_a_result_with_no_range_is_not_in_range(pid):
    src = _source(pid)
    _lab(pid, src, "k", 7.2)
    _lab(pid, src, "g", 90, "normal")
    _lab(pid, src, "t", 9, "high")
    assert records.summary(pid)["lab_status"] == {"total": 3, "flagged": 1, "in_range": 1, "not_assessed": 1}


def test_counts_cover_more_than_the_results_listed(pid):
    src = _source(pid)
    for i in range(20):
        _lab(pid, src, f"c{i}", 5, "normal")
    assert records.summary(pid)["lab_status"]["in_range"] == 20


def test_a_lab_that_says_normal_without_a_range_is_in_range(pid):
    _lab(pid, _source(pid), "n", 1, "normal")
    assert records.summary(pid)["lab_status"]["in_range"] == 1


@pytest.mark.parametrize("value,text,expected", [
    (410, "70-99 mg/dL", "high"), (50, "70–99", "low"), (80, "70 - 99 mg/dL", "normal"), (6.1, "<5.7", "high"), (5.7, "<5.7", "high"),
    (5.0, "<5.7 mg/dL", "normal"), (5.7, "<=5.7", "normal"), (45, ">60", "low"), (60, ">60", "low"), (75, ">=60", "normal"), (60, "≥60", "normal"),
    (410, "NON-REACTIVE", None), (410, "See comment", None), (410, "Adults: 70-99", None), (410, "70-99 mmol/L", None), (410, "99-70", None),
    (410, "", None), (410, "70-99 fasting", None), (410, "<5.7 if diabetic", None),
])
def test_simple_written_ranges_are_compared(value, text, expected):
    assert _obs(value, text)["interpretation"] == expected


def test_a_written_range_is_shown_as_written_and_stored_as_numbers():
    rec = _obs(410, "70-99 mg/dL")
    assert rec["ref_text"].startswith("70–99") and (rec["ref_low"], rec["ref_high"]) == (70, 99)


def test_a_words_only_result_stays_not_assessed():
    res = {"resourceType": "Observation", "id": "h", "status": "final", "category": [{"coding": [{"code": "laboratory"}]}],
           "code": {"text": "HIV antibody"}, "effectiveDateTime": "2026-01-01", "valueString": "Reactive", "referenceRange": [{"text": "NON-REACTIVE"}]}
    assert fn.normalize_observation(res)["interpretation"] is None


def test_stored_written_ranges_are_compared_once(pid):
    res = {"resourceType": "Observation", "id": "g", "status": "final", "category": [{"coding": [{"code": "laboratory"}]}],
           "code": {"text": "Glucose"}, "effectiveDateTime": "2026-01-01", "valueQuantity": {"value": 410, "unit": "mg/dL"}, "referenceRange": [{"text": "70-99 mg/dL"}]}
    src = _source(pid)
    rec = fn.normalize_observation(res)
    rec.update(resource_type="Observation", resource_id="g", raw=res, interpretation=None, ref_low=None, ref_high=None)
    records.upsert_records(pid, src, "Lab", [rec])
    with db.read() as conn:
        assert conn.execute("SELECT interpretation FROM clinical_records").fetchone()[0] is None
    cursor = None
    while True:
        with db.db() as conn:
            cursor = renormalize.written_ranges_step(conn, cursor, 100)
        if cursor is None:
            break
    with db.read() as conn:
        row = conn.execute("SELECT interpretation, ref_high FROM clinical_records").fetchone()
    assert row["interpretation"] == "high" and row["ref_high"] == 99


def test_the_all_in_range_sentences_are_gone():
    for path in ("views/report.js", "views/overview.js"):
        text = (STATIC / path).read_text(encoding="utf-8")
        assert "within reference ranges" not in text and "lab results are in range" not in text


def test_ai_apps_get_the_counts(pid):
    from app import mcp_server
    _lab(pid, _source(pid), "k", 7.2)
    assert mcp_server.t_health_summary({})["lab_status"]["not_assessed"] == 1
