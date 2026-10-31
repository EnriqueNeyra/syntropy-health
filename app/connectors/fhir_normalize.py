"""
FHIR R4 → Syntropy unified record normalization.

Each ``normalize_*`` function turns one FHIR resource into the flat record shape
stored in ``clinical_records`` (see ``app.store.records``), preserving the raw
resource for full fidelity. Codings are chosen by preferred terminology (LOINC for
observations, RxNorm for medications, SNOMED CT / ICD-10 for problems, CVX for
vaccines), a result keeps what the source said (value, unit, comparator, range) with a
normalized value beside it, and every record gets a ``dedup_key`` that is identical
across institutions for the same real-world fact.
"""

from __future__ import annotations

import base64
import codecs
import logging
import re
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from app.connectors import units

log = logging.getLogger("syntropy.normalize")

# A lab's own range text written with a hyphen ("15-150", "3.5-5.0 mmol/L"), shown with the dash used elsewhere.
_RANGE_DASH = re.compile(r"^(\d+(?:\.\d+)?) ?- ?(\d+(?:\.\d+)?)(?=\s|$)")

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
CVX = "http://hl7.org/fhir/sid/cvx"
ICD10 = ("http://hl7.org/fhir/sid/icd-10-cm", "http://hl7.org/fhir/sid/icd-10")
V3_INTERPRETATION = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"

# The same code systems written as OIDs (C-CDA derived data does this).
_SYSTEM_OIDS = {
    "urn:oid:2.16.840.1.113883.6.1": LOINC, "urn:oid:2.16.840.1.113883.6.96": SNOMED,
    "urn:oid:2.16.840.1.113883.6.88": RXNORM, "urn:oid:2.16.840.1.113883.12.292": CVX,
    "urn:oid:2.16.840.1.113883.6.90": ICD10[0], "urn:oid:2.16.840.1.113883.6.3": ICD10[1],
}

INTERPRETATION_MAP = {
    "H": "high", "HH": "critical_high", "HU": "critical_high", ">": "high", "HX": "high",
    "L": "low", "LL": "critical_low", "LU": "critical_low", "<": "low", "LX": "low",
    "A": "abnormal", "AA": "critical", "POS": "abnormal", "DET": "abnormal", "RR": "abnormal",
    "N": "normal", "NEG": "normal", "ND": "normal", "NR": "normal",
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
COMPONENT_NAMES = {"8480-6": "Systolic blood pressure", "8462-4": "Diastolic blood pressure"}

TEXT_CONTENT_TYPES = ("text/plain", "text/html", "text/rtf", "application/rtf", "application/xhtml+xml", "text/xml", "application/xml")
UNNAMED_MEDICATION = "Medication (name not sent by the source)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm_name(text: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _system(coding: dict[str, Any]) -> Optional[str]:
    system = coding.get("system")
    return _SYSTEM_OIDS.get(system, system)


def pick_coding(concept: Optional[dict[str, Any]], preferred: tuple[str, ...] = ()) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Returns (system, code, display) choosing the first coding from a preferred system."""
    if not isinstance(concept, dict):
        return None, None, None
    codings = [c for c in concept.get("coding", []) if isinstance(c, dict)]
    for system in preferred:
        for c in codings:
            if _system(c) == system and c.get("code"):
                return system, c.get("code"), c.get("display")
    for c in codings:
        if c.get("code"):
            return _system(c), c.get("code"), c.get("display")
    return None, None, None


def _code_in(concept: Optional[dict[str, Any]], systems: tuple[str, ...]) -> Optional[str]:
    return next((c.get("code") for c in (concept or {}).get("coding", []) if _system(c) in systems and c.get("code")), None)


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
        return concept.lower()
    code = first_code(concept)
    return code.lower() if isinstance(code, str) else None


def ref_display(ref: Any) -> Optional[str]:
    """A reference's human-readable name; None when it has only a raw reference (``Practitioner/3e17…``)."""
    if isinstance(ref, dict):
        return ref.get("display") or None
    return None


_FHIR_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}.*)?)?)?$")


def _date(*values: Any) -> Optional[str]:
    """The first value that is a date. Free text ("as a child", "Spring 2020") sorts above every real date, so it never is."""
    for v in values:
        if isinstance(v, str) and _FHIR_DATE.match(v.strip()):
            return v.strip()
    return None


def _minute(when: Optional[str]) -> str:
    """A result's moment for its identity: the UTC minute when the source gave an offset, so 08:30-05:00 and 13:30Z are one
    moment. A time without an offset or a bare date stays as written: neither is a known instant."""
    if not when:
        return ""
    if "T" in when:
        try:
            moment = datetime.fromisoformat(when)
        except ValueError:
            return when[:16]
        if moment.tzinfo:
            return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    return when[:16]


def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


class _TextOnly(HTMLParser):
    """What a person would read: no head, scripts or styles, a line per block, and table cells kept apart."""
    BLOCK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article", "ul", "ol", "blockquote", "pre"}
    SKIP = {"head", "script", "style", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skipping = 0
        self.cells = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self.skipping += 1
        elif tag == "br":
            self.out.append("\n")
        elif tag == "tr":
            self.cells = 0
        elif tag in ("td", "th"):
            if self.cells:
                self.out.append("\t")
            self.cells += 1

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        if tag == "br":
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.out.append(data)


def strip_html(text: str) -> str:
    parser = _TextOnly()
    parser.feed(text)
    parser.close()
    lines = [re.sub(r"[ \r\f\v]+", " ", line).strip(" ") for line in "".join(parser.out).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


_RTF_TOKEN = re.compile(r"\\(?:([a-zA-Z]+)(-?\d+)? ?|'([0-9a-fA-F]{2})|(.))|([{}])|([^\\{}\r\n]+)|[\r\n]+", re.S)
# Groups that hold settings rather than the text of the note.
_RTF_SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "header", "footer", "themedata", "datastore", "latentstyles"}
_RTF_BREAKS = {"par": "\n", "line": "\n", "sect": "\n", "page": "\n", "tab": "\t", "cell": "\t", "row": "\n", "emdash": "—", "endash": "–",
               "bullet": "•", "lquote": "‘", "rquote": "’", "ldblquote": "“", "rdblquote": "”"}


def rtf_to_text(rtf: str) -> str:
    """The text of an RTF note: enough of the format for what clinical systems write."""
    encoding = "cp1252"
    out: list[str] = []
    stack: list[tuple[bool, int]] = []
    skip, uc, drop = False, 1, 0
    for word, arg, hex_byte, symbol, brace, text in (m.groups() for m in _RTF_TOKEN.finditer(rtf)):
        if brace == "{":
            stack.append((skip, uc))
        elif brace == "}":
            if stack:
                skip, uc = stack.pop()
        elif word:
            if word in _RTF_SKIP:
                skip = True
            elif word == "ansicpg" and arg:
                encoding = f"cp{arg}"
            elif word == "uc" and arg:
                uc = int(arg)
            elif skip:
                continue
            elif word == "u" and arg:
                out.append(chr(int(arg) % 65536))
                drop = uc
            elif word in _RTF_BREAKS:
                out.append(_RTF_BREAKS[word])
        elif symbol:
            if symbol == "*":
                skip = True
            elif not skip and symbol in "\\{}":
                out.append(symbol)
            elif not skip and symbol == "~":
                out.append(" ")
        elif hex_byte:
            if drop:
                drop -= 1
            elif not skip:
                try:
                    out.append(bytes.fromhex(hex_byte).decode(encoding))
                except (LookupError, UnicodeDecodeError):
                    out.append("�")
        elif text and not skip:
            if drop:
                text, drop = text[drop:], max(0, drop - len(text))
            out.append(text)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.strip() for line in "".join(out).split("\n"))).strip()


def decode_body(raw: bytes, content_type: Optional[str]) -> str:
    """Readable text from the bytes of a note: in the charset the source named, else UTF-8, else the Windows encoding that
    the rest of what's out there uses."""
    ctype = (content_type or "text/plain").lower()
    charset = re.search(r"charset=[\"']?([\w.-]+)", ctype)
    text = None
    if charset:
        try:
            codecs.lookup(charset.group(1))
            text = raw.decode(charset.group(1), errors="replace")
        except LookupError:
            pass
    if text is None:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
    text = text.lstrip("﻿")
    if "rtf" in ctype or text.startswith("{\\rtf"):
        return rtf_to_text(text)
    if "html" in ctype or "xml" in ctype:
        return strip_html(text)
    return text


def decode_attachment(att: dict[str, Any]) -> Optional[str]:
    data = att.get("data")
    if not data:
        return None
    ctype = (att.get("contentType") or "text/plain").split(";")[0].strip().lower()
    if ctype not in TEXT_CONTENT_TYPES and not ctype.startswith("text/"):
        return None
    try:
        raw = base64.b64decode(data)
    except (ValueError, TypeError):
        return None
    return decode_body(raw, att.get("contentType"))


def text_preference(content_type: Optional[str]) -> int:
    """Which of a note's formats to read first, whichever order the source lists them in."""
    ctype = (content_type or "").lower()
    return 0 if "html" in ctype else 1 if "plain" in ctype else 3 if "rtf" in ctype else 2


def body_fingerprint(res: Optional[dict[str, Any]]) -> Optional[str]:
    """What says a note's or report's text may have changed: its version, date and status, and what its attachments are.
    Not the whole resource, or a field that changes on every read would fetch every body on every sync."""
    if not isinstance(res, dict):
        return None
    meta = res.get("meta") or {}
    forms = [c.get("attachment") or {} for c in res.get("content") or []] + list(res.get("presentedForm") or [])
    parts = [str(meta.get("versionId") or ""), str(meta.get("lastUpdated") or ""), str(res.get("docStatus") or ""), str(res.get("status") or "")]
    parts += sorted(f"{a.get('hash') or ''}:{a.get('size') or ''}:{urlsplit(a.get('url') or '').path}" for a in forms if isinstance(a, dict))
    return "|".join(parts)


def _name_key(prefix: str, code: Optional[str], title: Optional[str], fallback: str) -> Optional[str]:
    """Identity from a code or a real name. A record with only our fallback title ("Medication") has neither: two of those
    say nothing about being the same thing, so they stay separate rows."""
    if code:
        return f"{prefix}|{code}"
    name = _norm_name(title)
    return f"{prefix}|{name}" if name and name != _norm_name(fallback) else None


def _retracted(*values: Any) -> bool:
    return any(isinstance(v, str) and v.lower() == "entered-in-error" for v in values)


def retracted(res: dict[str, Any]) -> bool:
    """Whether the source itself says this resource was a mistake: the one reason a stored record is ever removed for its content."""
    verification = status_code(res.get("verificationStatus"))
    return _retracted(res.get("status"), res.get("docStatus"), res.get("lifecycleStatus"), verification) or verification == "refuted"


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

def _word_interpretation(text: str) -> Optional[str]:
    """A flag from words, in the order that keeps them apart: "Above normal" is high, "Not detected" is normal."""
    t = text.lower()
    if re.search(r"\bnot (detected|reactive)\b|\bnon-?reactive\b|\bnegative\b|\bwithin\b", t):
        return "normal"
    if re.search(r"\b(not|no)\b", t):
        return None
    up = re.search(r"\b(high|hi|above|elevated|increased)\b", t)
    down = re.search(r"\b(low|lo|below|decreased|reduced)\b", t)
    if re.search(r"\b(critical|critically|panic)\b", t):
        return "critical_high" if up else "critical_low" if down else "critical"
    if up:
        return "high"
    if down:
        return "low"
    if re.search(r"\b(abnormal|positive|detected|reactive)\b", t):
        return "abnormal"
    if re.search(r"\bnormal\b", t):
        return "normal"
    return None


def _stated_interpretation(res: dict[str, Any]) -> tuple[Optional[str], bool]:
    """The source's own flag, and whether it was only words. A server may put its proprietary code first, so every coding is read,
    the standard system before the rest."""
    concepts = [c for c in res.get("interpretation") or [] if isinstance(c, dict)]
    codings = [c for concept in concepts for c in concept.get("coding") or [] if isinstance(c, dict)]
    for standard in (True, False):
        for c in codings:
            if (c.get("system") == V3_INTERPRETATION) == standard:
                mapped = INTERPRETATION_MAP.get(str(c.get("code") or "").upper())
                if mapped:
                    return mapped, False
    words = [c.get("display") for c in codings] + [concept.get("text") for concept in concepts]
    for text in words:
        mapped = _word_interpretation(text) if isinstance(text, str) else None
        if mapped:
            return mapped, True
    return None, False


def _flag_from_range(value: Optional[float], low: Optional[float], high: Optional[float], comparator: Optional[str]) -> Optional[str]:
    """A comparator makes the value a bound: "<5" is somewhere below 5. A flag is only given when all of that interval says the same."""
    if value is None or (low is None and high is None) or comparator == "ad":
        return None
    if comparator in ("<", "<="):
        if low is not None and (value < low or (comparator == "<" and value == low)):
            return "low"
        return "normal" if (low is None or low <= 0) and high is not None and value <= high else None
    if comparator in (">", ">="):
        if high is not None and (value > high or (comparator == ">" and value == high)):
            return "high"
        return "normal" if high is None and low is not None and value >= low else None
    if high is not None and value > high:
        return "high"
    if low is not None and value < low:
        return "low"
    return "normal"


def _interpretation(res: dict[str, Any], value: Optional[float], low: Optional[float], high: Optional[float],
                    comparator: Optional[str] = None) -> Optional[str]:
    stated, words_only = _stated_interpretation(res)
    computed = _flag_from_range(value, low, high, comparator)
    if stated == "critical":     # says how serious, not which way: the range can tell
        return {"high": "critical_high", "low": "critical_low"}.get(computed or "", "abnormal")
    if stated == "normal" and words_only and computed in ("high", "low"):
        return computed
    return stated or computed


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


_COMPARATORS = ("<", "<=", ">", ">=", "ad")
_NUMERIC_TEXT = re.compile(r"^\s*(<=|>=|≤|≥|<|>)?\s*(\d+(?:\.\d+)?|\.\d+)\s*$")


def _with_unit(number: str, unit: Optional[str]) -> str:
    shown = units.pretty_unit(unit) or ""
    # "15/min", not "15 /min"; "98 %", "5.2 mmol/L" keep their space.
    return f"{number}{'' if shown.startswith('/') else ' '}{shown}".strip()


def _quantity_text(q: dict[str, Any]) -> str:
    value, unit = _quantity(q)
    return _with_unit(f"{q.get('comparator') or ''}{_fmt(value)}", unit) if value is not None else ""


def _range_text(r: dict[str, Any]) -> str:
    low, high = r.get("low") or {}, r.get("high") or {}
    lo, hi = _num(low.get("value")), _num(high.get("value"))
    unit = _quantity(high)[1] or _quantity(low)[1]
    if lo is not None and hi is not None:
        return _with_unit(f"{_fmt(lo)}–{_fmt(hi)}", unit)
    if lo is not None:
        return _with_unit(f"≥ {_fmt(lo)}", unit)
    return _with_unit(f"≤ {_fmt(hi)}", unit) if hi is not None else ""


def _ratio_text(r: dict[str, Any]) -> str:
    top, bottom = _quantity_text(r.get("numerator") or {}), _quantity_text(r.get("denominator") or {})
    return f"{top}:{bottom}" if top and bottom else top or bottom


def _read_value(src: dict[str, Any]) -> dict[str, Any]:
    """What an Observation or one of its components reports. ``num`` only where it is one measurement: a titer of 1:40 as 0.025
    would chart backwards, and a range has no single value. ``text`` is None when nothing was reported."""
    out: dict[str, Any] = {"text": None, "num": None, "unit": None, "comparator": None, "convert_unit": None, "present": True}
    if "valueQuantity" in src:
        q = src["valueQuantity"] if isinstance(src["valueQuantity"], dict) else {}
        out["num"], out["unit"] = _quantity(q)
        out["convert_unit"] = units.quantity_unit(q)
        out["comparator"] = q.get("comparator") if q.get("comparator") in _COMPARATORS else None
        out["text"] = _quantity_text(q) or None
        if out["num"] is None:
            out["unit"] = None
    elif "valueString" in src:
        text = src["valueString"]
        out["text"] = str(text) if text not in (None, "") else None
        found = _NUMERIC_TEXT.match(out["text"] or "")
        if found:
            out["num"] = float(found.group(2))
            out["comparator"] = {"≤": "<=", "≥": ">="}.get(found.group(1) or "", found.group(1))
    elif "valueCodeableConcept" in src:
        out["text"] = concept_text(src["valueCodeableConcept"]) or first_code(src["valueCodeableConcept"])
    elif "valueBoolean" in src:
        out["text"] = "Yes" if src["valueBoolean"] else "No"
    elif "valueInteger" in src:
        out["num"] = _num(src["valueInteger"])
        out["text"] = _fmt(out["num"]) or None
    elif "valueRatio" in src:
        out["text"] = _ratio_text(src["valueRatio"] or {}) or None
    elif "valueRange" in src:
        out["text"] = _range_text(src["valueRange"] or {}) or None
    elif "valueTime" in src or "valueDateTime" in src:
        out["text"] = src.get("valueTime") or src.get("valueDateTime")
    elif "valuePeriod" in src:
        period = src["valuePeriod"] or {}
        out["text"] = "–".join(p for p in (period.get("start"), period.get("end")) if p) or None
    elif "valueSampledData" in src:
        out["text"] = "Waveform data, see the original"
    elif any(k.startswith("value") and k[5:6].isupper() for k in src):
        out["text"] = "See the original"
    else:
        out["present"] = False
    return out


def _blood_pressure(comps: list[dict[str, Any]]) -> Optional[str]:
    """Systolic and diastolic by code, then by name, then by which is higher. Never by the order the source happened to list them in."""
    vals = [c for c in comps if c["value"] is not None]
    systolic = next((c for c in vals if c["code"] == "8480-6"), None) or next((c for c in vals if re.search(r"\bsys", c["name"], re.I)), None)
    diastolic = next((c for c in vals if c["code"] == "8462-4"), None) or next((c for c in vals if re.search(r"\bdia", c["name"], re.I)), None)
    if not systolic and not diastolic and len(vals) == 2:
        systolic, diastolic = sorted(vals, key=lambda c: -c["value"])
    if systolic and diastolic:
        return f"{_fmt(systolic['value'])}/{_fmt(diastolic['value'])} mmHg"
    if systolic:
        return f"Systolic {_fmt(systolic['value'])} mmHg"
    if diastolic:
        return f"Diastolic {_fmt(diastolic['value'])} mmHg"
    return None


def _age_at(person: Optional[dict[str, Any]], when: Optional[str]) -> Optional[float]:
    """Age in years when a result was taken, from whatever precision of date the source gave."""
    born = (person or {}).get("birth_date")
    if not born or not when or len(str(born)) < 4 or len(when) < 4:
        return None
    try:
        b = date.fromisoformat(str(born)[:10]) if len(str(born)) >= 10 else date(int(str(born)[:4]), 7, 1)
        w = date.fromisoformat(when[:10]) if len(when) >= 10 else date(int(when[:4]), 7, 1)
    except ValueError:
        return None
    return (w - b).days / 365.25


_AGE_UNIT_YEARS = {"a": 1.0, "yr": 1.0, "y": 1.0, "year": 1.0, "years": 1.0, "mo": 1 / 12, "month": 1 / 12, "months": 1 / 12,
                   "wk": 7 / 365.25, "week": 7 / 365.25, "weeks": 7 / 365.25, "d": 1 / 365.25, "day": 1 / 365.25, "days": 1 / 365.25}


def _in_years(q: Optional[dict[str, Any]]) -> Optional[float]:
    if not q or _num(q.get("value")) is None:
        return None
    factor = _AGE_UNIT_YEARS.get(str(q.get("code") or q.get("unit") or "a").lower())
    return None if factor is None else _num(q.get("value")) * factor


def _range_population(rr: dict[str, Any], person: Optional[dict[str, Any]], when: Optional[str]) -> Any:
    """"general" for a range with no stated population, else True (this person), False (someone else) or None (can't tell)."""
    if not rr.get("appliesTo") and not rr.get("age"):
        return "general"
    verdicts: list[Optional[bool]] = []
    gender = ((person or {}).get("gender") or "").lower()
    for concept in rr.get("appliesTo") or []:
        words = " ".join(str(w) for w in (concept_text(concept), first_code(concept)) if w).lower()
        sex = ("female" if re.search(r"\b(female|women|woman|248152002)\b", words)
               else "male" if re.search(r"\b(male|men|man|248153007)\b", words) else None)
        verdicts.append(sex == gender if sex and gender else None)
    if rr.get("age"):
        age, low, high = _age_at(person, when), _in_years(rr["age"].get("low")), _in_years(rr["age"].get("high"))
        verdicts.append(None if age is None else (low is None or age >= low) and (high is None or age <= high))
    if False in verdicts:
        return False
    return None if None in verdicts else True


def _bounds(rr: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    return _num((rr.get("low") or {}).get("value")), _num((rr.get("high") or {}).get("value"))


def _range_kind(rr: dict[str, Any]) -> str:
    return (status_code(rr.get("type")) or "").replace(" ", "-")


def _choose_range(ranges: list[dict[str, Any]], person: Optional[dict[str, Any]], when: Optional[str]) -> tuple[Optional[dict[str, Any]], bool]:
    """The range that applies to this result, and whether several could and nothing here can choose between them. The spec treats
    ranges as alternatives for different populations, so the first isn't simply the one. A critical range is never a normal range."""
    usable = [rr for rr in ranges if isinstance(rr, dict) and not re.search(r"critical|panic|alert", _range_kind(rr))]
    normal = [rr for rr in usable if _range_kind(rr) in ("", "normal")]
    pool = normal or usable
    if len(pool) == 1 and _range_population(pool[0], person, when) is not False:
        return pool[0], False

    def one(candidates: list[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], bool]:
        if len({_bounds(rr) + (rr.get("text"),) for rr in candidates}) == 1:
            return candidates[0], False
        return None, True

    verdicts = [(rr, _range_population(rr, person, when)) for rr in pool]
    for_this_person = [rr for rr, v in verdicts if v is True]
    if for_this_person:
        return one(for_this_person)
    general = [rr for rr, v in verdicts if v == "general"]
    if general:
        return one([rr for rr in general if _range_kind(rr) == "normal"] or general)
    return None, len(pool) > 1


def _range_label(rr: dict[str, Any]) -> Optional[str]:
    bits = [concept_text(c) or first_code(c) for c in rr.get("appliesTo") or []]
    age = rr.get("age") or {}
    lo, hi = _quantity_text(age.get("low") or {}), _quantity_text(age.get("high") or {})
    if lo or hi:
        bits.append(f"age {lo}–{hi}" if lo and hi else f"age from {lo}" if lo else f"age up to {hi}")
    kind = _range_kind(rr)
    if kind and kind != "normal":
        bits.append(concept_text(rr.get("type")) or kind)
    return ", ".join(b for b in bits if b) or None


def _range_shown(rr: dict[str, Any], unit: Optional[str] = None) -> Optional[str]:
    low, high = _bounds(rr)
    text = rr["text"].strip() if isinstance(rr.get("text"), str) else None
    if text:
        text = _RANGE_DASH.sub("\\1–\\2", text)
    elif low is not None and high is not None:
        text = f"{_fmt(low)}–{_fmt(high)}"
    elif low is not None:
        text = f"≥ {_fmt(low)}"
    elif high is not None:
        text = f"≤ {_fmt(high)}"
    return _with_unit(text, unit) if text and unit else text


def _range_unit(rr: dict[str, Any]) -> Optional[str]:
    for side in ("low", "high"):
        q = rr.get(side) or {}
        if q.get("value") is not None and (q.get("unit") or q.get("code")):
            return units.quantity_unit(q) or q.get("unit")
    return None


def normalize_observation(res: dict[str, Any], person: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """``person``: the patient's gender and birth_date when known, for choosing between ranges for different populations."""
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
    comps = []
    for comp in res.get("component") or []:
        c_sys, c_code, c_disp = pick_coding(comp.get("code"), (LOINC,))
        c_value = _read_value(comp)
        entry = {"code": c_code, "name": concept_text(comp.get("code"), c_disp or COMPONENT_NAMES.get(c_code or "") or "Component"),
                 "value": c_value["num"], "unit": units.pretty_unit(c_value["unit"]),
                 "text": (c_value["text"] or concept_text(comp.get("dataAbsentReason")) or None) if c_value["num"] is None or c_value["comparator"] else None}
        if c_value["comparator"]:
            entry["comparator"] = c_value["comparator"]
        comps.append(entry)

    reported = _read_value(res)
    value: Optional[float] = reported["num"]
    unit: Optional[str] = reported["unit"]
    comparator: Optional[str] = reported["comparator"]
    rec["value_text"] = reported["text"]
    if not reported["present"] and comps:
        vals = [c for c in comps if c["value"] is not None]
        is_pressure = code in ("85354-6", "55284-4") or len(vals) == 2 and all(c["unit"] in ("mmHg", "mm[Hg]") for c in vals)
        pressure = _blood_pressure(comps) if is_pressure else None
        if pressure:
            unit = "mmHg"
        rec["value_text"] = pressure or " · ".join(f"{c['name']}: {c['text'] or _with_unit(_fmt(c['value']), c['unit']) or 'No value'}" for c in comps)
    if rec["value_text"] is None:
        if res.get("dataAbsentReason"):
            rec["value_text"] = concept_text(res["dataAbsentReason"], "Not available")
        elif reported["present"]:
            rec["value_text"] = "No value"
    if rec["value_text"] is None and value is None and not comps:
        return None        # a panel's header (hasMember only): its members are the results

    all_ranges = [rr for rr in res.get("referenceRange") or [] if isinstance(rr, dict)]
    chosen, several = _choose_range(all_ranges, person, rec["effective_at"])
    value_norm, unit_norm = units.normalize(code, value, unit, reported["convert_unit"])
    is_converted = value_norm is not None and units.converted(unit, unit_norm)
    low = high = None
    ref_low = ref_high = None
    ref_text = None
    compare = (value, None, None)
    source_range = None
    if chosen:
        low, high = _bounds(chosen)
        range_unit = _range_unit(chosen)
        same_unit = not range_unit or units.unit_key(range_unit) in (units.unit_key(unit), units.unit_key(reported["convert_unit"])) or value is None
        if same_unit:
            # Converted with the value; compared as reported, before rounding can move a result across a limit.
            ref_low = units.normalize(code, low, unit, reported["convert_unit"])[0] if low is not None else None
            ref_high = units.normalize(code, high, unit, reported["convert_unit"])[0] if high is not None else None
            compare = (value, low, high)
            ref_text = _range_shown(chosen)
            if is_converted and (low is not None or high is not None):
                source_range = {"low": low, "high": high}
        else:
            low_n, low_u = units.normalize(code, low, range_unit)
            high_n, high_u = units.normalize(code, high, range_unit)
            comparable = value_norm is not None and all(
                not units.converted(u, unit_norm) for n, u in ((low_n, low_u), (high_n, high_u)) if n is not None)
            ref_text = _range_shown(chosen, None if chosen.get("text") else range_unit)
            if comparable:
                ref_low, ref_high = low_n, high_n
                compare = (value_norm, low_n, high_n)
    elif several:
        ref_text = "Several ranges, see the record"
    rec.update({
        "value_num": value, "unit": units.pretty_unit(unit), "value_norm": value_norm,
        "unit_norm": units.pretty_unit(unit_norm), "ref_low": ref_low, "ref_high": ref_high, "ref_text": ref_text,
        "interpretation": _interpretation(res, compare[0], compare[1], compare[2], comparator),
    })
    details: dict[str, Any] = {"components": comps} if comps else {}
    if comparator:
        details["comparator"] = comparator
    if is_converted:
        details["converted_value"] = _with_unit(f"{comparator or ''}{_fmt(value_norm)}", rec["unit_norm"])
    if source_range:
        details["source_range"] = source_range
    if len(all_ranges) > 1:
        details["ranges"] = [{"text": _range_shown(rr), "for": _range_label(rr)} for rr in all_ranges]
    rec["details"] = details
    if res.get("note"):
        rec["narrative"] = " ".join(n.get("text", "") for n in res["note"] if n.get("text")) or None
    # By the normalized number, so spellings of a unit (and a lab's rounding of a conversion, where it agrees) don't split a result.
    key_value = (f"{comparator or ''}{_fmt(value_norm)} {units.unit_key(rec['unit_norm'])}".strip()
                 if value_norm is not None else rec["value_text"] or "")
    key_name = code or _norm_name(title)
    rec["dedup_key"] = f"obs|{key_name}|{_minute(rec['effective_at'])}|{key_value}" if code or key_name != "observation" else None
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
    text = f"{v:.{max(0, 4 - len(str(int(abs(v)))))}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


# ---------------------------------------------------------------------------
# Problem list, medications, allergies, immunizations
# ---------------------------------------------------------------------------

def _meta_updated(res: dict[str, Any]) -> Optional[str]:
    return (res.get("meta") or {}).get("lastUpdated")


def _notes(res: dict[str, Any]) -> Optional[str]:
    return " ".join(n.get("text", "") for n in res.get("note") or [] if n.get("text")) or None


def normalize_condition(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    verification = status_code(res.get("verificationStatus"))
    if verification in ("entered-in-error", "refuted"):
        return None
    concept = res.get("code")
    system, code, display = pick_coding(concept, (SNOMED, *ICD10))
    title = concept_text(concept, display or "Condition")
    icd = _code_in(concept, ICD10)
    snomed = _code_in(concept, (SNOMED,))
    categories = [concept_text(c) or first_code(c) for c in res.get("category") or []]
    category_codes = [(first_code(c) or "").lower() for c in res.get("category") or [] if first_code(c)]
    abatement_end = _date(res.get("abatementDateTime"), (res.get("abatementPeriod") or {}).get("end"), (res.get("abatementPeriod") or {}).get("start"))
    abatement_text = res.get("abatementString") or _quantity_text(res.get("abatementAge") or {}) or None
    clinical_status = status_code(res.get("clinicalStatus"))
    abated = bool(abatement_end or abatement_text or res.get("abatementBoolean"))
    onset_text = res.get("onsetString") or _quantity_text(res.get("onsetAge") or {}) or None
    rec = _base(res, "conditions", title, code_system=system, code=code, code_display=display,
                # No stated status is not "active": it ended if the source gave an end, and otherwise nobody said.
                status=clinical_status or ("resolved" if abated else None),
                effective_at=_date(res.get("onsetDateTime"), (res.get("onsetPeriod") or {}).get("start"), res.get("recordedDate")),
                effective_end=abatement_end)
    rec["details"] = {"icd10": icd, "snomed": snomed, "verification": verification, "categories": categories,
                      "category_codes": category_codes, "clinical_status": clinical_status,
                      "recorded": res.get("recordedDate"), "last_updated": _meta_updated(res), "abated": abated or None,
                      "onset_text": onset_text, "abatement_text": abatement_text,
                      "severity": concept_text(res.get("severity")) or None}
    rec["narrative"] = _notes(res)
    rec["dedup_key"] = _name_key("cond", snomed or icd, title, "Condition")
    return rec


_PERIOD_WORDS = {"h": "hour", "d": "day", "wk": "week", "mo": "month", "a": "year", "min": "minute"}


def _timing_text(timing: Optional[dict[str, Any]]) -> Optional[str]:
    if not isinstance(timing, dict):
        return None
    named = concept_text(timing.get("code"))
    if named:
        return named
    repeat = timing.get("repeat") or {}
    freq, period, word = _num(repeat.get("frequency")), _num(repeat.get("period")), _PERIOD_WORDS.get(repeat.get("periodUnit") or "")
    if not word or period is None:
        return None
    every = f"a {word}" if period == 1 else f"every {_fmt(period)} {word}s"
    times = "once" if freq in (None, 1) else "twice" if freq == 2 else f"{_fmt(freq)} times"
    return f"{times} {every}"


def _dose_text(d: dict[str, Any]) -> Optional[str]:
    """Instructions as written when the source wrote them, otherwise from the structured dose, timing and route."""
    if d.get("text"):
        return d["text"]
    if d.get("patientInstruction"):
        return d["patientInstruction"]
    bits = []
    for rate in d.get("doseAndRate") or []:
        shown = _quantity_text(rate.get("doseQuantity") or {}) or _range_text(rate.get("doseRange") or {})
        if shown:
            bits.append(shown)
            break
    bits.append(_timing_text(d.get("timing")))
    bits.append(concept_text(d.get("route")) or None)
    if d.get("asNeededBoolean") or d.get("asNeededCodeableConcept"):
        reason = concept_text(d.get("asNeededCodeableConcept"))
        bits.append(f"as needed for {reason}" if reason else "as needed")
    return " ".join(b for b in bits if b) or None


def _medication_concept(res: dict[str, Any], resolve: Callable[[str], Optional[dict[str, Any]]]) -> Optional[dict[str, Any]]:
    if res.get("medicationCodeableConcept"):
        return res["medicationCodeableConcept"]
    reference = res.get("medicationReference") or {}
    ref = reference.get("reference") or ""
    if ref.startswith("#"):
        med = next((c for c in res.get("contained") or [] if isinstance(c, dict) and c.get("resourceType") == "Medication"
                    and str(c.get("id")) == ref[1:]), None)
    else:
        med = resolve(ref)
    concept = (med or {}).get("code")
    if isinstance(concept, dict) and (concept.get("text") or concept.get("coding")):
        return concept
    return {"text": reference["display"]} if reference.get("display") else None


def normalize_medication_request(res: dict[str, Any], resolve: Callable[[str], Optional[dict[str, Any]]] = lambda r: None) -> Optional[dict[str, Any]]:
    if (res.get("status") or "").lower() == "entered-in-error":
        return None
    concept = _medication_concept(res, resolve)
    system, code, display = pick_coding(concept, (RXNORM,))
    fallback = ref_display(res.get("medicationReference")) or display or UNNAMED_MEDICATION
    title = concept_text(concept, fallback)
    dosage = [d for d in res.get("dosageInstruction") or [] if isinstance(d, dict)]
    dose_text = "; ".join(t for t in (_dose_text(d) for d in dosage) if t) or None
    as_needed = any(d.get("asNeededBoolean") or d.get("asNeededCodeableConcept") for d in dosage)
    dispense = res.get("dispenseRequest") or {}
    validity = dispense.get("validityPeriod") or {}
    categories = res.get("category") or []
    category_codes = [(first_code(c) or "").lower() for c in (categories if isinstance(categories, list) else [categories])]
    requester = ((res.get("requester") or {}).get("reference") or "") if isinstance(res.get("requester"), dict) else ""
    reported = bool(res.get("reportedBoolean") or res.get("reportedReference") or "patientspecified" in category_codes
                    or requester.startswith(("Patient/", "RelatedPerson/")))
    rec = _base(res, "medications", title, code_system=system, code=code, code_display=display,
                status=(res.get("status") or "").lower() or None,
                effective_at=_date(res.get("authoredOn"), validity.get("start")),
                value_text=dose_text)
    rec["details"] = {"dosage": dose_text, "as_needed": as_needed, "prescriber": ref_display(res.get("requester")),
                      "intent": res.get("intent"), "rxnorm": code if system == RXNORM else None,
                      "reason": [concept_text(r) for r in res.get("reasonCode") or []],
                      "refills": dispense.get("numberOfRepeatsAllowed"),
                      "valid_until": _date(validity.get("end")), "reported": reported or None,
                      "category_codes": [c for c in category_codes if c]}
    rec["narrative"] = dose_text
    rec["dedup_key"] = _name_key("med", code if system == RXNORM else None, title, UNNAMED_MEDICATION)
    return rec


def normalize_medication_statement(res: dict[str, Any], resolve: Callable[[str], Optional[dict[str, Any]]] = lambda r: None) -> Optional[dict[str, Any]]:
    shim = dict(res)
    shim["authoredOn"] = res.get("dateAsserted") or _date((res.get("effectivePeriod") or {}).get("start"), res.get("effectiveDateTime"))
    shim["dosageInstruction"] = res.get("dosage")
    shim["resourceType"] = "MedicationStatement"
    rec = normalize_medication_request(shim, resolve)
    if rec:
        rec["raw"] = res
    return rec


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
        reactions.append({"manifestation": manifest or r.get("description") or None, "severity": r.get("severity")})
    narrative = "; ".join(f"{r['manifestation'] or 'Reaction'}{' (' + r['severity'] + ')' if r['severity'] else ''}" for r in reactions) or None
    clinical_status = status_code(res.get("clinicalStatus"))
    status = clinical_status or "active"
    codes = {c.get("code") for c in (res.get("code") or {}).get("coding") or []}
    if codes & NO_KNOWN_ALLERGY_CODES:
        status = "no-known-allergies"
    elif codes & ALLERGIES_NOT_ASKED_CODES or title.strip().lower() == "not on file":
        status = "not-asked"
    rec = _base(res, "allergies", title, code_system=system, code=code, code_display=display, status=status,
                effective_at=_date(res.get("onsetDateTime"), res.get("recordedDate")), narrative=narrative)
    cats = res.get("category") or []
    rec["details"] = {"category": cats[0] if cats else None, "criticality": res.get("criticality"),
                      "type": res.get("type"), "verification": verification, "reactions": reactions,
                      "recorded": res.get("recordedDate"), "last_updated": _meta_updated(res), "note": _notes(res),
                      "onset_text": res.get("onsetString")}
    rec["dedup_key"] = _name_key("alg", code, title, "Allergy")
    return rec


def normalize_immunization(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    if (res.get("status") or "").lower() == "entered-in-error":
        return None
    system, code, display = pick_coding(res.get("vaccineCode"), (CVX,))
    title = concept_text(res.get("vaccineCode"), display or "Vaccine")
    date_text = res.get("occurrenceString") if not _date(res.get("occurrenceString")) else None
    when = _date(res.get("occurrenceDateTime"), res.get("occurrenceString"), None if date_text else res.get("recorded"))
    status = (res.get("status") or "completed").lower()
    rec = _base(res, "immunizations", title, code_system=system, code=code, code_display=display,
                status=status, effective_at=when)
    dose = res.get("doseQuantity") or {}
    rec["details"] = {"lot": res.get("lotNumber"), "manufacturer": ref_display(res.get("manufacturer")),
                      "site": concept_text(res.get("site")) or None, "route": concept_text(res.get("route")) or None,
                      "dose": f"{_fmt(_num(dose.get('value')))} {dose.get('unit') or ''}".strip() or None,
                      "performer": ref_display(((res.get("performer") or [{}])[0]).get("actor")),
                      "status_reason": concept_text(res.get("statusReason")) or None, "date_text": date_text}
    # A refusal never merges with, or hides, a dose given the same day.
    base = _name_key("imm", code, title, "Vaccine")
    rec["dedup_key"] = f"{base}|{(when or '')[:10]}{'|not-done' if status == 'not-done' else ''}" if base else None
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
    reasons += [ref_display(r) for r in res.get("reasonReference") or [] if ref_display(r) and ref_display(r) not in reasons]
    diagnoses = [ref_display(d.get("condition")) for d in res.get("diagnosis") or [] if isinstance(d, dict) and ref_display(d.get("condition"))]
    clinicians = [ref_display(p.get("individual")) for p in res.get("participant") or [] if ref_display(p.get("individual"))]
    locations = [ref_display(l.get("location")) for l in res.get("location") or [] if ref_display(l.get("location"))]
    rec = _base(res, "encounters", title, status=res.get("status"),
                effective_at=_date(period.get("start")), effective_end=_date(period.get("end")),
                narrative=", ".join(reasons) or None)
    rec["details"] = {"class": class_label, "class_code": klass.get("code"), "reasons": reasons, "diagnoses": diagnoses,
                      "clinicians": clinicians, "locations": locations,
                      "provider": ref_display(res.get("serviceProvider"))}
    return rec


def normalize_procedure(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    if (res.get("status") or "").lower() in ("entered-in-error", "not-done"):
        return None
    system, code, display = pick_coding(res.get("code"), (SNOMED,))
    title = concept_text(res.get("code"), display or "Procedure")
    performed = res.get("performedString")
    date_text = performed if performed and not _date(performed) else None
    when = _date(res.get("performedDateTime"), (res.get("performedPeriod") or {}).get("start"), performed)
    performers = [ref_display(p.get("actor")) for p in res.get("performer") or [] if ref_display(p.get("actor"))]
    outcome = concept_text(res.get("outcome")) or None
    reasons = [concept_text(r) for r in res.get("reasonCode") or [] if concept_text(r)]
    rec = _base(res, "procedures", title, code_system=system, code=code, code_display=display,
                status=res.get("status"), effective_at=when,
                effective_end=_date((res.get("performedPeriod") or {}).get("end")), narrative=outcome)
    rec["details"] = {"performers": performers, "outcome": outcome, "reasons": reasons, "date_text": date_text,
                      "body_site": ", ".join(concept_text(b) for b in res.get("bodySite") or []) or None}
    base = _name_key("proc", code, title, "Procedure")
    rec["dedup_key"] = f"{base}|{(when or '')[:10]}" if base else None
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
    forms = sorted(res.get("presentedForm") or [], key=lambda f: text_preference(f.get("contentType")))
    text_parts = [t for t in (decode_attachment(f) for f in forms) if t]
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
                        for f in forms if f.get("url") and not f.get("data")],
    }
    base = _name_key("rep", code, title, "Report")
    rec["dedup_key"] = f"{base}|{(when or '')[:10]}" if base else None
    return rec


def normalize_document_reference(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    if _retracted(res.get("status"), res.get("docStatus")):
        return None
    doc_type = concept_text(res.get("type"), "Clinical note")
    title = res.get("description") or doc_type
    attachments = [c.get("attachment") or {} for c in res.get("content") or []]
    text = None
    for att in sorted(attachments, key=lambda a: text_preference(a.get("contentType"))):
        text = decode_attachment(att)
        if text:
            break
    first = attachments[0] if attachments else {}
    context = res.get("context") or {}
    when = _date(res.get("date"), (context.get("period") or {}).get("start"), first.get("creation"))
    # A replaced note can still say docStatus final: the badge says what became of it.
    shown_status = "superseded" if (res.get("status") or "").lower() == "superseded" else res.get("docStatus") or res.get("status")
    rec = _base(res, "notes", title, status=shown_status, effective_at=when,
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
    if _retracted(res.get("status")):
        return None
    members = []
    unnamed = 0
    for p in res.get("participant") or []:
        name = ref_display(p.get("member"))
        if not name:
            unnamed += 1 if (p.get("member") or {}).get("reference") else 0
            continue
        role = concept_text((p.get("role") or [{}])[0]) or "Care team member"
        members.append({"name": name, "role": role})
    if not members and not unnamed:
        return None
    named = ", ".join(f"{m['name']} ({m['role']})" for m in members)
    extra = f"{unnamed} {'member' if unnamed == 1 else 'members'} whose names this source didn't send" if unnamed else ""
    rec = _base(res, "care_team", res.get("name") or "Care team", status=res.get("status"),
                effective_at=_date((res.get("period") or {}).get("start")),
                narrative=", ".join(p for p in (named, extra) if p))
    rec["details"] = {"members": members, "unnamed_members": unnamed or None}
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
    summary = (res.get("text") or {}).get("div")
    rec = _base(res, "care_plans", title, status=res.get("status"),
                effective_at=_date((res.get("period") or {}).get("start")), effective_end=_date((res.get("period") or {}).get("end")),
                narrative=res.get("description") or "; ".join(a["description"] for a in activities)
                or (strip_html(summary) if isinstance(summary, str) else None) or None)
    rec["details"] = {"activities": activities, "addresses": [ref_display(a) for a in res.get("addresses") or [] if ref_display(a)]}
    return rec


def normalize_goal(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    if _retracted(res.get("lifecycleStatus")):
        return None
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
    rec = _base(res, "goals", title, status=res.get("lifecycleStatus"), effective_at=_date(res.get("startDate")),
                value_text=", ".join(targets) or None)
    rec["details"] = {"targets": targets, "achievement": concept_text(res.get("achievementStatus")) or None}
    rec["dedup_key"] = _name_key("goal", None, title, "Goal")
    return rec


def normalize_device(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    if _retracted(res.get("status")):
        return None
    names = res.get("deviceName") or []
    title = (names[0].get("name") if names else None) or concept_text(res.get("type"), "Device")
    udi = ((res.get("udiCarrier") or [{}])[0]).get("deviceIdentifier")
    rec = _base(res, "devices", title, status=res.get("status"),
                effective_at=_date(res.get("manufactureDate")), narrative=concept_text(res.get("type")) or None)
    rec["details"] = {"type": concept_text(res.get("type")) or None, "manufacturer": res.get("manufacturer"),
                      "model": res.get("modelNumber"), "udi": udi, "serial": res.get("serialNumber")}
    rec["dedup_key"] = _name_key("dev", udi, title, "Device")
    return rec


def normalize_coverage(res: dict[str, Any]) -> Optional[dict[str, Any]]:
    if _retracted(res.get("status")):
        return None
    payor = ", ".join(ref_display(p) for p in res.get("payor") or [] if ref_display(p)) or None
    plan = concept_text(res.get("type")) or None
    for cls in res.get("class") or []:
        if first_code(cls.get("type")) in ("plan", "group"):
            plan = cls.get("name") or cls.get("value") or plan
    title = plan or payor or "Health insurance"
    period = res.get("period") or {}
    rec = _base(res, "coverage", title, status=res.get("status"), effective_at=_date(period.get("start")),
                effective_end=_date(period.get("end")), narrative=payor)
    rec["details"] = {"payor": payor, "plan": plan, "subscriber_id": res.get("subscriberId"),
                      "relationship": concept_text(res.get("relationship")) or None}
    rec["dedup_key"] = _name_key("cov", res.get("subscriberId"), title, "Health insurance")
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
_OLD_NAMES = {"MedicationOrder": "MedicationRequest"}


def _from_dstu2(res: dict[str, Any]) -> dict[str, Any]:
    """Apple Health Records and older portals send FHIR DSTU2, where a few fields have other names or shapes. A copy with the R4
    names: anything already R4 has none of these and comes through unchanged. The stored raw resource is always the original."""
    rtype = res.get("resourceType")
    out = dict(res)
    if isinstance(res.get("category"), dict):
        out["category"] = [res["category"]]
    if rtype == "MedicationOrder":
        out["resourceType"] = "MedicationRequest"
        out.setdefault("intent", "order")
        out.setdefault("authoredOn", res.get("dateWritten"))
        if res.get("prescriber"):
            out.setdefault("requester", res["prescriber"])
    elif rtype == "AllergyIntolerance":
        if res.get("substance"):
            out.setdefault("code", res["substance"])
        out.setdefault("recordedDate", res.get("recordedDate") or res.get("onset"))
        if isinstance(res.get("category"), str):
            out["category"] = [res["category"]]
        old =(res.get("status") or "").lower() if isinstance(res.get("status"), str) else ""
        if old and "clinicalStatus" not in res:
            out["clinicalStatus"] = old if old in ("active", "inactive", "resolved") else "active"
            if old in ("unconfirmed", "confirmed", "refuted", "entered-in-error"):
                out.setdefault("verificationStatus", old)
    elif rtype == "Condition":
        out.setdefault("recordedDate", res.get("dateRecorded"))
    elif rtype == "Immunization":
        out.setdefault("occurrenceDateTime", res.get("date"))
        if res.get("wasNotGiven") is True:
            out["status"] = "not-done"
    elif rtype == "Procedure":
        if res.get("notPerformed") is True:
            out["status"] = "not-done"
    elif rtype == "Encounter":
        if isinstance(res.get("class"), str):
            out["class"] = {"code": res["class"]}
    elif rtype == "DocumentReference":
        out.setdefault("date", res.get("indexed"))
    return out


def normalize_one(res: dict[str, Any], resolve: Callable[[str], Optional[dict[str, Any]]] = lambda r: None,
                  person: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """One resource to its record, or None when it isn't one. Raises on a resource too malformed to read."""
    shaped = _from_dstu2(res)
    rtype = shaped.get("resourceType")
    fn = NORMALIZERS.get(rtype or "")
    if fn is None:
        return None
    if rtype in ("MedicationRequest", "MedicationStatement"):
        rec = fn(shaped, resolve)
    elif rtype == "Observation":
        rec = fn(shaped, person)
    else:
        rec = fn(shaped)
    if rec:
        rec["raw"] = res
    return rec


def normalize_report(resources: list[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, int]]]:
    """Like ``normalize_resources``, and also what couldn't be kept, by type: {"failed": ..., "skipped": ...}. A resource the
    source retracted or a panel's empty header isn't counted: dropping those is right."""
    index: dict[str, dict[str, Any]] = {}
    aliases: dict[str, str] = {}       # a bundle's fullUrl or urn:uuid -> Type/id
    for r in resources:
        if not isinstance(r, dict):
            continue
        full_url = r.pop("_full_url", None)
        if r.get("id"):
            key = f"{r.get('resourceType')}/{r.get('id')}"
            index[key] = r
            aliases[f"urn:uuid:{r['id']}"] = key
            if full_url:
                aliases[full_url] = key

    def canonical_ref(ref: str) -> str:
        return aliases.get(ref) or "/".join(ref.rstrip("/").split("/")[-2:])

    def resolve(ref: str) -> Optional[dict[str, Any]]:
        if not ref or ref.startswith("#"):
            return None
        return index.get(canonical_ref(ref))

    person = None
    for res in resources:
        if isinstance(res, dict) and res.get("resourceType") == "Patient":
            person = {"gender": res.get("gender"), "birth_date": res.get("birthDate")}

    patient = None
    records: list[dict[str, Any]] = []
    problems: dict[str, dict[str, int]] = {"failed": {}, "skipped": {}}
    for res in resources:
        if not isinstance(res, dict):
            continue
        rtype = res.get("resourceType")
        if rtype == "Patient":
            patient = normalize_patient(res)
            continue
        if rtype not in NORMALIZERS and rtype not in _OLD_NAMES:
            continue
        try:
            rec = normalize_one(res, resolve, person)
        except Exception as exc:  # noqa: BLE001 - one malformed resource must never break a sync
            log.warning("Couldn't read a %s: %s", rtype, type(exc).__name__)
            problems["failed"][rtype] = problems["failed"].get(rtype, 0) + 1
            continue
        if rec and rec["resource_id"]:
            records.append(rec)
        elif rec or not (retracted(res) or res.get("hasMember") or (res.get("status") or "").lower() in ("cancelled", "not-done", "revoked")):
            problems["skipped"][rtype] = problems["skipped"].get(rtype, 0) + 1

    panel_of: dict[str, str] = {}
    for rec in records:
        if rec["category"] == "reports":
            refs = [canonical_ref(r) for r in rec["details"].get("result_refs") or [] if isinstance(r, str)]
            if refs:
                rec["details"]["result_refs"] = refs
            if rec["details"].get("kind") == "lab":
                for ref in refs:
                    panel_of[ref.split("/")[-1]] = rec["title"]
    if panel_of:
        for rec in records:
            if rec["resource_type"] == "Observation" and rec["resource_id"] in panel_of:
                rec["details"]["panel"] = panel_of[rec["resource_id"]]
    return patient, records, problems


def normalize_resources(resources: list[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    """
    Normalizes a flat list of FHIR resources (search results + includes).
    Returns (patient, records). Medication references and lab panel membership are
    resolved within the set.
    """
    patient, records, _ = normalize_report(resources)
    return patient, records
