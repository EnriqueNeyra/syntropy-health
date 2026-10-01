"""
Deterministic synthetic FHIR R4 record generator.

``build_chart(persona, tenant)`` returns every FHIR resource an institution would hold
for a persona. Output is a pure function of (persona, tenant, today's date):
resource ids and historical values never change, and new visits appear as time
passes. Different institutions hold different slices of the same person's history
(primary care vs. specialist/hospital) while sharing long-lived facts (problem list,
medications, allergies) under their own resource ids — exactly the situation the
cross-source de-duplication has to handle.
"""

from __future__ import annotations

import base64
import hashlib
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Optional

from app.simulator.personas import CBC, CMP, LIPID, PERSONAS, THYROID, Lab, Persona

REF_END = date(2026, 9, 1)            # persona "today" values are anchored here
REF_START = REF_END - timedelta(days=5 * 365)
HISTORY_YEARS = 5

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
CVX = "http://hl7.org/fhir/sid/cvx"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
UCUM = "http://unitsofmeasure.org"
OBS_CAT = "http://terminology.hl7.org/CodeSystem/observation-category"
INTERP = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"

PANEL_LOINC = {CBC: "58410-2", CMP: "24323-8", LIPID: "57698-3", THYROID: "24348-5"}

CLINICIANS = [
    "Maya Okafor, MD", "Daniel Brooks, MD", "Priya Raman, DO", "Elena Sorensen, MD", "Marcus Webb, NP",
    "Hannah Liu, MD", "Tomás Alvarez, MD", "Grace Kim, PA-C", "Samuel Adeyemi, MD", "Olivia Hart, MD",
]
MANUFACTURERS = {"08": "Merck", "20": "Sanofi Pasteur", "10": "Sanofi Pasteur", "03": "Merck", "21": "Merck",
                 "115": "GSK", "187": "GSK", "208": "Pfizer", "300": "Pfizer", "133": "Pfizer", "33": "Merck",
                 "150": "Sanofi Pasteur", "83": "GSK", "49": "Merck", "116": "Merck", "165": "Merck"}


def _h(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def _hint(*parts: Any) -> int:
    return int(_h(*parts)[:12], 16)


def _rid(tenant: str, persona: str, kind: str, key: Any) -> str:
    return f"{kind[:3].lower()}-{_h(tenant, persona, kind, key)[:14]}"


def _dt(d: date, hour: int = 9, minute: int = 0) -> str:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _years_ago(years: float, today: date) -> date:
    return REF_END - timedelta(days=int(years * 365.25)) if REF_END <= today else today - timedelta(days=int(years * 365.25))


def _interp(start: float, end: float, when: date) -> float:
    span = (REF_END - REF_START).days
    frac = min(max((when - REF_START).days / span, 0.0), 1.2)
    return start + (end - start) * frac


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@dataclass
class TenantProfile:
    primary: bool
    specialist: bool
    imperial_units: bool
    binary_notes: bool
    medication_refs: bool
    visit_month: int
    visit_day: int
    pcp: str
    specialist_name: str


def tenant_profile(tenant: str, persona: Persona, platform: str) -> TenantProfile:
    n = _hint("tenant", tenant, persona.key)
    role = n % 3                               # 0 primary care, 1 specialist/hospital, 2 integrated system
    return TenantProfile(
        primary=role in (0, 2),
        specialist=role in (1, 2),
        imperial_units=(n >> 4) % 2 == 0,
        binary_notes=(n >> 6) % 3 == 0,
        medication_refs=platform == "epic",
        visit_month=1 + (n >> 8) % 12,
        visit_day=1 + (n >> 12) % 27,
        pcp=CLINICIANS[(n >> 16) % len(CLINICIANS)],
        specialist_name=CLINICIANS[(n >> 20) % len(CLINICIANS)],
    )


class ChartBuilder:
    def __init__(self, persona: Persona, tenant: str, tenant_name: str, platform: str, today: date):
        self.p = persona
        self.tenant = tenant
        self.tenant_name = tenant_name
        self.platform = platform
        self.today = today
        self.tp = tenant_profile(tenant, persona, platform)
        self.rng = random.Random(_hint("rng", tenant, persona.key))
        self.patient_id = f"{_h('patient', tenant, persona.key)[:16]}"
        self.mrn = str(_hint("mrn", tenant, persona.key) % 90000000 + 10000000)
        self.resources: list[dict[str, Any]] = []
        self.patient_ref = {"reference": f"Patient/{self.patient_id}", "display": persona.full_name}

    # -- helpers -----------------------------------------------------------
    def add(self, res: dict[str, Any]) -> dict[str, Any]:
        self.resources.append(res)
        return res

    def id(self, kind: str, key: Any) -> str:
        return _rid(self.tenant, self.p.key, kind, key)

    def jitter(self, key: Any, value: float, rel: float) -> float:
        r = random.Random(_hint("j", self.tenant, self.p.key, key))
        return value * (1 + r.uniform(-rel, rel))

    def age_on(self, d: date) -> float:
        b = date.fromisoformat(self.p.birth_date)
        return (d - b).days / 365.25

    # -- build ---------------------------------------------------------------
    def build(self) -> list[dict[str, Any]]:
        self.patient()
        visits = self.schedule_visits()
        for v in visits:
            self.encounter(v)
        self.conditions()
        self.medications()
        self.allergies()
        if self.tp.primary:
            self.immunizations()
        if self.tp.specialist:
            self.procedures_and_imaging()
            self.devices()
        self.care_team()
        self.care_plans_and_goals()
        self.coverage()
        return self.resources

    def patient(self) -> None:
        p = self.p
        self.add({
            "resourceType": "Patient", "id": self.patient_id,
            "meta": {"lastUpdated": _dt(self.today)},
            "identifier": [{
                "use": "usual",
                "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MR"}], "text": "MRN"},
                "system": f"urn:syntropy:sim:{self.tenant}:mrn", "value": self.mrn,
            }],
            "active": True,
            "name": [{"use": "official", "family": p.family, "given": [p.given], "text": p.full_name}],
            "telecom": [{"system": "phone", "value": p.phone, "use": "mobile"},
                        {"system": "email", "value": p.email}],
            "gender": p.gender, "birthDate": p.birth_date,
            "address": [{"use": "home", "line": [f"{100 + _hint('addr', p.key) % 8800} Maple Street"],
                         "city": p.city, "state": p.state, "postalCode": p.postal, "country": "US"}],
            "communication": [{"language": {"coding": [{"system": "urn:ietf:bcp:47", "code": "en-US"}], "text": "English"}}],
        })

    def schedule_visits(self) -> list[dict[str, Any]]:
        visits = []
        first_year = REF_END.year - HISTORY_YEARS
        for year in range(first_year, self.today.year + 1):
            if self.tp.primary:
                d = date(year, self.tp.visit_month, self.tp.visit_day)
                if d <= self.today and d >= REF_START:
                    visits.append({"date": d, "kind": "annual", "labs": "all"})
                sick = random.Random(_hint("sick", self.tenant, self.p.key, year))
                if sick.random() < 0.6:
                    sd = date(year, 1 + sick.randrange(12), 1 + sick.randrange(27))
                    if REF_START <= sd <= self.today:
                        visits.append({"date": sd, "kind": "sick", "labs": None})
            if self.tp.specialist:
                for half in (0, 1):
                    m = ((self.tp.visit_month + 3 + half * 6 - 1) % 12) + 1
                    d = date(year, m, min(self.tp.visit_day + 1, 28))
                    if REF_START <= d <= self.today:
                        visits.append({"date": d, "kind": "specialist", "labs": "specialty"})
        visits.sort(key=lambda v: v["date"])
        return visits

    # -- encounters, vitals, labs, notes ------------------------------------
    def encounter(self, v: dict[str, Any]) -> None:
        d: date = v["date"]
        kind = v["kind"]
        clinician = self.tp.specialist_name if kind == "specialist" else self.tp.pcp
        type_text = {
            "annual": "Well child visit" if self.p.pediatric else "Annual physical examination",
            "sick": "Office visit — acute concern",
            "specialist": f"{self.p.specialty} follow-up",
        }[kind]
        reason = {
            "annual": "Routine health maintenance",
            "sick": random.Random(_hint("reason", self.tenant, d)).choice(
                ["Upper respiratory symptoms", "Low back pain", "Rash", "Headache", "Cough", "Ear pain", "Fatigue"]),
            "specialist": self.p.conditions[0].name,
        }[kind]
        eid = self.id("Encounter", d.isoformat() + kind)
        self.add({
            "resourceType": "Encounter", "id": eid, "status": "finished",
            "class": {"system": ACT_CODE, "code": "AMB", "display": "ambulatory"},
            "type": [{"text": type_text}],
            "subject": self.patient_ref,
            "participant": [{"individual": {"display": clinician}}],
            "period": {"start": _dt(d, 9, 30), "end": _dt(d, 10, 10)},
            "reasonCode": [{"text": reason}],
            "location": [{"location": {"display": f"{self.tenant_name} — {'Specialty Clinic' if kind == 'specialist' else 'Primary Care'}"}}],
            "serviceProvider": {"display": self.tenant_name},
        })
        enc_ref = {"reference": f"Encounter/{eid}"}
        vitals = self.vitals(d, enc_ref)
        if not self.p.pediatric:
            self.smoking_status(d, enc_ref)
        results = []
        if v["labs"]:
            results = self.labs(d, enc_ref, v["labs"])
        self.note(d, eid, kind, type_text, reason, clinician, vitals, results)

    def vitals(self, d: date, enc_ref: dict[str, str]) -> dict[str, Any]:
        p, key = self.p, d.isoformat()
        vt = p.vitals
        out: dict[str, Any] = {}

        def val(metric: str, rel: float = 0.02) -> float:
            start, end = vt[metric]
            if p.pediatric and metric in ("weight_kg", "height_cm"):
                age = self.age_on(d)
                frac = min(max((age - 2.0) / 5.3, 0), 1.2)
                return start + (end - start) * frac
            return self.jitter((metric, key), _interp(start, end, d), rel)

        def obs(code: str, display: str, value: float, unit: str, ucum: str, decimals: int = 0,
                components: Optional[list[dict[str, Any]]] = None) -> None:
            res: dict[str, Any] = {
                "resourceType": "Observation", "id": self.id("Observation", (code, key)), "status": "final",
                "category": [{"coding": [{"system": OBS_CAT, "code": "vital-signs", "display": "Vital Signs"}]}],
                "code": {"coding": [{"system": LOINC, "code": code, "display": display}], "text": display},
                "subject": self.patient_ref, "encounter": enc_ref,
                "effectiveDateTime": _dt(d, 9, 40), "issued": _dt(d, 9, 45),
            }
            if components:
                res["component"] = components
            else:
                res["valueQuantity"] = {"value": round(value, decimals) if decimals else int(round(value)),
                                        "unit": unit, "system": UCUM, "code": ucum}
            self.add(res)

        sys_, dia = val("systolic", 0.04), val("diastolic", 0.04)
        out["bp"] = f"{int(round(sys_))}/{int(round(dia))}"
        obs("85354-6", "Blood pressure panel with all children optional", 0, "", "", components=[
            {"code": {"coding": [{"system": LOINC, "code": "8480-6", "display": "Systolic blood pressure"}]},
             "valueQuantity": {"value": int(round(sys_)), "unit": "mmHg", "system": UCUM, "code": "mm[Hg]"}},
            {"code": {"coding": [{"system": LOINC, "code": "8462-4", "display": "Diastolic blood pressure"}]},
             "valueQuantity": {"value": int(round(dia)), "unit": "mmHg", "system": UCUM, "code": "mm[Hg]"}},
        ])
        hr = val("heart_rate", 0.06)
        out["hr"] = int(round(hr))
        obs("8867-4", "Heart rate", hr, "beats/minute", "/min")
        obs("9279-1", "Respiratory rate", val("resp", 0.05), "breaths/minute", "/min")
        obs("2708-6", "Oxygen saturation in Arterial blood", min(val("spo2", 0.01), 100), "%", "%")
        temp_c = val("temp_c", 0.004)
        if self.tp.imperial_units:
            obs("8310-5", "Body temperature", temp_c * 9 / 5 + 32, "degF", "[degF]", 1)
        else:
            obs("8310-5", "Body temperature", temp_c, "Cel", "Cel", 1)
        weight = val("weight_kg", 0.01)
        height = val("height_cm", 0.002)
        out["weight_kg"] = weight
        if self.tp.imperial_units:
            obs("29463-7", "Body weight", weight * 2.2046226, "lb", "[lb_av]", 1)
            obs("8302-2", "Body height", height / 2.54, "in", "[in_i]", 1)
        else:
            obs("29463-7", "Body weight", weight, "kg", "kg", 1)
            obs("8302-2", "Body height", height, "cm", "cm", 1)
        obs("39156-5", "Body mass index (BMI) [Ratio]", weight / (height / 100) ** 2, "kg/m2", "kg/m2", 1)
        return out

    def smoking_status(self, d: date, enc_ref: dict[str, str]) -> None:
        # Filed under "survey", as Synthea-based vendor sandboxes do, rather than US Core's "social-history".
        self.add({
            "resourceType": "Observation", "id": self.id("Observation", ("72166-2", d.isoformat())), "status": "final",
            "category": [{"coding": [{"system": OBS_CAT, "code": "survey", "display": "Survey"}]}],
            "code": {"coding": [{"system": LOINC, "code": "72166-2", "display": "Tobacco smoking status"}],
                     "text": "Tobacco smoking status"},
            "subject": self.patient_ref, "encounter": enc_ref, "effectiveDateTime": _dt(d, 9, 35),
            "valueCodeableConcept": {"coding": [{"system": "http://snomed.info/sct", "code": "266919005",
                                                 "display": "Never smoked tobacco"}], "text": "Never smoked tobacco"},
        })

    def labs(self, d: date, enc_ref: dict[str, str], which: str) -> list[dict[str, Any]]:
        p = self.p
        labs = p.labs
        if which == "specialty":
            specialty_codes = {"4548-4", "3016-3", "3024-7", "2160-0", "33914-3", "9318-7", "2276-4", "2823-3", "13457-7"}
            labs = [l for l in labs if l.loinc in specialty_codes]
        by_panel: dict[Optional[str], list[dict[str, Any]]] = {}
        results = []
        for lab in labs:
            obs = self.lab_observation(lab, d, enc_ref)
            by_panel.setdefault(lab.panel, []).append(obs)
            results.append({"name": lab.name, "value": obs["valueQuantity"]["value"], "unit": lab.unit,
                            "flag": obs["interpretation"][0]["coding"][0]["code"]})
        for panel, observations in by_panel.items():
            if panel is None:
                continue
            self.add({
                "resourceType": "DiagnosticReport", "id": self.id("DiagnosticReport", (panel, d.isoformat())),
                "status": "final",
                "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "LAB", "display": "Laboratory"}]}],
                "code": {"coding": [{"system": LOINC, "code": PANEL_LOINC[panel], "display": panel}], "text": panel},
                "subject": self.patient_ref, "encounter": enc_ref,
                "effectiveDateTime": _dt(d, 8, 5), "issued": _dt(d, 14, 0),
                "performer": [{"display": f"{self.tenant_name} Laboratory"}],
                "result": [{"reference": f"Observation/{o['id']}", "display": o["code"]["text"]} for o in observations],
            })
        return results

    def lab_observation(self, lab: Lab, d: date, enc_ref: dict[str, str]) -> dict[str, Any]:
        value = self.jitter((lab.loinc, d.isoformat()), _interp(lab.start, lab.end, d), lab.noise)
        value = round(value, lab.decimals) if lab.decimals else int(round(value))
        flag = "N"
        if lab.low is not None and value < lab.low:
            flag = "L"
        if lab.high is not None and value > lab.high:
            flag = "H"
        rr: dict[str, Any] = {}
        if lab.low is not None:
            rr["low"] = {"value": lab.low, "unit": lab.unit, "system": UCUM, "code": lab.unit}
        if lab.high is not None:
            rr["high"] = {"value": lab.high, "unit": lab.unit, "system": UCUM, "code": lab.unit}
        rr["text"] = (f"{lab.low}-{lab.high}" if lab.low is not None and lab.high is not None
                      else f">{lab.low}" if lab.low is not None else f"<{lab.high}")
        return self.add({
            "resourceType": "Observation", "id": self.id("Observation", (lab.loinc, d.isoformat())), "status": "final",
            "category": [{"coding": [{"system": OBS_CAT, "code": "laboratory", "display": "Laboratory"}]}],
            "code": {"coding": [{"system": LOINC, "code": lab.loinc, "display": lab.name}], "text": lab.name},
            "subject": self.patient_ref, "encounter": enc_ref,
            "effectiveDateTime": _dt(d, 8, 5), "issued": _dt(d, 14, 0),
            "valueQuantity": {"value": value, "unit": lab.unit, "system": UCUM, "code": lab.unit},
            "interpretation": [{"coding": [{"system": INTERP, "code": flag,
                                            "display": {"H": "High", "L": "Low", "N": "Normal"}[flag]}]}],
            "referenceRange": [rr],
        })

    def note(self, d: date, eid: str, kind: str, type_text: str, reason: str, clinician: str,
             vitals: dict[str, Any], results: list[dict[str, Any]]) -> None:
        p = self.p
        active = [c for c in p.conditions if c.status == "active" and _years_ago(c.onset_years_ago, self.today) <= d]
        meds = [m for m in p.medications if m.status == "active" and _years_ago(m.started_years_ago, self.today) <= d]
        lines = [
            f"{type_text.upper()}",
            f"Date of service: {d.strftime('%B %d, %Y')}",
            f"Clinician: {clinician}",
            f"Facility: {self.tenant_name}",
            "",
            "CHIEF COMPLAINT",
            reason,
            "",
            "VITAL SIGNS",
            f"BP {vitals['bp']} mmHg · HR {vitals['hr']} bpm · Weight {vitals['weight_kg']:.1f} kg",
            "",
            "ASSESSMENT",
        ]
        lines += [f"- {c.name} ({c.icd10})" for c in active] or ["- No chronic conditions on file."]
        if results:
            abnormal = [r for r in results if r["flag"] != "N"]
            lines += ["", "RESULTS REVIEWED"]
            lines += [f"- {r['name']}: {r['value']} {r['unit']} ({'high' if r['flag'] == 'H' else 'low'})" for r in abnormal] \
                or ["- Laboratory results within reference ranges."]
        lines += ["", "PLAN"]
        lines += [f"- Continue {m.name.split(' ')[0].lower()}: {m.dose.lower()}." for m in meds[:5]]
        if kind == "annual":
            lines.append("- Age-appropriate screening and immunizations reviewed.")
        lines.append("- Return to clinic in " + ("6 months." if kind == "specialist" else "12 months or sooner as needed."))
        lines += ["", "This note was generated by the Syntropy Health EHR simulator (synthetic data)."]
        text = "\n".join(lines)
        did = self.id("DocumentReference", eid)
        loinc_type = ("11506-3", "Progress note") if kind != "annual" else ("34117-2", "History and physical note")
        attachment: dict[str, Any]
        if self.tp.binary_notes:
            bid = self.id("Binary", eid)
            html = "<html><body><pre>" + text.replace("&", "&amp;").replace("<", "&lt;") + "</pre></body></html>"
            self.add({"resourceType": "Binary", "id": bid, "contentType": "text/html", "data": _b64(html)})
            attachment = {"contentType": "text/html", "url": f"Binary/{bid}", "title": type_text, "size": len(html)}
        else:
            attachment = {"contentType": "text/plain", "data": _b64(text), "title": type_text, "size": len(text)}
        self.add({
            "resourceType": "DocumentReference", "id": did, "status": "current", "docStatus": "final",
            "type": {"coding": [{"system": LOINC, "code": loinc_type[0], "display": loinc_type[1]}], "text": loinc_type[1]},
            "category": [{"coding": [{"system": "http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category",
                                      "code": "clinical-note", "display": "Clinical Note"}]}],
            "subject": self.patient_ref, "date": _dt(d, 11, 0), "author": [{"display": clinician}],
            "description": f"{type_text} — {reason}",
            "content": [{"attachment": attachment}],
            "context": {"encounter": [{"reference": f"Encounter/{eid}"}], "period": {"start": _dt(d, 9, 30)}},
        })

    # -- problem list, medications, allergies --------------------------------
    def conditions(self) -> None:
        for c in self.p.conditions:
            if c.status != "active" and not self.tp.primary:
                continue
            onset = _years_ago(c.onset_years_ago, self.today)
            self.add({
                "resourceType": "Condition", "id": self.id("Condition", c.snomed),
                "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": c.status}]},
                "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-ver-status", "code": "confirmed"}]},
                "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category",
                                          "code": "problem-list-item", "display": "Problem List Item"}]}],
                "code": {"coding": [{"system": SNOMED, "code": c.snomed, "display": c.name},
                                    {"system": ICD10, "code": c.icd10, "display": c.name}], "text": c.name},
                "subject": self.patient_ref,
                "onsetDateTime": onset.isoformat(),
                "recordedDate": _dt(onset + timedelta(days=_hint("rec", self.tenant, c.snomed) % 200)),
                **({"abatementDateTime": (onset + timedelta(days=21)).isoformat()} if c.status == "resolved" else {}),
            })

    def medications(self) -> None:
        for m in self.p.medications:
            if m.status != "active" and not self.tp.primary:
                continue
            start = _years_ago(m.started_years_ago, self.today)
            authored = start + timedelta(days=(self.today - start).days // 2 if m.status == "active" else 0)
            res: dict[str, Any] = {
                "resourceType": "MedicationRequest", "id": self.id("MedicationRequest", m.rxnorm),
                "status": m.status, "intent": "order",
                "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/medicationrequest-category",
                                          "code": "outpatient"}]}],
                "subject": self.patient_ref, "authoredOn": authored.isoformat(),
                "requester": {"display": self.tp.pcp},
                "dosageInstruction": [{"text": m.dose, "asNeededBoolean": m.prn}],
                "dispenseRequest": {"numberOfRepeatsAllowed": 3,
                                    "quantity": {"value": 90, "unit": "tablet"},
                                    "validityPeriod": {"start": authored.isoformat()}},
            }
            concept = {"coding": [{"system": RXNORM, "code": m.rxnorm, "display": m.name}], "text": m.name}
            if self.tp.medication_refs:
                mid = self.id("Medication", m.rxnorm)
                self.add({"resourceType": "Medication", "id": mid, "code": concept})
                res["medicationReference"] = {"reference": f"Medication/{mid}", "display": m.name}
            else:
                res["medicationCodeableConcept"] = concept
            self.add(res)

    def allergies(self) -> None:
        for a in self.p.allergies:
            self.add({
                "resourceType": "AllergyIntolerance", "id": self.id("AllergyIntolerance", a.code),
                "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-clinical", "code": "active"}]},
                "verificationStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/allergyintolerance-verification", "code": "confirmed"}]},
                "type": "allergy", "category": [a.category], "criticality": a.criticality,
                "code": {"coding": [{"system": a.system, "code": a.code, "display": a.name}], "text": a.name},
                "patient": self.patient_ref,
                "recordedDate": _years_ago(6 + _hint("alg", a.code) % 5, self.today).isoformat(),
                "reaction": [{"manifestation": [{"text": a.reaction}], "severity": a.severity}],
            })

    def immunizations(self) -> None:
        for idx, (cvx, name, years) in enumerate(self.p.immunizations):
            d = _years_ago(years, self.today)
            if d > self.today:
                continue
            self._immunization(("fixed", idx, cvx), cvx, name, d)
        if self.p.annual_flu:
            for year in range(REF_END.year - HISTORY_YEARS, self.today.year + 1):
                d = date(year, 10, 1 + _hint("flu", self.p.key, year) % 25)
                if REF_START <= d <= self.today:
                    self._immunization(("flu", year), "150" if not self.p.pediatric else "161",
                                       "Influenza, injectable, quadrivalent, preservative free", d)

    def _immunization(self, key: Any, cvx: str, name: str, d: date) -> None:
        # Vaccines given elsewhere show up at every institution (state registry reconciliation),
        # with the same date but a different resource id — good de-duplication fodder.
        self.add({
            "resourceType": "Immunization", "id": self.id("Immunization", key), "status": "completed",
            "vaccineCode": {"coding": [{"system": CVX, "code": cvx, "display": name}], "text": name},
            "patient": self.patient_ref, "occurrenceDateTime": _dt(d, 10, 15), "primarySource": True,
            "lotNumber": f"{_h('lot', self.p.key, key)[:6].upper()}", "manufacturer": {"display": MANUFACTURERS.get(cvx, "Pfizer")},
            "site": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ActSite", "code": "LA", "display": "Left arm"}]},
            "doseQuantity": {"value": 0.5, "unit": "mL", "system": UCUM, "code": "mL"},
        })

    # -- specialist / hospital -------------------------------------------------
    def procedures_and_imaging(self) -> None:
        for proc in self.p.procedures:
            d = _years_ago(proc.years_ago, self.today)
            eid = self.id("Encounter", ("proc", proc.snomed))
            self.add({
                "resourceType": "Encounter", "id": eid, "status": "finished",
                "class": {"system": ACT_CODE, "code": "IMP" if "replacement" in proc.name.lower() else "AMB",
                          "display": "inpatient encounter" if "replacement" in proc.name.lower() else "ambulatory"},
                "type": [{"text": f"Procedure — {proc.name}"}], "subject": self.patient_ref,
                "participant": [{"individual": {"display": self.tp.specialist_name}}],
                "period": {"start": _dt(d, 7, 0), "end": _dt(d + timedelta(days=2 if "replacement" in proc.name.lower() else 0), 15, 0)},
                "reasonCode": [{"text": proc.reason or proc.name}],
                "serviceProvider": {"display": self.tenant_name},
                "location": [{"location": {"display": f"{self.tenant_name} — Surgical Services"}}],
            })
            self.add({
                "resourceType": "Procedure", "id": self.id("Procedure", proc.snomed), "status": "completed",
                "code": {"coding": [{"system": SNOMED, "code": proc.snomed, "display": proc.name}], "text": proc.name},
                "subject": self.patient_ref, "encounter": {"reference": f"Encounter/{eid}"},
                "performedDateTime": _dt(d, 8, 0),
                "performer": [{"actor": {"display": self.tp.specialist_name}}],
                "reasonCode": [{"text": proc.reason}] if proc.reason else [],
                "outcome": {"text": "Successful; no complications."},
            })
        for img in self.p.imaging:
            d = _years_ago(img.years_ago, self.today)
            text = f"EXAM: {img.name}\nDATE: {d.isoformat()}\n\nIMPRESSION:\n{img.conclusion}\n"
            self.add({
                "resourceType": "DiagnosticReport", "id": self.id("DiagnosticReport", img.loinc), "status": "final",
                "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": img.category,
                                          "display": "Radiology" if img.category == "RAD" else "Cardiac Ultrasound"}]}],
                "code": {"coding": [{"system": LOINC, "code": img.loinc, "display": img.name}], "text": img.name},
                "subject": self.patient_ref, "effectiveDateTime": _dt(d, 13, 0), "issued": _dt(d, 17, 30),
                "performer": [{"display": f"{self.tenant_name} Imaging"}],
                "conclusion": img.conclusion,
                "presentedForm": [{"contentType": "text/plain", "data": _b64(text), "title": img.name}],
            })

    def devices(self) -> None:
        for idx, (name, type_text, mfr) in enumerate(self.p.devices):
            self.add({
                "resourceType": "Device", "id": self.id("Device", idx), "status": "active",
                "deviceName": [{"name": name, "type": "user-friendly-name"}],
                "type": {"text": type_text}, "manufacturer": mfr,
                "udiCarrier": [{"deviceIdentifier": f"0{_hint('udi', self.p.key, idx) % 10**13:013d}"}],
                "patient": self.patient_ref,
            })

    def care_team(self) -> None:
        participants = [{"role": [{"text": "Primary care physician"}], "member": {"display": self.tp.pcp}}]
        if self.tp.specialist:
            participants.append({"role": [{"text": self.p.specialty}], "member": {"display": self.tp.specialist_name}})
        participants.append({"role": [{"text": "Care coordinator"}], "member": {"display": CLINICIANS[_hint("cc", self.tenant) % len(CLINICIANS)]}})
        self.add({
            "resourceType": "CareTeam", "id": self.id("CareTeam", "main"), "status": "active",
            "name": f"{self.tenant_name} care team", "subject": self.patient_ref, "participant": participants,
        })

    def care_plans_and_goals(self) -> None:
        active = [c for c in self.p.conditions if c.status == "active"][:2]
        for c in active:
            meds = [m.name.split(" ")[0] for m in self.p.medications if m.status == "active"][:3]
            self.add({
                "resourceType": "CarePlan", "id": self.id("CarePlan", c.snomed), "status": "active", "intent": "plan",
                "title": f"{c.name} management", "subject": self.patient_ref,
                "period": {"start": _years_ago(c.onset_years_ago, self.today).isoformat()},
                "addresses": [{"display": c.name}],
                "activity": [{"detail": {"status": "in-progress", "description": d}} for d in
                             [f"Medication adherence: {', '.join(meds)}" if meds else "Lifestyle modification",
                              "Follow-up visit every 6 months", "Home monitoring and symptom log"]],
            })
        for idx, (desc, target) in enumerate(self.p.goals):
            self.add({
                "resourceType": "Goal", "id": self.id("Goal", idx), "lifecycleStatus": "active",
                "description": {"text": desc}, "subject": self.patient_ref,
                "startDate": _years_ago(1.5, self.today).isoformat(),
                "target": [{"detailString": target}],
            })

    def coverage(self) -> None:
        payor, plan = self.p.coverage
        self.add({
            "resourceType": "Coverage", "id": self.id("Coverage", "primary"), "status": "active",
            "type": {"text": f"{payor} {plan}"},
            "subscriberId": f"{payor[:3].upper()}{_hint('member', self.p.key) % 10**9:09d}",
            "beneficiary": self.patient_ref, "relationship": {"text": "self" if not self.p.pediatric else "child"},
            "period": {"start": date(self.today.year, 1, 1).isoformat(), "end": date(self.today.year, 12, 31).isoformat()},
            "payor": [{"display": payor}],
        })


@lru_cache(maxsize=64)
def _cached(persona_key: str, tenant: str, tenant_name: str, platform: str, today: date) -> tuple[dict[str, Any], ...]:
    builder = ChartBuilder(PERSONAS[persona_key], tenant, tenant_name, platform, today)
    return tuple(builder.build())


def build_chart(persona_key: str, tenant: str, tenant_name: str, platform: str = "epic",
                today: Optional[date] = None) -> list[dict[str, Any]]:
    return list(_cached(persona_key, tenant, tenant_name, platform, today or date.today()))


def patient_id_for(persona_key: str, tenant: str) -> str:
    return _h("patient", tenant, persona_key)[:16]
