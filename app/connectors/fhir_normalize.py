"""
FHIR R4 → Syntropy unified record normalization.

Each ``normalize_*`` function turns one FHIR resource into the flat record shape
stored in ``clinical_records`` (see ``app.store.records``), preserving the raw
resource for full fidelity. Codings are chosen by preferred terminology (LOINC for
observations, RxNorm for medications, SNOMED CT / ICD-10 for problems, CVX for
vaccines), values are unit-normalized, and every record gets a ``dedup_key`` that is
identical across institutions for the same real-world fact.
"""

from __future__ import annotations

import base64
import html as html_lib
import re
from typing import Any, Callable, Optional

from app.connectors import units

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
CVX = "http://hl7.org/fhir/sid/cvx"
ICD10 = ("http://hl7.org/fhir/sid/icd-10-cm", "http://hl7.org/fhir/sid/icd-10")

INTERPRETATION_MAP = {
    "H": "high", "HH": "critical_high", "HU": "critical_high", ">": "high",
    "L": "low", "LL": "critical_low", "LU": "critical_low", "<": "low",
    "A": "abnormal", "AA": "abnormal", "POS": "abnormal", "DET": "abnormal",
    "N": "normal", "NEG": "normal", "ND": "normal",
    "HIGH": "high", "LOW": "low", "ABNORMAL": "abnormal", "NORMAL": "normal",
    "CRITICAL_HIGH": "critical_high", "CRITICAL_LOW": "critical_low",
}

# Short, familiar names for common vital-sign LOINC codes.
FRIENDLY_TITLES = {
    "85354-6": "Blood pressure", "55284-4": "Blood pressure", "8867-4": "Heart rate", "9279-1": "Respiratory rate",
    "2708-6": "Oxygen saturation", "59408-5": "Oxygen saturation", "8310-5": "Body temperature",
    "29463-7": "Weight", "3141-9": "Weight", "8302-2": "Height", "8306-3": "Height", "39156-5": "BMI",
    "8287-5": "Head circumference", "72514-3": "Pain severity",
}

TEXT_CONTENT_TYPES = ("text/plain", "text/html", "text/rtf", "application/xhtml+xml", "text/xml", "application/xml")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm_name(text: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def pick_coding(concept: Optional[dict[str, Any]], preferred: tuple[str, ...] = ()) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Returns (system, code, display) choosing the first coding from a preferred system."""
    if not isinstance(concept, dict):
        return None, None, None
    codings = [c for c in concept.get("coding", []) if isinstance(c, dict)]
    for system in preferred:
        for c in codings:
            if c.get("system") == system and c.get("code"):
                return c.get("system"), c.get("code"), c.get("display")
    for c in codings:
        if c.get("code"):
            return c.get("system"), c.get("code"), c.get("display")
    return None, None, None


def concept_text(concept: Optional[dict[str, Any]], default: str = "") -> str:
    if not isinstance(concept, dict):
        return default
    if concept.get("text"):
        return concept["text"]
    for c in concept.get("coding", []):
        if c.get("display"):
            return c["display"]
    return default


def first_code(concept: Optional[dict[str, Any]]) -> Optional[str]:
    if not isinstance(concept, dict):
        return None
    for c in concept.get("coding", []):
        if c.get("code"):
            return c["code"]
    return concept.get("text")


def status_code(concept: Any) -> Optional[str]:
    if isinstance(concept, str):
        return concept
    code = first_code(concept)
    return code.lower() if isinstance(code, str) else None


def ref_display(ref: Any) -> Optional[str]:
    if isinstance(ref, dict):
        return ref.get("display") or ref.get("reference")
    return None


def _date(*values: Any) -> Optional[str]:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h[1-6]>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def decode_attachment(att: dict[str, Any]) -> Optional[str]:
    data = att.get("data")
    if not data:
        return None
    ctype = (att.get("contentType") or "text/plain").split(";")[0].strip().lower()
    if ctype not in TEXT_CONTENT_TYPES and not ctype.startswith("text/"):
        return None
    try:
        text = base64.b64decode(data).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return None
    if "html" in ctype or "xml" in ctype:
        return strip_html(text)
    if "rtf" in ctype:
        return re.sub(r"\\[a-z]+-?\d* ?|[{}]", "", text).strip()
    return text


def _base(res: dict[str, Any], category: str, title: str, **extra: Any) -> dict[str, Any]:
    rec = {
        "resource_type": res["resourceType"], "resource_id": str(res.get("id") or ""),
        "category": category, "title": title or "Untitled", "raw": res,
        "code_system": None, "code": None, "code_display": None, "effective_at": None, "effective_end": None,
        "status": None, "value_num": None, "value_text": None, "unit": None, "value_norm": None, "unit_norm": None,
        "ref_low": None, "ref_high": None, "ref_text": None, "interpretation": None, "details": {},
        "narrative": None, "dedup_key": None,
    }
    rec.update(extra)
    return rec


# ---------------------------------------------------------------------------
# Patient
# ---------------------------------------------------------------------------

def normalize_patient(res: dict[str, Any]) -> dict[str, Any]:
    names = res.get("name") or []
    official = next((n for n in names if n.get("use") == "official"), names[0] if names else {})
    given = " ".join(official.get("given") or [])
    full = official.get("text") or f"{given} {official.get('family') or ''}".strip() or "Unknown"
    mrn = None
    for ident in res.get("identifier") or []:
        codes = [c.get("code") for c in (ident.get("type") or {}).get("coding", [])]
        if "MR" in codes or "mrn" in (ident.get("system") or "").lower():
            mrn = ident.get("value")
            break
    addresses = []
    for a in res.get("address") or []:
        line = ", ".join(a.get("line") or [])
        parts = [p for p in (line, a.get("city"), a.get("state"), a.get("postalCode")) if p]
        if parts:
            addresses.append(", ".join(parts))
    return {
        "id": res.get("id"), "name": full, "given": given, "family": official.get("family"),
        "gender": res.get("gender"), "birth_date": res.get("birthDate"), "mrn": mrn,
        "telecom": [{"system": t.get("system"), "value": t.get("value")} for t in res.get("telecom") or [] if t.get("value")],
        "address": addresses, "raw": res,
    }


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

def _interpretation(res: dict[str, Any], value: Optional[float], low: Optional[float], high: Optional[float]) -> Optional[str]:
    for interp in res.get("interpretation") or []:
        code = first_code(interp)
        if code:
            mapped = INTERPRETATION_MAP.get(str(code).upper())
            if mapped:
                return mapped
        text = (interp.get("text") or "").lower()
        for word in ("critical", "high", "low", "abnormal", "normal"):
            if word in text:
                return {"critical": "abnormal"}.get(word, word)
    if value is not None:
        if high is not None and value > high:
            return "high"
        if low is not None and value < low:
            return "low"
        if low is not None or high is not None:
            return "normal"
    return None


def _obs_category(res: dict[str, Any]) -> str:
    codes = set()
    for cat in res.get("category") or []:
        for c in cat.get("coding", []):
            codes.add((c.get("code") or "").lower())
        codes.add((cat.get("text") or "").lower())
    if "vital-signs" in codes or "vital signs" in codes:
        return "vitals"
    if codes & {"laboratory", "lab"}:
        return "labs"
    if codes & {"social-history", "survey", "exam", "activity", "sdoh", "functional-status", "imaging", "procedure"}:
        return "observations"
    return "labs"


def _quantity(q: dict[str, Any]) -> tuple[Optional[float], Optional[str]]:
    return _num(q.get("value")), q.get("unit") or q.get("code")


def normalize_observation(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    if (res.get("status") or "").lower() in ("entered-in-error", "cancelled"):
        return None
    system, code, display = pick_coding(res.get("code"), (LOINC,))
    title = concept_text(res.get("code"), display or "Observation")
    category = _obs_category(res)
    if category == "vitals" and code in FRIENDLY_TITLES:
        title = FRIENDLY_TITLES[code]
    rec = _base(res, category, title, code_system=system, code=code, code_display=display,
                status=res.get("status"),
                effective_at=_date(res.get("effectiveDateTime"), (res.get("effectivePeriod") or {}).get("start"),
                                   res.get("effectiveInstant"), res.get("issued")))
    value: Optional[float] = None
    unit: Optional[str] = None
    comps = []
    for comp in res.get("component") or []:
        c_sys, c_code, c_disp = pick_coding(comp.get("code"), (LOINC,))
        c_val, c_unit = _quantity(comp.get("valueQuantity") or {})
        comps.append({"code": c_code, "name": concept_text(comp.get("code"), c_disp or "Component"),
                      "value": c_val, "unit": units.pretty_unit(c_unit),
                      "text": comp.get("valueString") or concept_text(comp.get("valueCodeableConcept")) or None})
    if "valueQuantity" in res:
        value, unit = _quantity(res["valueQuantity"])
        comparator = res["valueQuantity"].get("comparator")
        rec["value_text"] = f"{comparator or ''}{_fmt(value)} {units.pretty_unit(unit) or ''}".strip()
    elif "valueString" in res:
        rec["value_text"] = res["valueString"]
    elif "valueCodeableConcept" in res:
        rec["value_text"] = concept_text(res["valueCodeableConcept"])
    elif "valueBoolean" in res:
        rec["value_text"] = "Yes" if res["valueBoolean"] else "No"
    elif "valueInteger" in res:
        value = _num(res["valueInteger"])
        rec["value_text"] = _fmt(value)
    elif comps:
        vals = [c for c in comps if c["value"] is not None]
        if code == "85354-6" or len(vals) == 2 and all(c["unit"] in ("mmHg", "mm[Hg]") for c in vals):
            sys_ = next((c for c in vals if c["code"] == "8480-6"), vals[0] if vals else None)
            dia = next((c for c in vals if c["code"] == "8462-4"), vals[1] if len(vals) > 1 else None)
            if sys_ and dia:
                rec["value_text"] = f"{_fmt(sys_['value'])}/{_fmt(dia['value'])} mmHg"
                unit = "mmHg"
        else:
            rec["value_text"] = " · ".join(f"{c['name']}: {_fmt(c['value']) if c['value'] is not None else c['text']} {c['unit'] or ''}".strip() for c in comps)
    elif res.get("dataAbsentReason"):
        rec["value_text"] = concept_text(res["dataAbsentReason"], "Not available")
    if rec["value_text"] is None and value is None and not comps:
        return None

    low = high = None
    ref_text = None
    for rr in res.get("referenceRange") or []:
        low = _num((rr.get("low") or {}).get("value"))
        high = _num((rr.get("high") or {}).get("value"))
        ref_text = rr.get("text")
        if not ref_text:
            if low is not None and high is not None:
                ref_text = f"{_fmt(low)}–{_fmt(high)}"
            elif low is not None:
                ref_text = f"≥ {_fmt(low)}"
            elif high is not None:
                ref_text = f"≤ {_fmt(high)}"
        break

    value_norm, unit_norm = units.normalize(code, value, unit)
    rec.update({
        "value_num": value, "unit": units.pretty_unit(unit), "value_norm": value_norm,
        "unit_norm": units.pretty_unit(unit_norm), "ref_low": low, "ref_high": high, "ref_text": ref_text,
        "interpretation": _interpretation(res, value, low, high),
    })
    if value_norm is not None and value is not None and rec["unit_norm"] != rec["unit"]:
        # Display in the canonical unit so every source reads the same; keep the original for provenance.
        rec["details_original"] = rec["value_text"]
        rec["value_text"] = f"{_fmt(round(value_norm, 1))} {rec['unit_norm']}"
        # Reference range is expressed in the source unit; convert it too.
        rec["ref_low"] = units.normalize(code, low, unit)[0] if low is not None else None
        rec["ref_high"] = units.normalize(code, high, unit)[0] if high is not None else None
    rec["details"] = {"components": comps} if comps else {}
    if rec.get("details_original"):
        rec["details"]["original_value"] = rec.pop("details_original")
    if res.get("note"):
        rec["narrative"] = " ".join(n.get("text", "") for n in res["note"] if n.get("text")) or None
    key_value = rec["value_text"] or _fmt(value)
    rec["dedup_key"] = f"obs|{code or _norm_name(title)}|{(rec['effective_at'] or '')[:16]}|{key_value}"
    return rec


def _fmt(v: Optional[float]) -> str:
    """Display text: at most 4 significant digits (whole-number digits are never dropped).
    Synthetic sandboxes report values like 84.9863 mg/dL; value_num keeps the full precision."""
    if v is None:
        return ""
    v = float(v)
    if v.is_integer():
        return str(int(v))
    if abs(v) < 1:
        return f"{v:.4g}"
    decimals = max(0, 4 - len(str(int(abs(v)))))
    return f"{v:.{decimals}f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Problem list, medications, allergies, immunizations
# ---------------------------------------------------------------------------

def normalize_condition(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    verification = status_code(res.get("verificationStatus"))
    if verification in ("entered-in-error", "refuted"):
        return None
    system, code, display = pick_coding(res.get("code"), (SNOMED, *ICD10))
    title = concept_text(res.get("code"), display or "Condition")
    icd = next((c.get("code") for c in (res.get("code") or {}).get("coding", []) if c.get("system") in ICD10), None)
    snomed = next((c.get("code") for c in (res.get("code") or {}).get("coding", []) if c.get("system") == SNOMED), None)
    categories = [concept_text(c) or first_code(c) for c in res.get("category") or []]
    rec = _base(res, "conditions", title, code_system=system, code=code, code_display=display,
                status=status_code(res.get("clinicalStatus")) or "active",
                effective_at=_date(res.get("onsetDateTime"), (res.get("onsetPeriod") or {}).get("start"), res.get("recordedDate")),
                effective_end=_date(res.get("abatementDateTime"), (res.get("abatementPeriod") or {}).get("end")))
    rec["details"] = {"icd10": icd, "snomed": snomed, "verification": verification, "categories": categories,
                      "recorded": res.get("recordedDate"), "severity": concept_text(res.get("severity")) or None}
    if res.get("note"):
        rec["narrative"] = " ".join(n.get("text", "") for n in res["note"] if n.get("text")) or None
    rec["dedup_key"] = f"cond|{snomed or icd or _norm_name(title)}"
    return rec


def normalize_medication_request(res: dict[str, Any], resolve: Callable[[str], Optional[dict[str, Any]]] = lambda r: None) -> Optional[dict[str, Any]]:
    if (res.get("status") or "").lower() == "entered-in-error":
        return None
    concept = res.get("medicationCodeableConcept")
    if not concept and res.get("medicationReference"):
        ref = res["medicationReference"].get("reference") or ""
        med = resolve(ref)
        if med:
            concept = med.get("code")
        if not concept:
            concept = {"text": res["medicationReference"].get("display") or "Medication"}
    system, code, display = pick_coding(concept, (RXNORM,))
    title = concept_text(concept, display or "Medication")
    dosage = res.get("dosageInstruction") or []
    dose_text = "; ".join(d.get("text") for d in dosage if d.get("text")) or None
    as_needed = any(d.get("asNeededBoolean") for d in dosage)
    rec = _base(res, "medications", title, code_system=system, code=code, code_display=display,
                status=(res.get("status") or "active").lower(),
                effective_at=_date(res.get("authoredOn"), ((res.get("dispenseRequest") or {}).get("validityPeriod") or {}).get("start")),
                value_text=dose_text)
    rec["details"] = {"dosage": dose_text, "as_needed": as_needed, "prescriber": ref_display(res.get("requester")),
                      "intent": res.get("intent"), "rxnorm": code if system == RXNORM else None,
                      "reason": [concept_text(r) for r in res.get("reasonCode") or []],
                      "refills": (res.get("dispenseRequest") or {}).get("numberOfRepeatsAllowed")}
    rec["narrative"] = dose_text
    rec["dedup_key"] = f"med|{code if system == RXNORM and code else _norm_name(title)}"
    return rec


def normalize_medication_statement(res: dict[str, Any], resolve: Callable[[str], Optional[dict[str, Any]]] = lambda r: None) -> Optional[dict[str, Any]]:
    shim = dict(res)
    shim["authoredOn"] = res.get("dateAsserted") or _date((res.get("effectivePeriod") or {}).get("start"), res.get("effectiveDateTime"))
    shim["dosageInstruction"] = res.get("dosage")
    shim["resourceType"] = "MedicationStatement"
    return normalize_medication_request(shim, resolve)


# SNOMED "no allergy" statements: these record the absence of (information about) allergies, not an allergen.
NO_KNOWN_ALLERGY_CODES = {"716186003", "409137002", "428607008", "429625007"}
ALLERGIES_NOT_ASKED_CODES = {"1631000175102"}


def normalize_allergy(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    verification = status_code(res.get("verificationStatus"))
    if verification in ("entered-in-error", "refuted"):
        return None
    system, code, display = pick_coding(res.get("code"), (RXNORM, SNOMED))
    title = concept_text(res.get("code"), display or "Allergy")
    reactions = []
    for r in res.get("reaction") or []:
        manifest = ", ".join(concept_text(m) for m in r.get("manifestation") or [] if concept_text(m))
        reactions.append({"manifestation": manifest or None, "severity": r.get("severity")})
    narrative = "; ".join(f"{r['manifestation'] or 'Reaction'}{' (' + r['severity'] + ')' if r['severity'] else ''}" for r in reactions) or None
    status = status_code(res.get("clinicalStatus")) or "active"
    codes = {c.get("code") for c in (res.get("code") or {}).get("coding") or []}
    if codes & NO_KNOWN_ALLERGY_CODES:
        status = "no-known-allergies"
    elif codes & ALLERGIES_NOT_ASKED_CODES or title.strip().lower() == "not on file":
        status = "not-asked"
    rec = _base(res, "allergies", title, code_system=system, code=code, code_display=display, status=status,
                effective_at=_date(res.get("onsetDateTime"), res.get("recordedDate")), narrative=narrative)
    cats = res.get("category") or []
    rec["details"] = {"category": cats[0] if cats else None, "criticality": res.get("criticality"),
                      "type": res.get("type"), "verification": verification, "reactions": reactions}
    rec["dedup_key"] = f"alg|{code or _norm_name(title)}"
    return rec


def normalize_immunization(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    if (res.get("status") or "").lower() == "entered-in-error":
        return None
    system, code, display = pick_coding(res.get("vaccineCode"), (CVX,))
    title = concept_text(res.get("vaccineCode"), display or "Vaccine")
    when = _date(res.get("occurrenceDateTime"), res.get("occurrenceString"), res.get("recorded"))
    rec = _base(res, "immunizations", title, code_system=system, code=code, code_display=display,
                status=res.get("status") or "completed", effective_at=when)
    dose = res.get("doseQuantity") or {}
    rec["details"] = {"lot": res.get("lotNumber"), "manufacturer": ref_display(res.get("manufacturer")),
                      "site": concept_text(res.get("site")) or None, "route": concept_text(res.get("route")) or None,
                      "dose": f"{_fmt(_num(dose.get('value')))} {dose.get('unit') or ''}".strip() or None,
                      "performer": ref_display(((res.get("performer") or [{}])[0]).get("actor"))}
    rec["dedup_key"] = f"imm|{code or _norm_name(title)}|{(when or '')[:10]}"
    return rec


# ---------------------------------------------------------------------------
# Visits, procedures, reports, notes
# ---------------------------------------------------------------------------

def normalize_encounter(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    if (res.get("status") or "").lower() in ("entered-in-error", "cancelled"):
        return None
    types = res.get("type") or []
    title = concept_text(types[0]) if types else ""
    klass = res.get("class") or {}
    class_label = klass.get("display") or klass.get("code") or None
    if not title:
        title = {"AMB": "Office visit", "EMER": "Emergency visit", "IMP": "Hospital admission",
                 "VR": "Virtual visit", "HH": "Home health visit"}.get(klass.get("code", ""), "Visit")
    period = res.get("period") or {}
    reasons = [concept_text(r) for r in res.get("reasonCode") or [] if concept_text(r)]
    clinicians = [ref_display(p.get("individual")) for p in res.get("participant") or [] if ref_display(p.get("individual"))]
    locations = [ref_display(l.get("location")) for l in res.get("location") or [] if ref_display(l.get("location"))]
    rec = _base(res, "encounters", title, status=res.get("status"),
                effective_at=_date(period.get("start")), effective_end=_date(period.get("end")),
                narrative=", ".join(reasons) or None)
    rec["details"] = {"class": class_label, "class_code": klass.get("code"), "reasons": reasons,
                      "clinicians": clinicians, "locations": locations,
                      "provider": ref_display(res.get("serviceProvider"))}
    return rec


def normalize_procedure(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    if (res.get("status") or "").lower() in ("entered-in-error", "not-done"):
        return None
    system, code, display = pick_coding(res.get("code"), (SNOMED,))
    title = concept_text(res.get("code"), display or "Procedure")
    when = _date(res.get("performedDateTime"), (res.get("performedPeriod") or {}).get("start"), res.get("performedString"))
    performers = [ref_display(p.get("actor")) for p in res.get("performer") or [] if ref_display(p.get("actor"))]
    outcome = concept_text(res.get("outcome")) or None
    reasons = [concept_text(r) for r in res.get("reasonCode") or [] if concept_text(r)]
    rec = _base(res, "procedures", title, code_system=system, code=code, code_display=display,
                status=res.get("status"), effective_at=when,
                effective_end=(res.get("performedPeriod") or {}).get("end"), narrative=outcome)
    rec["details"] = {"performers": performers, "outcome": outcome, "reasons": reasons,
                      "body_site": ", ".join(concept_text(b) for b in res.get("bodySite") or []) or None}
    rec["dedup_key"] = f"proc|{code or _norm_name(title)}|{(when or '')[:10]}"
    return rec


def normalize_diagnostic_report(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    if (res.get("status") or "").lower() in ("entered-in-error", "cancelled"):
        return None
    system, code, display = pick_coding(res.get("code"), (LOINC,))
    title = concept_text(res.get("code"), display or "Report")
    cat_codes = [first_code(c) for c in res.get("category") or []]
    cat_labels = [concept_text(c) for c in res.get("category") or []]
    kind = "lab" if any(c in ("LAB", "laboratory") for c in cat_codes if c) else "imaging" if any(
        c in ("RAD", "CUS", "IMG", "CT", "MR", "US", "NMR", "RX") for c in cat_codes if c) else "other"
    when = _date(res.get("effectiveDateTime"), (res.get("effectivePeriod") or {}).get("start"), res.get("issued"))
    text_parts = [t for t in (decode_attachment(f) for f in res.get("presentedForm") or []) if t]
    rec = _base(res, "reports", title, code_system=system, code=code, code_display=display,
                status=res.get("status"), effective_at=when,
                narrative=res.get("conclusion") or (text_parts[0][:4000] if text_parts else None))
    rec["details"] = {
        "kind": kind, "categories": [c for c in cat_labels if c],
        "performer": ", ".join(ref_display(p) for p in res.get("performer") or [] if ref_display(p)) or None,
        "result_refs": [r.get("reference") for r in res.get("result") or [] if r.get("reference")],
        "full_text": "\n\n".join(text_parts) or None,
        # Linked report bodies (Epic sends imaging reports this way); fetched after the sync like note bodies.
        "attachments": [{"content_type": f.get("contentType"), "url": f.get("url"), "title": f.get("title")}
                        for f in res.get("presentedForm") or [] if f.get("url") and not f.get("data")],
    }
    rec["dedup_key"] = f"rep|{code or _norm_name(title)}|{(when or '')[:10]}"
    return rec


def normalize_document_reference(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    if (res.get("status") or "").lower() == "entered-in-error":
        return None
    doc_type = concept_text(res.get("type"), "Clinical note")
    title = res.get("description") or doc_type
    attachments = [c.get("attachment") or {} for c in res.get("content") or []]
    text = None
    for att in attachments:
        text = decode_attachment(att)
        if text:
            break
    first = attachments[0] if attachments else {}
    context = res.get("context") or {}
    when = _date(res.get("date"), (context.get("period") or {}).get("start"), first.get("creation"))
    rec = _base(res, "notes", title, status=res.get("docStatus") or res.get("status"), effective_at=when,
                code_system=pick_coding(res.get("type"), (LOINC,))[0], code=pick_coding(res.get("type"), (LOINC,))[1],
                narrative=text)
    rec["details"] = {
        "type": doc_type,
        "categories": [concept_text(c) for c in res.get("category") or [] if concept_text(c)],
        "authors": [ref_display(a) for a in res.get("author") or [] if ref_display(a)],
        "attachments": [{"content_type": a.get("contentType"), "url": a.get("url"), "title": a.get("title"),
                         "size": a.get("size"), "inline": bool(a.get("data"))} for a in attachments],
        "encounter": [e.get("reference") for e in context.get("encounter") or [] if e.get("reference")],
    }
    return rec


# ---------------------------------------------------------------------------
# Care coordination, devices, coverage
# ---------------------------------------------------------------------------

def normalize_care_team(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    members = []
    for p in res.get("participant") or []:
        name = ref_display(p.get("member"))
        if not name:
            continue
        role = concept_text((p.get("role") or [{}])[0]) or "Care team member"
        members.append({"name": name, "role": role})
    if not members:
        return None
    rec = _base(res, "care_team", res.get("name") or "Care team", status=res.get("status"),
                effective_at=(res.get("period") or {}).get("start"),
                narrative=", ".join(f"{m['name']} ({m['role']})" for m in members))
    rec["details"] = {"members": members}
    return rec


def normalize_care_plan(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    if (res.get("status") or "").lower() in ("entered-in-error", "revoked"):
        return None
    activities = []
    for act in res.get("activity") or []:
        detail = act.get("detail") or {}
        desc = detail.get("description") or concept_text(detail.get("code"))
        if desc:
            activities.append({"description": desc, "status": detail.get("status")})
    title = res.get("title") or concept_text((res.get("category") or [{}])[0]) or "Care plan"
    rec = _base(res, "care_plans", title, status=res.get("status"),
                effective_at=(res.get("period") or {}).get("start"), effective_end=(res.get("period") or {}).get("end"),
                narrative=res.get("description") or "; ".join(a["description"] for a in activities) or None)
    rec["details"] = {"activities": activities, "addresses": [ref_display(a) for a in res.get("addresses") or [] if ref_display(a)]}
    return rec


def normalize_goal(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    title = concept_text(res.get("description"), "Goal")
    targets = []
    for t in res.get("target") or []:
        if t.get("detailString"):
            targets.append(t["detailString"])
        elif t.get("detailQuantity"):
            q = t["detailQuantity"]
            targets.append(f"{_fmt(_num(q.get('value')))} {q.get('unit') or ''}".strip())
        elif t.get("detailRange"):
            r = t["detailRange"]
            targets.append(f"{_fmt(_num((r.get('low') or {}).get('value')))}–{_fmt(_num((r.get('high') or {}).get('value')))}")
    rec = _base(res, "goals", title, status=res.get("lifecycleStatus"), effective_at=res.get("startDate"),
                value_text=", ".join(targets) or None)
    rec["details"] = {"targets": targets, "achievement": concept_text(res.get("achievementStatus")) or None}
    rec["dedup_key"] = f"goal|{_norm_name(title)}"
    return rec


def normalize_device(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    names = res.get("deviceName") or []
    title = (names[0].get("name") if names else None) or concept_text(res.get("type"), "Device")
    udi = ((res.get("udiCarrier") or [{}])[0]).get("deviceIdentifier")
    rec = _base(res, "devices", title, status=res.get("status"),
                effective_at=res.get("manufactureDate"), narrative=concept_text(res.get("type")) or None)
    rec["details"] = {"type": concept_text(res.get("type")) or None, "manufacturer": res.get("manufacturer"),
                      "model": res.get("modelNumber"), "udi": udi, "serial": res.get("serialNumber")}
    rec["dedup_key"] = f"dev|{udi or _norm_name(title)}"
    return rec


def normalize_coverage(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    payor = ", ".join(ref_display(p) for p in res.get("payor") or [] if ref_display(p)) or None
    plan = concept_text(res.get("type")) or None
    for cls in res.get("class") or []:
        if first_code(cls.get("type")) in ("plan", "group"):
            plan = cls.get("name") or cls.get("value") or plan
    title = plan or payor or "Health insurance"
    period = res.get("period") or {}
    rec = _base(res, "coverage", title, status=res.get("status"), effective_at=period.get("start"),
                effective_end=period.get("end"), narrative=payor)
    rec["details"] = {"payor": payor, "plan": plan, "subscriber_id": res.get("subscriberId"),
                      "relationship": concept_text(res.get("relationship")) or None}
    rec["dedup_key"] = f"cov|{res.get('subscriberId') or _norm_name(title)}"
    return rec


NORMALIZERS: dict[str, Callable[..., Optional[dict[str, Any]]]] = {
    "Observation": normalize_observation,
    "Condition": normalize_condition,
    "MedicationRequest": normalize_medication_request,
    "MedicationStatement": normalize_medication_statement,
    "AllergyIntolerance": normalize_allergy,
    "Immunization": normalize_immunization,
    "Encounter": normalize_encounter,
    "Procedure": normalize_procedure,
    "DiagnosticReport": normalize_diagnostic_report,
    "DocumentReference": normalize_document_reference,
    "CareTeam": normalize_care_team,
    "CarePlan": normalize_care_plan,
    "Goal": normalize_goal,
    "Device": normalize_device,
    "Coverage": normalize_coverage,
}


def normalize_resources(resources: list[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    """
    Normalizes a flat list of FHIR resources (search results + includes).
    Returns (patient, records). Medication references and lab panel membership are
    resolved within the set.
    """
    index = {f"{r.get('resourceType')}/{r.get('id')}": r for r in resources if r.get("id")}

    def resolve(ref: str) -> Optional[dict[str, Any]]:
        if ref.startswith("#"):
            return None
        return index.get("/".join(ref.split("/")[-2:]))

    patient = None
    records: list[dict[str, Any]] = []
    for res in resources:
        rtype = res.get("resourceType")
        if rtype == "Patient":
            patient = normalize_patient(res)
            continue
        fn = NORMALIZERS.get(rtype or "")
        if fn is None:
            continue
        try:
            rec = fn(res, resolve) if rtype in ("MedicationRequest", "MedicationStatement") else fn(res)
        except Exception:  # noqa: BLE001 - one malformed resource must never break a sync
            rec = None
        if rec and rec["resource_id"]:
            records.append(rec)

    panel_of: dict[str, str] = {}
    for rec in records:
        if rec["category"] == "reports" and rec["details"].get("kind") == "lab":
            for ref in rec["details"].get("result_refs") or []:
                panel_of[ref.split("/")[-1]] = rec["title"]
    if panel_of:
        for rec in records:
            if rec["resource_type"] == "Observation" and rec["resource_id"] in panel_of:
                rec["details"]["panel"] = panel_of[rec["resource_id"]]
    return patient, records
