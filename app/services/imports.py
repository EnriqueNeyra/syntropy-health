"""
File imports (Apple Health export, FHIR bundles) with background job tracking.

Uploads are written to ``<data_dir>/uploads`` and processed off the event loop; the
UI polls ``/api/imports/{job_id}`` for progress. Uploaded files are deleted once
processed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.connectors import apple_health
from app.core import config
from app.core.db import new_id
from app.services import sync
from app.store import activity, biometrics, connections

log = logging.getLogger("syntropy.imports")

BATCH = 5000
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def upload_dir() -> Path:
    path = config.data_dir() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def _update(job_id: str, **fields: Any) -> None:
    with _jobs_lock:
        _jobs[job_id].update(fields)


def _new_job(kind: str, filename: str, profile_id: str) -> str:
    job_id = new_id("job")
    with _jobs_lock:
        _jobs[job_id] = {"id": job_id, "kind": kind, "filename": filename, "profile_id": profile_id,
                         "status": "queued", "processed": 0, "inserted": 0, "started_at": time.time(),
                         "finished_at": None, "error": None, "result": None}
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


def start_apple_health_import(profile_id: str, path: Path, filename: str) -> str:
    job_id = _new_job("apple_health", filename, profile_id)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_apple_health, job_id, profile_id, path, filename)
    return job_id


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
            processed = inserted = workouts = events = 0
            batch: list[dict[str, Any]] = []
            others: dict[str, list[dict[str, Any]]] = {"workout": [], "event": []}

            def flush_others() -> None:
                nonlocal workouts, events
                workouts += activity.insert_workouts(profile_id, conn["id"], others["workout"])
                events += activity.insert_events(profile_id, conn["id"], others["event"])
                others["workout"], others["event"] = [], []

            with opener() as stream:
                for kind, item in apple_health.iter_items(stream, apple_health.zip_reader(path)):
                    if kind != "sample":
                        others[kind].append(item)
                        if len(others[kind]) >= 200:
                            flush_others()
                        continue
                    batch.append(item)
                    if len(batch) >= BATCH:
                        r = biometrics.insert_samples(profile_id, conn["id"], batch)
                        processed += len(batch)
                        inserted += r["inserted"]
                        batch = []
                        _update(job_id, processed=processed, inserted=inserted)
            if batch:
                r = biometrics.insert_samples(profile_id, conn["id"], batch)
                processed += len(batch)
                inserted += r["inserted"]
            flush_others()
            stats = {"samples": processed, "inserted": inserted, "workouts": workouts, "events": events}
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
