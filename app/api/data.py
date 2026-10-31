"""Imports (Apple Health, FHIR files, archives) and exports (FHIR, CSV, archive)."""

from __future__ import annotations

import errno
import json
import re
import shutil
import time
import unicodedata
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse

from app.api.deps import check_access, resolve_profile
from app.core import auth, context
from app.core.db import audit, db, new_id
from app.services import exports, imports
from app.store import accounts, biometrics, profiles, records

router = APIRouter(prefix="/api", tags=["data"], dependencies=[Depends(auth.require_user)])

MAX_FHIR_UPLOAD = 200 * 1024 * 1024


@router.post("/imports/fhir")
async def import_fhir(file: UploadFile = File(...), profile_id: Optional[str] = Form(None)) -> dict:
    prof = resolve_profile(profile_id)
    raw = await file.read(MAX_FHIR_UPLOAD + 1)
    if len(raw) > MAX_FHIR_UPLOAD:
        raise HTTPException(413, "File is too large (max 200 MB).")
    try:
        # Parsing and storing a large file takes a while: in a thread, so the server keeps answering meanwhile.
        return await run_in_threadpool(imports.import_fhir_file, prof["id"], file.filename or "upload.json", raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/imports/apple-health")
async def import_apple_health(file: UploadFile = File(...), profile_id: Optional[str] = Form(None)) -> dict:
    prof = resolve_profile(profile_id)
    dest = imports.upload_dir() / f"{new_id('upload')}{Path(file.filename or 'export.zip').suffix or '.zip'}"
    imports.check_room(file.size)
    try:
        with open(dest, "wb") as fh:
            while chunk := await file.read(1024 * 1024):
                fh.write(chunk)
    except BaseException as exc:
        dest.unlink(missing_ok=True)
        if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise HTTPException(507, imports.NO_ROOM) from exc
        raise
    job_id = await imports.start_apple_health_import(prof["id"], dest, file.filename or "export.zip")
    return {"job_id": job_id}


@router.get("/imports")
def list_imports(profile: Optional[str] = None) -> dict:
    """This person's recent Apple Health imports, so a page can find one again after being left or after a restart."""
    prof = resolve_profile(profile)
    return {"imports": imports.recent_jobs(prof["id"])}


@router.post("/imports/archive")
async def import_archive(file: UploadFile = File(...), profile_id: Optional[str] = Form(None)) -> dict:
    prof = resolve_profile(profile_id, "manage")
    principal = context.principal()
    dest = imports.upload_dir() / f"{new_id('upload')}.zip"
    imports.check_room(file.size)
    try:
        with open(dest, "wb") as fh:
            while chunk := await file.read(1024 * 1024):
                fh.write(chunk)
    except BaseException as exc:
        dest.unlink(missing_ok=True)
        if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise HTTPException(507, imports.NO_ROOM) from exc
        raise
    return {"job_id": await imports.start_archive_import(prof["id"], principal.account_id if principal else None, dest, file.filename or "archive.zip")}


@router.get("/imports/{job_id}")
def import_status(job_id: str) -> dict:
    job = imports.get_job(job_id)
    if not job:
        raise HTTPException(404, "Import job not found")
    check_access(job["profile_id"])
    return job


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _person(prof: dict[str, Any]) -> str:
    """The person's name for a file name. Only safe characters go in the header: a name in another script failed to
    encode (a 500), and a quote broke it."""
    ascii_name = unicodedata.normalize("NFKD", prof["name"]).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-") or "record"


@router.get("/export/fhir")
def export_fhir(profile: Optional[str] = None) -> StreamingResponse:
    prof = resolve_profile(profile)
    with db() as conn:
        audit(conn, "user", "export.fhir", None, prof["id"])
    filename = f"syntropy-{_person(prof)}-fhir-{_stamp()}.json"
    return StreamingResponse(exports.bundle_stream(prof["id"]), media_type="application/fhir+json",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/export/ips")
def export_patient_summary(profile: Optional[str] = None) -> Response:
    """An International Patient Summary, for a clinician anywhere."""
    prof = resolve_profile(profile)
    document = exports.patient_summary(prof)
    with db() as conn:
        audit(conn, "user", "export.ips", None, prof["id"])
    return Response(json.dumps(document, ensure_ascii=False), media_type="application/fhir+json",
                    headers={"Content-Disposition": f'attachment; filename="syntropy-{_person(prof)}-summary-{_stamp()}.json"'})


@router.get("/export/csv")
def export_csv(kind: str = "records", profile: Optional[str] = None) -> StreamingResponse:
    prof = resolve_profile(profile)
    if kind not in exports.CSV_KINDS:
        raise HTTPException(400, "kind must be 'records', 'daily', 'workouts' or 'events'")
    data = exports.csv_bytes(kind, prof["id"])
    with db() as conn:
        audit(conn, "user", f"export.csv.{kind}", None, prof["id"])
    return StreamingResponse(iter([data]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="syntropy-{_person(prof)}-{kind}-{_stamp()}.csv"'})


@router.get("/export/archive")
def export_archive(background: BackgroundTasks, profile: Optional[str] = None) -> FileResponse:
    """Everything kept about one person, for whoever manages them. Their chats are per account: this account's come along."""
    prof = resolve_profile(profile, "manage")
    principal = context.principal()
    path = exports.build_archive(prof, principal.account_id if principal else None)
    with db() as conn:
        audit(conn, "user", "export.archive", None, prof["id"])
    background.add_task(shutil.rmtree, path.parent, ignore_errors=True)
    return FileResponse(path, media_type="application/zip", filename=f"syntropy-{_person(prof)}-archive-{_stamp()}.zip")


@router.delete("/profiles/{profile_id}/data")
def wipe_profile_data(profile_id: str) -> dict:
    """Deletes everything kept about a person but keeps the person. Takes a while with millions of samples, a little at
    a time, while other requests carry on."""
    prof = resolve_profile(profile_id)
    principal = context.principal()
    auth.refuse_if_limited(principal)
    account = accounts.for_profile(prof["id"])
    if principal and account and account["id"] != principal.account_id:
        raise HTTPException(403, f"Only {prof['name']} can delete all of their own data.")
    profiles.delete_all_data(prof["id"])
    return {"ok": True}


@router.get("/report")
def visit_report(profile: Optional[str] = None) -> dict:
    """Everything the printable visit summary needs, in one call."""
    prof = resolve_profile(profile)
    summary = records.summary(prof["id"])
    return {"profile": prof, "generated_at": time.time(), **summary,
            # A summary for clinicians never shows demo data as if it were measured.
            "biometrics": [h for h in biometrics.headline(prof["id"])
                           if "(simulated)" not in ((h.get("latest") or {}).get("source") or "")],
            "care_team": records.list_records(prof["id"], "care_team", limit=20)["items"],
            "coverage": records.list_records(prof["id"], "coverage", limit=10)["items"]}
