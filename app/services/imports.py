"""
File imports (Apple Health export, FHIR bundles) with background job tracking.

Uploads are written to ``<data_dir>/uploads`` and processed on a thread of their own; the UI polls ``/api/imports/{job_id}`` for
progress. Uploaded files are deleted once processed, or at the next start if the server stopped first. A server that's stopping
asks them to stop between batches (stop_running). Jobs are also kept in the database, so a page can find one again and a restart
can say what became of it.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
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

from app.connectors import apple_health, fhir_normalize
from app.core import config, data_fixes
from app.core.db import db, dumps, new_id, read, rows_to_dicts
from app.services import ccda, sync
from app.store import activity, biometrics, connections, record_files, records

log = logging.getLogger("syntropy.imports")

BATCH = 5000
KEEP_FILE_HASHES = 50
ROUTE_POINTS_HELD = 200_000      # workouts waiting to be saved: a few hundred bytes of memory each
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
NO_ROOM = "There isn't enough disk space for this file. Free up some space and try again."
INTERRUPTED = "This import stopped when Syntropy Health was closed or restarted. What it had read so far is kept. Import the file again to finish: nothing is counted twice."
STOPPED_EARLY = ("This file stops partway or has a fault at {where}, so the import stopped there. What came before it was imported. "
                 "If records are missing, export from the Health app again, or copy the file again.")
CANT_READ = "This file couldn't be read as an Apple Health export ({detail}). Export it from the Health app again, or copy the file again."
_running: dict[threading.Thread, threading.Event] = {}
STOPPED = ("Syntropy Health was closed during this import, so it stopped part way. Import the file again: what was already "
           "added won't be added twice.")


class _Stopped(Exception):
    pass


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
                    if not isinstance(entry, dict):
                        continue
                    res, full_url = entry.get("resource"), entry.get("fullUrl")
                    if isinstance(res, dict) and isinstance(full_url, str) and not res.get("resourceType") == "Bundle":
                        res["_full_url"] = full_url      # normalization takes this back out, for references by fullUrl
                        if not res.get("id"):            # generated bundles may identify a resource only by its fullUrl
                            res["id"] = full_url.rstrip("/").split("/")[-1].removeprefix("urn:uuid:").removeprefix("urn:oid:")
                    collect(res)
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
    summary = ccda.read_upload(raw, filename) if ccda.looks_like(raw) else None
    resources = summary.resources if summary else parse_fhir_payload(raw, bundles)
    if not resources:
        raise ValueError("This file couldn't be read. Syntropy Health takes a health summary (C-CDA, CCD or XDM) or a FHIR file (.json or .ndjson).")
    digest = hashlib.sha256(raw).hexdigest()
    keys = _source_keys(resources, bundles)
    earlier = [c for c in connections.list_for_profile(profile_id, "import") if c["provider"] == "fhir_file"]
    same = [c for c in earlier if digest in ((c.get("metadata") or {}).get("file_hashes") or [])]
    if same:
        held = len(fhir_normalize.normalize_report(resources)[1])
        return {"connection_id": same[0]["id"], "sources": [{"connection_id": c["id"], "name": c["display_name"]} for c in same],
                "resources": len(resources), "records": held, "inserted": 0, "updated": 0, "unchanged": held, "kept_both": 0,
                "already_imported": True}
    reuse = next((c for c in earlier if keys and set(keys) & set((c.get("metadata") or {}).get("source_keys") or [])), None)
    known = (reuse.get("metadata") or {}) if reuse else {}
    result = sync.import_fhir_resources(profile_id, f"Imported · {filename}", resources, reuse=reuse, metadata={
        "file_hashes": [*(known.get("file_hashes") or []), digest][-KEEP_FILE_HASHES:],
        "source_keys": sorted({*keys, *(known.get("source_keys") or [])}),
    })
    if summary:
        _keep_originals(profile_id, result, summary)
        result["warnings"] = [*result.get("warnings", []), *summary.warnings]
    return result


def _keep_originals(profile_id: str, result: dict[str, Any], summary: ccda.Summary) -> None:
    """Each document as it came, on the record that stands for it in whichever source the import put it."""
    made = [s["connection_id"] for s in result.get("sources") or []]
    for original in summary.originals:
        with read() as conn:
            row = conn.execute(f"SELECT connection_id FROM clinical_records WHERE resource_type = 'DocumentReference' AND resource_id = ? "
                               f"AND connection_id IN ({','.join('?' * len(made))})", (original["resource_id"], *made)).fetchone()
        if row:
            record_files.save(profile_id, row["connection_id"], "DocumentReference", original["resource_id"], original["name"],
                              original["content_type"], original["data"])


def _start_thread(target: Any, *args: Any) -> None:
    """With the request's context, so what the import records is attributed to whoever started it. Not on the event loop's
    executor: shutting that down waits for whatever runs on it, so a long import kept a closed app running with no window."""
    stop = threading.Event()
    thread = threading.Thread(target=contextvars.copy_context().run, args=(target, *args, stop), daemon=True, name="import")
    with _jobs_lock:
        _running[thread] = stop
    thread.start()


async def start_apple_health_import(profile_id: str, path: Path, filename: str) -> str:
    job_id = await asyncio.to_thread(_new_job, "apple_health", filename, profile_id)
    _start_thread(_run_apple_health, job_id, profile_id, path, filename)
    return job_id


def stop_running(wait: float = 15.0) -> None:
    """The server is stopping: each import finishes its batch and stops, rather than dying in the middle of one."""
    with _jobs_lock:
        running = dict(_running)
    for stop in running.values():
        stop.set()
    deadline = time.monotonic() + wait
    for thread in running:
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            log.warning("An import is still finishing as the server stops")


def _where(exc: ParseError) -> str:
    position = getattr(exc, "position", None)
    return f"line {position[0]:,}" if position else "an unreadable point"


async def start_archive_import(profile_id: str, account_id: Optional[str], path: Path, filename: str) -> str:
    job_id = await asyncio.to_thread(_new_job, "archive", filename, profile_id)
    _start_thread(_run_archive, job_id, profile_id, account_id, path)
    return job_id


def _run_archive(job_id: str, profile_id: str, account_id: Optional[str], path: Path, stop: Optional[threading.Event] = None) -> None:
    from app.services import exports

    stop = stop or threading.Event()

    def progress(n: int) -> None:
        if stop.is_set():
            raise _Stopped
        _update(job_id, processed=n, inserted=n)

    _update(job_id, status="running")
    try:
        result = exports.import_archive(profile_id, account_id, path, progress)
        _update(job_id, status="completed", finished_at=time.time(), result=result)
    except _Stopped:
        log.warning("Stopped an archive import because the server is stopping")
        _update(job_id, status="error", finished_at=time.time(), error=STOPPED)
    except (ValueError, biometrics.SourceGone) as exc:
        _update(job_id, status="error", finished_at=time.time(), error=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("Archive import failed")
        _update(job_id, status="error", finished_at=time.time(), error=str(exc))
    finally:
        with _jobs_lock:
            _running.pop(threading.current_thread(), None)
        path.unlink(missing_ok=True)


def _run_apple_health(job_id: str, profile_id: str, path: Path, filename: str, stop: Optional[threading.Event] = None) -> None:
    stop = stop or threading.Event()
    _update(job_id, status="running")
    run: Optional[tuple[int, str]] = None
    export = None
    try:
        export = apple_health.ExportArchive(path)
        opener, fhir_resources = export.xml_opener(), export.clinical_records()
        result: dict[str, Any] = {}
        if opener is not None:
            conn = next((c for c in connections.list_for_profile(profile_id, "import")
                         if c["provider"] == "apple_health_export"), None) or connections.create(
                profile_id, "import", "apple_health_export", "Apple Health export", mode="live")
            run_id = connections.start_run(conn["id"], "import")
            run = (run_id, conn["id"])
            processed = inserted = updated = rejected = flagged = workouts = events = 0
            batch: list[dict[str, Any]] = []
            others: dict[str, list[dict[str, Any]]] = {"workout": [], "event": []}
            route_points = 0
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
                nonlocal workouts, events, route_points
                workouts += activity.insert_workouts(profile_id, conn["id"], others["workout"])
                events += activity.insert_events(profile_id, conn["id"], others["event"])
                others["workout"], others["event"] = [], []
                route_points = 0

            try:
                with opener() as stream:
                    try:
                        for kind, item in apple_health.iter_items(apple_health.CleanStream(stream), export.route_reader(), tally):
                            if stop.is_set():
                                raise _Stopped
                            if kind != "sample":
                                others[kind].append(item)
                                route_points += len(item.get("route") or ())
                                if len(others[kind]) >= 200 or route_points >= ROUTE_POINTS_HELD:
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
            except _Stopped:
                raise
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
            # One source per person, as the export above: a newer export updates what the last one brought in.
            earlier = next((c for c in connections.list_for_profile(profile_id, "import")
                            if c["provider"] == "apple_health_records"), None)
            result["clinical"] = sync.import_fhir_resources(profile_id, "Apple Health Records", fhir_resources,
                                                            provider="apple_health_records", reuse=earlier)
        if opener is None and not fhir_resources:
            raise ValueError("No export.xml or clinical records were found in the upload.")
        if export.skipped:
            result["skipped_files"] = export.skipped[:50]
        _update(job_id, status="completed", finished_at=time.time(), result=result)
    except biometrics.SourceGone as exc:
        _update(job_id, status="error", finished_at=time.time(), error=str(exc))
    except _Stopped:
        log.warning("Stopped an Apple Health import because the server is stopping")
        if run:
            connections.finish_run(run[0], run[1], "error", {}, STOPPED)
        _update(job_id, status="error", finished_at=time.time(), error=STOPPED)
    except Exception as exc:  # noqa: BLE001
        log.exception("Apple Health import failed")
        _update(job_id, status="error", finished_at=time.time(), error=str(exc))
    finally:
        if export:
            export.close()
        with _jobs_lock:
            _running.pop(threading.current_thread(), None)
        try:
            path.unlink()
        except OSError as exc:
            # A whole, unencrypted export: the next start tries again (sweep_uploads).
            log.warning("Couldn't delete an uploaded export: %s", exc)


def sweep_uploads() -> None:
    """At start-up, when nothing can be importing: what an import that was cut off, or couldn't delete, left behind."""
    folder = config.data_dir() / "uploads"
    for leftover in folder.iterdir() if folder.is_dir() else []:
        try:
            if leftover.is_file():
                leftover.unlink()
        except OSError as exc:
            log.warning("Couldn't delete an uploaded export: %s", exc)


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
