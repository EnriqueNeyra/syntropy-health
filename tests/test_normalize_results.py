"""A lab result stays what the source said: number, unit, comparator and range, with a normalized value beside it."""

import pytest

from app.connectors import fhir_normalize as fn
from app.connectors import units


def obs(code="2345-7", display="Glucose", **kw):
    base = {"resourceType": "Observation", "id": "o1", "status": "final",
            "category": [{"coding": [{"code": "laboratory"}]}],
            "code": {"coding": [{"system": "http://loinc.org", "code": code, "display": display}]},
            "effectiveDateTime": "2025-01-01T08:00:00Z"}
    base.update(kw)
    return base


def norm(**kw):
    return fn.normalize_observation(obs(**kw))


# ---- F001: digits

@pytest.mark.parametrize("value, shown", [
    (1000.5, "1000"), (1250.4, "1250"), (1200.5, "1200"), (1999.6, "2000"), (10500.3, "10500"),
    (999.96, "1000"), (9999.6, "10000"), (84.9863, "84.99"), (0.0123, "0.0123"), (7.0, "7"), (2000.0, "2000"), (5.20, "5.2"),
])
def test_values_keep_all_their_whole_digits(value, shown):
    assert fn._fmt(value) == shown


def test_large_result_and_range_read_true():
    r = norm(code="2157-6", display="CK", valueQuantity={"value": 1250.4, "unit": "U/L"},
             referenceRange=[{"low": {"value": 1000.4}, "high": {"value": 2000.4}}])
    assert r["value_text"] == "1250 U/L" and r["ref_text"] == "1000–2000"


# ---- F002: comparators

def test_a_converted_bound_stays_a_bound():
    r = norm(valueQuantity={"value": 2.2, "unit": "mmol/L", "comparator": "<"})
    assert r["value_text"] == "<2.2 mmol/L"
    assert r["details"]["comparator"] == "<"
    assert r["details"]["converted_value"] == "<39.6 mg/dL"
    assert r["value_norm"] == 39.6


@pytest.mark.parametrize("comparator, value, low, high, flag", [
    ("<", 5, None, 3, None),           # could be under 3 or over
    (">", 1000, None, 2000, None),
    (">", 60, 60, None, "normal"),     # eGFR >60
    (">=", 60, 60, None, "normal"),
    ("<", 0.01, None, 0.04, "normal"),  # troponin
    ("<", 0.3, 0.1, 3, None),          # could be under the low limit
    ("<", 0.3, 0, 3, "normal"),        # nothing can be under zero
    ("<", 5, 10, 20, "low"),
    ("<", 10, 10, 20, "low"),
    ("<=", 10, 10, 20, None),
    (">", 25, 10, 20, "high"),
    (">", 20, 10, 20, "high"),
    (">=", 20, 10, 20, None),
    ("ad", 5, 1, 10, None),
])
def test_a_bound_is_only_flagged_when_all_of_it_agrees(comparator, value, low, high, flag):
    rng = {**({"low": {"value": low}} if low is not None else {}), **({"high": {"value": high}} if high is not None else {})}
    r = norm(code="1-1", display="x", valueQuantity={"value": value, "unit": "mg/L", "comparator": comparator}, referenceRange=[rng])
    assert r["interpretation"] == flag


def test_the_source_flag_still_wins_over_a_bound():
    r = norm(code="1-1", display="x", valueQuantity={"value": 5, "unit": "mg/L", "comparator": "<"},
             referenceRange=[{"high": {"value": 3}}], interpretation=[{"coding": [{"code": "N"}]}])
    assert r["interpretation"] == "normal"


def test_numeric_text_is_a_number_and_a_bound_in_text_stays_one():
    assert norm(code="1-1", display="x", valueString="5.4")["value_num"] == 5.4
    r = norm(code="1-1", display="x", valueString="<0.5", referenceRange=[{"high": {"value": 3}}])
    assert r["value_num"] == 0.5 and r["details"]["comparator"] == "<" and r["value_text"] == "<0.5" and r["interpretation"] == "normal"
    assert norm(code="1-1", display="x", valueString="1:40")["value_num"] is None


# ---- F003: units

def test_the_ucum_code_converts_when_the_display_is_free_text():
    r = norm(code="29463-7", display="Weight", category=[{"coding": [{"code": "vital-signs"}]}],
             valueQuantity={"value": 180, "unit": "pounds", "system": "http://unitsofmeasure.org", "code": "[lb_av]"})
    assert r["value_norm"] == 81.647 and r["unit_norm"] == "kg" and r["value_text"] == "180 lb"
    r = norm(code="8310-5", display="Temp", category=[{"coding": [{"code": "vital-signs"}]}],
             valueQuantity={"value": 98.6, "unit": "degrees F", "code": "[degF]"})
    assert r["value_norm"] == 37.0 and r["unit_norm"] == "°C"


def test_a_code_from_another_system_is_not_a_unit():
    q = {"value": 5, "unit": "mg/dL", "system": "http://snomed.info/sct", "code": "258797006"}
    assert norm(valueQuantity=q)["unit_norm"] == "mg/dL"
    assert units.quantity_unit(q) == "mg/dL"


@pytest.mark.parametrize("code, value, unit, expected", [
    ("1989-3", 74.88, "nmol/L", (30.0, "ng/mL")),
    ("14635-7", 74.88, "nmol/L", (30.0, "ng/mL")),
    ("4548-4", 53, "mmol/mol", (7.0, "%")),
    ("2986-8", 10, "nmol/L", (288.4, "ng/dL")),
    ("2132-9", 300, "pmol/L", (406.5, "pg/mL")),
    ("1988-5", 1.0, "mg/dL", (10.0, "mg/L")),
    ("1751-7", 40, "g/L", (4.0, "g/dL")),
    ("1975-2", 17.1, "µmol/L", (1.0, "mg/dL")),
    ("2951-2", 140, "mEq/L", (140, "mmol/L")),
    ("2160-0", 0.85, "mg/dl", (0.85, "mg/dL")),
    ("2160-0", 77, "umol/L", (0.871, "mg/dL")),
])
def test_known_conversions(code, value, unit, expected):
    assert units.normalize(code, value, unit) == expected


def test_a_unit_with_no_known_factor_is_never_converted():
    assert units.normalize("1989-3", 30, "mg/dL") == (30, "mg/dL")
    assert units.normalize("9999-9", 30, "nmol/L") == (30, "nmol/L")


@pytest.mark.parametrize("a, b", [
    ("mg/dL", "mg/dl"), ("mg/dL", "mg/dL (calc)"), ("mg/dL", "mg/dL{calc}"), ("ng/mL", "ug/L"), ("ng/mL", "mcg/L"), ("pg/mL", "ng/L"),
    ("mIU/L", "uIU/mL"), ("mIU/L", "m[IU]/L"), ("10*3/uL", "10*9/L"), ("10*3/uL", "K/uL"), ("10*3/uL", "x10E3/uL"), ("10*3/uL", "×10³/µL"),
    ("µg/dL", "ug/dL"), ("U/L", "IU/L"), ("lb", "pounds"), ("°F", "degrees F"),
])
def test_the_same_unit_spelled_differently_is_one_unit(a, b):
    assert units.unit_key(a) == units.unit_key(b)


@pytest.mark.parametrize("a, b", [("mg/dL", "mg/L"), ("ng/mL", "ng/dL"), ("mmol/L", "umol/L"), ("mg/dL", "mmol/L"), ("g/L", "10*9/L")])
def test_different_units_stay_different(a, b):
    assert units.unit_key(a) != units.unit_key(b)


@pytest.mark.parametrize("a, b", [
    ("K/uL", "K/mcL"), ("10*3/uL", "10*3/mcL"), ("K/uL", "thou/mcL"), ("10*3/uL", "x10(3)/mcL"), ("10*6/uL", "M/mcL"), ("10*6/uL", "mill/uL"),
    ("mL/min/1.73m²", "mL/min/1.73m2"), ("mL/min/1.73m²", "mL/min/1.73 m2"), ("mL/min/1.73m²", "mL/min/{1.73_m2}"), ("mL/min/1.73m²", "mL/min/1.73_m2"),
    ("mL/min/1.73m²", "mL/min/1.73 m^2"), ("mL/min/1.73m²", "mL/min per 1.73 m2"), ("mL/min/1.73m²", "mL/min/1.73 sq m"), ("mm/hr", "mm/h"), ("U/L", "mIU/mL"),
])
def test_common_spellings_of_one_unit_are_one_unit(a, b):
    assert units.unit_key(a) == units.unit_key(b)


@pytest.mark.parametrize("a, b", [("mL/min/1.73m2", "mL/min"), ("K/uL", "/uL"), ("mcL", "mL"), ("mIU/L", "mIU/mL")])
def test_units_that_only_look_alike_stay_different(a, b):
    assert units.unit_key(a) != units.unit_key(b)


# ---- F273: nothing blank

def test_blood_pressure_with_no_values_says_so_rather_than_nothing():
    systolic = {"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]}}
    r = norm(code="85354-6", display="Blood pressure", component=[{**systolic, "dataAbsentReason": {"text": "Patient refused"}}])
    assert r["value_text"] == "Systolic blood pressure: Patient refused" and r["unit"] is None
    assert norm(code="85354-6", display="Blood pressure", component=[systolic])["value_text"] == "Systolic blood pressure: No value"
    r = norm(code="85354-6", display="Blood pressure", component=[{**systolic, "valueQuantity": {"value": 120, "unit": "mmHg"}}])
    assert r["value_text"] == "Systolic 120 mmHg" and r["unit"] == "mmHg"


# ---- F008, F079: ranges

def test_value_and_range_read_in_the_same_unit():
    r = norm(code="2160-0", display="Creatinine", valueQuantity={"value": 77, "unit": "umol/L"},
             referenceRange=[{"low": {"value": 62}, "high": {"value": 106}}])
    assert r["value_text"] == "77 umol/L" and r["ref_text"] == "62–106"
    assert r["value_norm"] == 0.871 and r["ref_high"] == 1.199 and r["interpretation"] == "normal"
    assert r["details"]["source_range"] == {"low": 62, "high": 106}


def test_rounding_a_conversion_cant_move_a_result_across_the_limit():
    r = norm(code="2160-0", display="Creatinine", valueQuantity={"value": 106, "unit": "umol/L"},
             referenceRange=[{"low": {"value": 62}, "high": {"value": 106}}])
    assert r["interpretation"] == "normal"


def test_a_range_in_its_own_unit_converts_from_that_unit():
    r = norm(valueQuantity={"value": 100, "unit": "mg/dL"},
             referenceRange=[{"low": {"value": 3.9, "unit": "mmol/L"}, "high": {"value": 5.5, "unit": "mmol/L"}}])
    assert r["ref_low"] == 70.3 and r["ref_high"] == 99.1 and r["interpretation"] == "high"
    assert r["ref_text"] == "3.9–5.5 mmol/L"


def test_a_range_in_an_unknown_other_unit_gives_no_flag():
    r = norm(code="1-1", display="x", valueQuantity={"value": 100, "unit": "ng/mL"},
             referenceRange=[{"low": {"value": 1, "unit": "nmol/L"}, "high": {"value": 5, "unit": "nmol/L"}}])
    assert r["interpretation"] is None and r["ref_low"] is None and r["ref_text"] == "1–5 nmol/L"


@pytest.mark.parametrize("value_unit, range_unit", [("mg/dl", "mg/dL"), ("µmol/L", "umol/L"), ("K/uL", "10*3/uL")])
def test_a_range_spelled_differently_still_flags(value_unit, range_unit):
    r = norm(code="1-1", display="x", valueQuantity={"value": 100, "unit": value_unit},
             referenceRange=[{"high": {"value": 50, "unit": range_unit}}])
    assert r["interpretation"] == "high"


def test_a_critical_range_listed_first_is_not_the_normal_range():
    r = norm(code="2823-3", display="K", valueQuantity={"value": 6, "unit": "mmol/L"}, referenceRange=[
        {"type": {"coding": [{"code": "critical"}]}, "low": {"value": 2.5}, "high": {"value": 6.5}},
        {"type": {"coding": [{"code": "normal"}]}, "low": {"value": 3.5}, "high": {"value": 5.0}}])
    assert r["interpretation"] == "high" and r["ref_text"] == "3.5–5"


MALE = {"appliesTo": [{"coding": [{"code": "male"}], "text": "Male"}], "low": {"value": 13.5}, "high": {"value": 17.5}}
FEMALE = {"appliesTo": [{"coding": [{"code": "female"}], "text": "Female"}], "low": {"value": 12.0}, "high": {"value": 15.5}}


def test_ranges_for_each_sex_choose_by_the_person():
    res = obs(code="718-7", display="Hgb", valueQuantity={"value": 12.5, "unit": "g/dL"}, referenceRange=[MALE, FEMALE])
    r = fn.normalize_observation(res, {"gender": "female", "birth_date": "1980-01-01"})
    assert r["ref_text"] == "12–15.5" and r["interpretation"] == "normal"
    r = fn.normalize_observation(res, {"gender": "male"})
    assert r["ref_text"] == "13.5–17.5" and r["interpretation"] == "low"


def test_ranges_for_each_sex_with_nothing_to_choose_by_give_no_flag():
    r = norm(code="718-7", display="Hgb", valueQuantity={"value": 12.5, "unit": "g/dL"}, referenceRange=[MALE, FEMALE])
    assert r["interpretation"] is None and r["ref_low"] is None and r["ref_high"] is None
    assert r["ref_text"] == "Several ranges, see the record"
    assert [x["for"] for x in r["details"]["ranges"]] == ["Male", "Female"]


def test_an_age_range_applies_by_age_at_the_result():
    child = {"age": {"low": {"value": 0, "unit": "a"}, "high": {"value": 12, "unit": "a"}}, "low": {"value": 9.5}, "high": {"value": 11}}
    adult = {"age": {"low": {"value": 18, "unit": "a"}}, "low": {"value": 12}, "high": {"value": 17}}
    res = obs(code="718-7", display="Hgb", valueQuantity={"value": 13, "unit": "g/dL"}, referenceRange=[child, adult])
    assert fn.normalize_observation(res, {"birth_date": "1980-01-01"})["interpretation"] == "normal"
    assert fn.normalize_observation(res, {"birth_date": "2020-01-01"})["interpretation"] == "high"


def test_the_patient_in_the_set_chooses_the_range():
    patient = {"resourceType": "Patient", "id": "p", "gender": "female", "birthDate": "1980-01-01"}
    _, recs = fn.normalize_resources([obs(code="718-7", display="Hgb", valueQuantity={"value": 12.5, "unit": "g/dL"},
                                          referenceRange=[MALE, FEMALE]), patient])
    assert recs[0]["interpretation"] == "normal"


# ---- F079, F135: flags

@pytest.mark.parametrize("text, flag", [
    ("Above normal", "high"), ("Elevated", "high"), ("Increased", "high"), ("Below normal", "low"), ("Decreased", "low"),
    ("Critical high", "critical_high"), ("Critically low", "critical_low"),("Critical low", "critical_low"),
    ("Abnormal", "abnormal"), ("Normal", "normal"), ("Not detected", "normal"), ("Detected", "abnormal"), ("Positive", "abnormal"),
])
def test_words_for_a_flag(text, flag):
    r = norm(code="1-1", display="x", valueString="see note", interpretation=[{"text": text}])
    assert r["interpretation"] == flag


def test_a_proprietary_code_listed_first_doesnt_hide_the_standard_one():
    r = norm(code="1-1", display="x", valueString="see note", interpretation=[{"coding": [
        {"system": "urn:oid:1.2.3", "code": "123"}, {"system": fn.V3_INTERPRETATION, "code": "H"}]}])
    assert r["interpretation"] == "high"


def test_a_display_only_flag_counts():
    r = norm(code="1-1", display="x", valueString="see note", interpretation=[{"coding": [{"display": "High"}]}])
    assert r["interpretation"] == "high"


def test_a_number_out_of_range_overrides_the_word_normal():
    r = norm(code="1-1", display="x", valueQuantity={"value": 9, "unit": "x"}, referenceRange=[{"high": {"value": 5}}],
             interpretation=[{"text": "Normal"}])
    assert r["interpretation"] == "high"


@pytest.mark.parametrize("value, flag", [(9, "critical_high"), (0.5, "critical_low"), (3, "abnormal")])
def test_critical_takes_its_direction_from_the_range(value, flag):
    r = norm(code="1-1", display="x", valueQuantity={"value": value, "unit": "x"}, referenceRange=[{"low": {"value": 1}, "high": {"value": 5}}],
             interpretation=[{"text": "Critical"}])
    assert r["interpretation"] == flag


# ---- F030, F273: every kind of value

@pytest.mark.parametrize("field, value, text", [
    ("valueRatio", {"numerator": {"value": 1}, "denominator": {"value": 40}}, "1:40"),
    ("valueRatio", {"numerator": {"value": 1, "comparator": "<"}, "denominator": {"value": 40}}, "<1:40"),
    ("valueRange", {"low": {"value": 2, "unit": "/[HPF]"}, "high": {"value": 5, "unit": "/[HPF]"}}, "2–5/HPF"),
    ("valueRange", {"low": {"value": 2, "unit": "/HPF"}}, "≥ 2/HPF"),
    ("valueTime", "08:30:00", "08:30:00"),
    ("valueDateTime", "2025-02-03", "2025-02-03"),
    ("valuePeriod", {"start": "2025-01-01", "end": "2025-01-05"}, "2025-01-01–2025-01-05"),
    ("valueSampledData", {"data": "1 2 3"}, "Waveform data, see the original"),
    ("valueAttachment", {"url": "x"}, "See the original"),
    ("valueCodeableConcept", {"coding": [{"code": "POS"}]}, "POS"),
])
def test_results_that_arent_a_single_number_are_kept_as_text(field, value, text):
    r = norm(code="1-1", display="x", **{field: value})
    assert r["value_text"] == text and r["value_num"] is None and r["raw"][field] == value


def test_a_panel_header_is_still_dropped():
    res = obs(code="24323-8", display="CMP", hasMember=[{"reference": "Observation/1"}])
    assert fn.normalize_observation(res) is None


def test_a_quantity_with_no_value_doesnt_show_a_bare_unit():
    assert norm(code="1-1", display="x", valueQuantity={"unit": "mg/dL"})["value_text"] == "No value"
    r = norm(code="1-1", display="x", valueQuantity={"unit": "mg/dL"}, dataAbsentReason={"text": "Not performed"})
    assert r["value_text"] == "Not performed"


def bp(*comps, code="85354-6"):
    return norm(code=code, display="BP", category=[{"coding": [{"code": "vital-signs"}]}], component=list(comps))


def comp(code, name, value):
    return {"code": {"coding": [{"code": code, **({"display": name} if name else {})}]}, "valueQuantity": {"value": value, "unit": "mmHg"}}


def test_blood_pressure_never_reads_by_position():
    assert bp(comp("8462-4", None, 80), comp("8480-6", None, 120))["value_text"] == "120/80 mmHg"
    assert bp(comp("L2", "Dia BP", 80), comp("L1", "Syst BP", 120))["value_text"] == "120/80 mmHg"
    assert bp(comp("L2", "Lower", 80), comp("L1", "Upper", 120))["value_text"] == "120/80 mmHg"
    assert bp(comp("8480-6", None, 120))["value_text"] == "Systolic 120 mmHg"
    assert bp(comp("8462-4", None, 80))["value_text"] == "Diastolic 80 mmHg"
    names = {c["name"] for c in bp(comp("8462-4", None, 80), comp("8480-6", None, 120))["details"]["components"]}
    assert names == {"Systolic blood pressure", "Diastolic blood pressure"}


def test_components_of_any_kind_print_their_value():
    r = norm(code="1-1", display="Titers", component=[
        {"code": {"text": "A"}, "valueRatio": {"numerator": {"value": 1}, "denominator": {"value": 80}}},
        {"code": {"text": "B"}, "valueInteger": 4},
        {"code": {"text": "C"}, "valueRange": {"low": {"value": 1}, "high": {"value": 2}}},
        {"code": {"text": "D"}, "valueQuantity": {"value": 2, "unit": "mg/L", "comparator": "<"}}])
    assert r["value_text"] == "A: 1:80 · B: 4 · C: 1–2 · D: <2 mg/L"


def test_dropped_resources_are_counted():
    _, recs, problems = fn.normalize_report([
        {"resourceType": "Observation", "id": "a", "status": "final", "code": {"text": "empty"}},
        obs(id="b", code="24323-8", display="Panel", hasMember=[{"reference": "Observation/1"}]),
        obs(id="c", status="entered-in-error", valueQuantity={"value": 1}),
        {"resourceType": "Observation", "id": "d", "code": {"text": "x"}, "category": 5, "valueString": "y"}])
    assert not recs
    assert problems == {"failed": {"Observation": 1}, "skipped": {"Observation": 1}}


# ---- F043: identity across sources

def test_one_moment_written_two_ways_is_one_result():
    a = norm(valueQuantity={"value": 99, "unit": "mg/dL"}, effectiveDateTime="2025-01-01T08:30:00-05:00")
    b = norm(valueQuantity={"value": 99, "unit": "mg/dl"}, effectiveDateTime="2025-01-01T13:30:00Z")
    assert a["dedup_key"] == b["dedup_key"]


def test_a_time_with_no_zone_or_a_bare_date_isnt_an_instant():
    a = norm(valueQuantity={"value": 99, "unit": "mg/dL"}, effectiveDateTime="2025-01-01T13:30:00")
    b = norm(valueQuantity={"value": 99, "unit": "mg/dL"}, effectiveDateTime="2025-01-01T13:30:00Z")
    c = norm(valueQuantity={"value": 99, "unit": "mg/dL"}, effectiveDateTime="2025-01-01")
    assert a["dedup_key"].split("|")[2] == "2025-01-01T13:30" and c["dedup_key"].split("|")[2] == "2025-01-01"
    assert b["dedup_key"].split("|")[2] == "2025-01-01T13:30"


def test_a_bound_and_a_measurement_dont_merge():
    a = norm(code="1-1", display="x", valueQuantity={"value": 40, "unit": "mg/L", "comparator": "<"})
    b = norm(code="1-1", display="x", valueQuantity={"value": 40, "unit": "mg/L"})
    assert a["dedup_key"] != b["dedup_key"]


def test_code_systems_written_as_oids():
    loinc = norm(code="2345-7", display="Glucose", valueString="x")
    res = obs(valueString="x")
    res["code"] = {"coding": [{"system": "urn:oid:2.16.840.1.113883.6.99", "code": "L"},
                              {"system": "urn:oid:2.16.840.1.113883.6.1", "code": "2345-7", "display": "Glucose"}]}
    r = fn.normalize_observation(res)
    assert r["code"] == loinc["code"] and r["code_system"] == fn.LOINC
