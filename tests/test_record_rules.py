"""What the record says once sources are merged: current medications, active problems, allergies, and one test's history."""

from datetime import date, timedelta

import pytest

from app.services import sync
from app.store import records
from tests.conftest import connect_institution

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"


def recent(days=30):
    return (date.today() - timedelta(days=days)).isoformat()


def lab(rid, value, unit, when, code="1989-3", title="Vitamin D", comparator=None, ranges=None, **kw):
    q = {"value": value, **({"unit": unit} if unit else {}), **({"comparator": comparator} if comparator else {})}
    code_block = {"coding": [{"system": LOINC, "code": code, "display": title}], "text": title} if code else {"text": title}
    return {"resourceType": "Observation", "id": rid, "status": kw.pop("status", "final"), "category": [{"coding": [{"code": "laboratory"}]}],
            "code": code_block, "effectiveDateTime": when, "valueQuantity": q,
            **({"referenceRange": ranges} if ranges else {}), **kw}


def condition(rid, title, status="active", snomed=None, icd=None, category="problem-list-item", when="2015-01-01", **kw):
    coding = ([{"system": SNOMED, "code": snomed, "display": title}] if snomed else []) + ([{"system": ICD10, "code": icd}] if icd else [])
    return {"resourceType": "Condition", "id": rid, **({"clinicalStatus": {"coding": [{"code": status}]}} if status else {}),
            "code": {"coding": coding, "text": title}, **({"category": [{"coding": [{"code": category}]}]} if category else {}),
            "onsetDateTime": when, **kw}


def allergy(rid, title, status="active", when="2010-01-01", **kw):
    return {"resourceType": "AllergyIntolerance", "id": rid, "clinicalStatus": {"coding": [{"code": status}]},
            "code": {"coding": [{"system": RXNORM, "code": "7980", "display": title}], "text": title}, "recordedDate": when, **kw}


def med(rid, title, status="active", when=None, rxnorm=None, intent="order", **kw):
    coding = [{"system": RXNORM, "code": rxnorm, "display": title}] if rxnorm else []
    return {"resourceType": "MedicationRequest", "id": rid, "status": status, "intent": intent, "authoredOn": when or recent(),
            "medicationCodeableConcept": {"coding": coding, "text": title}, **kw}


@pytest.fixture
def prof(client):
    return client.get("/api/profiles").json()["profiles"][0]["id"]


def load(prof, source, *resources):
    sync.import_fhir_resources(prof, source, list(resources))


def titles(rows):
    return [r["title"] for r in rows]


# ---- F003: one axis, one unit

def test_a_second_unit_is_a_separate_series(prof):
    load(prof, "A", lab("a", 30, "ng/mL", "2024-01-01", code="62292-8"), lab("b", 75, "umol/kg", "2025-01-01", code="62292-8"))
    s = records.observation_series(prof, "62292-8", "labs")
    assert s["unit"] == "umol/kg" and [p["v"] for p in s["points"]] == [75]
    assert [(o["unit"], [p["v"] for p in o["points"]]) for o in s["other_units"]] == [("ng/mL", [30])]
    assert records.observation_catalog(prof, "labs")[0]["trend"] is None


def test_a_known_conversion_joins_the_series(prof):
    load(prof, "A", lab("a", 30, "ng/mL", "2024-01-01"), lab("b", 74.88, "nmol/L", "2025-01-01", code="14635-7"))
    s = records.observation_series(prof, "1989-3", "labs")
    assert s["unit"] == "ng/mL" and [p["v"] for p in s["points"]] == [30, 30.0] and not s["other_units"]
    assert s["points"][1]["converted"] is True and s["points"][1]["text"] == "74.88 nmol/L"
    assert records.observation_catalog(prof, "labs")[0]["trend"] == "flat"


def test_the_same_unit_spelled_two_ways_doesnt_split_a_series(prof):
    load(prof, "A", lab("a", 100, "ng/mL", "2024-01-01", code="2276-4", title="Ferritin"),
         lab("b", 120, "ug/L", "2025-01-01", code="2276-4", title="Ferritin"), lab("c", 90, "mcg/L", "2026-01-01", code="2276-4", title="Ferritin"))
    s = records.observation_series(prof, "2276-4", "labs")
    assert len(s["points"]) == 3 and not s["other_units"]


def test_weight_in_pounds_converts_on_the_chart(prof):
    weight = {"category": [{"coding": [{"code": "vital-signs"}]}]}
    load(prof, "A", lab("a", 80, "kg", "2024-01-01", code="29463-7", title="Weight", **weight),
         lab("b", 180, "pounds", "2025-01-01", code="29463-7", title="Weight", **weight))
    s = records.observation_series(prof, "29463-7", "vitals")
    assert s["unit"] == "kg" and [p["v"] for p in s["points"]] == [80, 81.647]


def test_the_range_comes_only_from_the_same_unit(prof):
    load(prof, "A", lab("a", 30, "ng/mL", "2024-01-01", code="62292-8", ranges=[{"low": {"value": 20}, "high": {"value": 50}}]),
         lab("b", 75, "umol/kg", "2025-01-01", code="62292-8"))
    s = records.observation_series(prof, "62292-8", "labs")
    assert s["ref_low"] is None and s["ref_high"] is None and s["other_units"][0]["ref_high"] == 50


def test_results_in_one_foreign_unit_stay_in_it_with_their_range(prof):
    """Someone reading mmol/L sees mmol/L: the value and the range the lab reported, not a rounded conversion."""
    load(prof, "A", lab("a", 5.1, "mmol/L", "2024-01-01", code="2345-7", title="Glucose", ranges=[{"low": {"value": 3.9}, "high": {"value": 5.5}}]),
         lab("b", 5.6, "mmol/L", "2025-01-01", code="2345-7", title="Glucose", ranges=[{"low": {"value": 3.9}, "high": {"value": 5.5}}]))
    s = records.observation_series(prof, "2345-7", "labs")
    assert s["unit"] == "mmol/L" and [p["v"] for p in s["points"]] == [5.1, 5.6] and (s["ref_low"], s["ref_high"]) == (3.9, 5.5)


def test_a_result_with_no_unit_joins_the_only_unit_and_otherwise_stands_apart(prof):
    load(prof, "A", lab("a", 5, "mg/L", "2024-01-01", code="1-1", title="X"), lab("b", 6, None, "2025-01-01", code="1-1", title="X"))
    assert [p["v"] for p in records.observation_series(prof, "1-1")["points"]] == [5, 6]
    load(prof, "B", lab("c", 7, None, "2025-02-01", code="2-2", title="Y"), lab("d", 8, "mg/L", "2025-03-01", code="2-2", title="Y"),
         lab("e", 9, "ng/L", "2025-04-01", code="2-2", title="Y"))
    s = records.observation_series(prof, "2-2")
    assert s["unit"] == "ng/L" and {o["unit"] for o in s["other_units"]} == {"mg/L", None}


# ---- F002: a bound everywhere

def test_series_carry_the_bound_and_a_bound_isnt_a_trend(prof):
    load(prof, "A", lab("a", 3, "mg/L", "2024-01-01", code="1-1", title="X"),
         lab("b", 2, "mg/L", "2025-01-01", code="1-1", title="X", comparator="<"))
    s = records.observation_series(prof, "1-1")
    assert s["points"][1]["comparator"] == "<" and s["points"][1]["text"] == "<2 mg/L" and "comparator" not in s["points"][0]
    assert records.observation_catalog(prof, "labs")[0]["trend"] is None


def test_a_report_lists_its_results_with_what_the_lab_said(client, prof):
    report = {"resourceType": "DiagnosticReport", "id": "r", "status": "final", "category": [{"coding": [{"code": "LAB"}]}],
              "code": {"text": "Panel"}, "effectiveDateTime": "2025-01-01", "result": [{"reference": "Observation/a"}]}
    load(prof, "A", lab("a", 0.3, "mg/L", "2025-01-01", code="1-1", title="X", comparator="<"), report)
    rid = next(r["id"] for r in client.get("/api/records", params={"category": "reports"}).json()["items"])
    assert client.get(f"/api/records/{rid}").json()["results"][0]["value_text"] == "<0.3 mg/L"


def test_results_that_arent_numbers_are_in_the_history(prof):
    text = lab("t", None, None, "2025-02-01", code="1-1", title="X")
    del text["valueQuantity"]
    text["valueString"] = "Hemolyzed, see note"
    load(prof, "A", lab("a", 3, "mg/L", "2024-01-01", code="1-1", title="X"), text)
    s = records.observation_series(prof, "1-1")
    assert [p["v"] for p in s["points"]] == [3] and [t["text"] for t in s["text_results"]] == ["Hemolyzed, see note"]


# ---- F078, F082, F140: one test, however it is filed

def test_equivalent_codes_are_one_test_so_a_new_result_replaces_an_old_flag(prof):
    high = [{"high": {"value": 5.6}}]
    load(prof, "A", lab("old", 7.9, "%", "2017-01-01", code="17856-6", title="HbA1c", ranges=high),
         lab("new", 5.4, "%", recent(), code="4548-4", title="Hemoglobin A1c", ranges=high),
         lab("o2", 190, "mg/dL", "2018-01-01", code="13457-7", title="LDL calc", ranges=[{"high": {"value": 100}}]),
         lab("n2", 80, "mg/dL", recent(), code="18262-6", title="LDL direct", ranges=[{"high": {"value": 100}}]))
    assert records.flagged_labs(prof) == [] and records.summary(prof)["flagged_labs"] == []
    catalog = records.observation_catalog(prof, "labs")
    assert sorted(i["count"] for i in catalog) == [2, 2]
    s = records.observation_series(prof, "17856-6")
    assert [p["v"] for p in s["points"]] == [7.9, 5.4] and set(s["codes"]) == {"17856-6", "4548-4"}


def test_a1c_in_mmol_per_mol_joins_the_percent_history(prof):
    load(prof, "A", lab("a", 53, "mmol/mol", "2024-01-01", code="59261-8", title="A1c IFCC"), lab("b", 7.1, "%", "2025-01-01", code="4548-4", title="A1c"))
    s = records.observation_series(prof, "4548-4")
    assert s["unit"] == "%" and [p["v"] for p in s["points"]] == [7.0, 7.1]


def test_serum_and_whole_blood_glucose_stay_apart(prof):
    load(prof, "A", lab("a", 90, "mg/dL", "2024-01-01", code="2345-7", title="Glucose"), lab("b", 90, "mg/dL", "2024-02-01", code="2339-0", title="Glucose, blood"))
    assert len(records.observation_catalog(prof, "labs")) == 2


def test_vitamin_d3_and_total_d_stay_apart(prof):
    load(prof, "A", lab("a", 30, "ng/mL", "2024-01-01", code="1989-3"), lab("b", 30, "ng/mL", "2024-02-01", code="62292-8"))
    assert len(records.observation_catalog(prof, "labs")) == 2


def test_a_local_code_joins_by_its_name(prof):
    local = lab("b", 80, "mg/dL", "2025-01-01", code=None, title="LDL cholesterol")
    local["code"] = {"coding": [{"system": "urn:example:lab", "code": "LOC1", "display": "LDL cholesterol"}], "text": "LDL cholesterol"}
    load(prof, "A", lab("a", 100, "mg/dL", "2024-01-01", code="13457-7", title="LDL"), local)
    assert len(records.observation_catalog(prof, "labs")) == 1
    for code in ("13457-7", "LOC1"):
        assert [p["v"] for p in records.observation_series(prof, code)["points"]] == [100, 80]


def test_a_flagged_result_this_old_is_marked_as_older(prof):
    load(prof, "A", lab("a", 260, "mg/dL", "2016-01-01", code="2093-3", title="Cholesterol", ranges=[{"high": {"value": 200}}]),
         lab("b", 260, "mg/L", recent(), code="1-1", title="X", ranges=[{"high": {"value": 200}}]))
    flagged = {l["title"]: l for l in records.flagged_labs(prof)}
    assert flagged["Cholesterol"]["older_result"] is True and "older_result" not in flagged["X"]


def test_a_lab_with_no_code_has_a_chart(client, prof):
    load(prof, "A", lab("a", 9.9, "mg/dL", "2025-01-01", code=None, title="In-house potassium"),
         lab("b", 9.1, "mg/dL", "2025-02-01", code=None, title="In-house potassium"))
    item = client.get("/api/observations", params={"category": "labs"}).json()["items"][0]
    assert item["code"] is None and item["key"]
    s = client.get("/api/observations/series", params={"title": item["title"], "category": "labs"})
    assert s.status_code == 200 and [p["v"] for p in s.json()["points"]] == [9.9, 9.1]


def test_the_chart_endpoint_says_what_it_needs(client):
    assert client.get("/api/observations/series").status_code == 422


def test_an_unknown_test_is_an_empty_chart(client):
    s = client.get("/api/observations/series", params={"title": "Nothing like this"}).json()
    assert s["points"] == [] and s["other_units"] == [] and s["text_results"] == []


# ---- F007: safety beats tidiness

def test_a_newer_inactive_entry_doesnt_hide_an_active_allergy(prof):
    load(prof, "Site A", allergy("a", "Penicillin", criticality="high",
                                 reaction=[{"manifestation": [{"text": "Anaphylaxis"}], "severity": "severe"}]))
    load(prof, "Site B", allergy("b", "Penicillin", status="inactive", when="2024-05-01"))
    s = records.summary(prof)
    assert titles(s["allergies"]) == ["Penicillin"]
    shown = s["allergies"][0]
    assert shown["details"]["criticality"] == "high" and shown["narrative"].startswith("Anaphylaxis")
    assert shown["status_conflict"] is True
    assert shown["status_note"] == "Marked inactive by Site B on May 1, 2024. Still active at Site A."
    assert {x["source_name"]: x["status"] for x in shown["sources"]} == {"Site A": "active", "Site B": "inactive"}


def test_an_allergy_a_source_resolved_itself_stays_resolved(prof):
    load(prof, "Site A", allergy("a", "Penicillin"), allergy("b", "Penicillin", status="resolved", when="2024-05-01"))
    s = records.summary(prof)
    assert s["allergies"] == [] and s["allergy_note"] == "No active allergies. 1 marked inactive."


def test_a_resolved_elsewhere_problem_stays_active_with_a_note(prof):
    load(prof, "Site A", condition("a", "Hypertension", snomed="38341003"))
    load(prof, "Site B", condition("b", "Hypertension", status="resolved", snomed="38341003", recordedDate="2023-01-01"))
    active = records.summary(prof)["active_conditions"]
    assert titles(active) == ["Hypertension"] and "Still active at Site A" in active[0]["status_note"]


def test_a_visit_diagnosis_doesnt_replace_or_revive_a_problem(prof):
    load(prof, "A", condition("p", "Hypertension", snomed="38341003", when="2015-01-01"),
         condition("v", "Hypertension", status="resolved", snomed="38341003", category="encounter-diagnosis", when="2024-01-01"))
    assert titles(records.summary(prof)["active_conditions"]) == ["Hypertension"]

    load(prof, "B", condition("p2", "Migraine", status="resolved", snomed="37796009", when="2015-01-01"),
         condition("v2", "Migraine", status=None, snomed="37796009", category="encounter-diagnosis", when="2011-01-01"))
    s = records.summary(prof)
    assert "Migraine" not in titles(s["active_conditions"]) and "Migraine" not in titles(s["conditions_status_unknown"])


def test_a_problem_resolved_at_its_own_source_is_resolved(prof):
    load(prof, "A", condition("a", "Asthma", snomed="195967001", recordedDate="2015-01-01"),
         condition("b", "Asthma", status="resolved", snomed="195967001", recordedDate="2023-01-01"))
    assert records.summary(prof)["active_conditions"] == []


def test_a_medication_active_at_one_source_stays_with_a_note(prof):
    load(prof, "A", med("a", "Amlodipine 5 MG", rxnorm="197361", when=recent(100)))
    load(prof, "B", med("b", "Amlodipine 5 MG", status="stopped", rxnorm="197361", when=recent(10)))
    current = records.summary(prof)["active_medications"]
    assert titles(current) == ["Amlodipine 5 MG"] and "Marked stopped by B" in current[0]["status_note"]


def test_a_stopped_order_at_one_source_ends_it_there(prof):
    load(prof, "A", med("a", "Amlodipine 5 MG", rxnorm="197361", when=recent(100)), med("b", "Amlodipine 5 MG", status="stopped", rxnorm="197361", when=recent(10)))
    assert records.summary(prof)["active_medications"] == []


def test_also_reported_by_says_what_each_source_says(client, prof):
    load(prof, "Site A", allergy("a", "Penicillin"))
    load(prof, "Site B", allergy("b", "Penicillin", status="inactive", when="2024-05-01"))
    shown = client.get("/api/summary").json()["allergies"][0]
    dup = client.get(f"/api/records/{shown['id']}").json()["duplicates"]
    assert [(d["source_name"], d["status"]) for d in dup] == [("Site B", "inactive")]


# ---- F029: active problems

def test_only_real_active_problems_are_active(prof):
    load(prof, "A",
         condition("a", "Hypertension", snomed="1"),
         condition("b", "Relapsing thing", status="relapse", snomed="2"),
         condition("c", "Back again", status="recurrence", snomed="3"),
         condition("d", "No category", snomed="4", category=None),
         condition("e", "Concern", snomed="5", category="health-concern"),
         condition("f", "Possible lupus", snomed="6", verificationStatus={"coding": [{"code": "differential"}]}),
         condition("g", "Maybe", snomed="7", verificationStatus={"coding": [{"code": "provisional"}]}),
         condition("h", "Perhaps", snomed="8", verificationStatus={"coding": [{"code": "unconfirmed"}]}),
         condition("i", "Acute sinusitis", snomed="9", category="encounter-diagnosis"),
         condition("j", "Sprained ankle", status=None, snomed="10", abatementDateTime="2019-01-01"),
         condition("k", "Flu", snomed="11", abatementDateTime="2019-02-01"),
         condition("l", "Long ago", snomed="12", abatementString="as a child"),
         condition("m", "Nobody said", status=None, snomed="13"),
         condition("n", "Refuted", snomed="14", verificationStatus={"coding": [{"code": "refuted"}]}),
         condition("o", "Mistake", snomed="15", verificationStatus={"coding": [{"code": "entered-in-error"}]}))
    s = records.summary(prof)
    assert set(titles(s["active_conditions"])) == {"Hypertension", "Relapsing thing", "Back again", "No category", "Concern"}
    assert set(titles(s["unconfirmed_conditions"])) == {"Possible lupus", "Maybe", "Perhaps"}
    assert titles(s["visit_diagnoses"]) == ["Acute sinusitis"]
    assert titles(s["conditions_status_unknown"]) == ["Nobody said"]
    assert s["counts"]["conditions"] == 13


def test_a_category_stored_as_display_text_is_understood(prof):
    load(prof, "A", {**condition("a", "Sinusitis", snomed="9"), "category": [{"text": "Encounter Diagnosis"}]},
         {**condition("b", "Diabetes", snomed="8"), "category": [{"coding": [{"display": "Problem List Item"}]}]})
    s = records.summary(prof)
    assert titles(s["active_conditions"]) == ["Diabetes"] and titles(s["visit_diagnoses"]) == ["Sinusitis"]


# ---- F025: current medications

def test_current_medications_are_only_what_is_current(prof):
    old = recent(1200)
    load(prof, "A",
         med("a", "Lisinopril 20 MG", when=recent(20)),
         med("b", "Amlodipine 5 MG", status="draft"),
         med("c", "Vitamin D 1000 UNT", status="intended"),
         med("d", "Atenolol 25 MG", status="on-hold"),
         med("e", "Atorvastatin 40 MG", intent="plan"),
         med("f", "Simvastatin 20 MG", intent="proposal", when="2017-01-01"),
         med("g", "Hydrochlorothiazide 25 MG", when="2018-01-01", dispenseRequest={"validityPeriod": {"end": "2019-01-01"}}),
         med("h", "Sertraline 50 MG", when=old),
         med("i", "Warfarin 5 MG", status="unknown"),
         med("j", "Ibuprofen 600 MG", status="completed"),
         med("k", "Naproxen 500 MG", status="stopped"),
         med("l", "Cephalexin 500 MG", status="cancelled"),
         med("m", "Morphine 2 MG", category=[{"coding": [{"code": "inpatient"}]}]),
         med("n", "Aspirin 81 MG", intent="plan", reportedBoolean=True),
         {"resourceType": "MedicationStatement", "id": "o", "status": "active", "dateAsserted": "2015-01-01",
          "medicationCodeableConcept": {"text": "Levothyroxine 50 MCG"}})
    s = records.summary(prof)
    assert set(titles(s["active_medications"])) == {"Lisinopril 20 MG", "Levothyroxine 50 MCG"}
    check = {m["title"]: m["med_reason"] for m in s["medications_to_check"]}
    assert set(check) == {"Atenolol 25 MG", "Atorvastatin 40 MG", "Hydrochlorothiazide 25 MG", "Sertraline 50 MG", "Warfarin 5 MG",
                          "Morphine 2 MG", "Aspirin 81 MG"}
    assert check["Atenolol 25 MG"] == "On hold."
    assert check["Hydrochlorothiazide 25 MG"] == "The prescription ran out on Jan 1, 2019."
    assert check["Sertraline 50 MG"].startswith("Last prescribed") and "no renewal" in check["Sertraline 50 MG"]
    assert check["Morphine 2 MG"] == "Ordered during a hospital stay."
    assert check["Aspirin 81 MG"].startswith("Reported rather than prescribed")
    assert set(titles(s["medications_planned"])) == {"Amlodipine 5 MG", "Vitamin D 1000 UNT", "Simvastatin 20 MG"}


def test_a_draft_renewal_doesnt_hide_the_order_in_force(prof):
    load(prof, "A", med("a", "Amlodipine 5 MG", rxnorm="197361", when=recent(100)), med("b", "Amlodipine 5 MG", status="draft", rxnorm="197361", when=recent(1)))
    assert titles(records.summary(prof)["active_medications"]) == ["Amlodipine 5 MG"]


def test_strengths_of_one_drug_are_side_by_side_newest_first_and_never_merged(prof):
    load(prof, "A", med("a", "Lisinopril 10 MG Oral Tablet", when="2019-01-01", rxnorm="314076"), med("x", "Metformin 500 MG", when=recent(40)))
    load(prof, "B", med("b", "Lisinopril 20 MG Oral Tablet", when=recent(30), rxnorm="314077"), med("y", "metformin hcl 500mg tab", when=recent(20)))
    s = records.summary(prof)
    assert titles(s["active_medications"]) == ["Lisinopril 20 MG Oral Tablet", "metformin hcl 500mg tab", "Metformin 500 MG"] or \
        titles(s["active_medications"]) == ["metformin hcl 500mg tab", "Metformin 500 MG", "Lisinopril 20 MG Oral Tablet"]
    newest = next(m for m in s["active_medications"] if m["title"].startswith("Lisinopril"))
    assert newest["group_note"] == "2 strengths on record" and newest["also_on_record"][0]["title"] == "Lisinopril 10 MG Oral Tablet"
    older = s["medications_to_check"][0]
    assert older["title"] == "Lisinopril 10 MG Oral Tablet" and "Lisinopril 20 MG Oral Tablet is on record" in older["med_reason"]
    both = [m for m in s["active_medications"] if m["title"].lower().startswith("metformin")]
    assert both[0]["group_note"] == "Listed 2 times"


@pytest.mark.parametrize("a, b, same", [
    ("Lisinopril 10 MG Oral Tablet", "lisinopril 20mg tab", True),
    ("Metformin 500 MG", "metformin hcl 500mg tab", True),
    ("Lisinopril 20 MG / Hydrochlorothiazide 12.5 MG Oral Tablet", "Lisinopril 20 MG Oral Tablet", False),
    ("Insulin glargine 100 UNT/ML", "Insulin lispro 100 UNT/ML", False),
    ("Levothyroxine Sodium 0.075 MG Oral Tablet", "levothyroxine 75 mcg tablet", True),
    ("Sodium chloride 0.9%", "Potassium chloride 20 MEQ", False),
    ("atorvastatin 20 MG Oral Tablet [Lipitor]", "Atorvastatin 40 MG", True),
])
def test_drug_names(a, b, same):
    assert (records.drug_key(a) == records.drug_key(b)) is same


def test_an_old_order_is_never_hidden(prof):
    load(prof, "A", med("a", "Warfarin 5 MG", when="2015-01-01"))
    s = records.summary(prof)
    assert s["active_medications"] == [] and titles(s["medications_to_check"]) == ["Warfarin 5 MG"]


# ---- F004: refused vaccines

def vaccine(rid, status="completed", when="2024-10-01", **kw):
    return {"resourceType": "Immunization", "id": rid, "status": status, "occurrenceDateTime": when,
            "vaccineCode": {"coding": [{"system": "http://hl7.org/fhir/sid/cvx", "code": "158", "display": "Influenza"}], "text": "Influenza"}, **kw}


def test_a_refused_vaccine_isnt_a_given_one(prof):
    load(prof, "A", vaccine("a", when="2024-10-01"), vaccine("b", status="not-done", when="2026-10-01",
                                                             statusReason={"text": "Patient refused"}))
    s = records.summary(prof)
    assert [i["effective_at"] for i in s["immunizations"]] == ["2024-10-01"]
    assert [(i["effective_at"], i["details"]["status_reason"]) for i in s["immunizations_not_given"]] == [("2026-10-01", "Patient refused")]


@pytest.mark.parametrize("order", [("Given", "Refused"), ("Refused", "Given")])
def test_a_same_day_refusal_and_dose_stay_separate(prof, order):
    for source in order:
        load(prof, source, vaccine(source, status="completed" if source == "Given" else "not-done"))
    s = records.summary(prof)
    assert len(s["immunizations"]) == 1 and len(s["immunizations_not_given"]) == 1


# ---- F028: same-day reports

def report(rid, conclusion=None, when="2025-03-01T10:00:00Z", **kw):
    return {"resourceType": "DiagnosticReport", "id": rid, "status": "final", "code": {"coding": [{"system": LOINC, "code": "18748-4", "display": "Imaging"}]},
            "effectiveDateTime": when, **({"conclusion": conclusion} if conclusion else {}), **kw}


def test_same_day_reports_from_one_source_are_all_there(client, prof):
    load(prof, "A", report("a", "Chest: clear"), report("b", "Knee: torn meniscus"))
    items = client.get("/api/records", params={"category": "reports"}).json()
    assert {r["narrative"] for r in items["items"]} == {"Chest: clear", "Knee: torn meniscus"}
    assert records.category_counts(prof)["reports"] == 2


def test_one_report_from_two_sources_is_still_one(client, prof):
    load(prof, "A", report("a", "Chest: clear"))
    load(prof, "B", report("b", "Chest: clear"))
    items = client.get("/api/records", params={"category": "reports"}).json()["items"]
    assert len(items) == 1 and len(items[0]["sources"]) == 2 and records.category_counts(prof)["reports"] == 1
    assert len(client.get(f"/api/records/{items[0]['id']}").json()["duplicates"]) == 1


def test_the_same_file_imported_twice_is_one_report_each(client, prof):
    for _ in range(2):
        load(prof, "File", report("a", "Chest: clear"), report("b", "Knee: torn meniscus"))
    assert len(client.get("/api/records", params={"category": "reports"}).json()["items"]) == 2


def test_two_sources_with_two_cbcs_pair_up(client, prof):
    for source in ("A", "B"):
        load(prof, source, report(f"{source}1", when="2025-03-01T06:00:00Z"), report(f"{source}2", when="2025-03-01T18:00:00Z"))
    items = client.get("/api/records", params={"category": "reports"}).json()["items"]
    assert len(items) == 2 and all(len(r["sources"]) == 2 for r in items)


def test_a_siblings_report_isnt_also_reported_by(client, prof):
    load(prof, "A", report("a", "Chest: clear"), report("b", "Knee"))
    rid = client.get("/api/records", params={"category": "reports"}).json()["items"][0]["id"]
    assert client.get(f"/api/records/{rid}").json()["duplicates"] == []


# ---- F043: conditions across sources

def test_icd_only_joins_the_one_snomed_that_carries_it(client, prof):
    load(prof, "A", condition("a", "Hypertension", snomed="38341003", icd="I10"))
    load(prof, "B", condition("b", "Essential hypertension", icd="I10", recordedDate="2020-01-01"))
    s = records.summary(prof)
    assert len(s["active_conditions"]) == 1 and len(s["active_conditions"][0]["sources"]) == 2 and s["counts"]["conditions"] == 1
    dup = client.get(f"/api/records/{s['active_conditions'][0]['id']}").json()["duplicates"]
    assert len(dup) == 1


def test_a_coarse_icd_shared_by_two_snomed_concepts_merges_nothing(prof):
    load(prof, "A", condition("a", "Foot swelling", snomed="1", icd="M79.89"), condition("b", "Soft tissue mass", snomed="2", icd="M79.89"))
    load(prof, "B", condition("c", "Other soft tissue disorder", icd="M79.89"))
    assert len(records.summary(prof)["active_conditions"]) == 3


def test_oid_code_systems_dedupe_with_the_uri(prof):
    load(prof, "A", med("a", "Amlodipine 5 MG", rxnorm="197361"))
    oid = med("b", "amlodipine local", rxnorm="197361")
    oid["medicationCodeableConcept"]["coding"][0]["system"] = "urn:oid:2.16.840.1.113883.6.88"
    load(prof, "B", oid)
    assert len(records.summary(prof)["active_medications"]) == 1


# ---- F133: what hasn't happened

def visit(rid, status, when):
    return {"resourceType": "Encounter", "id": rid, "status": status, "class": {"code": "AMB"}, "type": [{"text": rid}], "period": {"start": when}}


def test_planned_visits_are_upcoming_not_history(client, prof):
    future = (date.today() + timedelta(days=200)).isoformat()
    load(prof, "A", visit("Annual", "planned", future), visit("Checkup", "finished", "2025-01-01"), visit("Never happened", "planned", "2020-01-01"))
    s = records.summary(prof)
    assert titles(s["recent_encounters"]) == ["Checkup"] and titles(s["upcoming_encounters"]) == ["Annual"]
    assert [i["title"] for i in client.get("/api/timeline").json()["items"]] == ["Checkup"]


def test_a_procedure_not_yet_done_isnt_history(client, prof):
    load(prof, "A", {"resourceType": "Procedure", "id": "p", "status": "preparation", "code": {"text": "Colonoscopy"}, "performedDateTime": "2030-01-01"})
    assert client.get("/api/timeline").json()["items"] == []
    assert client.get("/api/records", params={"category": "procedures"}).json()["items"][0]["status"] == "preparation"


# ---- the simulated institution keeps working through all of it

def test_a_simulated_connection_reads_as_a_real_chart(client):
    connect_institution(client, "epic-stanford-health-care")
    s = client.get("/api/summary").json()
    assert s["active_medications"] and s["active_conditions"] and s["allergies"]
    for m in s["active_medications"]:
        assert m["med_tier"] == "current" and m["last_prescribed"]
