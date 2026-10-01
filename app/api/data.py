"""Imports (Apple Health, FHIR files) and exports (FHIR bundle, CSV, full backup)."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.api.deps import check_access, resolve_profile
from app.core import auth, config
from app.core.db import audit, db, new_id
from app.services import imports
from app.store import activity, biometrics, records, sample_counts

router = APIRouter(prefix="/api", tags=["data"], dependencies=[Depends(auth.require_user)])

MAX_FHIR_UPLOAD = 200 * 1024 * 1024


@router.post("/imports/fhir")
async def import_fhir(file: UploadFile = File(...), profile_id: Optional[str] = Form(None)) -> dict:
    prof = resolve_profile(profile_id)
    raw = await file.read(MAX_FHIR_UPLOAD + 1)
    if len(raw) > MAX_FHIR_UPLOAD:
        raise HTTPException(413, "File is too large (max 200 MB).")
    try:
        return imports.import_fhir_file(prof["id"], file.filename or "upload.json", raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/imports/apple-health")
async def import_apple_health(file: UploadFile = File(...), profile_id: Optional[str] = Form(None)) -> dict:
    prof = resolve_profile(profile_id)
    dest = imports.upload_dir() / f"{new_id('upload')}{Path(file.filename or 'export.zip').suffix or '.zip'}"
    with open(dest, "wb") as fh:
        while chunk := await file.read(1024 * 1024):
            fh.write(chunk)
    job_id = imports.start_apple_health_import(prof["id"], dest, file.filename or "export.zip")
    return {"job_id": job_id}


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


@router.get("/export/fhir")
def export_fhir(profile: Optional[str] = None) -> StreamingResponse:
    prof = resolve_profile(profile)

    def stream() -> Iterator[bytes]:
        yield json.dumps({"resourceType": "Bundle", "type": "collection",
                          "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "meta": {"tag": [{"system": "urn:syntropy-health", "code": "export"}]}})[:-1].encode()
        yield b',"entry":['
        first = True
        for res in records.raw_resources(prof["id"]):
            yield (b"" if first else b",") + json.dumps({"resource": res}).encode()
            first = False
        yield b"]}"

    with db() as conn:
        audit(conn, "user", "export.fhir", None, prof["id"])
    filename = f"syntropy-{prof['name'].lower().replace(' ', '-')}-fhir-{_stamp()}.json"
    return StreamingResponse(stream(), media_type="application/fhir+json",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


class _SafeWriter:
    """csv.writer that neutralizes spreadsheet formulas in text cells (CSV injection): a leading
    = + - @ tab or CR is prefixed with an apostrophe. Numbers are written unchanged."""

    def __init__(self, buf: io.StringIO) -> None:
        self._w = csv.writer(buf)

    @staticmethod
    def _cell(v: Any) -> Any:
        if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + v
        return v

    def writerow(self, row: list[Any]) -> None:
        self._w.writerow([self._cell(v) for v in row])


@router.get("/export/csv")
def export_csv(kind: str = "records", profile: Optional[str] = None) -> StreamingResponse:
    prof = resolve_profile(profile)
    buf = io.StringIO()
    writer = _SafeWriter(buf)
    if kind == "records":
        writer.writerow(["category", "title", "code_system", "code", "effective_at", "status", "value", "unit",
                         "value_normalized", "unit_normalized", "reference_low", "reference_high", "interpretation",
                         "source", "narrative"])
        for r in records.list_records(prof["id"], dedupe=False, limit=10**7)["items"]:
            writer.writerow([r["category"], r["title"], r.get("code_system"), r.get("code"), r.get("effective_at"),
                             r.get("status"), r.get("value_num") if r.get("value_num") is not None else r.get("value_text"),
                             r.get("unit"), r.get("value_norm"), r.get("unit_norm"), r.get("ref_low"), r.get("ref_high"),
                             r.get("interpretation"), r.get("source_name"), (r.get("narrative") or "")[:2000]])
    elif kind == "daily":
        writer.writerow(["day", "metric", "source", "value", "unit", "min", "max", "samples"])
        with db() as conn:
            for row in conn.execute("SELECT * FROM biometric_daily WHERE profile_id = ? ORDER BY day, metric_type",
                                    (prof["id"],)):
                writer.writerow([row["day"], row["metric_type"], row["source_name"], row["value"], row["unit"],
                                 row["min_value"], row["max_value"], row["sample_count"]])
    elif kind == "workouts":
        writer.writerow(["start", "end", "name", "duration_min", "distance_km", "active_energy_kcal", "total_energy_kcal",
                         "avg_hr", "max_hr", "elevation_gain_m", "indoor", "source", "id"])
        for w in activity.list_workouts(prof["id"], None, 10**7, dedupe=False):   # every recording, as stored
            writer.writerow([w["start_date"], w["end_date"], w["name"],
                             round(w["duration_s"] / 60, 2) if w["duration_s"] is not None else None,
                             round(w["distance_m"] / 1000, 3) if w["distance_m"] is not None else None,
                             *[round(w[k], 1) if w[k] is not None else None
                               for k in ("active_energy_kcal", "total_energy_kcal", "avg_hr", "max_hr", "elevation_ascent_m")],
                             w["indoor"], w["source_name"], w["id"]])
    elif kind == "events":
        writer.writerow(["start", "end", "category", "type", "name", "value", "label", "source"])
        for e in reversed(activity.list_events(prof["id"], None, limit=10**7)):
            writer.writerow([e["start_date"], e["end_date"], e["category"], e["event_type"], e["name"], e["value"],
                             e["value_label"], e["source_name"]])
    else:
        raise HTTPException(400, "kind must be 'records', 'daily', 'workouts' or 'events'")
    with db() as conn:
        audit(conn, "user", f"export.csv.{kind}", None, prof["id"])
    data = buf.getvalue().encode()
    return StreamingResponse(iter([data]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="syntropy-{kind}-{_stamp()}.csv"'})


@router.get("/export/backup", dependencies=[Depends(auth.require_owner)])
def export_backup(background: BackgroundTasks) -> FileResponse:
    """Consistent online snapshot of the whole database (includes encrypted tokens; keep it safe). It holds everyone's
    data, so it's for owners (who can copy the database file anyway)."""
    tmp = Path(tempfile.mkdtemp()) / f"syntropy-backup-{_stamp()}.db"
    src = sqlite3.connect(str(config.db_path()))
    dst = sqlite3.connect(str(tmp))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    with db() as conn:
        audit(conn, "user", "export.backup")
    background.add_task(lambda: tmp.unlink(missing_ok=True))
    return FileResponse(tmp, media_type="application/vnd.sqlite3", filename=tmp.name)


@router.delete("/profiles/{profile_id}/data")
async def wipe_profile_data(profile_id: str) -> dict:
    """Deletes all records, samples and connections for a profile but keeps the profile itself."""
    prof = resolve_profile(profile_id)
    with db() as conn:
        conn.execute("DELETE FROM clinical_records WHERE profile_id = ?", (prof["id"],))
        conn.execute("DELETE FROM patient_identities WHERE profile_id = ?", (prof["id"],))
        conn.execute("DELETE FROM biometric_samples WHERE profile_id = ?", (prof["id"],))
        conn.execute("DELETE FROM biometric_daily WHERE profile_id = ?", (prof["id"],))
        conn.execute("DELETE FROM sync_batches WHERE profile_id = ?", (prof["id"],))
        conn.execute("DELETE FROM workouts WHERE profile_id = ?", (prof["id"],))
        conn.execute("DELETE FROM health_events WHERE profile_id = ?", (prof["id"],))
        conn.execute("DELETE FROM chats WHERE profile_id = ?", (prof["id"],))
        # The paired phones' Syntropy tabs are signed out along with their tokens.
        conn.execute("DELETE FROM user_sessions WHERE device_id IN (SELECT id FROM devices WHERE profile_id = ?)", (prof["id"],))
        conn.execute("DELETE FROM devices WHERE profile_id = ?", (prof["id"],))
        conn.execute("DELETE FROM connections WHERE profile_id = ?", (prof["id"],))
        audit(conn, "user", "profile.data_deleted", None, prof["id"])
    sample_counts.forget()
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
