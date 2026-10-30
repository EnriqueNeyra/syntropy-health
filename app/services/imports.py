"""
File imports (Apple Health export, FHIR bundles) with background job tracking.

Uploads are written to ``<data_dir>/uploads`` and processed off the event loop; the
UI polls ``/api/imports/{job_id}`` for progress. Uploaded files are deleted once
processed.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.connectors import apple_health, fhir_normalize
from app.core import config, data_fixes
from app.core.db import new_id
from app.services import sync
from app.store import activity, biometrics, connections, records

log = logging.getLogger("syntropy.imports")

BATCH = 5000
KEEP_FILE_HASHES = 50
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
        # Finished jobs are kept for a day, for the page polling them; then they go.
        for old in [k for k, j in _jobs.items() if j["finished_at"] and j["finished_at"] < time.time() - 86400]:
            del _jobs[old]
        _jobs[job_id] = {"id": job_id, "kind": kind, "filename": filename, "profile_id": profile_id,
                         "status": "queued", "processed": 0, "inserted": 0, "started_at": time.time(),
                         "finished_at": None, "error": None, "result": None}
    return job_id


def parse_fhir_payload(raw: bytes, bundles: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    """The resources in a file. ``bundles`` collects the bundles they came in, which can say where the file is from."""
    text = raw.decode("utf-8-sig", errors="replace").strip()
    resources: list[dict[str, Any]] = []

    def collect(obj: Any) -> None:
        if isinstance(obj, list):
            for o in obj:
                collect(o)
        elif isinstance(obj, dict):
            if obj.get("resourceType") == "Bundle":
                if bundles is not None:
                    bundles.append(obj)
                for entry in obj.get("entry") or []:
                    if isinstance(entry, dict):
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


def _source_keys(resources: list[dict[str, Any]], bundles: list[dict[str, Any]]) -> list[str]:
    """What says a file comes from the same place, about the same person, as an earlier one: a patient identifier with
    its issuing system, or the bundle's own identifier or source, each together with the birth date. Never a bare id or a
    file name: ids like "1" collide between hospitals, and one person's file would overwrite another's records."""
    patients = {r.get("id"): r for r in resources if r.get("resourceType") == "Patient"}
    if len(patients) != 1:
        return []
    patient = next(iter(patients.values()))
    born = patient.get("birthDate")
    if not born:
        return []
    keys = [fhir_normalize._system_value(i) for i in patient.get("identifier") or []]
    for bundle in bundles[:1]:
        keys.append(fhir_normalize._system_value(bundle.get("identifier")))
        source = (bundle.get("meta") or {}).get("source") if isinstance(bundle.get("meta"), dict) else None
        if source:
            keys.append(f"source|{source}")
    return sorted(hashlib.sha256(f"{k}|{born}".encode()).hexdigest()[:32] for k in keys if k)


def import_fhir_file(profile_id: str, filename: str, raw: bytes) -> dict[str, Any]:
    bundles: list[dict[str, Any]] = []
    resources = parse_fhir_payload(raw, bundles)
    if not resources:
        raise ValueError("The file does not contain FHIR JSON resources.")
    digest = hashlib.sha256(raw).hexdigest()
    keys = _source_keys(resources, bundles)
    earlier = [c for c in connections.list_for_profile(profile_id, "import") if c["provider"] == "fhir_file"]
    for conn in earlier:
        if digest in ((conn.get("metadata") or {}).get("file_hashes") or []):
            return {"connection_id": conn["id"], "resources": len(resources), "records": conn.get("record_count") or 0,
                    "inserted": 0, "updated": 0, "unchanged": True}
    reuse = next((c for c in earlier if keys and set(keys) & set((c.get("metadata") or {}).get("source_keys") or [])), None)
    known = (reuse.get("metadata") or {}) if reuse else {}
    return sync.import_fhir_resources(profile_id, f"Imported · {filename}", resources, reuse=reuse, metadata={
        "file_hashes": [*(known.get("file_hashes") or []), digest][-KEEP_FILE_HASHES:],
        "source_keys": sorted({*keys, *(known.get("source_keys") or [])}),
    })


def start_apple_health_import(profile_id: str, path: Path, filename: str) -> str:
    job_id = _new_job("apple_health", filename, profile_id)
    loop = asyncio.get_running_loop()
    # With the request's context, so what the import records is attributed to whoever started it.
    loop.run_in_executor(None, contextvars.copy_context().run, _run_apple_health, job_id, profile_id, path, filename)
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
            # One source per person, as the export above: a newer export updates what the last one brought in.
            earlier = next((c for c in connections.list_for_profile(profile_id, "import")
                            if c["provider"] == "apple_health_records"), None)
            result["clinical"] = sync.import_fhir_resources(profile_id, "Apple Health Records", fhir_resources,
                                                            provider="apple_health_records", reuse=earlier)
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


# ---------------------------------------------------------------------------
# Corrections to what earlier versions stored
# ---------------------------------------------------------------------------

def _fill_missing_dedup_keys(conn: Any, cursor: Any, size: int) -> Any:
    """Visits, notes, care teams and plans had no way of being recognised as the same one from another file or source."""
    rows = conn.execute("SELECT rowid, resource_type, raw_json FROM clinical_records WHERE rowid > ? AND dedup_key IS NULL "
                        "AND category IN ('encounters', 'notes', 'care_team', 'care_plans') ORDER BY rowid LIMIT ?",
                        (cursor or 0, size)).fetchall()
    for row in rows:
        normalizer = fhir_normalize.NORMALIZERS.get(row["resource_type"])
        try:
            rec = normalizer(json.loads(row["raw_json"])) if normalizer and row["raw_json"] else None
        except Exception:  # noqa: BLE001 - one odd old row stays as it was
            rec = None
        if rec and rec.get("dedup_key"):
            conn.execute("UPDATE clinical_records SET dedup_key = ? WHERE rowid = ?", (rec["dedup_key"], row["rowid"]))
    return rows[-1]["rowid"] if rows else None


def _fold_apple_health_records(conn: Any, cursor: Any, size: int) -> Any:
    """Every export used to make another "Apple Health Records" source with another copy of everything. Folds them into
    the newest for each person. A record only an older export had stays."""
    spare = conn.execute(
        """SELECT c.id, c.profile_id, (SELECT k.id FROM connections k WHERE k.profile_id = c.profile_id AND k.kind = 'import'
                  AND k.provider = 'apple_health_records' ORDER BY k.created_at DESC, k.id DESC LIMIT 1) AS keeper
           FROM connections c WHERE c.kind = 'import' AND c.provider = 'apple_health_records' ORDER BY c.created_at, c.id""").fetchall()
    spare = next((s for s in spare if s["id"] != s["keeper"]), None)
    if spare is None:
        return None
    rows = conn.execute("SELECT id, resource_type, resource_id FROM clinical_records WHERE connection_id = ? LIMIT ?",
                        (spare["id"], size)).fetchall()
    for row in rows:
        moved = records.record_id(spare["keeper"], row["resource_type"], row["resource_id"])
        if conn.execute("SELECT 1 FROM clinical_records WHERE id = ?", (moved,)).fetchone():
            conn.execute("DELETE FROM clinical_records WHERE id = ?", (row["id"],))
        else:
            conn.execute("UPDATE clinical_records SET id = ?, connection_id = ? WHERE id = ?", (moved, spare["keeper"], row["id"]))
    if not rows:
        conn.execute("DELETE FROM patient_identities WHERE connection_id = ?", (spare["id"],))
        conn.execute("DELETE FROM connections WHERE id = ?", (spare["id"],))
    return (cursor or 0) + 1


data_fixes.register("imports-fold-apple-health-records", _fold_apple_health_records)
data_fixes.register("records-dedup-keys-visits-notes-teams-plans", _fill_missing_dedup_keys)
