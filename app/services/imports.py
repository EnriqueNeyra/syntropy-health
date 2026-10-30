"""
File imports (Apple Health export, FHIR bundles) with background job tracking.

Uploads are written to ``<data_dir>/uploads`` and processed off the event loop; the UI polls ``/api/imports/{job_id}`` for
progress. Uploaded files are deleted once processed, or at the next start if the server stopped first. Jobs are also kept in
the database, so a page can find one again and a restart can say what became of it.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional
from xml.etree.ElementTree import ParseError

from fastapi import HTTPException

from app.connectors import apple_health
from app.core import config
from app.core.db import db, dumps, new_id, read, rows_to_dicts
from app.services import sync
from app.store import activity, biometrics, connections

log = logging.getLogger("syntropy.imports")

BATCH = 5000
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
NO_ROOM = "There isn't enough disk space for this file. Free up some space and try again."
INTERRUPTED = "This import stopped when Syntropy Health was closed or restarted. What it had read so far is kept. Import the file again to finish: nothing is counted twice."
STOPPED_EARLY = ("This file stops partway or has a fault at {where}, so the import stopped there. What came before it was imported. "
                 "If records are missing, export from the Health app again, or copy the file again.")
CANT_READ = "This file couldn't be read as an Apple Health export ({detail}). Export it from the Health app again, or copy the file again."


def upload_dir() -> Path:
    path = config.data_dir() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def check_room(size: Optional[int]) -> None:
    """Before an upload is copied: the file itself, then about as much again while the import runs."""
    if size and shutil.disk_usage(upload_dir()).free < size * 2:
        raise HTTPException(507, NO_ROOM)


def _stored(row: Any) -> dict[str, Any]:
    return rows_to_dicts([row], ["result_json"])[0]


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job:
            return dict(job)
    with read() as conn:
        row = conn.execute("SELECT * FROM import_jobs WHERE id = ?", (job_id,)).fetchone()
    return _stored(row) if row else None


def recent_jobs(profile_id: str, limit: int = 5) -> list[dict[str, Any]]:
    with read() as conn:
        rows = conn.execute("SELECT * FROM import_jobs WHERE profile_id = ? ORDER BY started_at DESC LIMIT ?", (profile_id, limit)).fetchall()
    return [_stored(r) for r in rows]


def _save_job(job: dict[str, Any]) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO import_jobs(id, kind, filename, profile_id, status, processed, inserted, started_at, finished_at, error, result_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET status = excluded.status, processed = excluded.processed, inserted = excluded.inserted,
                   finished_at = excluded.finished_at, error = excluded.error, result_json = excluded.result_json""",
            (job["id"], job["kind"], job["filename"], job["profile_id"], job["status"], job["processed"], job["inserted"],
             job["started_at"], job["finished_at"], job["error"], dumps(job["result"])))


def _update(job_id: str, **fields: Any) -> None:
    with _jobs_lock:
        _jobs[job_id].update(fields)
        job = dict(_jobs[job_id])
    try:
        _save_job(job)
    except sqlite3.Error:
        log.warning("Couldn't note how import %s is going", job_id)


def recover_at_start_up() -> None:
    """Nothing is importing when the server starts. A job that says otherwise was cut off, and its run with it, and the copy of
    the upload it was reading is of no more use: it can be gigabytes of someone's health data, and backups would include it."""
    now = time.time()
    with db() as conn:
        conn.execute("UPDATE import_jobs SET status = 'interrupted', error = ?, finished_at = ? WHERE status IN ('queued', 'running')",
                     (INTERRUPTED, now))
        conn.execute("UPDATE sync_runs SET status = 'error', finished_at = ?, error = ? WHERE trigger = 'import' AND status = 'running'",
                     (now, INTERRUPTED))
        conn.execute("DELETE FROM import_jobs WHERE started_at < ?", (now - 90 * 86400,))
    folder = config.data_dir() / "uploads"
    if folder.is_dir():
        for leftover in folder.iterdir():
            if leftover.is_file():
                leftover.unlink(missing_ok=True)


def _new_job(kind: str, filename: str, profile_id: str) -> str:
    job_id = new_id("job")
    with _jobs_lock:
        # Finished jobs are kept for a day, for the page polling them; then they go.
        for old in [k for k, j in _jobs.items() if j["finished_at"] and j["finished_at"] < time.time() - 86400]:
            del _jobs[old]
        _jobs[job_id] = {"id": job_id, "kind": kind, "filename": filename, "profile_id": profile_id,
                         "status": "queued", "processed": 0, "inserted": 0, "started_at": time.time(),
                         "finished_at": None, "error": None, "result": None}
        job = dict(_jobs[job_id])
    _save_job(job)
    return job_id


def parse_fhir_payload(raw: bytes) -> list[dict[str, Any]]:
    text = raw.decode("utf-8-sig", errors="replace").strip()
    resources: list[dict[str, Any]] = []

    def collect(obj: Any) -> None:
        if isinstance(obj, list):
            for o in obj:
                collect(o)
        elif isinstance(obj, dict):
            if obj.get("resourceType") == "Bundle":
                for entry in obj.get("entry") or []:
                    collect(entry.get("resource"))
            elif obj.get("resourceType"):
                resources.append(obj)

    try:
        collect(json.loads(text))
    except ValueError:
        for line in text.splitlines():  # NDJSON (bulk data export)
            line = line.strip()
            if line:
                try:
                    collect(json.loads(line))
                except ValueError:
                    continue
    return resources


def import_fhir_file(profile_id: str, filename: str, raw: bytes) -> dict[str, Any]:
    resources = parse_fhir_payload(raw)
    if not resources:
        raise ValueError("The file does not contain FHIR JSON resources.")
    return sync.import_fhir_resources(profile_id, f"Imported · {filename}", resources)


async def start_apple_health_import(profile_id: str, path: Path, filename: str) -> str:
    job_id = await asyncio.to_thread(_new_job, "apple_health", filename, profile_id)
    loop = asyncio.get_running_loop()
    # With the request's context, so what the import records is attributed to whoever started it.
    loop.run_in_executor(None, contextvars.copy_context().run, _run_apple_health, job_id, profile_id, path, filename)
    return job_id


def _where(exc: ParseError) -> str:
    position = getattr(exc, "position", None)
    return f"line {position[0]:,}" if position else "an unreadable point"


def _run_apple_health(job_id: str, profile_id: str, path: Path, filename: str) -> None:
    _update(job_id, status="running")
    try:
        opener, fhir_resources = apple_health.open_export(path)
        result: dict[str, Any] = {}
        if opener is not None:
            conn = next((c for c in connections.list_for_profile(profile_id, "import")
                         if c["provider"] == "apple_health_export"), None) or connections.create(
                profile_id, "import", "apple_health_export", "Apple Health export", mode="live")
            run_id = connections.start_run(conn["id"], "import")
            processed = inserted = updated = rejected = flagged = workouts = events = 0
            batch: list[dict[str, Any]] = []
            others: dict[str, list[dict[str, Any]]] = {"workout": [], "event": []}
            tally: dict[str, dict[str, int]] = {"imported": {}, "skipped": {}}
            stopped: Optional[str] = None

            def flush_samples() -> None:
                nonlocal batch, processed, inserted, updated, rejected, flagged
                if batch:
                    r = biometrics.insert_samples(profile_id, conn["id"], batch)
                    processed += len(batch)
                    inserted, updated = inserted + r["inserted"], updated + r["updated"]
                    rejected, flagged = rejected + r["rejected"], flagged + r["flagged"]
                    batch = []
                    _update(job_id, processed=processed, inserted=inserted)

            def flush_others() -> None:
                nonlocal workouts, events
                workouts += activity.insert_workouts(profile_id, conn["id"], others["workout"])
                events += activity.insert_events(profile_id, conn["id"], others["event"])
                others["workout"], others["event"] = [], []

            try:
                with opener() as stream:
                    try:
                        for kind, item in apple_health.iter_items(apple_health.CleanStream(stream), apple_health.zip_reader(path), tally):
                            if kind != "sample":
                                others[kind].append(item)
                                if len(others[kind]) >= 200:
                                    flush_others()
                                continue
                            batch.append(item)
                            if len(batch) >= BATCH:
                                flush_samples()
                    except ParseError as exc:
                        # What came before the fault is good. The health records beside it in the zip still get imported.
                        stopped = _where(exc)
                        log.warning("An Apple Health export stopped at %s", stopped)
                flush_samples()
                flush_others()
            except Exception as exc:
                connections.finish_run(run_id, conn["id"], "error", {"samples": processed, "inserted": inserted}, str(exc))
                raise
            stats = {"samples": processed, "inserted": inserted, "updated": updated, "rejected": rejected, "flagged": flagged,
                     "workouts": workouts, "events": events, "types": tally}
            read_something = processed or workouts or events
            if stopped and not read_something:
                if not fhir_resources:
                    connections.finish_run(run_id, conn["id"], "error", stats, CANT_READ.format(detail=stopped))
                    raise ValueError(CANT_READ.format(detail=stopped))
                connections.finish_run(run_id, conn["id"], "error", stats, CANT_READ.format(detail=stopped))
            elif stopped:
                connections.finish_run(run_id, conn["id"], "partial", {**stats, "warnings": [STOPPED_EARLY.format(where=stopped)]})
                result["warning"] = STOPPED_EARLY.format(where=stopped)
                result["stopped_early"] = True
            else:
                connections.finish_run(run_id, conn["id"], "success", stats)
            result.update({**stats, "connection_id": conn["id"]})
            _update(job_id, processed=processed, inserted=inserted)
        if fhir_resources:
            result["clinical"] = sync.import_fhir_resources(profile_id, "Apple Health Records", fhir_resources,
                                                            provider="apple_health_records")
        if opener is None and not fhir_resources:
            raise ValueError("No export.xml or clinical records were found in the upload.")
        _update(job_id, status="completed", finished_at=time.time(), result=result)
    except Exception as exc:  # noqa: BLE001
        log.exception("Apple Health import failed")
        _update(job_id, status="error", finished_at=time.time(), error=str(exc))
    finally:
        try:
            path.unlink()
        except OSError:
            pass
