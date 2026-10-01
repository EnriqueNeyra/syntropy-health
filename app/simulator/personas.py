"""
Synthetic patients for the built-in EHR simulator.

Every persona is entirely fictional. Each has a coherent multi-year clinical story
(chronic conditions, medications, allergies, trending labs) so the full pipeline —
aggregation across institutions, de-duplication, unit normalization, trend charts —
can be exercised without real patient data or EHR registrations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

SIM_PASSWORD = "syntropy"


@dataclass
class Lab:
    loinc: str
    name: str
    unit: str
    low: Optional[float]
    high: Optional[float]
    start: float                 # value ~5 years ago
    end: float                   # value today
    noise: float = 0.03          # relative jitter
    decimals: int = 1
    panel: Optional[str] = None


@dataclass
class Condition:
    snomed: str
    name: str
    icd10: str
    onset_years_ago: float
    status: str = "active"


@dataclass
class Medication:
    rxnorm: str
    name: str
    dose: str
    started_years_ago: float
    status: str = "active"
    prn: bool = False


@dataclass
class Allergy:
    code: str
    system: str
    name: str
    category: str                # medication | food | environment
    reaction: str
    severity: str                # mild | moderate | severe
    criticality: str = "low"


@dataclass
class Procedure:
    snomed: str
    name: str
    years_ago: float
    reason: Optional[str] = None


@dataclass
class Imaging:
    loinc: str
    name: str
    years_ago: float
    conclusion: str
    category: str = "RAD"


@dataclass
class Persona:
    key: str
    username: str
    given: str
    family: str
    gender: str
    birth_date: str
    phone: str
    email: str
    city: str
    state: str
    postal: str
    summary: str
    conditions: list[Condition]
    medications: list[Medication]
    allergies: list[Allergy]
    labs: list[Lab]
    vitals: dict[str, tuple[float, float]]      # metric -> (value 5y ago, value today)
    procedures: list[Procedure] = field(default_factory=list)
    imaging: list[Imaging] = field(default_factory=list)
    immunizations: list[tuple[str, str, float]] = field(default_factory=list)  # (cvx, name, years_ago)
    annual_flu: bool = True
    coverage: tuple[str, str] = ("Evergreen Health Plan", "PPO")
    devices: list[tuple[str, str, str]] = field(default_factory=list)          # (name, type, manufacturer)
    pediatric: bool = False
    specialty: str = "Cardiology"
    goals: list[tuple[str, str]] = field(default_factory=list)                 # (description, target)

    @property
    def full_name(self) -> str:
        return f"{self.given} {self.family}"


CBC = "CBC"
CMP = "Comprehensive metabolic panel"
LIPID = "Lipid panel"
THYROID = "Thyroid panel"

PERSONAS: dict[str, Persona] = {
    "alex": Persona(
        key="alex", username="alex.rivera", given="Alex", family="Rivera", gender="male",
        birth_date="1981-04-12", phone="555-0142", email="alex.rivera@example.com",
        city="Oakland", state="CA", postal="94610",
        summary="Hypertension, high cholesterol, prediabetes",
        conditions=[
            Condition("59621000", "Essential hypertension", "I10", 6.5),
            Condition("55822004", "Hyperlipidemia", "E78.5", 5.5),
            Condition("714628002", "Prediabetes", "R73.03", 2.2),
            Condition("367498001", "Seasonal allergic rhinitis", "J30.2", 12),
            Condition("44465007", "Sprain of ankle", "S93.409A", 3.1, status="resolved"),
        ],
        medications=[
            Medication("314076", "Lisinopril 10 MG Oral Tablet", "Take 1 tablet by mouth once daily", 6.3),
            Medication("617312", "Atorvastatin 20 MG Oral Tablet", "Take 1 tablet by mouth nightly", 3.8),
            Medication("1014678", "Cetirizine 10 MG Oral Tablet", "Take 1 tablet by mouth daily as needed for allergies", 8, prn=True),
            Medication("198405", "Ibuprofen 600 MG Oral Tablet", "Take 1 tablet every 6 hours as needed for pain", 3.1, status="completed", prn=True),
        ],
        allergies=[
            Allergy("7980", "http://www.nlm.nih.gov/research/umls/rxnorm", "Penicillin G", "medication", "Hives", "moderate", "high"),
            Allergy("256259004", "http://snomed.info/sct", "Pollen", "environment", "Sneezing and itchy eyes", "mild"),
        ],
        labs=[
            Lab("4548-4", "Hemoglobin A1c", "%", 4.0, 5.6, 5.6, 6.1, 0.01, 1),
            Lab("2345-7", "Glucose", "mg/dL", 70, 99, 101, 116, 0.05, 0, CMP),
            Lab("2160-0", "Creatinine", "mg/dL", 0.7, 1.3, 0.95, 1.02, 0.04, 2, CMP),
            Lab("33914-3", "eGFR", "mL/min/{1.73_m2}", 60, None, 98, 92, 0.03, 0, CMP),
            Lab("2951-2", "Sodium", "mmol/L", 135, 145, 140, 139, 0.01, 0, CMP),
            Lab("2823-3", "Potassium", "mmol/L", 3.5, 5.1, 4.2, 4.5, 0.04, 1, CMP),
            Lab("1742-6", "ALT", "U/L", 7, 56, 34, 41, 0.1, 0, CMP),
            Lab("2093-3", "Cholesterol, total", "mg/dL", None, 200, 248, 176, 0.04, 0, LIPID),
            Lab("13457-7", "LDL cholesterol (calculated)", "mg/dL", None, 100, 168, 98, 0.05, 0, LIPID),
            Lab("2085-9", "HDL cholesterol", "mg/dL", 40, None, 41, 47, 0.05, 0, LIPID),
            Lab("2571-8", "Triglycerides", "mg/dL", None, 150, 212, 148, 0.08, 0, LIPID),
            Lab("718-7", "Hemoglobin", "g/dL", 13.5, 17.5, 15.1, 14.8, 0.02, 1, CBC),
            Lab("6690-2", "White blood cell count", "10*3/uL", 4.5, 11.0, 6.8, 7.1, 0.08, 1, CBC),
            Lab("777-3", "Platelets", "10*3/uL", 150, 400, 245, 238, 0.06, 0, CBC),
            Lab("1989-3", "Vitamin D, 25-hydroxy", "ng/mL", 30, 100, 24, 34, 0.08, 0),
        ],
        vitals={"systolic": (144, 127), "diastolic": (93, 81), "heart_rate": (76, 68), "weight_kg": (95.5, 90.2),
                "height_cm": (178, 178), "temp_c": (36.8, 36.7), "spo2": (98, 98), "resp": (15, 14)},
        procedures=[Procedure("73761001", "Colonoscopy", 1.4, "Screening for colon cancer")],
        imaging=[Imaging("36643-5", "XR Ankle 3+ views, left", 3.1,
                         "No acute fracture or dislocation. Mild soft tissue swelling over the lateral malleolus.")],
        immunizations=[("115", "Tdap", 4.5), ("208", "COVID-19, mRNA, LNP-S, PF, 30 mcg/0.3 mL", 4.0),
                       ("300", "COVID-19, mRNA, LNP-S, bivalent, PF, 30 mcg/0.3 mL", 2.0), ("187", "Zoster recombinant", 0.4)],
        coverage=("Evergreen Health Plan", "PPO"),
        specialty="Cardiology",
        goals=[("Blood pressure below 130/80", "130/80 mm[Hg]"), ("Hemoglobin A1c below 5.7%", "5.7 %")],
    ),
    "jordan": Persona(
        key="jordan", username="jordan.chen", given="Jordan", family="Chen", gender="female",
        birth_date="1991-11-03", phone="555-0188", email="jordan.chen@example.com",
        city="Seattle", state="WA", postal="98103",
        summary="Hypothyroidism, asthma, iron-deficiency anemia",
        conditions=[
            Condition("40930008", "Hypothyroidism", "E03.9", 4.2),
            Condition("426979002", "Mild intermittent asthma", "J45.20", 20),
            Condition("37796009", "Migraine", "G43.909", 9),
            Condition("87522002", "Iron deficiency anemia", "D50.9", 1.3),
        ],
        medications=[
            Medication("966222", "Levothyroxine Sodium 0.075 MG Oral Tablet", "Take 1 tablet by mouth every morning on an empty stomach", 3.9),
            Medication("745679", "Albuterol 0.09 MG/ACTUAT Metered Dose Inhaler", "Inhale 2 puffs every 4-6 hours as needed for wheezing", 15, prn=True),
            Medication("313205", "Sumatriptan 50 MG Oral Tablet", "Take 1 tablet at migraine onset; may repeat once after 2 hours", 6, prn=True),
            Medication("310325", "Ferrous sulfate 325 MG Oral Tablet", "Take 1 tablet by mouth daily with food", 1.2),
        ],
        allergies=[
            Allergy("256349002", "http://snomed.info/sct", "Peanut", "food", "Anaphylaxis", "severe", "high"),
            Allergy("10180", "http://www.nlm.nih.gov/research/umls/rxnorm", "Sulfamethoxazole", "medication", "Rash", "mild"),
        ],
        labs=[
            Lab("3016-3", "TSH", "m[IU]/L", 0.4, 4.0, 6.9, 2.1, 0.08, 2, THYROID),
            Lab("3024-7", "Free T4", "ng/dL", 0.8, 1.8, 0.7, 1.2, 0.05, 2, THYROID),
            Lab("718-7", "Hemoglobin", "g/dL", 12.0, 15.5, 12.8, 11.4, 0.02, 1, CBC),
            Lab("4544-3", "Hematocrit", "%", 36, 46, 38.5, 34.9, 0.02, 1, CBC),
            Lab("6690-2", "White blood cell count", "10*3/uL", 4.5, 11.0, 6.2, 5.9, 0.08, 1, CBC),
            Lab("2276-4", "Ferritin", "ng/mL", 15, 150, 38, 11, 0.1, 0),
            Lab("1989-3", "Vitamin D, 25-hydroxy", "ng/mL", 30, 100, 21, 29, 0.08, 0),
            Lab("2345-7", "Glucose", "mg/dL", 70, 99, 84, 88, 0.05, 0, CMP),
            Lab("2160-0", "Creatinine", "mg/dL", 0.5, 1.1, 0.74, 0.78, 0.04, 2, CMP),
        ],
        vitals={"systolic": (112, 114), "diastolic": (72, 74), "heart_rate": (72, 78), "weight_kg": (61.0, 63.5),
                "height_cm": (165, 165), "temp_c": (36.7, 36.8), "spo2": (99, 98), "resp": (14, 15)},
        imaging=[Imaging("24558-9", "US Thyroid", 4.1,
                         "Diffusely heterogeneous thyroid gland without discrete nodules, consistent with thyroiditis.", "RAD")],
        immunizations=[("115", "Tdap", 6.0), ("208", "COVID-19, mRNA, LNP-S, PF, 30 mcg/0.3 mL", 4.0),
                       ("165", "HPV9", 14.0)],
        coverage=("Summit Mutual", "HMO"),
        specialty="Endocrinology",
        goals=[("TSH within reference range", "0.4-4.0 m[IU]/L"), ("Ferritin above 30 ng/mL", "30 ng/mL")],
    ),
    "sam": Persona(
        key="sam", username="sam.patel", given="Sam", family="Patel", gender="male",
        birth_date="1958-02-27", phone="555-0107", email="sam.patel@example.com",
        city="Phoenix", state="AZ", postal="85016",
        summary="Type 2 diabetes, CKD 3a, atrial fibrillation",
        conditions=[
            Condition("44054006", "Type 2 diabetes mellitus", "E11.9", 11),
            Condition("700378005", "Chronic kidney disease stage 3a", "N18.31", 2.5),
            Condition("49436004", "Atrial fibrillation", "I48.91", 4.1),
            Condition("59621000", "Essential hypertension", "I10", 15),
            Condition("239873007", "Osteoarthritis of knee", "M17.11", 7),
            Condition("55822004", "Hyperlipidemia", "E78.5", 12),
        ],
        medications=[
            Medication("861007", "Metformin hydrochloride 1000 MG Oral Tablet", "Take 1 tablet by mouth twice daily with meals", 10),
            Medication("1545658", "Empagliflozin 10 MG Oral Tablet", "Take 1 tablet by mouth once daily", 2.0),
            Medication("1364445", "Apixaban 5 MG Oral Tablet", "Take 1 tablet by mouth twice daily", 4.0),
            Medication("866427", "Metoprolol succinate 50 MG Extended Release Oral Tablet", "Take 1 tablet by mouth daily", 4.0),
            Medication("197361", "Amlodipine 5 MG Oral Tablet", "Take 1 tablet by mouth daily", 8),
            Medication("617310", "Atorvastatin 40 MG Oral Tablet", "Take 1 tablet by mouth nightly", 11),
            Medication("310798", "Hydrochlorothiazide 25 MG Oral Tablet", "Take 1 tablet by mouth daily", 9, status="stopped"),
        ],
        allergies=[
            Allergy("2670", "http://www.nlm.nih.gov/research/umls/rxnorm", "Codeine", "medication", "Nausea and vomiting", "moderate"),
            Allergy("227037002", "http://snomed.info/sct", "Shellfish", "food", "Urticaria", "moderate", "high"),
        ],
        labs=[
            Lab("4548-4", "Hemoglobin A1c", "%", 4.0, 5.6, 8.1, 7.1, 0.015, 1),
            Lab("2345-7", "Glucose", "mg/dL", 70, 99, 162, 138, 0.08, 0, CMP),
            Lab("2160-0", "Creatinine", "mg/dL", 0.7, 1.3, 1.22, 1.46, 0.03, 2, CMP),
            Lab("33914-3", "eGFR", "mL/min/{1.73_m2}", 60, None, 63, 51, 0.02, 0, CMP),
            Lab("3094-0", "Urea nitrogen (BUN)", "mg/dL", 7, 20, 19, 25, 0.06, 0, CMP),
            Lab("2951-2", "Sodium", "mmol/L", 135, 145, 139, 138, 0.01, 0, CMP),
            Lab("2823-3", "Potassium", "mmol/L", 3.5, 5.1, 4.6, 5.0, 0.03, 1, CMP),
            Lab("9318-7", "Albumin/creatinine ratio, urine", "mg/g", None, 30, 22, 46, 0.1, 0),
            Lab("2093-3", "Cholesterol, total", "mg/dL", None, 200, 172, 151, 0.04, 0, LIPID),
            Lab("13457-7", "LDL cholesterol (calculated)", "mg/dL", None, 100, 94, 72, 0.05, 0, LIPID),
            Lab("2085-9", "HDL cholesterol", "mg/dL", 40, None, 38, 42, 0.05, 0, LIPID),
            Lab("2571-8", "Triglycerides", "mg/dL", None, 150, 188, 162, 0.08, 0, LIPID),
            Lab("718-7", "Hemoglobin", "g/dL", 13.5, 17.5, 14.2, 13.1, 0.02, 1, CBC),
            Lab("777-3", "Platelets", "10*3/uL", 150, 400, 210, 198, 0.06, 0, CBC),
            Lab("2857-1", "Prostate specific antigen", "ng/mL", 0, 4.0, 1.8, 2.3, 0.08, 1),
        ],
        vitals={"systolic": (148, 134), "diastolic": (86, 78), "heart_rate": (88, 71), "weight_kg": (99.0, 94.1),
                "height_cm": (172, 171), "temp_c": (36.6, 36.6), "spo2": (96, 96), "resp": (16, 16)},
        procedures=[
            Procedure("609588000", "Total knee replacement, right", 2.8, "Osteoarthritis of knee"),
            Procedure("180325003", "Electrical cardioversion", 3.9, "Atrial fibrillation"),
            Procedure("73761001", "Colonoscopy", 4.6, "Screening for colon cancer"),
        ],
        imaging=[
            Imaging("34552-0", "Echocardiogram, transthoracic", 3.9,
                    "Left ventricular ejection fraction 55-60%. Mild left atrial enlargement. No significant valvular disease.", "CUS"),
            Imaging("36554-4", "XR Chest 2 views", 1.6, "No acute cardiopulmonary process. Stable mild cardiomegaly."),
            Imaging("37631-9", "XR Knee 3 views, right", 2.6, "Status post total knee arthroplasty with components in expected alignment."),
        ],
        immunizations=[("33", "Pneumococcal polysaccharide PPV23", 5.5), ("133", "Pneumococcal conjugate PCV13", 6.5),
                       ("187", "Zoster recombinant", 3.0), ("115", "Tdap", 7.0),
                       ("300", "COVID-19, mRNA, LNP-S, bivalent, PF, 30 mcg/0.3 mL", 2.0)],
        coverage=("Medicare", "Part B"),
        devices=[("Continuous glucose monitor", "Glucose monitoring system", "Dexcom"),
                 ("Right knee prosthesis", "Knee joint prosthesis", "Stryker")],
        specialty="Nephrology",
        goals=[("Hemoglobin A1c below 7.0%", "7.0 %"), ("Blood pressure below 130/80", "130/80 mm[Hg]"),
               ("Walk 30 minutes 5 days per week", "150 min/wk")],
    ),
    "riley": Persona(
        key="riley", username="riley.morgan", given="Riley", family="Morgan", gender="female",
        birth_date="2018-06-15", phone="555-0163", email="morgan.family@example.com",
        city="Austin", state="TX", postal="78704",
        summary="Pediatric: eczema, well-child care",
        conditions=[
            Condition("24079001", "Atopic dermatitis", "L20.9", 5.5),
            Condition("65363002", "Otitis media", "H66.90", 2.4, status="resolved"),
        ],
        medications=[
            Medication("106258", "Hydrocortisone 1% Topical Cream", "Apply thin layer to affected areas twice daily as needed", 5, prn=True),
            Medication("308182", "Amoxicillin 250 MG/5ML Oral Suspension", "Take 7.5 mL by mouth twice daily for 10 days", 2.4, status="completed"),
        ],
        allergies=[],
        labs=[
            Lab("5671-3", "Lead, blood", "ug/dL", None, 3.5, 1.2, 1.0, 0.1, 1),
            Lab("718-7", "Hemoglobin", "g/dL", 11.0, 14.5, 11.9, 12.6, 0.02, 1),
        ],
        vitals={"systolic": (94, 102), "diastolic": (58, 64), "heart_rate": (112, 92), "weight_kg": (12.5, 23.8),
                "height_cm": (86, 122), "temp_c": (36.9, 36.8), "spo2": (99, 99), "resp": (26, 20)},
        immunizations=[
            ("08", "Hepatitis B, pediatric", 7.2), ("20", "DTaP", 7.0), ("10", "IPV", 7.0), ("49", "Hib (PRP-OMP)", 7.0),
            ("133", "Pneumococcal conjugate PCV13", 7.0), ("116", "Rotavirus, pentavalent", 7.0),
            ("20", "DTaP", 6.8), ("10", "IPV", 6.8), ("03", "MMR", 6.2), ("21", "Varicella", 6.2),
            ("83", "Hepatitis A, pediatric", 6.0), ("20", "DTaP", 2.5), ("03", "MMR", 2.5), ("21", "Varicella", 2.5),
        ],
        coverage=("Summit Mutual", "HMO — dependent"),
        pediatric=True,
        specialty="Pediatric dermatology",
        goals=[("Eczema flares under control", "fewer than 2 flares per month")],
    ),
}

BY_USERNAME = {p.username: p for p in PERSONAS.values()}


def authenticate(username: str, password: str) -> Optional[Persona]:
    persona = BY_USERNAME.get((username or "").strip().lower())
    if persona and password == SIM_PASSWORD:
        return persona
    return None
