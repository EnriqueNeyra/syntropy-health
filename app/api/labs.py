"""Lab results entered by hand or read from a report (PDF or photo), on this machine or with the person's AI provider."""

from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.api.deps import check_access, resolve_profile
from app.connectors import fhir_normalize
from app.core import auth
from app.core.db import audit, db, new_id
from app.services import ai, lab_report
from app.store import connections, lab_catalog, records

router = APIRouter(prefix="/api/labs", tags=["labs"], dependencies=[Depends(auth.require_user)])

MANUAL_PROVIDER = "manual"
MANUAL_NAME = "Entered by you"
OBS_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
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


def to_observation(r: LabResult, collected: str, lab: Optional[str]) -> dict:
    """A FHIR R4 Observation, so manual results flow through the same normalization, trends and exports as
    results downloaded from a patient portal."""
    when = r.date if r.date and DATE.match(r.date) else collected
    code: dict = {"text": r.test.strip()}
    if r.loinc and re.fullmatch(r"\d{1,7}-\d", r.loinc.strip()):
        code["coding"] = [{"system": fhir_normalize.LOINC, "code": r.loinc.strip(), "display": r.test.strip()}]
    res: dict = {
        "resourceType": "Observation", "id": new_id("man"), "status": "final",
        "category": [{"coding": [{"system": OBS_CATEGORY, "code": "laboratory", "display": "Laboratory"}]}],
        "code": code, "effectiveDateTime": when,
    }
    if r.value is not None:
        res["valueQuantity"] = {"value": r.value, **({"unit": r.unit.strip()} if r.unit else {})}
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
async def add_results(entry: LabEntry) -> dict:
    if not DATE.match(entry.collected):
        raise HTTPException(400, "Collection date must be YYYY-MM-DD.")
    prof = resolve_profile(entry.profile_id)
    resources = [to_observation(r, entry.collected, entry.lab) for r in entry.results]
    _, recs = fhir_normalize.normalize_resources(resources)
    conn = _manual_connection(prof["id"])
    write = records.upsert_records(prof["id"], conn["id"], entry.lab or MANUAL_NAME, recs)
    with db() as c:
        audit(c, "user", "labs.entered", {"results": len(recs), "lab": entry.lab}, prof["id"])
    return {"connection_id": conn["id"], "records": len(recs), **write}


@router.delete("/{record_id}")
async def delete_result(record_id: str) -> dict:
    rec = records.get_record(record_id)
    conn = connections.get(rec["connection_id"]) if rec else None
    if not rec or not conn or conn["provider"] != MANUAL_PROVIDER:
        raise HTTPException(404, "Only results you entered yourself can be deleted here.")
    check_access(rec["profile_id"], "manage")
    records.delete_record(record_id)
    return {"ok": True}


@router.get("/reader")
def reader() -> dict:
    """Whether photos and scans can be read on this machine (PDFs with a text layer always can)."""
    return {"ocr": lab_report.ocr_available()}


@router.post("/extract")
async def extract(file: UploadFile = File(...), method: str = Form("local")) -> dict:
    """Reads results from a lab report, on this machine ("local") or with the person's AI provider ("ai"). Nothing is
    saved: the person reviews and corrects the rows, then saves them with POST /api/labs."""
    if method not in ("local", "ai"):
        raise HTTPException(400, "Unknown reading method.")
    data = await file.read(lab_report.MAX_BYTES + 1)
    content_type, filename = file.content_type or "", file.filename or ""
    try:
        if method == "ai":
            out = await ai.extract_lab_results(data, content_type, filename)
        else:
            out = await run_in_threadpool(lab_report.read, data, content_type, filename)
    except (ai.AIError, lab_report.ReportError) as exc:
        raise HTTPException(400, str(exc))
    for r in out["results"]:
        # Fill in a LOINC code from the common list when the reader didn't give one.
        codes = lab_catalog.loinc_codes(r["test"]) if not r.get("loinc") and isinstance(r.get("test"), str) else []
        if codes:
            r["loinc"] = codes[0]
    with db() as c:
        audit(c, "user", "labs.report_read", {"method": method, "read_as": out.get("read_as"), "results": len(out["results"])})
    return out
