"""Wearable & sensor biometrics, companion-app ingest and pairing."""

from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import check_access, phone_origin, request_origin, resolve_profile, visible_profile_ids
from app.core import auth, config, context
from app.core.uploads import UPLOAD_ENCODINGS
from app.services import healthkit_import
from app.store import accounts, activity, biometrics, connections, devices, sleep, training

router = APIRouter(tags=["biometrics"])
user = Depends(auth.require_user)


@router.get("/api/biometrics/overview", dependencies=[user])
def overview(profile: Optional[str] = None, brief: bool = False, tiles: Optional[str] = None) -> dict:
    """``brief`` returns only the headline tiles and whether any samples exist (the Overview page), skipping the
    per-source totals that have to count every sample."""
    prof = resolve_profile(profile)
    if brief:
        chosen = [m for m in (tiles or "").split(",") if m] or None
        return {"headline": biometrics.headline(prof["id"], chosen), "has_samples": biometrics.has_samples(prof["id"])}
    return {**biometrics.overview(prof["id"]), "activity": activity.counts(prof["id"])}


@router.get("/api/biometrics/metrics", dependencies=[user])
def metrics(profile: Optional[str] = None, latest: bool = False) -> dict:
    """``latest`` adds each metric's most recent daily value (the Trends list)."""
    prof = resolve_profile(profile)
    found = biometrics.available_metrics(prof["id"])
    return {"metrics": biometrics.with_latest_values(prof["id"], found) if latest else found}


@router.get("/api/biometrics/daily", dependencies=[user])
def daily(metric: str, profile: Optional[str] = None, days: int = Query(30, ge=1, le=3650),
                source: Optional[str] = None) -> dict:
    prof = resolve_profile(profile)
    return biometrics.daily_series(prof["id"], metric, days=days, source=source)


@router.get("/api/biometrics/sleep", dependencies=[user])
def sleep_nights(profile: Optional[str] = None, days: int = Query(30, ge=1, le=3650), source: Optional[str] = None) -> dict:
    """Night by night: bedtime, wake time and stages, with averages and how regular the schedule was."""
    prof = resolve_profile(profile)
    return sleep.nights(prof["id"], days=days, source=source)


@router.get("/api/biometrics/samples", dependencies=[user])
def samples(profile: Optional[str] = None, metric: Optional[str] = None, limit: int = Query(100, ge=1, le=2000)) -> dict:
    prof = resolve_profile(profile)
    return {"samples": biometrics.recent_samples(prof["id"], metric, limit)}


# ---------------------------------------------------------------------------
# Companion app (iOS) — device-token authenticated
# ---------------------------------------------------------------------------

class IngestSample(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    id: str = Field(..., max_length=200)
    metric_type: str = Field(..., max_length=100)
    hk_identifier: Optional[str] = Field(None, max_length=200)
    value: float
    unit: str = Field("", max_length=40)
    start_date: str
    end_date: str
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    source_name: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class WorkoutHeartRate(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    t: str = Field(..., max_length=40)
    min: float
    avg: float
    max: float


class WorkoutRoutePoint(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    t: Optional[str] = Field(None, max_length=40)
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    alt: Optional[float] = None
    speed: Optional[float] = None


class IngestWorkout(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    id: str = Field(..., max_length=200)
    activity_type: Optional[int] = None
    name: str = Field("Workout", max_length=120)
    start: str
    end: str
    duration_s: Optional[float] = None
    active_energy_kcal: Optional[float] = None
    total_energy_kcal: Optional[float] = None
    distance_m: Optional[float] = None
    step_count: Optional[float] = None
    avg_hr: Optional[float] = None
    max_hr: Optional[float] = None
    min_hr: Optional[float] = None
    elevation_ascent_m: Optional[float] = None
    elevation_descent_m: Optional[float] = None
    indoor: Optional[bool] = None
    temperature_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    source_name: Optional[str] = Field(None, max_length=200)
    device_name: Optional[str] = Field(None, max_length=200)
    metadata: Optional[dict[str, Any]] = None
    heart_rate: list[WorkoutHeartRate] = Field(default_factory=list, max_length=20000)
    route: list[WorkoutRoutePoint] = Field(default_factory=list, max_length=100000)


class IngestEvent(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    id: str = Field(..., max_length=200)
    event_type: str = Field(..., max_length=80)
    category: str = Field("other", max_length=40)
    name: str = Field("", max_length=120)
    start: str
    end: Optional[str] = None
    value: Optional[float] = None
    value_label: Optional[str] = Field(None, max_length=200)
    source_name: Optional[str] = Field(None, max_length=200)
    metadata: Optional[dict[str, Any]] = None


class IngestBatch(BaseModel):
    device_id: str = Field(..., max_length=200)
    device_name: str = Field(..., max_length=200)
    os_version: Optional[str] = None
    app_version: Optional[str] = None
    sync_trigger: str = "manual"
    synced_at: Optional[float] = None
    samples: list[IngestSample] = Field(default_factory=list, max_length=20000)
    workouts: list[IngestWorkout] = Field(default_factory=list, max_length=2000)
    events: list[IngestEvent] = Field(default_factory=list, max_length=20000)
    profile_id: Optional[str] = None


def _ingest_target(principal: auth.Principal, profile_id: Optional[str]) -> tuple[str, Optional[str]]:
    if principal.kind == "device":
        dev = devices.get_device(principal.device_id or "")
        return dev["profile_id"], dev.get("connection_id")  # type: ignore[index]
    return resolve_profile(profile_id)["id"], None


def _store(profile_id: str, connection_id: Optional[str], samples: list[dict], workouts: list[dict], events: list[dict],
           batch_meta: dict[str, Any]) -> dict:
    try:
        result = biometrics.insert_samples(profile_id, connection_id, samples, batch_meta=batch_meta)
        result["workouts_received"] = len(workouts)
        result["workouts_inserted"] = activity.insert_workouts(profile_id, connection_id, workouts)
        result["events_received"] = len(events)
        result["events_inserted"] = activity.insert_events(profile_id, connection_id, events)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, f"Invalid item: {exc}")
    if connection_id:
        connections.update(connection_id, last_sync_at=time.time(), last_sync_status="success", status="active", last_error=None)
    return result


@router.post("/api/ingest/wearables")
async def ingest(batch: IngestBatch, principal: auth.Principal = Depends(auth.require_device_or_user)) -> dict:
    """Companion-app ingest: raw HealthKit samples, workouts and category events."""
    profile_id, connection_id = _ingest_target(principal, batch.profile_id)
    # Large backfills take a second or more; keep the event loop (and the dashboard) responsive.
    return await run_in_threadpool(
        _store, profile_id, connection_id, [s.model_dump() for s in batch.samples],
        [w.model_dump() for w in batch.workouts], [e.model_dump() for e in batch.events],
        {"device_id": batch.device_id, "device_name": batch.device_name, "os_version": batch.os_version,
         "app_version": batch.app_version, "sync_trigger": batch.sync_trigger},
    )


@router.post("/api/ingest/healthkit-export")
async def ingest_healthkit_export(request: Request, profile: Optional[str] = None,
                                 principal: auth.Principal = Depends(auth.require_device_or_user)) -> dict:
    """Accepts HealthKit export JSON (the Syntropy iPhone app's JSON export, or another HealthKit export
    app's REST automation). Authenticate with a device token in
    ``X-Syntropy-Device-Token`` or ``Authorization: Bearer``."""
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "Body must be JSON.")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Expected a JSON object with a 'data' key.")
    try:
        converted = await run_in_threadpool(healthkit_import.convert, payload)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise HTTPException(422, f"Could not read the export: {exc}")
    profile_id, connection_id = _ingest_target(principal, profile)
    name = request.headers.get("automation-name") or "HealthKit export"
    return await run_in_threadpool(
        _store, profile_id, connection_id, converted["samples"], converted["workouts"], converted["events"],
        {"device_id": principal.device_id or "healthkit-export", "device_name": name[:200],
         "sync_trigger": "healthkit-export"})


@router.get("/api/biometrics/workouts", dependencies=[user])
def workouts(profile: Optional[str] = None, days: int = Query(90, ge=0, le=36500),
             limit: int = Query(200, ge=1, le=2000), start: Optional[str] = Query(None, max_length=40),
             end: Optional[str] = Query(None, max_length=40), type: Optional[str] = Query(None, max_length=120)) -> dict:
    """Workouts newest first. ``start``/``end`` (dates or times) pick a window instead of the last ``days``; ``type``
    keeps one kind of workout. ``types`` lists every kind in the window, for filters."""
    prof = resolve_profile(profile)
    window = {"start": start, "end": end}
    try:
        everything = activity.workout_summary(prof["id"], days or None, **window)
        summary = activity.workout_summary(prof["id"], days or None, name=type, **window) if type else everything
        items = activity.list_workouts(prof["id"], days or None, limit, name=type, **window)
    except ValueError:
        raise HTTPException(400, "Dates must look like 2026-01-31.")
    return {"workouts": items, "summary": summary, "types": [{"name": t["name"], "count": t["count"]} for t in everything["by_type"]]}


@router.get("/api/biometrics/workouts/{workout_id}", dependencies=[user])
def workout_detail(workout_id: str, profile: Optional[str] = None, split: str = Query("km", pattern="^(km|mi)$")) -> dict:
    """One workout with its route, heart rate, zones, load, pace and splits (per kilometre or mile)."""
    prof = resolve_profile(profile)
    w = activity.get_workout(prof["id"], workout_id)
    if not w:
        raise HTTPException(404, "Workout not found")
    return training.detail(prof["id"], w, 1609.344 if split == "mi" else 1000.0)


@router.get("/api/biometrics/training", dependencies=[user])
def training_summary(profile: Optional[str] = None, weeks: int = Query(12, ge=1, le=260),
                     type: Optional[str] = Query(None, max_length=120)) -> dict:
    """Weekly volume and load, this week's load against the four before, heart-rate zones, and for one kind of workout
    its progress and best efforts."""
    prof = resolve_profile(profile)
    return training.summary(prof["id"], weeks, type)


@router.get("/api/biometrics/events", dependencies=[user])
def events(profile: Optional[str] = None, days: int = Query(90, ge=0, le=36500), type: Optional[str] = None,
           category: Optional[str] = None, limit: int = Query(500, ge=1, le=5000), start: Optional[str] = Query(None, max_length=40),
           end: Optional[str] = Query(None, max_length=40), q: Optional[str] = Query(None, max_length=100),
           source: Optional[str] = Query(None, pattern="^(journal|devices)$"),
           view: Optional[str] = Query(None, pattern="^(journal|signals)$")) -> dict:
    """Health events: symptoms, cycle tracking, mood, ECGs and notifications from devices, and entries logged here
    (``source=journal``) or only what devices sent (``source=devices``). ``view=journal`` keeps how the person feels
    (the Journal page); ``view=signals`` keeps ECGs and device notifications (Trends)."""
    prof = resolve_profile(profile)
    manual = None if source is None else source == "journal"
    try:
        # The unfiltered journal hides hourly stand events; exports and category filters still include them.
        items = activity.list_events(prof["id"], days or None, type, category, limit, hide_routine=True, start=start, end=end,
                                     search=(q or "").strip() or None, manual=manual, view=view)
        summary = activity.event_summary(prof["id"], days or None, start=start, end=end, view=view)
    except ValueError:
        raise HTTPException(400, "Dates must look like 2026-01-31.")
    return {"events": items, "summary": summary, "categories": activity.EVENT_CATEGORIES}


class CheckInDetails(BaseModel):
    energy: Optional[int] = Field(None, ge=1, le=5)
    stress: Optional[int] = Field(None, ge=1, le=5)


class JournalEntryIn(BaseModel):
    """An entry logged in the Journal. The web app picks the type, name and wording (symptom names match Apple
    Health's, so they sit alongside what the iPhone sends)."""
    model_config = ConfigDict(allow_inf_nan=False)

    event_type: str = Field(..., min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    category: str = Field(..., max_length=40)
    name: str = Field(..., min_length=1, max_length=120)
    start: str = Field(..., max_length=40)
    end: Optional[str] = Field(None, max_length=40)
    value: Optional[float] = None
    value_label: Optional[str] = Field(None, max_length=200)
    note: Optional[str] = Field(None, max_length=4000)
    details: Optional[CheckInDetails] = None


def _entry(req: JournalEntryIn) -> dict:
    if req.category not in activity.EVENT_CATEGORIES:
        raise HTTPException(400, "Unknown journal category.")
    entry = req.model_dump()
    entry["name"] = entry["name"].strip()
    entry["note"] = (entry.get("note") or "").strip() or None
    entry["details"] = {k: v for k, v in (entry.get("details") or {}).items() if v is not None} or None
    try:
        if entry.get("end") and activity.to_utc_iso(entry["end"]) < activity.to_utc_iso(entry["start"]):
            raise HTTPException(400, "The end is before the start.")
    except ValueError:
        raise HTTPException(400, "Times must be ISO 8601, e.g. 2026-01-31T08:30:00Z.")
    return entry


@router.post("/api/journal", dependencies=[user])
async def create_journal_entry(req: JournalEntryIn, profile: Optional[str] = None) -> dict:
    prof = resolve_profile(profile)
    return activity.create_entry(prof["id"], _entry(req))


@router.put("/api/journal/{entry_id}", dependencies=[user])
async def update_journal_entry(entry_id: str, req: JournalEntryIn, profile: Optional[str] = None) -> dict:
    prof = resolve_profile(profile)
    updated = activity.update_entry(prof["id"], entry_id, _entry(req))
    if not updated:
        raise HTTPException(404, "Only entries logged in the Journal can be changed.")
    return updated


@router.delete("/api/journal/{entry_id}", dependencies=[user])
async def delete_journal_entry(entry_id: str, profile: Optional[str] = None) -> dict:
    prof = resolve_profile(profile)
    if not activity.delete_entry(prof["id"], entry_id):
        raise HTTPException(404, "Only entries logged in the Journal can be deleted.")
    return {"ok": True}


@router.get("/api/wearables/summary")
def wearables_summary(principal: auth.Principal = Depends(auth.require_device_or_user)) -> dict:
    """Used by the companion app to show the server-side total."""
    profile_id = principal.profile_id or resolve_profile(None)["id"]
    ov = biometrics.overview(profile_id)
    return {"success": True, "data": {"total_samples": ov["total_samples"], "history_span": ov["history_span"],
                                      "last_sync": ov["last_batch"]}}


class HistoryStatus(BaseModel):
    complete: bool
    sent: int = Field(0, ge=0)
    types_done: Optional[int] = Field(None, ge=0)
    types_total: Optional[int] = Field(None, ge=0)
    current: Optional[str] = Field(None, max_length=120)


@router.post("/api/wearables/history")
def history_status(body: HistoryStatus, principal: auth.Principal = Depends(auth.require_device_or_user)) -> dict:
    """The iPhone app says how far it is with sending Health's history, so Sources can say it isn't finished yet
    rather than looking done at the first few hundred thousand samples."""
    if principal.kind != "device":
        raise HTTPException(400, "Only a paired device reports its history.")
    dev = devices.get_device(principal.device_id or "")
    connection = connections.get(dev["connection_id"]) if dev and dev.get("connection_id") else None
    if not connection:
        raise HTTPException(404, "This device has no source.")
    metadata = {**(connection.get("metadata") or {}), "history": {**body.model_dump(), "updated_at": time.time()}}
    connections.update(connection["id"], metadata=metadata)
    return {"ok": True}


@router.get("/api/wearables/pair")
def pairing_info(request: Request) -> dict:
    """Public reachability probe for the companion app (no secrets)."""
    origin = request_origin(request)
    return {"service": "syntropy-health", "version": config.APP_VERSION, "server_url": origin,
            "pair_endpoint": f"{origin}/api/devices/pair", "ingest_endpoint": f"{origin}/api/ingest/wearables",
            # Uploads may be compressed (health samples shrink about tenfold); older servers leave this out.
            "upload_encodings": list(UPLOAD_ENCODINGS),
            # The web app can run inside the iPhone app's native navigation (tab bar, title bars): companion mode.
            "app_shell": 1}


class PairingCodeRequest(BaseModel):
    profile_id: Optional[str] = None


@router.post("/api/devices/pairing-code", dependencies=[user])
async def pairing_code(req: PairingCodeRequest, request: Request) -> dict:
    """A code for a phone to send one person's Health data (someone the account manages: itself, or a child)."""
    prof = resolve_profile(req.profile_id, "manage")
    principal = context.principal()
    code = devices.create_pairing_code(prof["id"], principal.account_id if principal else None)
    return {**code, "server_url": phone_origin(request), "profile": prof}


class PairRequest(BaseModel):
    code: str = Field(..., max_length=20)
    device_name: str = Field("iPhone", max_length=80)
    platform: Optional[str] = Field("ios", max_length=20)


_pair_failures: dict[str, list[float]] = {}
PAIR_WINDOW, PAIR_MAX_FAILURES = 900, 10


@router.post("/api/devices/pair")
async def pair(req: PairRequest, request: Request) -> dict:
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    recent = [t for t in _pair_failures.get(ip, []) if now - t < PAIR_WINDOW]
    if len(recent) >= PAIR_MAX_FAILURES:
        raise HTTPException(429, "Too many attempts. Wait a few minutes and try again.")
    result = devices.claim(req.code, req.device_name, req.platform)
    if not result:
        _pair_failures[ip] = recent + [now]
        raise HTTPException(400, "That pairing code is invalid or has expired.")
    _pair_failures.pop(ip, None)
    return {**result, "server_url": request_origin(request)}


@router.post("/api/devices/web-session")
async def web_session(request: Request, principal: auth.Principal = Depends(auth.require_device_or_user)) -> dict:
    """Signs the paired iPhone app's Syntropy tab in to the web app, so it never asks for the password. The app
    sets the returned cookie in its web view. Each device has at most one such session, and revoking the device
    ends it."""
    if principal.kind != "device":
        raise HTTPException(403, "Only a paired device can open an app session.")
    if not auth.auth_required():
        return {"cookie": None}
    # Signed in as whoever paired it: the person's own phone opens everything they can see; a phone someone paired
    # for a person they look after opens only that person (see auth._account_principal).
    account = accounts.get(principal.account_id) or accounts.for_profile(principal.profile_id or "")
    if not account:
        raise HTTPException(403, "Pair this iPhone again to open Syntropy Health in the app.")
    token = auth.create_session(request, account["id"], device_id=principal.device_id)
    return {"cookie": {"name": auth.SESSION_COOKIE, "value": token, "max_age": auth.SESSION_TTL}}


@router.post("/api/devices/self/revoke")
async def revoke_self(principal: auth.Principal = Depends(auth.require_device_or_user)) -> dict:
    """The app disconnecting: revokes its own token (and web session) so it doesn't linger as a source."""
    if principal.kind != "device":
        raise HTTPException(403, "Only a paired device can revoke itself.")
    devices.revoke(principal.device_id)
    return {"ok": True}


@router.get("/api/devices", dependencies=[user])
def list_devices(profile: Optional[str] = None) -> dict:
    prof = resolve_profile(profile) if profile else None
    visible = visible_profile_ids()
    return {"devices": [d for d in devices.list_devices(prof["id"] if prof else None)
                        if visible is None or d["profile_id"] in visible]}


@router.delete("/api/devices/{device_id}", dependencies=[user])
async def revoke_device(device_id: str) -> dict:
    device = devices.get_device(device_id)
    if not device:
        raise HTTPException(404, "Device not found")
    check_access(device["profile_id"], "manage")
    devices.revoke(device_id)
    return {"ok": True}
