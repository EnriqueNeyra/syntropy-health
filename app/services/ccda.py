"""Health summaries from patient portals: C-CDA and CCD documents, alone or in an XDM zip, read into FHIR-shaped resources.

ONC's view, download and transmit criterion makes these the file a certified portal gives people, so this is what "download my
record" produces. Each entry becomes the resource the HL7 C-CDA on FHIR mapping makes of it and goes through the same normalizers,
raw storage and de-duplication as a live connection. What can't be mapped with certainty isn't guessed at: the section's own text
is kept as a note, so it stays readable and searchable.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional
from xml.parsers import expat

from app.connectors.apple_health import _implausible
from app.core.uploads import MAX_INFLATED

MAX_DOCUMENT = MAX_INFLATED
MAX_DEPTH = 100
MAX_ELEMENTS = 2_000_000
MAX_ZIP_MEMBERS = 2000
MAX_ZIP_DOCUMENTS = 50
MAX_ZIP_TOTAL = 512 * 1024 * 1024
MAX_NOTE_TEXT = 200_000

NOT_READABLE = "This file couldn't be read as a health summary. Download it from the portal again, or copy the file again."
TOO_BIG = "This health summary is too large for Syntropy Health to read."
NOT_ALLOWED_XML = "This file uses XML features that aren't allowed in a health summary (a document type or entities), so it wasn't read."
NOT_SUMMARY_XML = ("This XML file isn't a health summary. Syntropy Health takes a health summary (C-CDA, CCD or XDM) or a FHIR file. "
                   "An Apple Health export goes in the Apple Health box.")
NOT_SUMMARY_ZIP = ("No health summary was found in this zip. Syntropy Health takes a health summary (C-CDA, CCD or XDM) or a FHIR file. "
                   "An Apple Health export goes in the Apple Health box.")

HL7 = "urn:hl7-org:v3"
SSN_OID = "2.16.840.1.113883.4.1"
ACT_CODE_OID = "2.16.840.1.113883.5.4"
_T = "2.16.840.1.113883.10.20.22."
NOTE_ACTIVITY = f"{_T}4.202"
SECTIONS = {
    "48765-2": "allergies", "10160-0": "medications", "11450-4": "problems", "30954-2": "results", "8716-3": "vitals",
    "11369-6": "immunizations", "47519-4": "procedures", "46240-8": "encounters",
}
SECTION_TEMPLATES = {
    f"{_T}2.6": "allergies", f"{_T}2.6.1": "allergies", f"{_T}2.1": "medications", f"{_T}2.1.1": "medications",
    f"{_T}2.5": "problems", f"{_T}2.5.1": "problems", f"{_T}2.3": "results", f"{_T}2.3.1": "results",
    f"{_T}2.4": "vitals", f"{_T}2.4.1": "vitals", f"{_T}2.2": "immunizations", f"{_T}2.2.1": "immunizations",
    f"{_T}2.7": "procedures", f"{_T}2.7.1": "procedures", f"{_T}2.22": "encounters", f"{_T}2.22.1": "encounters",
}
CONCERN_STATUS = {"active": "active", "completed": "resolved", "aborted": "inactive", "suspended": "inactive"}
STATUS_VALUE = {"55561003": "active", "73425007": "inactive", "413322009": "resolved"}
MED_STATUS = {"active": "active", "completed": "completed", "aborted": "stopped", "suspended": "on-hold",
              "nullified": "entered-in-error", "obsolete": "stopped"}
PROCEDURE_STATUS = {"completed": "completed", "active": "in-progress", "aborted": "stopped", "cancelled": "not-done",
                    "nullified": "entered-in-error"}
RESULT_STATUS = {"completed": "final", "active": "preliminary", "aborted": "cancelled", "nullified": "entered-in-error"}
ALLERGY_CATEGORY = {"416098002": "medication", "419511003": "medication", "59037007": "medication", "414285001": "food",
                    "235719002": "food", "426232007": "environment", "232347008": "environment"}
SEVERITY = {"255604002": "mild", "6736007": "moderate", "24484000": "severe"}
CRITICALITY = {"CRITL": "low", "CRITH": "high", "CRITU": "unable-to-assess"}
PERIOD_UNITS = ("h", "d", "wk", "mo", "min", "a", "s")
NO_KNOWN_ALLERGY = "716186003"
# A statement can rule out one kind of allergy without ruling out the others, so each keeps what it covers.
NO_KNOWN_BY_CATEGORY = {"medication": ("409137002", "No known drug allergies"), "food": ("429625007", "No known food allergies"),
                        "environment": ("428607008", "No known environmental allergies")}
NO_KNOWN_WORDS = {"no known allergies": None, "no known allergy": None, "nka": None, "no allergies known": None,
                  "nkda": "medication", "nkma": "medication", "no known drug allergies": "medication", "no known drug allergy": "medication",
                  "no known medication allergies": "medication", "no known medication allergy": "medication",
                  "no known medicine allergies": "medication", "nkfa": "food", "no known food allergies": "food", "no known food allergy": "food",
                  "no known environmental allergies": "environment", "no known environmental allergy": "environment"}
SYSTOLIC, DIASTOLIC, PRESSURE_PANEL = "8480-6", "8462-4", "85354-9"
OBSERVATION_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
TEXT_ID_SYSTEM = "urn:syntropy-health:ccda-text"
_TS = re.compile(r"^(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(?:\.\d+)?([+-]\d{4}|Z)?$")
_SPACE = re.compile(r"[ \t\r\n]+")


@dataclass
class Summary:
    resources: list[dict[str, Any]] = field(default_factory=list)
    originals: list[dict[str, Any]] = field(default_factory=list)       # resource_id, name, content_type, data
    warnings: list[str] = field(default_factory=list)


def looks_like(raw: bytes) -> bool:
    """A zip or XML, as against the JSON a FHIR file is."""
    return raw[:4] == b"PK\x03\x04" or raw[:64].lstrip(b"\xef\xbb\xbf\xff\xfe\x00 \t\r\n")[:1] == b"<"


def parse_xml(raw: bytes) -> tuple[ET.Element, str]:
    """The document and its root's namespace, with every name reduced to its local part. Expat directly, because that's where a
    document type and entities can be refused outright, which rules out the billion laughs and external entities both."""
    if len(raw) > MAX_DOCUMENT:
        raise ValueError(TOO_BIG)
    builder = ET.TreeBuilder()
    depth = count = 0
    root_namespace = ""

    def refuse(*_: Any) -> None:
        raise ValueError(NOT_ALLOWED_XML)

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal depth, count, root_namespace
        depth += 1
        count += 1
        if depth > MAX_DEPTH or count > MAX_ELEMENTS:
            raise ValueError(TOO_BIG)
        namespace, _, local = name.rpartition(" ")
        if count == 1:
            root_namespace = namespace
        builder.start(local, {k.rpartition(" ")[2]: v for k, v in attrs.items()})

    def end(name: str) -> None:
        nonlocal depth
        depth -= 1
        builder.end(name.rpartition(" ")[2])

    parser = expat.ParserCreate(namespace_separator=" ")
    parser.buffer_text = True
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data
    try:
        parser.Parse(raw, True)
        return builder.close(), root_namespace
    except (expat.ExpatError, AssertionError, LookupError) as exc:
        raise ValueError(NOT_READABLE) from exc


# ---------------------------------------------------------------------------
# Small readings
# ---------------------------------------------------------------------------

def _time(value: Optional[str]) -> Optional[str]:
    """An HL7 timestamp in FHIR's form. Only as precise as the source was, and None for one that isn't a real date."""
    match = _TS.match((value or "").strip())
    if not match:
        return None
    y, m, d, hh, mm, ss, zone = match.groups()
    try:
        date(int(y), int(m or 1), int(d or 1))
    except ValueError:
        return None
    if not m or not d or not hh:
        return "-".join(p for p in (y, m, d) if p)
    if int(hh) > 23 or int(mm or 0) > 59 or int(ss or 0) > 59:
        return f"{y}-{m}-{d}"
    offset = "Z" if zone == "Z" else f"{zone[:3]}:{zone[3:]}" if zone else ""
    return f"{y}-{m}-{d}T{hh}:{mm or '00'}:{ss or '00'}{offset}"


def _times(el: ET.Element) -> tuple[Optional[str], Optional[str]]:
    """Start and end from an element's effectiveTime: one moment, or a low and a high."""
    for when in el.findall("effectiveTime"):
        if when.get("type") in ("PIVL_TS", "EIVL_TS", "SXPR_TS"):
            continue
        low, high = when.find("low"), when.find("high")
        if low is not None or high is not None:
            return _time(low.get("value") if low is not None else None), _time(high.get("value") if high is not None else None)
        center = when.find("center")
        return _time(when.get("value") or (center.get("value") if center is not None else None)), None
    return None, None


def _clean(text: Optional[str]) -> str:
    return _SPACE.sub(" ", text or "").strip()


def _templates(el: ET.Element) -> set[str]:
    return {t.get("root") or "" for t in el.findall("templateId")}


def _attr(el: ET.Element, path: str, name: str) -> str:
    found = el.find(path)
    return (found.get(name) or "") if found is not None else ""


def _prose(el: Optional[ET.Element]) -> str:
    """Narrative as lines of text: a table's rows stay rows, a list's items stay items."""
    if el is None:
        return ""
    lines: list[str] = []
    current: list[str] = []

    def flush() -> None:
        line = _clean(" ".join(current))
        if line:
            lines.append(line)
        current.clear()

    def walk(node: ET.Element) -> None:
        if node.tag == "br":
            flush()
        elif node.tag in ("td", "th"):
            current.append(f"{_clean(''.join(node.itertext()))} |")
        else:
            block = node.tag in ("paragraph", "item", "tr", "table", "list", "caption", "thead", "tbody", "tfoot")
            if block:
                flush()
            if node.text:
                current.append(node.text)
            for child in node:
                walk(child)
                if child.tail:
                    current.append(child.tail)
            if block:
                flush()

    walk(el)
    flush()
    return "\n".join(line.removesuffix(" |") for line in lines)


class _Section:
    def __init__(self, element: ET.Element) -> None:
        self.element = element
        self.ids = {n.get("ID"): n for n in element.iter() if n.get("ID")}

    def words(self, el: Optional[ET.Element]) -> Optional[str]:
        """The text of an originalText or text element, which often only points into the section's narrative."""
        if el is None:
            return None
        ref = el.find("reference")
        target = (ref.get("value") or "").lstrip("#") if ref is not None else ""
        if target and target in self.ids:
            return _clean("".join(self.ids[target].itertext())) or None
        return _clean("".join(el.itertext())) or None

    def concept(self, el: Optional[ET.Element]) -> Optional[dict[str, Any]]:
        if el is None:
            return None
        codings: list[dict[str, Any]] = []
        for coded in [el, *el.findall("translation")]:
            if coded.get("code"):
                oid = coded.get("codeSystem")
                codings.append({k: v for k, v in (("system", f"urn:oid:{oid}" if oid else None), ("code", coded.get("code")),
                                                  ("display", _clean(coded.get("displayName")) or None)) if v})
        text = self.words(el.find("originalText")) or next((c["display"] for c in codings if c.get("display")), None)
        if not codings and not text:
            return None
        return {**({"coding": codings} if codings else {}), **({"text": text} if text else {})}


def _digest(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8", "replace")).hexdigest()[:32]


def _resource_id(kind: str, el: ET.Element) -> str:
    """Stays the same when the same entry comes back in a later download. Hashed, because FHIR ids are short and these aren't."""
    for known in el.findall("id"):
        if known.get("root"):
            return f"ccda-{_digest(kind, known.get('root'), known.get('extension'))}"
    return f"ccda-{_digest(kind, ET.tostring(el))}"


def _person(el: Optional[ET.Element]) -> Optional[str]:
    if el is None:
        return None
    given = [_clean(g.text) for g in el.findall("given") if _clean(g.text)]
    family = _clean(el.findtext("family"))
    return " ".join([*given, family]).strip() or _clean("".join(el.itertext())) or None


def _performer(el: ET.Element) -> Optional[str]:
    entity = el.find("assignedEntity")
    if entity is None:
        entity = el.find("assignedAuthor")
    if entity is None:
        return None
    return _person(entity.find("assignedPerson/name")) or _clean(entity.findtext("representedOrganization/name")) or None


def _quantity(el: Optional[ET.Element]) -> Optional[dict[str, Any]]:
    if el is None or el.get("value") is None:
        return None
    try:
        number = float(el.get("value") or "")
    except ValueError:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    unit = el.get("unit")
    out: dict[str, Any] = {"value": int(number) if number.is_integer() else number}
    if unit and unit != "1":
        out.update(unit=unit, system="http://unitsofmeasure.org", code=unit)
    return out


def _status_of(el: ET.Element) -> str:
    return _attr(el, "statusCode", "code").lower()


def _related(el: ET.Element, type_code: Optional[str] = None) -> list[ET.Element]:
    return [r for r in el.findall("entryRelationship") if not type_code or r.get("typeCode") == type_code]


def _negated(el: ET.Element) -> bool:
    return (el.get("negationInd") or "").lower() == "true"


def _some(**fields: Any) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v}


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _keep(kept: dict[tuple[str, str], dict[str, Any]], res: dict[str, Any]) -> dict[str, Any]:
    """Different entries can carry one id: an order id reused across a panel's results, or a bare root. Each stays, under an id
    that's still the same when the document comes back. The same entry twice stays one."""
    found = res
    for tell_apart in (None, res.get("code"), res):
        res_id = res["id"] if tell_apart is None else f"ccda-{_digest(res['id'], json.dumps(tell_apart, sort_keys=True))}"
        found = kept.get((res["resourceType"], res_id))
        if found is None:
            return kept.setdefault((res["resourceType"], res_id), {**res, "id": res_id})
        if {**found, "id": res["id"]} == res:
            return found
    return found


class _Document:
    def __init__(self, root: ET.Element, digest: str) -> None:
        self.root = root
        self.digest = digest
        self.patient_id: Optional[str] = None
        self.date = _time(_attr(root, "effectiveTime", "value"))
        self.title = _clean(root.findtext("title"))
        self.resources: dict[tuple[str, str], dict[str, Any]] = {}
        self.missed = 0

    def add(self, res: dict[str, Any]) -> dict[str, Any]:
        return _keep(self.resources, res)

    def subject(self) -> dict[str, Any]:
        return {"subject": {"reference": f"Patient/{self.patient_id}"}} if self.patient_id else {}

    def patient(self) -> dict[str, Any]:
        return {"patient": {"reference": f"Patient/{self.patient_id}"}} if self.patient_id else {}


def _concern_observations(entry: ET.Element) -> tuple[Optional[ET.Element], list[ET.Element]]:
    act = entry.find("act")
    found = [r.find("observation") for r in _related(act)] if act is not None else [entry.find("observation")]
    return act, [o for o in found if o is not None]


def _no_known(category: Optional[str]) -> dict[str, Any]:
    code, words = NO_KNOWN_BY_CATEGORY.get(category or "", (NO_KNOWN_ALLERGY, "No known allergies"))
    return {"coding": [{"system": "http://snomed.info/sct", "code": code}], "text": words}


def _allergy(ctx: _Document, sec: _Section, entry: ET.Element) -> list[dict[str, Any]]:
    act, observations = _concern_observations(entry)
    out = []
    for obs in observations:
        material = obs.find("participant/participantRole/playingEntity")
        substance = sec.concept(material.find("code")) if material is not None else None
        if not substance and material is not None and _clean(material.findtext("name")):
            substance = {"text": _clean(material.findtext("name"))}
        named = " ".join(re.sub(r"[^a-z ]+", " ", ((substance or {}).get("text") or "").lower()).split())
        if named in NO_KNOWN_WORDS or (_negated(obs) and not substance):
            substance = _no_known(NO_KNOWN_WORDS[named] if named in NO_KNOWN_WORDS else ALLERGY_CATEGORY.get(_attr(obs, "value", "code")))
        elif _negated(obs) or not substance or named.startswith(("no known", "no allerg")):
            ctx.missed += 1     # ruling out one substance says nothing about any other, so it mustn't read as "no allergies"
            continue
        status = CONCERN_STATUS.get(_status_of(act)) if act is not None else None
        res: dict[str, Any] = {"resourceType": "AllergyIntolerance", "id": _resource_id("allergy", obs), "code": substance, **ctx.patient()}
        reactions = []
        for rel in _related(obs):
            inner = rel.find("observation")
            if inner is None:
                continue
            found = _templates(inner)
            code = _attr(inner, "value", "code")
            if f"{_T}4.28" in found and code in STATUS_VALUE:
                status = STATUS_VALUE[code]
            elif f"{_T}4.145" in found and code in CRITICALITY:
                res["criticality"] = CRITICALITY[code]
            elif rel.get("typeCode") == "MFST":
                manifestation = sec.concept(inner.find("value"))
                if manifestation:
                    reaction: dict[str, Any] = {"manifestation": [manifestation]}
                    for sev in _related(inner):
                        if _attr(sev, "observation/value", "code") in SEVERITY:
                            reaction["severity"] = SEVERITY[_attr(sev, "observation/value", "code")]
                    reactions.append(reaction)
        if status:
            res["clinicalStatus"] = {"coding": [{"code": status}]}
        category = ALLERGY_CATEGORY.get(_attr(obs, "value", "code"))
        res.update(_some(reaction=reactions, category=[category] if category else None, onsetDateTime=_times(obs)[0]))
        out.append(res)
    return out


def _dosage(sec: _Section, sa: ET.Element) -> dict[str, Any]:
    amount = _quantity(sa.find("doseQuantity"))
    dose = _some(text=sec.words(sa.find("text")), doseAndRate=[{"doseQuantity": amount}] if amount else None, route=sec.concept(sa.find("routeCode")))
    for when in sa.findall("effectiveTime"):
        period = when.find("period")
        if when.get("type") == "PIVL_TS" and period is not None and period.get("unit") in PERIOD_UNITS:
            how_often = _quantity(period)
            if how_often:
                dose["timing"] = {"repeat": {"frequency": 1, "period": how_often["value"], "periodUnit": period.get("unit")}}
    if sa.find("precondition") is not None:
        dose["asNeededBoolean"] = True
    return dose


def _medication(ctx: _Document, sec: _Section, entry: ET.Element) -> list[dict[str, Any]]:
    sa = entry.find("substanceAdministration")
    if sa is None or _negated(sa):
        return []
    material = sa.find("consumable/manufacturedProduct/manufacturedMaterial")
    if material is None:
        return []
    concept = sec.concept(material.find("code"))
    name = _clean(material.findtext("name"))
    if not concept and not name:
        return []
    concept = concept or {"text": name}
    if name and not concept.get("text"):
        concept["text"] = name
    start, end = _times(sa)
    status = MED_STATUS.get(_status_of(sa))
    dose = _dosage(sec, sa)
    reasons = [c for c in (sec.concept(r.find("observation/value")) for r in _related(sa, "RSON")) if c]
    author = next((p for p in (_performer(a) for a in sa.findall("author")) if p), None)
    supply = next((s for s in (r.find("supply") for r in _related(sa)) if s is not None and s.get("moodCode") == "INT"), None)
    if supply is not None and not author:
        author = next((p for p in (_performer(a) for a in supply.findall("author")) if p), None)
    base = {"id": _resource_id("med", sa), "status": status or "unknown", "medicationCodeableConcept": concept, **ctx.subject(), **_some(reasonCode=reasons)}
    period = _some(start=start, end=end)
    if sa.get("moodCode") == "EVN":
        # A stated end that has passed: nothing in the document says it's still being taken, whatever its status code.
        if status == "active" and end and end[:10] < date.today().isoformat():
            base["status"] = "completed"
        return [{"resourceType": "MedicationStatement", **base,
                 **_some(effectivePeriod=period, dateAsserted=ctx.date, dosage=[dose] if dose else None,
                         informationSource={"display": author} if author else None)}]
    dispense: dict[str, Any] = {"validityPeriod": period} if end else {}
    repeats = _attr(supply, "repeatNumber", "value") if supply is not None else ""
    if repeats.isdigit():
        dispense["numberOfRepeatsAllowed"] = max(0, int(repeats) - 1)     # HL7 counts every fill, FHIR the ones after the first
    return [{"resourceType": "MedicationRequest", **base, "intent": "order",
             **_some(authoredOn=start, dosageInstruction=[dose] if dose else None, dispenseRequest=dispense,
                     requester={"display": author} if author else None)}]


def _problem(ctx: _Document, sec: _Section, entry: ET.Element) -> list[dict[str, Any]]:
    act, observations = _concern_observations(entry)
    out = []
    for obs in observations:
        concept = sec.concept(obs.find("value"))
        if _negated(obs) or not concept:
            ctx.missed += 1     # "no such problem" reads as the opposite if it's only half understood
            continue
        status = CONCERN_STATUS.get(_status_of(act)) if act is not None else None
        for rel in _related(obs):
            if _attr(rel, "observation/value", "code") in STATUS_VALUE:
                status = STATUS_VALUE[_attr(rel, "observation/value", "code")]
        onset, ended = _times(obs)
        out.append({"resourceType": "Condition", "id": _resource_id("problem", obs), "code": concept, **ctx.subject(),
                    "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category", "code": "problem-list-item",
                                              "display": "Problem List Item"}]}],
                    **_some(clinicalStatus={"coding": [{"code": status}]} if status else None, onsetDateTime=onset, abatementDateTime=ended)})
    return out


def _value(sec: _Section, obs: ET.Element) -> dict[str, Any]:
    el = obs.find("value")
    if el is None:
        return {}
    kind = el.get("type") or ""
    if kind in ("PQ", "INT", "REAL"):
        amount = _quantity(el)
        if amount:
            return {"valueQuantity": amount}
        return {"valueString": el.get("value")} if el.get("value") else {}
    if kind == "IVL_PQ":
        low, high = _quantity(el.find("low")), _quantity(el.find("high"))
        return {"valueRange": _some(low=low, high=high)} if low or high else {}
    if kind in ("CD", "CE", "CO", "CV"):
        concept = sec.concept(el)
        return {"valueCodeableConcept": concept} if concept else {}
    if kind == "BL" and el.get("value") in ("true", "false"):
        return {"valueBoolean": el.get("value") == "true"}
    if kind == "TS" and _time(el.get("value")):
        return {"valueDateTime": _time(el.get("value"))}
    words = sec.words(el)
    return {"valueString": words} if words else {}


def _ranges(sec: _Section, obs: ET.Element) -> list[dict[str, Any]]:
    out = []
    for rr in obs.findall("referenceRange/observationRange"):
        if _attr(rr, "interpretationCode", "code") not in ("", "N"):
            continue        # a range labelled something other than normal, such as a critical limit, isn't the reference range
        value = rr.find("value")
        rng = _some(low=_quantity(value.find("low")) if value is not None else None,
                    high=_quantity(value.find("high")) if value is not None else None, text=sec.words(rr.find("text")))
        if rng:
            out.append(rng)
    return out


def _observation(ctx: _Document, sec: _Section, obs: ET.Element, category: str, default_when: Optional[str]) -> Optional[dict[str, Any]]:
    concept = sec.concept(obs.find("code"))
    value = _value(sec, obs)
    if not concept or not value or _negated(obs):
        ctx.missed += 1
        return None
    return {"resourceType": "Observation", "id": _resource_id(category, obs), "status": RESULT_STATUS.get(_status_of(obs), "unknown"),
            "category": [{"coding": [{"system": OBSERVATION_CATEGORY, "code": category}]}], "code": concept, **ctx.subject(), **value,
            **_some(effectiveDateTime=_times(obs)[0] or default_when,
                    interpretation=[c for c in (sec.concept(f) for f in obs.findall("interpretationCode")) if c], referenceRange=_ranges(sec, obs))}


def _components(entry: ET.Element) -> tuple[Optional[ET.Element], list[ET.Element]]:
    organizer = entry.find("organizer")
    if organizer is not None:
        return organizer, [o for o in (c.find("observation") for c in organizer.findall("component")) if o is not None]
    direct = entry.find("observation")
    return None, [direct] if direct is not None else []


def _result(ctx: _Document, sec: _Section, entry: ET.Element) -> list[dict[str, Any]]:
    organizer, observations = _components(entry)
    when = _times(organizer)[0] if organizer is not None else None
    out = [r for r in (_observation(ctx, sec, o, "laboratory", when) for o in observations) if r]
    panel = sec.concept(organizer.find("code")) if organizer is not None else None
    if out and panel and organizer is not None:
        out.append({"resourceType": "DiagnosticReport", "id": _resource_id("panel", organizer), "status": RESULT_STATUS.get(_status_of(organizer), "unknown"),
                    "category": [{"coding": [{"code": "LAB"}], "text": "Laboratory"}], "code": panel, **ctx.subject(),
                    **_some(effectiveDateTime=when or out[0].get("effectiveDateTime")),
                    "result": [{"reference": f"Observation/{r['id']}"} for r in out]})
    return out


def _vital(ctx: _Document, sec: _Section, entry: ET.Element) -> list[dict[str, Any]]:
    organizer, observations = _components(entry)
    when = _times(organizer)[0] if organizer is not None else None
    out = [r for r in (_observation(ctx, sec, o, "vital-signs", when) for o in observations) if r]
    # FHIR has one blood pressure with two parts where HL7 has two observations, and the charts and insights read the FHIR form.
    moments: dict[Optional[str], dict[str, dict[str, Any]]] = {}
    for r in out:
        code = next((c.get("code") for c in r["code"].get("coding", []) if c.get("code") in (SYSTOLIC, DIASTOLIC)), None)
        if code and "valueQuantity" in r:
            moments.setdefault(r.get("effectiveDateTime"), {})[code] = r
    for moment, parts in moments.items():
        if len(parts) == 2:
            out = [r for r in out if r["id"] not in (parts[SYSTOLIC]["id"], parts[DIASTOLIC]["id"])]
            out.append({"resourceType": "Observation", "id": f"ccda-{_digest('bp', parts[SYSTOLIC]['id'], parts[DIASTOLIC]['id'])}",
                        "status": parts[SYSTOLIC]["status"], "category": parts[SYSTOLIC]["category"],
                        "code": {"coding": [{"system": "http://loinc.org", "code": PRESSURE_PANEL, "display": "Blood pressure"}], "text": "Blood pressure"},
                        **ctx.subject(), **_some(effectiveDateTime=moment),
                        "component": [{"code": parts[c]["code"], "valueQuantity": parts[c]["valueQuantity"]} for c in (SYSTOLIC, DIASTOLIC)]})
    return out


def _immunization(ctx: _Document, sec: _Section, entry: ET.Element) -> list[dict[str, Any]]:
    sa = entry.find("substanceAdministration")
    if sa is None or sa.get("moodCode") != "EVN":
        return []
    material = sa.find("consumable/manufacturedProduct/manufacturedMaterial")
    concept = sec.concept(material.find("code")) if material is not None else None
    if not concept or material is None:
        return []
    maker = _clean(sa.findtext("consumable/manufacturedProduct/manufacturerOrganization/name"))
    who = next((p for p in (_performer(x) for x in sa.findall("performer")) if p), None)
    return [{"resourceType": "Immunization", "id": _resource_id("immunization", sa), "vaccineCode": concept, **ctx.patient(),
             "status": "entered-in-error" if _status_of(sa) == "nullified" else "not-done" if _negated(sa) else "completed",
             **_some(occurrenceDateTime=_times(sa)[0], lotNumber=_clean(material.findtext("lotNumberText")),
                     manufacturer={"display": maker} if maker else None, route=sec.concept(sa.find("routeCode")),
                     site=sec.concept(sa.find("approachSiteCode")), doseQuantity=_quantity(sa.find("doseQuantity")),
                     performer=[{"actor": {"display": who}}] if who else None)}]


def _procedure(ctx: _Document, sec: _Section, entry: ET.Element) -> list[dict[str, Any]]:
    el = next((e for e in (entry.find(t) for t in ("procedure", "act", "observation")) if e is not None), None)
    if el is None or _negated(el) or el.get("moodCode") not in (None, "EVN"):
        return []
    concept = sec.concept(el.find("code"))
    if not concept:
        return []
    start, end = _times(el)
    who = [p for p in (_performer(x) for x in el.findall("performer")) if p]
    return [{"resourceType": "Procedure", "id": _resource_id("procedure", el), "code": concept,
             "status": PROCEDURE_STATUS.get(_status_of(el), "unknown"), **ctx.subject(),
             **_some(performedPeriod=_some(start=start, end=end) if end else None, performedDateTime=None if end else start,
                     bodySite=[c for c in (sec.concept(s) for s in el.findall("targetSiteCode")) if c],
                     performer=[{"actor": {"display": p}} for p in who],
                     reasonCode=[c for c in (sec.concept(r.find("observation/value")) for r in _related(el, "RSON")) if c])}]


def _encounter(ctx: _Document, sec: _Section, entry: ET.Element) -> list[dict[str, Any]]:
    el = entry.find("encounter")
    if el is None or el.get("moodCode") not in (None, "EVN"):
        return []
    code = el.find("code")
    concept = sec.concept(code)
    start, end = _times(el)
    if not concept and not start:
        return []
    coded = next((c for c in ([code, *code.findall("translation")] if code is not None else [])
                  if c.get("codeSystem") == ACT_CODE_OID and c.get("code")), None)
    who = [p for p in (_performer(x) for x in el.findall("performer")) if p]
    places = [n for n in (_clean(p.findtext("participantRole/playingEntity/name")) for p in el.findall("participant") if p.get("typeCode") == "LOC") if n]
    diagnoses, reasons = [], []
    for rel in _related(el):
        act = rel.find("act")
        inner = act.find("entryRelationship/observation") if act is not None else rel.find("observation")
        found = sec.concept(inner.find("value")) if inner is not None else None
        if found and found.get("text"):
            (reasons if rel.get("typeCode") == "RSON" else diagnoses).append(found)
    return [{"resourceType": "Encounter", "id": _resource_id("encounter", el), "status": "in-progress" if _status_of(el) == "active" else "finished", **ctx.subject(),
             **_some(type=[concept] if concept else None, period=_some(start=start, end=end),
                     participant=[{"individual": {"display": p}} for p in who], location=[{"location": {"display": n}} for n in places],
                     diagnosis=[{"condition": {"display": c["text"]}} for c in diagnoses], reasonCode=reasons),
             **({"class": _some(code=coded.get("code"), display=coded.get("displayName"))} if coded is not None else {})}]


READERS = {"allergies": _allergy, "medications": _medication, "problems": _problem, "results": _result, "vitals": _vital,
           "immunizations": _immunization, "procedures": _procedure, "encounters": _encounter}


def _note(ctx: _Document, title: str, text: str, kind: str, code: Optional[dict[str, Any]] = None, when: Optional[str] = None) -> bool:
    """A piece of text kept as a note. Identified by what it says, so a later download saying the same thing is the same note."""
    text = text.strip()
    if not text:
        return False
    key = _digest(kind, title, text)
    ctx.add({"resourceType": "DocumentReference", "id": f"ccda-{key}", "status": "current",
             "masterIdentifier": {"system": TEXT_ID_SYSTEM, "value": key},
             "type": {**(code or {}), "text": title}, "description": title, **ctx.subject(), **_some(date=when or ctx.date),
             "content": [{"attachment": {"contentType": "text/plain", "title": title,
                                         "data": base64.b64encode(text[:MAX_NOTE_TEXT].encode()).decode()}}]})
    return True


def _section_kind(el: ET.Element) -> Optional[str]:
    known = SECTIONS.get(_attr(el, "code", "code"))
    return known or next((SECTION_TEMPLATES[t] for t in _templates(el) if t in SECTION_TEMPLATES), None)


def _read_section(ctx: _Document, el: ET.Element) -> None:
    sec = _Section(el)
    code = sec.concept(el.find("code"))
    title = _clean(el.findtext("title")) or (code or {}).get("text") or "Section"
    reader = READERS.get(_section_kind(el) or "")
    before = ctx.missed
    entries = el.findall("entry")
    for entry in entries:
        act = entry.find("act")
        if act is not None and NOTE_ACTIVITY in _templates(act):
            named = (sec.concept(act.find("code")) or {}).get("text")
            if not _note(ctx, _clean(named) or title, sec.words(act.find("text")) or "", "note", None, _times(act)[0]):
                ctx.missed += 1
            continue
        if not reader:
            continue
        missed = ctx.missed
        try:
            found = reader(ctx, sec, entry)
        except (ValueError, TypeError, AttributeError, KeyError):
            found = []
        results: list[dict[str, Any]] = []
        for res in found:
            if res["resourceType"] == "DiagnosticReport" and "result" in res:
                res["result"] = [{"reference": f"Observation/{r['id']}"} for r in results]
            results.append(ctx.add(res))
        if not found and ctx.missed == missed:
            ctx.missed += 1
    # What a section says stays readable when an entry couldn't be mapped, when it has no entries (often just "No known allergies"),
    # and for every kind of section nothing here maps.
    if ctx.missed > before or not reader or not entries:
        _note(ctx, title, _prose(el.find("text")), "section", code)
    for nested in el.findall("component/section"):
        _read_section(ctx, nested)


def _patient(ctx: _Document) -> Optional[dict[str, Any]]:
    role = ctx.root.find("recordTarget/patientRole")
    if role is None:
        return None
    person = role.find("patient")
    name = person.find("name") if person is not None else None
    numbers = [i for i in role.findall("id") if i.get("root") and i.get("extension") and i.get("root") != SSN_OID]
    born = _time(_attr(person, "birthTime", "value")) if person is not None else None
    sex = _attr(person, "administrativeGenderCode", "code") if person is not None else ""
    if not numbers and name is None:
        return None
    pairs = [(i.get("root"), i.get("extension")) for i in numbers] or [_person(name), born]
    res: dict[str, Any] = {"resourceType": "Patient", "id": f"ccda-{_digest('patient', *pairs)}",
                           **_some(identifier=[{"system": f"urn:oid:{i.get('root')}", "value": i.get("extension")} for i in numbers],
                                   birthDate=born[:10] if born else None, gender={"M": "male", "F": "female"}.get(sex))}
    if name is not None:
        res["name"] = [_some(family=_clean(name.findtext("family")), given=[_clean(g.text) for g in name.findall("given") if _clean(g.text)],
                             text=_person(name))]
    return res


def _unstructured(ctx: _Document, el: ET.Element, summary: Summary) -> None:
    """A document that is only a PDF, an image or plain text wrapped in XML."""
    title = ctx.title or "Health summary"
    media = (el.get("mediaType") or "text/plain").lower()
    body = "".join(el.itertext())
    if el.get("compression"):
        summary.warnings.append(f"{title}: its content is compressed in a way that isn't read here. The original is kept.")
    elif media.startswith("text/") and el.get("representation") != "B64":
        _note(ctx, title, body, "body")
    else:
        key = _digest("body", ctx.digest)
        ctx.add({"resourceType": "DocumentReference", "id": f"ccda-{key}", "status": "current", "type": {"text": title}, "description": title,
                 "masterIdentifier": {"system": TEXT_ID_SYSTEM, "value": key}, **ctx.subject(), **_some(date=ctx.date),
                 "content": [{"attachment": {"contentType": media, "title": title, "data": re.sub(r"\s+", "", body)}}]})


def _original(ctx: _Document, raw: bytes, name: str) -> dict[str, Any]:
    """The document as it came, as a note of its own: everything else here is a reading of it."""
    res_id = f"ccda-doc-{ctx.digest[:32]}"
    custodian = _clean(ctx.root.findtext("custodian/assignedCustodian/representedCustodianOrganization/name"))
    ctx.add({"resourceType": "DocumentReference", "id": res_id, "status": "current",
             "masterIdentifier": {"system": "urn:syntropy-health:ccda-document", "value": ctx.digest},
             "type": {**(_Section(ctx.root).concept(ctx.root.find("code")) or {}), "text": ctx.title or "Health summary"},
             "description": f"{ctx.title or 'Health summary'} (original document)", **ctx.subject(),
             **_some(date=ctx.date, custodian={"display": custodian} if custodian else None),
             "content": [{"attachment": {"contentType": "application/xml", "title": name, "size": len(raw)}}]})
    return {"resource_id": res_id, "name": name, "content_type": "application/xml", "data": raw}


def read_document(raw: bytes, name: str) -> Summary:
    """One C-CDA or CCD document. Raises ValueError, in words for the person, when it isn't one or can't be read."""
    root, namespace = parse_xml(raw)
    if root.tag != "ClinicalDocument" or namespace != HL7:
        raise ValueError(NOT_SUMMARY_XML)
    ctx = _Document(root, hashlib.sha256(raw).hexdigest())
    summary = Summary()
    patient = _patient(ctx)
    if patient:
        ctx.patient_id = patient["id"]
        summary.resources.append(patient)
    for el in root.findall("component/structuredBody/component/section"):
        _read_section(ctx, el)
    unstructured = root.find("component/nonXMLBody/text")
    if unstructured is not None:
        _unstructured(ctx, unstructured, summary)
    summary.originals.append(_original(ctx, raw, name))
    summary.resources.extend(ctx.resources.values())
    if ctx.missed:
        summary.warnings.append(f"{ctx.missed} {'entry' if ctx.missed == 1 else 'entries'} in {name} couldn't be read in detail. "
                                "The text of those sections is kept under Clinical notes.")
    return summary


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _zip_documents(raw: bytes) -> tuple[list[tuple[str, bytes]], list[str]]:
    """The documents in an XDM zip, within limits on what a small zip may unpack to. METADATA.XML is XML too, so what a member
    holds is what tells them apart."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError(NOT_READABLE) from exc
    documents: list[tuple[str, bytes]] = []
    others: list[str] = []
    total = 0
    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_MEMBERS:
            raise ValueError(TOO_BIG)
        for info in infos:
            leaf = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if leaf.lower() == "export.xml":    # Apple Health's: a large file to open only to say it isn't one of these
                raise ValueError(NOT_SUMMARY_ZIP)
            if leaf.lower().endswith(".pdf"):
                others.append(leaf)
            if not leaf.lower().endswith(".xml"):
                continue
            if info.file_size > MAX_DOCUMENT or _implausible(info):
                raise ValueError(TOO_BIG)
            try:
                with archive.open(info) as member:
                    body = member.read(MAX_DOCUMENT + 1)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error, OSError, EOFError) as exc:
                raise ValueError(NOT_READABLE) from exc
            total += len(body)
            if len(body) > MAX_DOCUMENT or total > MAX_ZIP_TOTAL:
                raise ValueError(TOO_BIG)
            if b"ClinicalDocument" in body[:8192] or "ClinicalDocument".encode("utf-16-le") in body[:8192]:
                documents.append((leaf, body))
                if len(documents) > MAX_ZIP_DOCUMENTS:
                    raise ValueError(TOO_BIG)
    return documents, others


def read_upload(raw: bytes, filename: str) -> Summary:
    """A C-CDA or CCD file, or an XDM zip of them."""
    if raw[:4] != b"PK\x03\x04":
        return read_document(raw, filename)
    documents, others = _zip_documents(raw)
    if not documents:
        raise ValueError(NOT_SUMMARY_ZIP)
    out = Summary()
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    damaged: list[tuple[str, ValueError]] = []
    for name, body in documents:
        try:
            one = read_document(body, name)
        except ValueError as exc:
            # A manifest can mention the word without being a document. One that can't be read mustn't cost the others.
            if str(exc) != NOT_SUMMARY_XML:
                damaged.append((name, exc))
            continue
        for res in one.resources:
            if res["resourceType"] == "Patient":
                seen.setdefault(("Patient", res["id"]), res)
            else:
                _keep(seen, res)
        out.originals.extend(one.originals)
        out.warnings.extend(one.warnings)
    if not out.originals:
        raise damaged[0][1] if damaged else ValueError(NOT_SUMMARY_ZIP)
    out.resources = list(seen.values())
    if damaged:
        names = [name for name, _ in damaged]
        out.warnings.append(f"Couldn't be read, so not imported: {', '.join(names[:5])}{' and others' if len(names) > 5 else ''}. "
                            "The other documents in this zip were imported.")
    if others:
        out.warnings.append(f"Not read from this zip: {', '.join(others[:5])}{' and others' if len(others) > 5 else ''}. "
                            "Only the health summary documents in it were imported.")
    return out
