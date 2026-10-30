"""
Sync orchestration.

``sync_connection`` pulls fresh data for one connection, whatever its kind, and
records a ``sync_runs`` entry. Connections are locked individually so a scheduled
sync and a manual "Sync now" never run concurrently for the same source.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import httpx

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
WINDOW_HELD_MAX_DAYS = 365
REFETCH_STRETCH_DAYS = 180


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
    patient, recs = await asyncio.to_thread(fhir_normalize.normalize_resources, [fetched.patient] + fetched.resources)  # type: ignore[list-item]
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
        "by_category": by_category, "queries": fetched.status, "warnings": warnings,
    }


async def _fetch_note_bodies(conn: dict[str, Any], client: smart.FhirClient, recs: list[dict[str, Any]], simulated: bool) -> int:
    """Clinical notes and reports often reference their content by URL (Binary); fetch text bodies we don't have yet."""
    pending = [r for r in recs if r["category"] in ("notes", "reports") and not r.get("narrative")
               and (r.get("details") or {}).get("attachments")]
    if not pending:
        return 0
    existing: dict[str, str] = {}
    with read() as c:       # without the write lock: this runs on the event loop, and a big upload may hold it
        rows = c.execute(
            "SELECT resource_id, narrative FROM clinical_records WHERE connection_id = ? "
            "AND category IN ('notes', 'reports') AND narrative IS NOT NULL",
            (conn["id"],),
        ).fetchall()
        existing = {r["resource_id"]: r["narrative"] for r in rows}
    fetched = 0
    async with smart.http_client(simulated) as http:
        for rec in pending:
            if rec["resource_id"] in existing:
                rec["narrative"] = existing[rec["resource_id"]]
                continue
            if fetched >= MAX_NOTE_FETCHES:
                break
            for att in rec["details"].get("attachments") or []:
                ctype = (att.get("content_type") or "").lower()
                if not att.get("url") or not (ctype.startswith("text/") or "xml" in ctype or not ctype):
                    continue
                try:
                    body = await client.read_binary_text(http, att["url"])
                except Exception:  # noqa: BLE001
                    body = None
                if body:
                    content_type, raw = body
                    text = raw.decode("utf-8", errors="replace")
                    if "html" in content_type or "xml" in content_type:
                        text = fhir_normalize.strip_html(text)
                    rec["narrative"] = text[:200_000]
                    fetched += 1
                    break
    return fetched


# ---------------------------------------------------------------------------
# Wearables
# ---------------------------------------------------------------------------

WEARABLE_MODULES = {"oura": oura, "whoop": whoop, "google": google_health}


async def _sync_wearable(conn: dict[str, Any]) -> dict[str, Any]:
    module = WEARABLE_MODULES.get(conn["provider"])
    if module is None:
        raise SmartError(f"Unknown wearable provider '{conn['provider']}'")
    # For someone ahead of UTC it's already tomorrow, and the providers' date ranges would leave that day out.
    today = max(datetime.now(timezone.utc).date(), datetime.now(biometrics._tz(conn["profile_id"])).date())
    meta = dict(conn.get("metadata") or {})
    # From the last sync that worked: after days of failures (an expired sign-in), the days in between are fetched too.
    last_ok = connections.last_success_at(conn)
    if last_ok:
        since = datetime.fromtimestamp(last_ok, timezone.utc).date() - timedelta(days=WEARABLE_OVERLAP_DAYS)
    else:
        since = today - timedelta(days=WEARABLE_INITIAL_DAYS)
    since = _hold_window(since, today, meta)

    if conn["mode"] == "simulated":
        # Days on this server's clock, so the made-up nights fall at night here.
        tz = biometrics._tz(conn["profile_id"])
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
        await _fetch_older_stretch(module, creds, data, meta, since)

    warnings = data.pop("_warnings", [])

    def store() -> tuple[dict[str, Any], dict[str, int]]:      # in a thread, like the EHR's records
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
            deleted_prefixes=module.deleted_prefixes(data) if hasattr(module, "deleted_prefixes") else (),
            must_have_source=True,
        )
        return result, activity.store_workouts(conn["profile_id"], conn["id"], workouts)
    try:
        result, workouts = await asyncio.to_thread(store)
    except biometrics.SourceRemoved:
        return {"since": since.isoformat(), "samples": 0, "inserted": 0, "warnings": [], "removed": True}
    await asyncio.to_thread(_remember_window, conn["id"], meta, since, today, bool(warnings))
    return {"since": since.isoformat(), "samples": result["received"], "inserted": result["inserted"], "updated": result["updated"],
            "duplicates": result["duplicates"], "rejected": result["rejected"], "flagged": result["flagged"],
            "days_updated": result["days_updated"], "workouts": workouts["inserted"], "workouts_updated": workouts["updated"],
            "warnings": warnings}


async def _fetch_older_stretch(module: Any, creds: dict[str, Any], data: dict[str, Any], meta: dict[str, Any], since: date) -> None:
    """Older days being fetched again (``refetch_from``), one stretch per sync and oldest first, so a long history doesn't run into
    a provider's page or rate limits all at once, and a restart carries on. If this stretch fails, the usual window still syncs."""
    start = _meta_date(meta, "refetch_from")
    if not start:
        return
    if start >= since:
        meta.pop("refetch_from", None)
        return
    stop = min(start + timedelta(days=REFETCH_STRETCH_DAYS), since)
    try:
        older = await getattr(module, "fetch_stretch", module.fetch_live)(creds, start, stop)
    except (SmartError, httpx.HTTPError) as exc:
        log.warning("Couldn't fetch older days for a wearable: %s", exc)
        return
    for key, rows in older.items():
        if key == "_warnings":
            data["_warnings"] = [*data.get("_warnings", []), *rows]
        else:
            data[key] = [*data.get(key, []), *rows]
    if stop >= since:
        meta.pop("refetch_from", None)
    else:
        meta["refetch_from"] = stop.isoformat()


def _meta_date(meta: dict[str, Any], key: str) -> Optional[date]:
    try:
        return date.fromisoformat(meta[key]) if isinstance(meta.get(key), str) else None
    except ValueError:
        return None


def _hold_window(since: date, today: date, meta: dict[str, Any]) -> date:
    """How far back to fetch. Further than the usual overlap when a data type was refused last time (``window_floor``), since a
    sync that moves on would leave a gap for good. And never past what was last fetched, once the runs that say so have aged out
    (only 50 are kept)."""
    for key in ("window_floor", "fetched_through"):
        found = _meta_date(meta, key)
        if found and found >= today - timedelta(days=WINDOW_HELD_MAX_DAYS):
            since = min(since, found - timedelta(days=WEARABLE_OVERLAP_DAYS if key == "fetched_through" else 0))
    return since


def _remember_window(connection_id: str, meta: dict[str, Any], since: date, today: date, refused: bool) -> None:
    meta["fetched_through"] = today.isoformat()
    if refused:
        meta["window_floor"] = min(since, date.fromisoformat(meta["window_floor"])).isoformat() if meta.get("window_floor") else since.isoformat()
    else:
        meta.pop("window_floor", None)
    connections.update(connection_id, metadata=meta)


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

def import_fhir_resources(profile_id: str, display_name: str, resources: list[dict[str, Any]],
                          provider: str = "fhir_file") -> dict[str, Any]:
    patient, recs = fhir_normalize.normalize_resources(resources)
    if not recs and not patient:
        raise ValueError("No supported FHIR R4 resources were found in the file.")
    conn = connections.create(profile_id, "import", provider, display_name, mode="live",
                              patient_ref=(patient or {}).get("id"),
                              metadata={"imported_at": time.time(), "resource_count": len(resources)})
    run_id = connections.start_run(conn["id"], "import")
    if patient:
        records.upsert_identity(conn["id"], profile_id, patient)
    write = records.upsert_records(profile_id, conn["id"], display_name, recs)
    stats = {"resources": len(resources), "records": len(recs), **write}
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
