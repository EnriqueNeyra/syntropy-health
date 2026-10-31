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
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from app.connectors import fhir_normalize, google_health, oura, smart, whoop
from app.connectors import wearable_common as wc
from app.connectors.smart import SmartError
from app.services import connect
from app.core.db import audit, db, read
from app.store import activity, biometrics, connections, records

log = logging.getLogger("syntropy.sync")

_locks: dict[str, asyncio.Lock] = {}
MAX_NOTE_FETCHES = 200
FAILING_RETRY_HOURS = 6
# How a source says, in a resource it still returns, that the record was wrong or never happened.
_TAKEN_BACK = {"entered-in-error", "cancelled", "revoked", "not-done"}
_NOT_TRUE = {"entered-in-error", "refuted"}
WEARABLE_INITIAL_DAYS = 90
WEARABLE_OVERLAP_DAYS = 3
WINDOW_HELD_MAX_DAYS = 365
REFETCH_STRETCH_DAYS = 180
# How far back "bring in older history" asks. A provider just has nothing for days before someone had the device.
HISTORY_DAYS = 3650
HISTORY_PAUSE_S = 5.0


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
        smart.GUARDED.set(connect.guard_for(conn))
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
                          {"connection": connection_id, "status": status, "trigger": trigger, "removed": stats.get("removed") or 0,
                           "held": sum((stats.get("held") or {}).values())}, conn["profile_id"])
            await asyncio.to_thread(finish)
            return {"status": status, **stats}
        except SmartError as exc:
            return await _stopped(run_id, conn, exc, started)
        except httpx.HTTPError as exc:
            return await _stopped(run_id, conn, _plain_failure(exc, conn["display_name"]), started)
        except Exception as exc:  # noqa: BLE001 - surface any failure on the connection card
            log.exception("Sync failed for %s", connection_id)
            stats = {"failure_kind": "error", "duration_s": round(time.time() - started, 2)}
            detail = str(exc) or type(exc).__name__
            await _record_failure(run_id, connection_id, stats, f"Unexpected error: {detail}")
            return {"status": "error", "error": detail, "kind": "error"}


def _plain_failure(exc: httpx.HTTPError, name: str) -> SmartError:
    """A vendor's trouble in words, and whether it's the kind that passes."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (408, 425, 429) or code >= 500:
            return SmartError(f"{name} isn't answering right now ({code}). Syntropy will try again.", "network",
                              retry_after=smart.retry_after_seconds(exc.response))
        return SmartError(f"{name} refused this sync ({code}).", "error")
    return SmartError(f"Couldn't reach {name}. Syntropy will try again.", "network")


async def _stopped(run_id: Optional[int], conn: dict[str, Any], exc: SmartError, started: float) -> dict[str, Any]:
    """A sync that couldn't finish. What it got before that is already stored, and the run says so."""
    kind = exc.kind
    if kind == "auth" and await asyncio.to_thread(connections.credentials_replaced, conn["id"], conn.get("credentials") or {}):
        kind = "superseded"     # the person signed in again while this ran; this failure was about the old tokens
    stats = {**exc.stats, "failure_kind": kind, "duration_s": round(time.time() - started, 2)}
    if exc.retry_after:
        stats["retry_after"] = exc.retry_after
    status = "partial" if exc.stats else "error"
    await _record_failure(run_id, conn["id"], stats, None if kind == "superseded" else str(exc), status)
    return {**exc.stats, "status": status, "error": str(exc), "kind": kind}


async def _record_failure(run_id: Optional[int], connection_id: str, stats: dict[str, Any], error: Optional[str],
                          status: str = "error") -> None:
    """A database too busy to start or finish the run may be too busy to note that as well. The next sync tries again."""
    try:
        if run_id is None:
            run_id = await asyncio.to_thread(connections.start_run, connection_id, "failed")
        await asyncio.to_thread(connections.finish_run, run_id, connection_id, status, stats, error)
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

    async def renewed(new: dict[str, Any]) -> None:
        await asyncio.to_thread(connections.save_renewed, conn["id"], conn["credentials"], new)
        conn["credentials"] = new
    if wc.needs_refresh(creds):
        creds = await smart.refresh(creds, simulated)
        await renewed(creds)

    saved = await asyncio.to_thread(connections.query_state, conn["id"])
    client = smart.FhirClient(conn["fhir_base_url"], creds, conn["patient_ref"], simulated, on_refresh=renewed)
    fetched = await client.fetch_all(saved)

    # Normalizing and storing thousands of records is plain CPU and SQLite work: it runs in a thread so the server
    # keeps answering (the page, the phone) meanwhile.
    unreadable: list[str] = []
    everything = ([fetched.patient] if fetched.patient else []) + fetched.resources
    patient, recs, problems = await asyncio.to_thread(fhir_normalize.normalize_report, everything, unreadable)
    notes = await _fetch_note_bodies(conn, client, recs, simulated)

    returned = {f"{r['resourceType']}/{r.get('id')}": r for r in fetched.resources}
    stored = {f"{r['resource_type']}/{r['resource_id']}" for r in recs}
    retracted = {key for key, res in returned.items() if key not in stored and key not in unreadable and _says_taken_back(res)}

    def store() -> tuple[dict[str, Any], dict[str, Any]]:
        if patient:
            records.upsert_identity(conn["id"], conn["profile_id"], patient)
        write = records.upsert_records(conn["profile_id"], conn["id"], conn["display_name"], recs)
        outcome = records.reconcile(conn["id"], set(returned) - retracted, fetched.prune_verdict, retracted)
        connections.save_query_state(conn["id"], fetched.state())
        return write, outcome
    write, outcome = await asyncio.to_thread(store)

    warnings = _query_warnings(fetched, saved)
    if unreadable:
        warnings.append(f"{len(unreadable)} records couldn't be read, so the copies already here were kept.")
    if notes["problem"]:
        warnings.append(notes["problem"])
    by_category: dict[str, int] = {}
    for r in recs:
        by_category[r["category"]] = by_category.get(r["category"], 0) + 1
    stats = {
        "resources": len(everything), "records": len(recs), "inserted": write["inserted"],
        "updated": write["updated"], "pruned": outcome["removed"], "removed": outcome["removed"], "held": outcome["held"],
        "notes_fetched": notes["fetched"], "notes_pending": notes["pending"],
        # Counted, not a warning: one odd resource in a chart would otherwise mark every sync "Partly synced" for good.
        "not_read": _count_unread(problems), "not_read_by_type": _unread_by_type(problems),
        "by_category": by_category, "queries": fetched.status, "counts": fetched.counts, "warnings": warnings,
        "more_to_fetch": fetched.more_to_fetch or bool(notes["pending"]),
    }
    if fetched.come_back_at:
        stats["retry_after"] = max(0.0, fetched.come_back_at - time.time())
    if fetched.stopped:
        if recs:
            fetched.stopped.stats = stats
        raise fetched.stopped
    return stats


def _unread_by_type(problems: dict[str, dict[str, int]]) -> dict[str, int]:
    """Resources that came in and couldn't be kept, so losing them is never silent."""
    counts: dict[str, int] = {}
    for by_type in problems.values():
        for rtype, n in by_type.items():
            counts[rtype] = counts.get(rtype, 0) + n
    return dict(sorted(counts.items()))


def _count_unread(problems: dict[str, dict[str, int]]) -> int:
    return sum(_unread_by_type(problems).values())


QUERY_WORDS = {
    "empty": "Nothing here", "not-granted": "Not shared when you signed in", "unsupported": "This source doesn't offer them",
    "forbidden": "This source didn't allow them to be read", "paused": "Still downloading. The rest comes in at the next sync.",
    "truncated": "The source sent the rest from another address, so it wasn't downloaded",
    "recent": "Newest checked. A history this long is read in full about once a month.",
}


def describe_queries(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """How each kind of record fared in the latest sync that got that far, in words. An extra kind of result a source
    never offered isn't worth a line."""
    stats = next((r.get("stats") or {} for r in runs if (r.get("stats") or {}).get("queries")), {})
    lines = []
    for query, status in (stats.get("queries") or {}).items():
        count = (stats.get("counts") or {}).get(query) or 0
        if query in smart.OPTIONAL_QUERIES and not count:
            continue
        words = ("Received" if query == "Patient" and status == "ok"
                 else f"{count:,} {'record' if count == 1 else 'records'}" if status == "ok"
                 else QUERY_WORDS.get(status, "Couldn't be downloaded this time. Syntropy will try again."))
        lines.append({"query": query, "name": smart.QUERY_NAMES.get(query, query), "status": status, "count": count, "words": words,
                      "ok": status in ("ok", "empty", "not-granted", "unsupported", "recent")})
    return lines


def _says_taken_back(res: dict[str, Any]) -> bool:
    """The source still returns this resource, but to say it was wrong or never happened."""
    if (res.get("status") or "").lower() in _TAKEN_BACK or (res.get("docStatus") or "").lower() == "entered-in-error":
        return True
    verification = res.get("verificationStatus")
    return isinstance(verification, dict) and bool(smart._codes(verification) & _NOT_TRUE)


def _query_warnings(fetched: smart.FetchResult, saved: dict[str, dict[str, Any]]) -> list[str]:
    """What the person should hear about. A query this server has never answered, for something it may simply not keep,
    isn't news. One that used to work and has stopped is."""
    warnings = []
    for label, status in fetched.status.items():
        if status in ("ok", "empty", "not-granted", "recent"):
            continue
        answered_before = (saved.get(label) or {}).get("ever_answered")
        if not answered_before and (status == "unsupported" or label in smart.OPTIONAL_QUERIES):
            continue
        name = smart.QUERY_NAMES.get(label, label)
        if status == "paused":
            warnings.append(f"{name}: still downloading. The rest comes in at the next sync.")
        elif status == "forbidden":
            warnings.append(f"{name}: this source didn't allow them to be read.")
        elif status == "unsupported":
            warnings.append(f"{name}: this source has stopped offering them.")
        elif status == "truncated":
            warnings.append(f"{name}: the source sent the rest from another address, so it wasn't downloaded.")
        else:
            warnings.append(f"{name}: couldn't be downloaded this time. What's here was kept, and Syntropy will try again.")
    return warnings


async def _fetch_note_bodies(conn: dict[str, Any], client: smart.FhirClient, recs: list[dict[str, Any]], simulated: bool) -> dict[str, Any]:
    """Clinical notes and reports often reference their content by URL (Binary). Fetch the bodies we don't have yet, and again when
    the source changed the note: an amended note under its old text says the wrong thing. The stored text is kept until a new one
    arrives, and a note that couldn't be fetched this time keeps its old mark, so the next sync tries again."""
    done: dict[str, Any] = {"fetched": 0, "pending": 0, "problem": None}
    pending = [r for r in recs if r["category"] in ("notes", "reports") and not r.get("narrative")
               and (r.get("details") or {}).get("attachments")]
    if not pending:
        return done
    existing: dict[tuple[str, str], Any] = {}
    with read() as c:       # without the write lock: this runs on the event loop, and a big upload may hold it
        rows = c.execute(
            "SELECT resource_type, resource_id, narrative, raw_json, details_json FROM clinical_records WHERE connection_id = ? "
            "AND category IN ('notes', 'reports') AND narrative IS NOT NULL",
            (conn["id"],),
        ).fetchall()
        existing = {(r["resource_type"], r["resource_id"]): r for r in rows}
    # Every body already here first, whatever happens below: storing a record without one would blank it.
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
    new.sort(key=lambda r: r.get("effective_at") or "", reverse=True)
    wanted = [*changed, *((r, r["details"]["body_fingerprint"]) for r in new)]
    attempts = 0
    async with smart.http_client(simulated) as http:
        for position, (rec, fingerprint) in enumerate(wanted):
            if attempts >= MAX_NOTE_FETCHES or client.time_left() <= 0:
                done["pending"] = len(wanted) - position
                break
            attempts += 1
            attachments = sorted(rec["details"].get("attachments") or [], key=lambda a: fhir_normalize.text_preference(a.get("content_type")))
            for att in attachments:
                ctype = (att.get("content_type") or "").lower()
                if not att.get("url") or not (ctype.startswith("text/") or "rtf" in ctype or "xml" in ctype or not ctype):
                    continue
                try:
                    body = await client.read_binary_text(http, att["url"])
                except SmartError as exc:
                    # Access has gone: the other notes would only fail the same way. They come with a later sync.
                    log.info("Stopped fetching note text from %s: %s", conn["display_name"], exc)
                    done["pending"] = len(wanted) - position
                    done["problem"] = "Clinical notes: the text of some couldn't be downloaded this time. Syntropy will try again."
                    return done
                except Exception:  # noqa: BLE001
                    done["problem"] = "Clinical notes: the text of some couldn't be downloaded this time. Syntropy will try again."
                    body = None
                if body:
                    content_type, raw = body
                    rec["narrative"] = fhir_normalize.decode_body(raw, content_type)[:200_000]
                    rec["details"]["body_fingerprint"] = fingerprint
                    rec["details"].pop("body_stale", None)
                    done["fetched"] += 1
                    break
    return done


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
    # For someone ahead of UTC it's already tomorrow, and the providers' date ranges would leave that day out.
    today = max(datetime.now(timezone.utc).date(), datetime.now(biometrics._tz(conn["profile_id"])).date())
    meta = dict(conn.get("metadata") or {})
    asked_older = meta.get("refetch_from")
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
        async def renew() -> dict[str, Any]:
            new = await module.refresh(conn["credentials"])
            await asyncio.to_thread(connections.save_renewed, conn["id"], conn["credentials"], new)
            conn["credentials"] = new
            return new
        if wc.needs_refresh(creds):
            creds = await renew()
        try:
            data = await module.fetch_live(creds, since, today)
        except SmartError as exc:
            if exc.kind != "auth" or not creds.get("refresh_token"):
                raise
            creds = await renew()
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
    await asyncio.to_thread(_remember_window, conn["id"], meta, asked_older, since, today, bool(warnings))
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


def ask_for_history(conn: dict[str, Any]) -> None:
    meta = dict(conn.get("metadata") or {})
    start = (datetime.now(timezone.utc).date() - timedelta(days=HISTORY_DAYS)).isoformat()
    meta["refetch_from"] = min(meta.get("refetch_from") or start, start)
    connections.update(conn["id"], metadata=meta)


async def fetch_whole_history(connection_id: str) -> None:
    """Syncs until the older days have all come. A pause between stretches stays within what the providers allow (WHOOP's limit
    isn't published), and if one fails, the regular syncs carry on from where this stopped."""
    seen = None
    while True:
        conn = await asyncio.to_thread(connections.get, connection_id)
        start = ((conn or {}).get("metadata") or {}).get("refetch_from")
        if not start or start == seen:
            return
        seen = start
        result = await sync_connection(connection_id, "history")
        if result.get("status") == "error":
            return
        await asyncio.sleep(HISTORY_PAUSE_S)


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


def _remember_window(connection_id: str, meta: dict[str, Any], asked_older: Any, since: date, today: date, refused: bool) -> None:
    """On what's stored now: older history may have been asked for while this sync ran, and that request has to survive it."""
    fresh = dict((connections.get(connection_id) or {}).get("metadata") or {})
    fresh["fetched_through"] = today.isoformat()
    if refused:
        held = _meta_date(fresh, "window_floor")
        fresh["window_floor"] = min(since, held).isoformat() if held else since.isoformat()
    else:
        fresh.pop("window_floor", None)
    if fresh.get("refetch_from") == asked_older:
        if meta.get("refetch_from"):
            fresh["refetch_from"] = meta["refetch_from"]
        else:
            fresh.pop("refetch_from", None)
    connections.update(connection_id, metadata=fresh)


# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

def _refuse_several_people(resources: list[dict[str, Any]]) -> None:
    """One profile is one person's record: a family export or a bulk file would silently mix one person's conditions into another's."""
    people = records.distinct_people([{"full_name": p["name"], "birth_date": p["birth_date"], "source_name": ""}
                                      for p in (fhir_normalize.normalize_patient(r) for r in resources if r.get("resourceType") == "Patient")])
    if len(people) > 1:
        names = " and ".join(f"{p['full_name'] or 'an unnamed patient'}{' (born ' + p['birth_date'] + ')' if p['birth_date'] else ''}" for p in people[:4])
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
                          provider: str = "fhir_file", reuse: Optional[dict[str, Any]] = None,
                          metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Stores a file's records as a new source, or in ``reuse``, the one an earlier copy of this file went into: there,
    the same resources are updated, and the file can take back what it now says was wrong. What it doesn't mention stays."""
    _refuse_several_people(resources)
    patient, recs, problems = fhir_normalize.normalize_report(resources)
    if not recs and not patient:
        raise ValueError("No supported FHIR R4 resources were found in the file.")
    info = {"imported_at": time.time(), "resource_count": len(resources), **(metadata or {})}
    if reuse:
        conn = reuse
        connections.update(conn["id"], metadata={**(conn.get("metadata") or {}), **info})
    else:
        conn = connections.create(profile_id, "import", provider, display_name, mode="live",
                                  patient_ref=(patient or {}).get("id"), metadata=info)
    run_id = connections.start_run(conn["id"], "import")
    if patient:
        records.upsert_identity(conn["id"], profile_id, patient)
    write = records.upsert_records(profile_id, conn["id"], conn["display_name"], recs)
    warnings = [f"{rtype}: {n} couldn't be read" for rtype, n in _unread_by_type(problems).items()]
    if provider == "fhir_file" and _other_subjects(resources) > 0:
        warnings.append("Some records in this file point at another patient ID. Check they all belong to this person.")
    stats: dict[str, Any] = {"resources": len(resources), "records": len(recs), **write, "warnings": warnings,
                             "not_read": _count_unread(problems)}
    if reuse:
        stored = {f"{r['resource_type']}/{r['resource_id']}" for r in recs}
        keys = {f"{r['resourceType']}/{r['id']}": r for r in resources if r.get("id")}
        stats["removed"] = records.retract(conn["id"], {k for k, r in keys.items() if k not in stored and _says_taken_back(r)})
        stats["matched_earlier"] = True
    connections.finish_run(run_id, conn["id"], "success", stats)
    return {"connection_id": conn["id"], **stats}


def due_connections(ehr_hours: float, wearable_hours: float) -> list[dict[str, Any]]:
    now = time.time()
    due = []
    for c in connections.list_active_all():
        if c["kind"] not in ("ehr", "wearable") or c["status"] == "needs_reauth" or c.get("access_ended"):
            continue
        # After a temporary failure, or with a history still coming in: when that said to try again, even if it's soon.
        if c.get("next_retry_at"):
            if now >= c["next_retry_at"]:
                due.append(c)
            continue
        interval = (ehr_hours if c["kind"] == "ehr" else wearable_hours) * 3600
        last = c.get("last_sync_at") or 0
        # Back off failing connections (retry at most every 6 hours).
        if c["status"] == "error":
            interval = max(interval, FAILING_RETRY_HOURS * 3600)
        if now - last >= interval:
            due.append(c)
    return due
