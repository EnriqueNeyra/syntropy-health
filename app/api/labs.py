"""Lab results entered by hand or read from a report (PDF or photo), on this machine or with the person's AI provider."""

from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError

from app.api.deps import check_access, resolve_profile
from app.connectors import fhir_normalize
from app.core import auth
from app.core.db import audit, db, new_id
from app.services import ai, lab_report
from app.store import connections, lab_catalog, record_files, records

router = APIRouter(prefix="/api/labs", tags=["labs"], dependencies=[Depends(auth.require_user)])

MANUAL_PROVIDER = "manual"
MANUAL_NAME = "Entered by you"
OBS_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
REPORT_CATEGORY = "http://terminology.hl7.org/CodeSystem/v2-0074"
INTERPRETATION = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.get("/catalog")
def catalog(profile: Optional[str] = None) -> dict:
    """Common tests with LOINC codes, plus the tests already on record (so new results continue those trends)."""
    prof = resolve_profile(profile)
    yours = [{"name": i["title"], "loinc": i["code"], "unit": (i["latest"] or {}).get("unit")}
             for i in records.observation_catalog(prof["id"], "labs") if i.get("code")]
    return {"common": lab_catalog.common_labs(), "yours": yours}


class LabResult(BaseModel):
    test: str = Field(..., min_length=1, max_length=200)
    loinc: Optional[str] = Field(None, max_length=20)
    value: Optional[float] = None
    value_text: Optional[str] = Field(None, max_length=200)
    unit: Optional[str] = Field(None, max_length=40)
    ref_low: Optional[float] = None
    ref_high: Optional[float] = None
    ref_text: Optional[str] = Field(None, max_length=120)
    flag: Optional[str] = Field(None, max_length=4)
    date: Optional[str] = None
    note: Optional[str] = Field(None, max_length=1000)


class LabEntry(BaseModel):
    profile_id: Optional[str] = None
    collected: str                                  # YYYY-MM-DD; a result's own date wins
    lab: Optional[str] = Field(None, max_length=120)
    results: list[LabResult] = Field(..., min_length=1, max_length=300)


def _check_date(value: str, label: str) -> None:
    """A typo in the year would make a result the newest forever. Tomorrow is allowed: it may already be tomorrow where the lab is."""
    if not DATE.match(value):
        raise HTTPException(400, f"{label} must be YYYY-MM-DD.")
    try:
        when = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"{label} isn't a real date.")
    if when > date.today() + timedelta(days=1):
        raise HTTPException(400, f"{label} is in the future. Check the date.")


COMPARED = re.compile(r"^\s*(<=|>=|≤|≥|<|>)\s*(\d+(?:\.\d+)?|\.\d+)\s*$")


def to_observation(r: LabResult, collected: str, lab: Optional[str]) -> dict:
    """A FHIR R4 Observation, so manual results flow through the same normalization, trends and exports as
    results downloaded from a patient portal."""
    when = r.date if r.date and DATE.match(r.date) else collected
    code: dict = {"text": r.test.strip()}
    if r.loinc and re.fullmatch(r"\d{1,7}-\d", r.loinc.strip()):
        code["coding"] = [{"system": fhir_normalize.LOINC, "code": r.loinc.strip(), "display": r.test.strip()}]
    res: dict = {
        "resourceType": "Observation", "id": f"man-{new_id()}", "status": "final",
        "category": [{"coding": [{"system": OBS_CATEGORY, "code": "laboratory", "display": "Laboratory"}]}],
        "code": code, "effectiveDateTime": when,
    }
    bound = COMPARED.match(r.value_text or "") if r.value is None else None
    if r.value is not None:
        res["valueQuantity"] = {"value": r.value, **({"unit": r.unit.strip()} if r.unit else {})}
    elif bound:
        # "<0.5" is a limit with a unit, not words.
        comparator = {"≤": "<=", "≥": ">="}.get(bound.group(1), bound.group(1))
        res["valueQuantity"] = {"value": float(bound.group(2)), "comparator": comparator, **({"unit": r.unit.strip()} if r.unit else {})}
    elif r.value_text:
        res["valueString"] = r.value_text.strip()
    else:
        raise HTTPException(400, f"{r.test}: enter a value.")
    rng: dict = {}
    if r.ref_low is not None:
        rng["low"] = {"value": r.ref_low, **({"unit": r.unit} if r.unit else {})}
    if r.ref_high is not None:
        rng["high"] = {"value": r.ref_high, **({"unit": r.unit} if r.unit else {})}
    if r.ref_text:
        rng["text"] = r.ref_text.strip()
    if rng:
        res["referenceRange"] = [rng]
    flag = (r.flag or "").strip().upper()[:1]
    if flag in ("H", "L"):
        res["interpretation"] = [{"coding": [{"system": INTERPRETATION, "code": flag}]}]
    if lab:
        res["performer"] = [{"display": lab.strip()}]
    if r.note:
        res["note"] = [{"text": r.note.strip()}]
    return res


def _manual_connection(profile_id: str) -> dict:
    existing = next((c for c in connections.list_for_profile(profile_id, "import")
                     if c["provider"] == MANUAL_PROVIDER), None)
    return existing or connections.create(profile_id, "import", MANUAL_PROVIDER, MANUAL_NAME, mode="live")


@router.post("")
def add_results(entry: LabEntry) -> dict:
    _check_date(entry.collected, "Collection date")
    for r in entry.results:
        if r.date and DATE.match(r.date):
            _check_date(r.date, f"The date for {r.test}")
    prof = resolve_profile(entry.profile_id)
    resources = [to_observation(r, entry.collected, entry.lab) for r in entry.results]
    _, recs = fhir_normalize.normalize_resources(resources)
    conn = _manual_connection(prof["id"])
    write = records.upsert_records(prof["id"], conn["id"], entry.lab or MANUAL_NAME, recs)
    with db() as c:
        audit(c, "user", "labs.entered", {"results": len(recs), "lab": entry.lab}, prof["id"])
    return {"connection_id": conn["id"], "records": len(recs), **write}


def _report_resources(entry: LabEntry, name: str, digest: str, known_refs: list[str]) -> tuple[str, list[dict]]:
    """The results, and a report that lists them and that the original file belongs to. Ids come from the file, so saving the same
    report again updates it instead of adding everything a second time."""
    report_id = f"lab-{digest[:16]}"
    seen: dict[str, int] = {}
    results = []
    for r in entry.results:
        when = r.date if r.date and DATE.match(r.date) else entry.collected
        key = f"{re.sub(r'[^a-z0-9]', '', r.test.lower())[:40]}-{when}"
        seen[key] = seen.get(key, 0) + 1
        res = to_observation(r, entry.collected, entry.lab)
        res["id"] = f"{report_id}-{key}" + (f"-{seen[key]}" if seen[key] > 1 else "")
        res["derivedFrom"] = [{"reference": f"DiagnosticReport/{report_id}", "display": name}]
        results.append(res)
    refs = list(dict.fromkeys([*known_refs, *(f"Observation/{res['id']}" for res in results)]))
    report: dict = {
        "resourceType": "DiagnosticReport", "id": report_id, "status": "final",
        "category": [{"coding": [{"system": REPORT_CATEGORY, "code": "LAB", "display": "Laboratory"}]}],
        "code": {"text": f"Lab report: {name}"}, "effectiveDateTime": entry.collected,
        "result": [{"reference": ref} for ref in refs],
    }
    if entry.lab:
        report["performer"] = [{"display": entry.lab.strip()}]
    return report_id, [report, *results]


def _save_report(entry: LabEntry, data: bytes, kind: str, name: str) -> dict:
    prof = resolve_profile(entry.profile_id)
    conn = _manual_connection(prof["id"])
    digest = hashlib.sha256(data).hexdigest()
    existing = records.get_record(records.record_id(conn["id"], "DiagnosticReport", f"lab-{digest[:16]}"))
    known_refs = (existing.get("details") or {}).get("result_refs") or [] if existing else []
    report_id, resources = _report_resources(entry, name, digest, known_refs)
    _, recs = fhir_normalize.normalize_resources(resources)
    text, _ = record_files.save(prof["id"], conn["id"], "DiagnosticReport", report_id, name, kind, data)
    for rec in recs:
        if rec["resource_type"] == "DiagnosticReport":
            record_files.put_text(rec, text)        # so search and the visit summary find what the report says, as with any other PDF
    write = records.upsert_records(prof["id"], conn["id"], entry.lab or MANUAL_NAME, recs)
    with db() as c:
        audit(c, "user", "labs.entered", {"results": len(recs) - 1, "lab": entry.lab, "original_kept": True}, prof["id"])
    return {"connection_id": conn["id"], "records": len(recs) - 1, "report_id": records.record_id(conn["id"], "DiagnosticReport", report_id), **write}


@router.post("/report")
async def add_report(file: UploadFile = File(...), entry: str = Form(...)) -> dict:
    """The reviewed rows and the file they were read from, saved together: the results go in, and the original stays with them."""
    try:
        parsed = LabEntry.model_validate_json(entry)
    except ValidationError as exc:
        raise HTTPException(400, "Those results couldn't be read. Please try again.") from exc
    data = await file.read(lab_report.MAX_BYTES + 1)
    if len(data) > lab_report.MAX_BYTES:
        raise HTTPException(400, "That file is larger than 15 MB.")
    kind = lab_report.file_type(data, file.content_type or "", file.filename or "")
    if not kind:
        raise HTTPException(400, "Upload a PDF or a photo (JPEG, PNG or HEIC).")
    _check_date(parsed.collected, "Collection date")
    for r in parsed.results:
        if r.date and DATE.match(r.date):
            _check_date(r.date, f"The date for {r.test}")
    return await run_in_threadpool(_save_report, parsed, data, kind, (file.filename or "Lab report")[:200])


class Check(BaseModel):
    test: str = Field(..., max_length=200)
    loinc: Optional[str] = Field(None, max_length=20)
    value: Optional[str] = Field(None, max_length=200)
    date: Optional[str] = None


class OnFile(BaseModel):
    profile_id: Optional[str] = None
    collected: str
    results: list[Check] = Field(..., max_length=300)


def _same_value(row: Check, found: dict[str, Any]) -> bool:
    said = (row.value or "").strip()
    try:
        return found["value_num"] is not None and float(said) == float(found["value_num"])
    except ValueError:
        return said.lower() == str(found.get("value_text") or "").strip().lower()


@router.post("/on-file")
def on_file(body: OnFile) -> dict:
    """Which of these rows the record already has a result for, on that test and day. By the test's meaning, not its spelling:
    "GLUCOSE" and "Glucose, serum" are one test."""
    prof = resolve_profile(body.profile_id, "view")
    days = {r.date if r.date and DATE.match(r.date) else body.collected for r in body.results}
    here = records.labs_on_days(prof["id"], [d for d in days if DATE.match(d)])
    by_test: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for found in here:
        by_test.setdefault((str(found["effective_at"])[:10], lab_catalog.series_key(found["code"], found["code_system"], found["title"])), []).append(found)
    answers: list[Optional[dict[str, Any]]] = []
    for r in body.results:
        loinc = r.loinc if r.loinc and re.fullmatch(r"\d{1,7}-\d", r.loinc.strip()) else None
        key = lab_catalog.series_key(loinc, fhir_normalize.LOINC if loinc else None, r.test.strip())
        matches = by_test.get((r.date if r.date and DATE.match(r.date) else body.collected, key)) or []
        same = next((m for m in matches if _same_value(r, m)), None)
        shown = same or (matches[0] if matches else None)
        answers.append({"same": same is not None, "value": shown["value_text"], "source": shown["source_name"], "id": shown["id"]} if shown else None)
    return {"results": answers}


@router.delete("/{record_id}")
def delete_result(record_id: str) -> dict:
    rec = records.get_record(record_id)
    conn = connections.get(rec["connection_id"]) if rec else None
    if not rec or not conn or conn["provider"] != MANUAL_PROVIDER:
        raise HTTPException(404, "Only results you entered yourself can be deleted here.")
    check_access(rec["profile_id"], "manage")
    records.delete_record(record_id)
    # A report goes with the file it was read from. The results read from it stay: they were checked, and each is deleted on its own.
    record_files.delete_for_record(rec["connection_id"], rec["resource_type"], rec["resource_id"])
    return {"ok": True}


@router.get("/reader")
def reader() -> dict:
    """Whether photos and scans can be read on this machine (PDFs with a text layer always can)."""
    found = lab_report.ocr_available()
    return {"ocr": found, "engine": lab_report.ocr.engine(), "install": None if found else lab_report.install_advice()}


def _pages_or_none(data: bytes, content_type: str, filename: str) -> list[dict[str, Any]]:
    try:
        return lab_report.page_previews(data, content_type, filename)
    except lab_report.ReportError:
        return []


@router.post("/extract")
async def extract(file: UploadFile = File(...), method: str = Form("local"), profile_id: Optional[str] = Form(None)) -> dict:
    """Reads results from a lab report, on this machine ("local") or with the person's AI provider ("ai"). Nothing is
    saved: the person reviews and corrects the rows beside the page, then saves them with POST /api/labs/report."""
    if method not in ("local", "ai"):
        raise HTTPException(400, "Unknown reading method.")
    data = await file.read(lab_report.MAX_BYTES + 1)
    content_type, filename = file.content_type or "", file.filename or ""
    try:
        if method == "ai":
            # An outside AI needs the agreement of the person the report is about.
            subject = (await run_in_threadpool(resolve_profile, profile_id))["id"] if profile_id else None
            sent, sent_type, sent_name = await run_in_threadpool(lab_report.for_ai, data, content_type, filename)
            out = await ai.extract_lab_results(sent, sent_type, sent_name, subject)
            # The pages to check it against are drawn here, whoever did the reading.
            out["pages"] = await run_in_threadpool(_pages_or_none, data, content_type, filename)
        else:
            out = await run_in_threadpool(lab_report.read, data, content_type, filename)
    except (ai.AIError, lab_report.ReportError) as exc:
        raise HTTPException(400, str(exc))
    for r in out["results"]:
        # Fill in a LOINC code from the common list when the reader didn't give one.
        codes = lab_catalog.loinc_codes(r["test"]) if not r.get("loinc") and isinstance(r.get("test"), str) else []
        if codes:
            r["loinc"] = codes[0]
            # Shown on the row for review: which test its history joins is the person's to check.
            r["matched_test"] = lab_catalog.matched_test(r["test"])["name"]

    def record() -> None:
        with db() as c:
            audit(c, "user", "labs.report_read", {"method": method, "read_as": out.get("read_as"), "engine": out.get("engine"), "results": len(out["results"])})
    await run_in_threadpool(record)
    return out
