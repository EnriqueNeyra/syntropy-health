"""Unit tests for FHIR normalization and unit conversion."""

import base64

from app.connectors import fhir_normalize as fn
from app.connectors import units


def obs(**kw):
    base = {"resourceType": "Observation", "id": "o1", "status": "final",
            "category": [{"coding": [{"code": "laboratory"}]}],
            "code": {"coding": [{"system": "http://loinc.org", "code": "2345-7", "display": "Glucose"}]},
            "effectiveDateTime": "2025-01-01T08:00:00Z"}
    base.update(kw)
    return base


def test_interpretation_codes_and_fallback_to_range():
    r = fn.normalize_observation(obs(valueQuantity={"value": 130, "unit": "mg/dL"},
                                     interpretation=[{"coding": [{"code": "HH"}]}]))
    assert r["interpretation"] == "critical_high"
    r = fn.normalize_observation(obs(valueQuantity={"value": 60, "unit": "mg/dL"},
                                     referenceRange=[{"low": {"value": 70}, "high": {"value": 99}}]))
    assert r["interpretation"] == "low" and r["ref_text"] == "70–99"


def test_mmol_glucose_normalized_with_reference_range():
    r = fn.normalize_observation(obs(valueQuantity={"value": 5.0, "unit": "mmol/L"},
                                     referenceRange=[{"low": {"value": 3.9}, "high": {"value": 5.5}}]))
    assert r["value_num"] == 5.0 and r["value_norm"] == 90.1 and r["unit_norm"] == "mg/dL"
    assert r["ref_high"] == 99.1


def test_blood_pressure_components():
    r = fn.normalize_observation(obs(
        category=[{"coding": [{"code": "vital-signs"}]}],
        code={"coding": [{"system": "http://loinc.org", "code": "85354-6", "display": "Blood pressure panel"}]},
        component=[
            {"code": {"coding": [{"code": "8480-6", "display": "Systolic"}]}, "valueQuantity": {"value": 120, "unit": "mmHg"}},
            {"code": {"coding": [{"code": "8462-4", "display": "Diastolic"}]}, "valueQuantity": {"value": 80, "unit": "mmHg"}},
        ]))
    assert r["category"] == "vitals" and r["title"] == "Blood pressure" and r["value_text"] == "120/80 mmHg"


def test_entered_in_error_is_dropped():
    assert fn.normalize_observation(obs(status="entered-in-error", valueQuantity={"value": 1})) is None
    assert fn.normalize_condition({"resourceType": "Condition", "id": "c", "verificationStatus": {"coding": [{"code": "entered-in-error"}]},
                                   "code": {"text": "x"}}) is None


def test_medication_reference_resolved_and_dedup_by_rxnorm():
    med = {"resourceType": "Medication", "id": "m1",
           "code": {"coding": [{"system": fn.RXNORM, "code": "314076", "display": "Lisinopril 10 MG"}]}}
    req = {"resourceType": "MedicationRequest", "id": "r1", "status": "active", "authoredOn": "2024-01-01",
           "medicationReference": {"reference": "Medication/m1"}, "dosageInstruction": [{"text": "daily"}]}
    _, recs = fn.normalize_resources([med, req])
    assert recs[0]["title"] == "Lisinopril 10 MG" and recs[0]["dedup_key"] == "med|314076"


def test_references_without_a_display_name_are_left_out():
    enc = {"resourceType": "Encounter", "id": "e1", "status": "finished", "type": [{"text": "Encounter for symptom"}],
           "period": {"start": "2020-08-12"},
           "participant": [{"individual": {"reference": "Practitioner/3e177bd6"}},
                           {"individual": {"reference": "Practitioner/9a1", "display": "Dr. Rivera"}}],
           "serviceProvider": {"reference": "Organization/5e76"}}
    d = fn.normalize_encounter(enc)["details"]
    assert d["clinicians"] == ["Dr. Rivera"] and d["provider"] is None


def test_html_note_decoded_and_stripped():
    html = "<html><body><p>Hello &amp; welcome</p><script>x()</script></body></html>"
    doc = {"resourceType": "DocumentReference", "id": "d1", "status": "current", "date": "2024-02-02",
           "type": {"text": "Progress note"},
           "content": [{"attachment": {"contentType": "text/html", "data": base64.b64encode(html.encode()).decode()}}]}
    r = fn.normalize_document_reference(doc)
    assert r["narrative"] == "Hello & welcome"


def test_weight_and_temperature_units():
    assert units.normalize("29463-7", 200, "[lb_av]") == (90.718, "kg")
    assert units.normalize("8310-5", 98.6, "[degF]") == (37.0, "°C")
    assert units.normalize("2160-0", 88.42, "umol/L") == (1.0, "mg/dL")
    assert units.normalize("9999-9", 5, "x") == (5, "x")


def test_display_value_uses_canonical_unit():
    r = fn.normalize_observation(obs(
        category=[{"coding": [{"code": "vital-signs"}]}],
        code={"coding": [{"system": "http://loinc.org", "code": "29463-7", "display": "Body weight"}]},
        valueQuantity={"value": 200, "unit": "lb", "code": "[lb_av]"}))
    assert r["value_text"] == "90.7 kg" and r["details"]["original_value"] == "200 lb"


def test_no_allergy_statements_are_not_allergies():
    from app.connectors.fhir_normalize import normalize_allergy
    not_asked = normalize_allergy({"resourceType": "AllergyIntolerance", "id": "a1", "code": {"coding": [
        {"system": "http://snomed.info/sct", "code": "1631000175102", "display": "Patient not asked"}], "text": "Not on File"},
        "clinicalStatus": {"coding": [{"code": "active"}]}})
    nka = normalize_allergy({"resourceType": "AllergyIntolerance", "id": "a2", "code": {"coding": [
        {"system": "http://snomed.info/sct", "code": "716186003", "display": "No known allergy"}]}})
    assert not_asked["status"] == "not-asked" and nka["status"] == "no-known-allergies"
