"""
Sync orchestration.

``sync_connection`` pulls fresh data for one connection, whatever its kind, and
records a ``sync_runs`` entry. Connections are locked individually so a scheduled
sync and a manual "Sync now" never run concurrently for the same source.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.connectors import fhir_normalize, google_health, oura, smart, whoop
from app.connectors import wearable_common as wc
from app.connectors.smart import SmartError
from app.core.db import audit, db, dumps, read
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
        except biometrics.SourceGone as exc:     # removed while this sync was fetching: there's nothing to report it on
            return {"status": "skipped", "reason": str(exc)}
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
        records.upsert_references(conn["id"], fhir_normalize.referenced_resources(fetched.resources))
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

# The tag every exported resource carries (app.services.exports): which source it came from.
SOURCE_TAG = "urn:syntropy-health:source"
SOURCE_URN = "urn:syntropy-health:source:"


def _take_source_tag(res: dict[str, Any]) -> tuple[dict[str, Any], Optional[str], Optional[str]]:
    """The resource as it was before export, and the source's id and name if it had one."""
    meta = res.get("meta")
    if not isinstance(meta, dict):
        return res, None, None
    tags = [t for t in meta.get("tag") or [] if isinstance(t, dict)]
    ours = next((t for t in tags if t.get("system") == SOURCE_TAG and t.get("code")), None)
    if not ours:
        return res, None, None
    left = {**meta, "tag": [t for t in tags if t is not ours]}
    if not left["tag"]:
        del left["tag"]
    if str(left.get("source") or "").startswith(SOURCE_URN):
        del left["source"]
    out = {k: v for k, v in res.items() if k != "meta"}
    if left:
        out["meta"] = left
    return out, str(ours["code"]), str(ours.get("display") or "") or None


def _patient_of(res: dict[str, Any]) -> Optional[str]:
    for field in ("subject", "patient", "beneficiary"):
        ref = (res.get(field) or {}).get("reference") if isinstance(res.get(field), dict) else None
        if isinstance(ref, str) and "Patient/" in ref:
            return ref.rsplit("Patient/", 1)[1]
    return None


def _group_by_source(resources: list[dict[str, Any]], display_name: str) -> list[dict[str, Any]]:
    """A file is split by the source its resources say they came from, so two hospitals that use the same id both keep their
    results. Without that, by patient: a file naming two people mustn't be stored as if it were one."""
    groups: dict[str, dict[str, Any]] = {}
    untagged: list[dict[str, Any]] = []
    for res in resources:
        clean, origin, name = _take_source_tag(res)
        if origin:
            groups.setdefault(origin, {"origin": origin, "name": name or display_name, "resources": []})["resources"].append(clean)
        else:
            untagged.append(clean)
    patients = [r for r in untagged if r.get("resourceType") == "Patient"]
    if len(patients) > 1:
        # A Medication or a prescriber belongs to nobody, and each person's records need it to be read.
        shared = [r for r in untagged if r.get("resourceType") in fhir_normalize.REFERENCED_TYPES]
        for patient in patients:
            who = str(patient.get("id") or len(groups))
            mine = [r for r in untagged if r is patient or _patient_of(r) == who]
            origin = f"file:{display_name}#{who}"
            groups[origin] = {"origin": origin, "name": display_name, "resources": mine + shared, "by_name": True}
        claimed = {id(r) for g in groups.values() for r in g["resources"]}
        rest = [r for r in untagged if id(r) not in claimed]
        if rest:
            first = groups[f"file:{display_name}#{patients[0].get('id') or 0}"] if patients[0].get("id") else next(iter(groups.values()))
            first["resources"].extend(rest)
    elif untagged:
        origin = f"file:{display_name}"
        groups[origin] = {"origin": origin, "name": display_name, "resources": untagged, "legacy_name": display_name, "by_name": True}
    return list(groups.values())


def _keep_both(connection_id: str, recs: list[dict[str, Any]]) -> int:
    """An import never overwrites. A resource with the id of one already here, and different contents, is kept beside it
    under an id made from its contents, so importing the same file again finds it instead of adding it once more."""
    with read() as c:
        stored = {(r[0], r[1]): r[2] for r in c.execute(
            "SELECT resource_type, resource_id, raw_json FROM clinical_records WHERE connection_id = ?", (connection_id,))}
    kept = 0
    for rec in recs:
        key = (rec["resource_type"], rec["resource_id"])
        text = dumps(rec["raw"])
        if key not in stored or stored[key] == text:
            stored[key] = text
            continue
        new_id = f"{rec['resource_id'][:53]}-k{hashlib.sha1((text or '').encode()).hexdigest()[:8]}"
        rec["resource_id"] = new_id
        rec["raw"] = {**rec["raw"], "id": new_id}
        new_key = (rec["resource_type"], new_id)
        if new_key not in stored:
            kept += 1
        stored[new_key] = dumps(rec["raw"])
    return kept


def _shares_a_record(connection_id: str, recs: list[dict[str, Any]]) -> bool:
    """A corrected result keeps its id, so the id is what's compared. Different contents are kept beside it (_keep_both)."""
    with read() as c:
        stored = {(r[0], r[1]) for r in c.execute(
            "SELECT resource_type, resource_id FROM clinical_records WHERE connection_id = ?", (connection_id,))}
    return not stored or any((r["resource_type"], r["resource_id"]) in stored for r in recs)


def _same_source(conn: dict[str, Any], patient: Optional[dict[str, Any]], resources: list[dict[str, Any]],
                 recs: list[dict[str, Any]], named_by_file: bool) -> bool:
    """Everyone's download is called export.json, so a name doesn't make a file the source already here under it. It has to
    be about the same patient or, if one of them doesn't say who, to have a record in common. Otherwise a second person's
    records would mix into the first's and their name replace the first's, and nothing would warn that these are two people."""
    here = records.identity_of(conn["id"])
    known = conn.get("patient_ref")
    if patient and here:
        if patient.get("id") and known and str(patient["id"]) != known:
            return False
        named = [{"full_name": None if p["name"] == "Unknown" else p["name"], "birth_date": p["birth_date"], "source_name": ""}
                 for p in ({"name": here["full_name"], "birth_date": here["birth_date"]},
                           {"name": patient.get("name"), "birth_date": patient.get("birth_date")})]
        return len(records.distinct_people(named)) == 1
    if not named_by_file:       # Apple Health Records: the one place these go, and they name no patient
        return True
    subjects = {s for s in map(_patient_of, resources) if s}
    if subjects and known:
        return known in subjects
    return _shares_a_record(conn["id"], recs)


def _source_of(profile_id: str, group: dict[str, Any], patient: Optional[dict[str, Any]], recs: list[dict[str, Any]],
               provider: str) -> tuple[Optional[dict[str, Any]], str, bool]:
    """The source this group goes in, if there's one already; the origin to save; and whether others share its name."""
    origin = group["origin"]
    candidates = connections.find_imports_named(profile_id, origin, group.get("legacy_name"))
    for conn in candidates:
        if not group.get("by_name") or _same_source(conn, patient, group["resources"], recs, provider == "fhir_file"):
            return conn, (conn.get("metadata") or {}).get("origin") or origin, False
    return None, f"{origin}~{uuid.uuid4().hex[:8]}" if candidates else origin, bool(candidates)


def import_fhir_resources(profile_id: str, display_name: str, resources: list[dict[str, Any]],
                          provider: str = "fhir_file") -> dict[str, Any]:
    """Each source in the file becomes one source here, found again if the same file comes back, and the result says what
    happened to every record."""
    total = {"resources": len(resources), "records": 0, "inserted": 0, "updated": 0, "unchanged": 0, "kept_both": 0}
    made: list[dict[str, Any]] = []
    for group in _group_by_source(resources, display_name):
        patient, recs = fhir_normalize.normalize_resources(group["resources"])
        if not recs and not patient:
            continue
        conn, origin, name_taken = _source_of(profile_id, group, patient, recs, provider)
        metadata = {"imported_at": time.time(), "resource_count": len(group["resources"]), "origin": origin}
        subjects = {s for s in map(_patient_of, group["resources"]) if s}
        patient_ref = (patient or {}).get("id") or (next(iter(subjects)) if len(subjects) == 1 else None)
        if conn:
            connections.update(conn["id"], metadata={**(conn.get("metadata") or {}), **metadata},
                               **({"patient_ref": patient_ref} if patient_ref and not conn.get("patient_ref") else {}))
        else:
            who = (patient or {}).get("name")
            name = f"{group['name']} ({who})" if name_taken and who and who != "Unknown" else group["name"]
            conn = connections.create(profile_id, "import", provider, name, mode="live", patient_ref=patient_ref, metadata=metadata)
        run_id = connections.start_run(conn["id"], "import")
        if patient:
            records.upsert_identity(conn["id"], profile_id, patient)
        total["kept_both"] += _keep_both(conn["id"], recs)
        write = records.upsert_records(profile_id, conn["id"], conn["display_name"], recs)
        records.upsert_references(conn["id"], fhir_normalize.referenced_resources(group["resources"]))
        stats = {"resources": len(group["resources"]), "records": len(recs), **write}
        connections.finish_run(run_id, conn["id"], "success", stats)
        total["records"] += len(recs)
        total["inserted"] += write["inserted"]
        total["updated"] += write["updated"]
        total["unchanged"] += len(recs) - write["inserted"] - write["updated"]
        made.append({"connection_id": conn["id"], "name": conn["display_name"]})
    if not made:
        raise ValueError("No supported FHIR R4 resources were found in the file.")
    return {"connection_id": made[0]["connection_id"], "sources": made, **total}


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
