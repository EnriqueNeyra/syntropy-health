"""
Sync orchestration.

``sync_connection`` pulls fresh data for one connection, whatever its kind, and
records a ``sync_runs`` entry. Connections are locked individually so a scheduled
sync and a manual "Sync now" never run concurrently for the same source.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.connectors import fhir_normalize, google_health, oura, smart, whoop
from app.connectors import wearable_common as wc
from app.connectors.smart import SmartError
from app.core.db import audit, db, read
from app.store import activity, biometrics, connections, records

log = logging.getLogger("syntropy.sync")

_locks: dict[str, asyncio.Lock] = {}
MAX_NOTE_FETCHES = 60
WEARABLE_INITIAL_DAYS = 90
WEARABLE_OVERLAP_DAYS = 3


def _lock(connection_id: str) -> asyncio.Lock:
    lock = _locks.get(connection_id)
    if lock is None:
        lock = _locks[connection_id] = asyncio.Lock()
    return lock


def is_running(connection_id: str) -> bool:
    lock = _locks.get(connection_id)
    return bool(lock and lock.locked())


async def sync_connection(connection_id: str, trigger: str = "manual") -> dict[str, Any]:
    conn = connections.get(connection_id, include_credentials=True)
    if not conn:
        raise LookupError("Connection not found")
    if conn["status"] == "disconnected":
        return {"status": "skipped", "reason": "disconnected"}
    if conn["kind"] in ("device", "import", "manual"):
        return {"status": "skipped", "reason": "This source pushes data to Syntropy; there is nothing to pull."}
    if conn.get("access_ended"):
        return {"status": "skipped", "sign_in": True,
                "reason": f"Sign in to {conn['display_name']} again to bring in new records."}
    if conn.get("credentials_unreadable"):
        return {"status": "skipped", "sign_in": True,
                "reason": f"The saved sign-in for {conn['display_name']} can't be read, because the encryption key has changed. "
                          "Put the original key back, or sign in again."}

    lock = _lock(connection_id)
    if lock.locked():
        return {"status": "skipped", "reason": "A sync for this source is already running."}
    async with lock:
        started = time.time()
        run_id = None
        try:
            run_id = await asyncio.to_thread(connections.start_run, connection_id, trigger)
            if conn["kind"] == "ehr":
                stats = await _sync_ehr(conn)
            elif conn["kind"] == "wearable":
                stats = await _sync_wearable(conn)
            else:
                raise SmartError(f"Unsupported connection kind '{conn['kind']}'")
            stats["duration_s"] = round(time.time() - started, 2)
            status = "partial" if stats.get("warnings") else "success"

            def finish() -> None:
                connections.finish_run(run_id, connection_id, status, stats)
                with db() as c:
                    audit(c, "scheduler" if trigger == "scheduled" else "user", "sync.completed",
                          {"connection": connection_id, "status": status, "trigger": trigger}, conn["profile_id"])
            await asyncio.to_thread(finish)
            return {"status": status, **stats}
        except SmartError as exc:
            stats = {"failure_kind": exc.kind, "duration_s": round(time.time() - started, 2)}
            await _record_failure(run_id, connection_id, stats, str(exc))
            return {"status": "error", "error": str(exc), "kind": exc.kind}
        except Exception as exc:  # noqa: BLE001 - surface any failure on the connection card
            log.exception("Sync failed for %s", connection_id)
            stats = {"failure_kind": "error", "duration_s": round(time.time() - started, 2)}
            await _record_failure(run_id, connection_id, stats, f"Unexpected error: {exc}")
            return {"status": "error", "error": str(exc), "kind": "error"}


async def _record_failure(run_id: Optional[int], connection_id: str, stats: dict[str, Any], error: str) -> None:
    """A database too busy to start or finish the run may be too busy to note that as well. The next sync tries again."""
    try:
        if run_id is None:
            run_id = await asyncio.to_thread(connections.start_run, connection_id, "failed")
        await asyncio.to_thread(connections.finish_run, run_id, connection_id, "error", stats, error)
    except sqlite3.Error:
        log.exception("Couldn't record a failed sync for %s", connection_id)


# ---------------------------------------------------------------------------
# EHR (SMART on FHIR)
# ---------------------------------------------------------------------------

async def _sync_ehr(conn: dict[str, Any]) -> dict[str, Any]:
    creds = conn.get("credentials") or {}
    if not creds.get("access_token"):
        raise SmartError("This connection has no access token. Reconnect to continue.", "auth")
    simulated = conn["mode"] == "simulated"
    if wc.needs_refresh(creds):
        creds = await smart.refresh(creds, simulated)
        await asyncio.to_thread(connections.update, conn["id"], credentials=creds)

    client = smart.FhirClient(conn["fhir_base_url"], creds, conn["patient_ref"], simulated)
    fetched = await client.fetch_all()
    if client.refreshed:
        await asyncio.to_thread(connections.update, conn["id"], credentials=client.credentials)

    # Normalizing and storing thousands of records is plain CPU and SQLite work: it runs in a thread so the server
    # keeps answering (the page, the phone) meanwhile.
    patient, recs, problems = await asyncio.to_thread(fhir_normalize.normalize_report, [fetched.patient] + fetched.resources)  # type: ignore[list-item]
    notes_fetched = await _fetch_note_bodies(conn, client, recs, simulated)

    def store() -> tuple[dict[str, Any], int]:
        if patient:
            records.upsert_identity(conn["id"], conn["profile_id"], patient)
        write = records.upsert_records(conn["profile_id"], conn["id"], conn["display_name"], recs)
        seen = {f"{r['resource_type']}/{r['resource_id']}" for r in recs}
        return write, records.prune_missing(conn["id"], fetched.complete_types, seen)
    write, pruned = await asyncio.to_thread(store)

    warnings = [f"{k}: {v}" for k, v in fetched.status.items() if v not in ("ok", "empty", "not-granted")]
    by_category: dict[str, int] = {}
    for r in recs:
        by_category[r["category"]] = by_category.get(r["category"], 0) + 1
    return {
        "resources": len(fetched.resources) + 1, "records": len(recs), "inserted": write["inserted"],
        "updated": write["updated"], "pruned": pruned, "notes_fetched": notes_fetched,
        # Counted, not a warning: one odd resource in a chart would otherwise mark every sync "Partly synced" for good.
        "not_read": _count_unread(problems), "not_read_by_type": _unread_by_type(problems),
        "by_category": by_category, "queries": fetched.status, "warnings": warnings,
    }


def _unread_by_type(problems: dict[str, dict[str, int]]) -> dict[str, int]:
    """Resources that came in and couldn't be kept, so losing them is never silent."""
    counts: dict[str, int] = {}
    for by_type in problems.values():
        for rtype, n in by_type.items():
            counts[rtype] = counts.get(rtype, 0) + n
    return dict(sorted(counts.items()))


def _count_unread(problems: dict[str, dict[str, int]]) -> int:
    return sum(_unread_by_type(problems).values())


async def _fetch_note_bodies(conn: dict[str, Any], client: smart.FhirClient, recs: list[dict[str, Any]], simulated: bool) -> int:
    """Clinical notes and reports often reference their content by URL (Binary). Fetch the bodies we don't have yet, and again when
    the source changed the note: an amended note under its old text says the wrong thing. The stored text is kept until a new one
    arrives, and a note that couldn't be fetched this time keeps its old mark, so the next sync tries again."""
    pending = [r for r in recs if r["category"] in ("notes", "reports") and not r.get("narrative")
               and (r.get("details") or {}).get("attachments")]
    if not pending:
        return 0
    existing: dict[tuple[str, str], Any] = {}
    with read() as c:       # without the write lock: this runs on the event loop, and a big upload may hold it
        rows = c.execute(
            "SELECT resource_type, resource_id, narrative, raw_json, details_json FROM clinical_records WHERE connection_id = ? "
            "AND category IN ('notes', 'reports') AND narrative IS NOT NULL",
            (conn["id"],),
        ).fetchall()
        existing = {(r["resource_type"], r["resource_id"]): r for r in rows}
    changed, new = [], []
    for rec in pending:
        rec["details"]["body_fingerprint"] = fingerprint = fhir_normalize.body_fingerprint(rec.get("raw"))
        row = existing.get((rec["resource_type"], rec["resource_id"]))
        if row is None:
            new.append(rec)
            continue
        rec["narrative"] = row["narrative"]
        stored = _stored_fingerprint(row)
        if stored != fingerprint:
            rec["details"]["body_fingerprint"] = stored     # until the new text is here
            rec["details"]["body_stale"] = True
            changed.append((rec, fingerprint))
    fetched = 0
    async with smart.http_client(simulated) as http:
        for rec, fingerprint in [*changed, *((r, r["details"]["body_fingerprint"]) for r in new)]:
            if fetched >= MAX_NOTE_FETCHES:
                break
            attachments = sorted(rec["details"].get("attachments") or [], key=lambda a: fhir_normalize.text_preference(a.get("content_type")))
            for att in attachments:
                ctype = (att.get("content_type") or "").lower()
                if not att.get("url") or not (ctype.startswith("text/") or "rtf" in ctype or "xml" in ctype or not ctype):
                    continue
                try:
                    body = await client.read_binary_text(http, att["url"])
                except Exception:  # noqa: BLE001
                    body = None
                if body:
                    content_type, raw = body
                    rec["narrative"] = fhir_normalize.decode_body(raw, content_type)[:200_000]
                    rec["details"]["body_fingerprint"] = fingerprint
                    rec["details"].pop("body_stale", None)
                    fetched += 1
                    break
    return fetched


def _stored_fingerprint(row: Any) -> Optional[str]:
    """What the stored text was fetched for. A row from before this was kept: its own stored resource says."""
    try:
        details = json.loads(row["details_json"]) if row["details_json"] else {}
        return details.get("body_fingerprint") or fhir_normalize.body_fingerprint(json.loads(row["raw_json"]) if row["raw_json"] else None)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Wearables
# ---------------------------------------------------------------------------

WEARABLE_MODULES = {"oura": oura, "whoop": whoop, "google": google_health}


async def _sync_wearable(conn: dict[str, Any]) -> dict[str, Any]:
    module = WEARABLE_MODULES.get(conn["provider"])
    if module is None:
        raise SmartError(f"Unknown wearable provider '{conn['provider']}'")
    today = datetime.now(timezone.utc).date()
    # From the last sync that worked: after days of failures (an expired sign-in), the days in between are fetched too.
    last_ok = connections.last_success_at(conn)
    if last_ok:
        since = datetime.fromtimestamp(last_ok, timezone.utc).date() - timedelta(days=WEARABLE_OVERLAP_DAYS)
    else:
        since = today - timedelta(days=WEARABLE_INITIAL_DAYS)

    if conn["mode"] == "simulated":
        # Days on this server's clock, so the made-up nights fall at night here.
        tz = biometrics._tz()
        until = datetime.now(tz).date()
        days = (until - since).days + 1
        data = await asyncio.to_thread(module.simulate, days=days, until=until, seed=f"{conn['provider']}-{conn['id']}", tz=tz)
    else:
        creds = conn.get("credentials") or {}
        if not creds.get("access_token"):
            raise SmartError("This connection has no access token. Reconnect to continue.", "auth")
        if wc.needs_refresh(creds):
            creds = await module.refresh(creds)
            await asyncio.to_thread(connections.update, conn["id"], credentials=creds)
        try:
            data = await module.fetch_live(creds, since, today)
        except SmartError as exc:
            if exc.kind != "auth" or not creds.get("refresh_token"):
                raise
            creds = await module.refresh(creds)
            await asyncio.to_thread(connections.update, conn["id"], credentials=creds)
            data = await module.fetch_live(creds, since, today)

    def store() -> tuple[dict[str, Any], int]:      # in a thread, like the EHR's records
        samples = module.normalize(data)
        workouts = module.normalize_workouts(data)
        if conn["mode"] == "simulated":
            # Label demo data so it is never mistaken for, or preferred over, real measurements.
            for item in (*samples, *workouts):
                item["source_name"] = f"{item['source_name']} (simulated)"
        result = biometrics.insert_samples(
            conn["profile_id"], conn["id"], samples,
            batch_meta={"device_id": f"{conn['provider']}-cloud", "device_name": conn["display_name"],
                        "sync_trigger": "simulated" if conn["mode"] == "simulated" else "cloud"},
        )
        return result, activity.insert_workouts(conn["profile_id"], conn["id"], workouts)
    result, workouts = await asyncio.to_thread(store)
    return {"since": since.isoformat(), "samples": result["received"], "inserted": result["inserted"],
            "duplicates": result["duplicates"], "days_updated": result["days_updated"], "workouts": workouts,
            "warnings": []}


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

def _refuse_several_people(resources: list[dict[str, Any]]) -> None:
    """One profile is one person's record: a family export or a bulk file would silently mix one person's conditions into another's."""
    people = records.distinct_people([{"full_name": p["name"], "birth_date": p["birth_date"], "source_name": ""}
                                      for p in (fhir_normalize.normalize_patient(r) for r in resources if r.get("resourceType") == "Patient")])
    if len(people) > 1:
        names = " and ".join(f"{p['full_name']}{' (born ' + p['birth_date'] + ')' if p['birth_date'] else ''}" for p in people[:4])
        raise ValueError(f"This file holds records for {len(people)} different people: {names}. Nothing was imported, so their records "
                         "don't mix. Export one person at a time, and import each into their own profile.")


def _other_subjects(resources: list[dict[str, Any]]) -> int:
    """How many other patient ids this file's records point at, beyond the patients it names. Can't be told apart from the same person
    at another hospital without a name to go on, so it's mentioned rather than refused."""
    known = {str(r.get("id")) for r in resources if r.get("resourceType") == "Patient"}
    subjects = set()
    for r in resources:
        for field in ("subject", "patient"):
            ref = r.get(field).get("reference") if isinstance(r.get(field), dict) else None
            if isinstance(ref, str) and "Patient/" in ref:
                subjects.add(ref.split("Patient/")[-1])
    return len(subjects - known) - (0 if known else 1)


def import_fhir_resources(profile_id: str, display_name: str, resources: list[dict[str, Any]],
                          provider: str = "fhir_file") -> dict[str, Any]:
    _refuse_several_people(resources)
    patient, recs, problems = fhir_normalize.normalize_report(resources)
    if not recs and not patient:
        raise ValueError("No supported FHIR R4 resources were found in the file.")
    conn = connections.create(profile_id, "import", provider, display_name, mode="live",
                              patient_ref=(patient or {}).get("id"),
                              metadata={"imported_at": time.time(), "resource_count": len(resources)})
    run_id = connections.start_run(conn["id"], "import")
    if patient:
        records.upsert_identity(conn["id"], profile_id, patient)
    write = records.upsert_records(profile_id, conn["id"], display_name, recs)
    warnings = [f"{rtype}: {n} couldn't be read" for rtype, n in _unread_by_type(problems).items()]
    if provider == "fhir_file" and _other_subjects(resources) > 0:
        warnings.append("Some records in this file point at another patient ID. Check they all belong to this person.")
    stats = {"resources": len(resources), "records": len(recs), **write, "warnings": warnings,
             "not_read": _count_unread(problems)}
    connections.finish_run(run_id, conn["id"], "success", stats)
    return {"connection_id": conn["id"], **stats}


def due_connections(ehr_hours: float, wearable_hours: float) -> list[dict[str, Any]]:
    now = time.time()
    due = []
    for c in connections.list_active_all():
        if c["kind"] not in ("ehr", "wearable") or c["status"] == "needs_reauth" or c.get("access_ended"):
            continue
        interval = (ehr_hours if c["kind"] == "ehr" else wearable_hours) * 3600
        last = c.get("last_sync_at") or 0
        # Back off failing connections (retry at most every 6 hours).
        if c["status"] == "error":
            interval = max(interval, 6 * 3600)
        if now - last >= interval:
            due.append(c)
    return due
