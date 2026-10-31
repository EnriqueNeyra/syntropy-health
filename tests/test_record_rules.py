"""What the record says once sources are merged: current medications, active problems, allergies, and one test's history."""

from datetime import date, timedelta

import pytest

from app.services import insights, sync
from app.store import lab_catalog, records
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
    assert flagged["Cholesterol"]["older"] is True and "older" not in flagged["X"]


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


def test_looking_for_results_with_no_standard_code_doesnt_walk_through_all_labs():
    """Done for every flagged test on every Overview: at 25,000 labs a walk was seconds, which is why this has an index."""
    from app.core import db
    db.ensure_migrated()
    with db.read() as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT DISTINCT title, code, code_system FROM clinical_records WHERE profile_id = ? AND category = ? "
            f"AND (code IS NULL OR code_system IS NULL OR code_system != '{LOINC}')", ("p", "labs")).fetchall()
    assert "idx_records_unstandard_code" in plan[0][-1]


def test_one_odd_resource_doesnt_mark_a_live_sync_partly_synced(client, monkeypatch):
    from app.connectors import fhir_normalize
    real = fhir_normalize.normalize_report

    def with_one_unread(*args):
        patient, recs, problems = real(*args)
        problems["skipped"]["Observation"] = 1
        return patient, recs, problems
    with monkeypatch.context() as patch:
        patch.setattr(fhir_normalize, "normalize_report", with_one_unread)
        cid = connect_institution(client, "epic-stanford-health-care")["connection_id"]
    conn = client.get(f"/api/connections/{cid}").json()
    assert conn["last_sync_status"] == "success"


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


# ---- F078: results that only share a name

def local_lab(rid, code, title, when, value=None, unit=None, text=None, ranges=None):
    res = lab(rid, value, unit, when, code=None, title=title, ranges=ranges)
    res["code"] = {"coding": [{"system": "urn:example:lab", "code": code, "display": title}], "text": title} if code else {"text": title}
    if text is not None:
        del res["valueQuantity"]
        res["valueString"] = text
    return res


def in_range(low, high, unit="mg/dL"):
    return [{"low": {"value": low, "unit": unit}, "high": {"value": high, "unit": unit}}]


def test_a_urine_result_titled_just_glucose_doesnt_hide_a_high_blood_glucose(prof):
    load(prof, "A", lab("a", 300, "mg/dL", "2026-01-01", code="2345-7", title="Glucose", ranges=in_range(70, 99)),
         local_lab("b", "UA-GLU", "Glucose", "2026-01-02", text="Negative"))
    assert titles(records.summary(prof)["flagged_labs"]) == ["Glucose"]
    assert titles(records.flagged_labs(prof)) == ["Glucose"]
    assert records.observation_series(prof, "2345-7")["text_results"] == []
    assert [t["text"] for t in records.observation_series(prof, "UA-GLU")["text_results"]] == ["Negative"]


def test_a_24_hour_urine_creatinine_stays_off_the_blood_chart(prof):
    load(prof, "A", lab("a", 1.0, "mg/dL", "2026-01-01", code="2160-0", title="Creatinine"), local_lab("b", "24U-CR", "Creatinine", "2026-01-02", 120, "mg/dL"))
    assert [p["v"] for p in records.observation_series(prof, "2160-0")["points"]] == [1.0]
    assert [p["v"] for p in records.observation_series(prof, "24U-CR")["points"]] == [120]
    entries = {e["code"]: e for e in records.observation_catalog(prof, "labs")}
    assert set(entries) == {"2160-0", "24U-CR"} and entries["2160-0"]["trend"] is None


@pytest.mark.parametrize("title", ["Glucose", "Creatinine", "Bilirubin", "Calcium", "Albumin", "Protein, total", "Sodium", "Potassium", "Chloride", "Uric acid",
                                   "Urea nitrogen", "Magnesium", "Cortisol", "WBC", "RBC", "Leukocytes", "White blood cells", "Hemoglobin"])
def test_a_name_labs_also_use_for_urine_says_nothing_about_the_test(title):
    assert lab_catalog.series_key("LOC", "urn:example:lab", title) == "LOC"
    assert lab_catalog.series_key(None, None, title) == title.lower()


@pytest.mark.parametrize("title, code", [("Glucose, Serum", "2345-7"), ("GLUCOSE,PLASMA", "2345-7"), ("Blood glucose", "2345-7"), ("Creatinine, Serum", "2160-0"),
                                         ("Calcium (serum)", "17861-6"), ("BUN", "3094-0"), ("Hemoglobin A1c", "4548-4"), ("LDL", "13457-7"), ("TSH", "3016-3")])
def test_a_name_that_can_only_be_blood_still_joins(title, code):
    assert lab_catalog.series_key("LOC", "urn:example:lab", title) == lab_catalog.series_key(code, LOINC, None)


@pytest.mark.parametrize("title", ["Glucose, Urine", "Urine Glucose", "Creatinine, 24 hr urine", "UA Glucose"])
def test_a_name_that_says_urine_stays_apart(title):
    assert lab_catalog.series_key("LOC", "urn:example:lab", title) == "LOC"


def test_two_local_codes_with_one_title_are_two_tests(prof):
    load(prof, "A", local_lab("a", "BMP-GLU", "Glucose", "2026-01-01", 300, "mg/dL", ranges=in_range(70, 99)),
         local_lab("b", "UA-GLU", "Glucose", "2026-01-02", text="Negative"), local_lab("c", None, "Glucose", "2026-01-03", 90, "mg/dL"))
    assert [p["v"] for p in records.observation_series(prof, "BMP-GLU")["points"]] == [300]
    assert records.observation_series(prof, "BMP-GLU")["text_results"] == []
    assert [p["v"] for p in records.observation_series(prof, title="Glucose")["points"]] == [90]
    assert len(records.summary(prof)["recent_labs"]) == 3


def test_a_text_only_result_says_what_its_code_is(prof):
    res = local_lab("a", None, "HIV Ab", "2026-01-01", text="Reactive")
    res["code"] = {"coding": [{"system": LOINC, "code": "7917-8", "display": "HIV Ab"}]}
    load(prof, "A", res)
    s = records.observation_series(prof, "7917-8", "labs")
    assert s["code_system"] == LOINC and s["title"] == "HIV Ab" and s["text_results"][0]["text"] == "Reactive"


# ---- F133: an appointment today or tomorrow

def test_a_planned_visit_today_or_tomorrow_is_upcoming(client, prof):
    today = date.today()
    load(prof, "A", visit("Tomorrow", "planned", (today + timedelta(days=1)).isoformat()), visit("Today", "planned", today.isoformat()),
         visit("Far", "planned", (today + timedelta(days=30)).isoformat()), visit("In the room", "in-progress", today.isoformat()),
         visit("Stale", "planned", (today - timedelta(days=30)).isoformat()))
    s = records.summary(prof)
    assert titles(s["upcoming_encounters"]) == ["Today", "Tomorrow", "Far"]
    assert titles(s["recent_encounters"]) == ["In the room"]
    assert titles(client.get("/api/timeline").json()["items"]) == ["In the room"]


# ---- F003: spellings of one unit

def test_one_egfr_spelled_three_ways_is_one_chart(prof):
    coded = lab("a", 61, None, "2024-01-01", code="98979-8", title="eGFR")
    coded["valueQuantity"].update(code="mL/min/{1.73_m2}", system="http://unitsofmeasure.org")
    load(prof, "A", coded, lab("b", 58, "mL/min/1.73m2", "2025-01-01", code="98979-8", title="eGFR"),
         lab("c", 55, "mL/min/1.73 m2", "2026-01-01", code="98979-8", title="eGFR"))
    s = records.observation_series(prof, "98979-8")
    assert [p["v"] for p in s["points"]] == [61, 58, 55] and s["other_units"] == []
    assert records.observation_catalog(prof, "labs")[0]["trend"] == "down"


def test_one_white_cell_count_spelled_two_ways_is_one_chart(prof):
    load(prof, "A", lab("a", 6.1, "K/uL", "2025-01-01", code="6690-2", title="WBC"), lab("b", 9, "K/mcL", "2026-01-01", code="6690-2", title="WBC"))
    s = records.observation_series(prof, "6690-2")
    assert [p["v"] for p in s["points"]] == [6.1, 9] and s["other_units"] == []
    assert records.observation_catalog(prof, "labs")[0]["trend"] == "up"


# ---- F008, F183: a range in another unit than its numbers

def test_a_range_in_another_unit_isnt_labelled_with_the_values_unit(prof):
    load(prof, "A", lab("a", 5.5, "mmol/L", "2026-01-01", code="2345-7", title="Glucose", ranges=in_range(70, 99)))
    s = records.observation_series(prof, "2345-7")
    assert (s["unit"], s["ref_low"], s["ref_high"]) == ("mg/dL", 70, 99)
    assert s["points"][0]["v"] == pytest.approx(99.1) and s["points"][0]["converted"] and s["points"][0]["text"] == "5.5 mmol/L"


def test_a_range_in_the_values_unit_keeps_the_results_as_reported(prof):
    load(prof, "A", lab("a", 5.5, "mmol/L", "2026-01-01", code="2345-7", title="Glucose", ranges=in_range(3.9, 5.5, "mmol/L")))
    s = records.observation_series(prof, "2345-7")
    assert (s["unit"], s["ref_low"], s["ref_high"], s["points"][0]["v"]) == ("mmol/L", 3.9, 5.5, 5.5)


def test_an_insight_gives_the_range_in_the_unit_of_its_numbers(prof):
    load(prof, "A", lab("a", 120, "mg/dL", "2025-01-01", code="2345-7", title="Glucose", ranges=in_range(70, 99)),
         lab("b", 8, "mmol/L", "2026-01-01", code="2345-7", title="Glucose", ranges=in_range(3.9, 5.5, "mmol/L")))
    text = insights._lab_notes(prof)[0]["text"]
    assert "144.1 mg/dL" in text and "120.0 mg/dL" in text and "(70.3–99.1 mg/dL)" in text and "3.9" not in text


def test_an_insight_keeps_the_labs_own_words_when_its_numbers_are_as_reported(prof):
    load(prof, "A", lab("a", 7, "mmol/L", "2025-01-01", code="2345-7", title="Glucose", ranges=in_range(3.9, 5.5, "mmol/L")),
         lab("b", 8, "mmol/L", "2026-01-01", code="2345-7", title="Glucose", ranges=in_range(3.9, 5.5, "mmol/L")))
    text = insights._lab_notes(prof)[0]["text"]
    assert "8 mmol/L" in text and "(3.9–5.5)" in text


# ---- F137: a Patient with no name isn't a second person

JANE = {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Jane"], "family": "Doe"}], "birthDate": "1980-01-01"}


def test_a_nameless_patient_in_a_file_isnt_a_second_person(prof):
    nameless = {"resourceType": "Patient", "id": "p2", "gender": "female"}
    for patients in ([JANE, nameless], [nameless, JANE]):
        assert sync.import_fhir_resources(prof, "File", [*patients, lab("a", 1, "mg/dL", "2026-01-01")])["records"] == 1


def test_a_nameless_patient_doesnt_hide_two_named_people(prof):
    john = {"resourceType": "Patient", "id": "p3", "name": [{"given": ["John"], "family": "Smith"}], "birthDate": "1970-05-05"}
    with pytest.raises(ValueError, match="2 different people: Jane Doe .* and John Smith"):
        sync.import_fhir_resources(prof, "File", [{"resourceType": "Patient", "id": "p2"}, JANE, john])


def test_a_nameless_patient_born_on_another_day_is_named_for_what_it_is(prof):
    with pytest.raises(ValueError, match=r"Jane Doe \(born Jan 1, 1980\) and an unnamed patient \(born Feb 2, 1990\)"):
        sync.import_fhir_resources(prof, "File", [JANE, {"resourceType": "Patient", "id": "p2", "birthDate": "1990-02-02"}])


def test_a_source_with_a_nameless_patient_isnt_flagged_as_someone_else():
    people = records.distinct_people([{"full_name": "Unknown", "birth_date": None, "source_name": "A"},
                                      {"full_name": "Jane Doe", "birth_date": "1980-01-01", "source_name": "B"},
                                      {"full_name": "Unknown", "birth_date": "1980-01-01", "source_name": "C"}])
    assert [(p["full_name"], p["birth_date"], p["sources"]) for p in people] == [("Jane Doe", "1980-01-01", ["A", "B", "C"])]


def test_one_name_with_and_without_its_accents_is_one_person():
    people = records.distinct_people([{"full_name": "José Pérez", "birth_date": "1980-01-01", "source_name": "A"},
                                      {"full_name": "Jose Perez", "birth_date": "1980-01-01", "source_name": "B"}])
    assert len(people) == 1


# ---- F132: a date only in words

def named_vaccine(title, **kw):
    return vaccine(title, vaccineCode={"text": title}, **kw)


def test_a_vaccine_dated_in_words_is_on_the_timeline_at_the_end(client, prof):
    load(prof, "A", named_vaccine("MMR", when=None, occurrenceString="as a child"), named_vaccine("Flu"),
         {"resourceType": "Procedure", "id": "p", "status": "completed", "code": {"text": "Tonsils out"}, "performedString": "as a teenager"},
         {"resourceType": "Immunization", "id": "x", "status": "completed", "vaccineCode": {"text": "No date at all"}})
    items = client.get("/api/timeline").json()["items"]
    assert titles(items)[0] == "Flu" and sorted(titles(items)[1:]) == ["MMR", "Tonsils out"]
    assert next(i for i in items if i["title"] == "MMR")["details"]["date_text"] == "as a child"
    assert titles(client.get("/api/timeline", params={"categories": "immunizations"}).json()["items"]) == ["Flu", "MMR"]
    assert titles(client.get("/api/timeline", params={"categories": "labs"}).json()["items"]) == []
    assert titles(client.get("/api/timeline", params={"q": "tonsils"}).json()["items"]) == ["Tonsils out"]


def test_words_for_a_date_come_once_after_the_last_page(client, prof):
    load(prof, "A", named_vaccine("MMR", when=None, occurrenceString="as a child"), *(named_vaccine(f"V{i}", when=f"2024-01-{i + 1:02d}") for i in range(4)))
    page = client.get("/api/timeline", params={"limit": 2}).json()
    assert "MMR" not in titles(page["items"]) and page["next_before"]
    seen = titles(page["items"])
    while page["next_before"]:
        page = client.get("/api/timeline", params={"limit": 2, "before": page["next_before"]}).json()
        seen += titles(page["items"])
    assert seen.count("MMR") == 1 and seen[-1] == "MMR"


# ---- the simulated institution keeps working through all of it

def test_a_simulated_connection_reads_as_a_real_chart(client):
    connect_institution(client, "epic-stanford-health-care")
    s = client.get("/api/summary").json()
    assert s["active_medications"] and s["active_conditions"] and s["allergies"]
    for m in s["active_medications"]:
        assert m["med_tier"] == "current" and m["last_prescribed"]
