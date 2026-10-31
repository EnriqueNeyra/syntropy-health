"""A blood pressure panel with one reading is that reading, not an invented pair."""

from __future__ import annotations

import pytest

from app.connectors import fhir_normalize
from app.core import data_fixes, db
from app.store import connections, profiles, records

SYSTOLIC, DIASTOLIC = "8480-6", "8462-4"


def _panel(*components):
    return {"resourceType": "Observation", "id": "bp", "status": "final", "effectiveDateTime": "2026-01-01T09:00:00Z",
            "category": [{"coding": [{"code": "vital-signs"}]}],
            "code": {"coding": [{"system": "http://loinc.org", "code": "85354-6", "display": "Blood pressure panel"}]},
            "component": [{"code": {"coding": [{"system": "http://loinc.org", "code": code, "display": code}]} if code else {"text": "Part"},
                           "valueQuantity": {"value": value, "unit": "mmHg"}} for code, value in components]}


@pytest.mark.parametrize("components,text", [
    ([(DIASTOLIC, 95)], "Diastolic 95 mmHg"),
    ([(SYSTOLIC, 140)], "Systolic 140 mmHg"),
    ([(SYSTOLIC, 120), (DIASTOLIC, 80)], "120/80 mmHg"),
    ([(DIASTOLIC, 80), (SYSTOLIC, 120)], "120/80 mmHg"),
    ([(None, 120), (None, 80)], "120/80 mmHg"),
    ([(SYSTOLIC, 120), (None, 80)], "120/80 mmHg"),
])
def test_panel_text(components, text):
    assert fhir_normalize.normalize_observation(_panel(*components))["value_text"] == text


def test_stored_doubled_pressures_are_corrected_once_and_others_left_alone(client):
    pid = profiles.default_profile()["id"]
    src = connections.create(pid, "import", "manual", "Clinic")["id"]

    def row(rid, text, comps, category="vitals"):
        records.upsert_records(pid, src, "Clinic", [{
            "resource_type": "Observation", "resource_id": rid, "category": category, "title": "Blood pressure", "code": "85354-6",
            "effective_at": "2026-01-01T09:00:00Z", "value_text": text, "dedup_key": f"bp|{rid}", "details": {"components": comps}}])
    row("doubled-dia", "95/95 mmHg", [{"code": DIASTOLIC, "name": "Diastolic", "value": 95}])
    row("doubled-sys", "140/140 mmHg", [{"code": SYSTOLIC, "name": "Systolic", "value": 140}, {"code": DIASTOLIC, "name": "Diastolic", "value": None}])
    row("really-equal", "90/90 mmHg", [{"code": SYSTOLIC, "name": "Systolic", "value": 90}, {"code": DIASTOLIC, "name": "Diastolic", "value": 90}])
    row("ordinary", "120/80 mmHg", [{"code": SYSTOLIC, "name": "Systolic", "value": 120}, {"code": DIASTOLIC, "name": "Diastolic", "value": 80}])
    assert "partial-blood-pressure-text" in data_fixes._fixes
    with db.db() as conn:
        cursor = records._fix_partial_blood_pressure(conn, None, 2)
        assert cursor and records._fix_partial_blood_pressure(conn, cursor, 100)
        assert records._fix_partial_blood_pressure(conn, 10**9, 100) is None

    def text(rid):
        with db.read() as conn:
            return conn.execute("SELECT value_text FROM clinical_records WHERE resource_id = ?", (rid,)).fetchone()[0]
    assert text("doubled-dia") == "Diastolic 95 mmHg" and text("doubled-sys") == "Systolic 140 mmHg"
    assert text("really-equal") == "90/90 mmHg" and text("ordinary") == "120/80 mmHg"
