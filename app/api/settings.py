"""Instance settings: EHR platform modes, wearable credentials, relay & sync configuration."""

from __future__ import annotations

from typing import Optional
from zoneinfo import available_timezones

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import directory
from app.core import auth, config, context, settings
from app.store import accounts, biometrics

# Anyone signed in can read these; changing them (they affect everyone on the server) is for owners. Units are each
# person's own.
router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(auth.require_user)])
owner = Depends(auth.require_owner)


def _source_order() -> dict:
    return {"orders": biometrics.source_orders(), "kinds": biometrics.SOURCE_KINDS,
            "groups": {g: {"label": spec["label"], "hint": spec["hint"], "default": spec["default"]}
                       for g, spec in biometrics.PRIORITY_GROUPS.items()}}


def _snapshot() -> dict:
    wearables = {}
    for provider, meta in settings.WEARABLES.items():
        creds = settings.wearable_credentials(provider)
        wearables[provider] = {"label": meta["label"], "client_id": creds["client_id"],
                               "client_secret_set": bool(creds["client_secret"])}
    platforms = []
    for key in settings.EHR_PLATFORMS:
        cfg = settings.platform_config(key)
        cfg["sandbox_base"] = directory.PLATFORMS[key]["sandbox_base"]
        cfg["sandbox_hint"] = directory.PLATFORMS[key]["sandbox_hint"]
        platforms.append(cfg)
    return {
        "platforms": platforms,
        "wearables": wearables,
        "relay": {"redirect_uri": settings.get("relay.redirect_uri"),
                  "wearable_redirect_uri": settings.get("relay.wearable_redirect_uri"),
                  "default_redirect_uri": config.DEFAULT_RELAY_URL,
                  "default_wearable_redirect_uri": config.DEFAULT_WEARABLE_RELAY_URL},
        "sync": {"ehr_interval_hours": settings.get("sync.ehr_interval_hours"),
                 "wearable_interval_hours": settings.get("sync.wearable_interval_hours"),
                 "scheduler_enabled": config.scheduler_enabled()},
        "source_order": _source_order(),
        "timezone": settings.timezone_name(),
        "units": _my_units(),
        "auth_required": auth.auth_required(),
        "data_dir": str(config.data_dir()),
        "version": config.APP_VERSION,
    }


def _my_units() -> Optional[str]:
    principal = context.principal()
    account = accounts.get(principal.account_id) if principal and principal.account_id else None
    return (account or {}).get("prefs", {}).get("units")


@router.get("")
def get_settings() -> dict:
    return _snapshot()


class PlatformPatch(BaseModel):
    mode: Optional[str] = None
    client_id: Optional[str] = Field(None, max_length=200)


@router.put("/platforms/{platform}", dependencies=[owner])
async def update_platform(platform: str, req: PlatformPatch) -> dict:
    try:
        return settings.set_platform_config(platform, req.mode, req.client_id)
    except KeyError:
        raise HTTPException(404, "Unknown platform")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class WearablePatch(BaseModel):
    client_id: Optional[str] = Field(None, max_length=200)
    client_secret: Optional[str] = Field(None, max_length=400)


@router.put("/wearables/{provider}", dependencies=[owner])
async def update_wearable(provider: str, req: WearablePatch) -> dict:
    try:
        settings.set_wearable_credentials(provider, req.client_id, req.client_secret)
    except KeyError:
        raise HTTPException(404, "Unknown wearable")
    return _snapshot()["wearables"][provider]


class GeneralPatch(BaseModel):
    redirect_uri: Optional[str] = None
    wearable_redirect_uri: Optional[str] = None
    ehr_interval_hours: Optional[float] = Field(None, ge=1, le=24 * 30)
    wearable_interval_hours: Optional[float] = Field(None, ge=0.5, le=24 * 7)
    timezone: Optional[str] = None
    units: Optional[str] = None     # "metric", "us", or "" to follow the browser


@router.put("/source-order", dependencies=[owner])
async def update_source_order(orders: dict[str, list[str]]) -> dict:
    """Which kind of device wins, per group of metrics, when several report the same day."""
    try:
        biometrics.set_source_orders(orders)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _source_order()


@router.put("/general")
async def update_general(req: GeneralPatch) -> dict:
    principal = context.principal()
    if req.units is not None:
        if req.units not in ("", "metric", "us"):
            raise HTTPException(400, "Units must be metric or us.")
        if principal and principal.account_id:
            accounts.set_pref(principal.account_id, "units", req.units or None)
    server_wide = req.model_dump(exclude_none=True, exclude={"units"})
    if server_wide and principal and not principal.owner:
        raise HTTPException(403, "Only the server's owner can change this.")
    for value in (req.redirect_uri, req.wearable_redirect_uri):
        if value is not None and value and not value.startswith(("http://", "https://")):
            raise HTTPException(400, "Redirect URIs must be absolute http(s) URLs.")
    if req.redirect_uri is not None:
        settings.set("relay.redirect_uri", req.redirect_uri or None)
    if req.wearable_redirect_uri is not None:
        settings.set("relay.wearable_redirect_uri", req.wearable_redirect_uri or None)
    if req.ehr_interval_hours is not None:
        settings.set("sync.ehr_interval_hours", req.ehr_interval_hours)
    if req.wearable_interval_hours is not None:
        settings.set("sync.wearable_interval_hours", req.wearable_interval_hours)
    if req.timezone is not None:
        if req.timezone and req.timezone not in available_timezones():
            raise HTTPException(400, "Unknown time zone.")
        settings.set("display.timezone", req.timezone or None)
        from app.store import biometrics, profiles
        for prof in profiles.list_profiles():
            biometrics.rebuild_daily(prof["id"])
    return _snapshot()


@router.get("/timezones")
def timezones() -> dict:
    # Region/City names, plus UTC (servers and containers often run on it), plus whatever is in use now even if it's an
    # older name, so the list always shows the current setting rather than falling back to its first entry.
    zones = {tz for tz in available_timezones() if "/" in tz and not tz.startswith(("Etc/", "SystemV/"))}
    current = settings.timezone_name()
    zones.discard("UTC")
    extra = [current] if current not in zones and current != "UTC" and current in available_timezones() else []
    return {"timezones": ["UTC", *extra, *sorted(zones)]}


# Live probes: ask each vendor's authorization server whether it recognizes a client ID.
PROBES = {
    "epic": ("https://fhir.epic.com/interconnect-fhir-oauth/oauth2/authorize",
             "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4"),
    "cerner": ("https://authorization.cerner.com/tenants/ec2458f2-1e24-41c8-b71b-0e701af7583d/protocols/oauth2/profiles/smart-v1/personas/patient/authorize",
               "https://fhir-myrecord.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d"),
    "va": ("https://sandbox-api.va.gov/oauth2/health/v1/authorization", "https://sandbox-api.va.gov/services/fhir/v0/r4"),
    "athena": ("https://api.preview.platform.athenahealth.com/oauth2/v1/authorize",
               "https://api.preview.platform.athenahealth.com/fhir/r4"),
}


@router.post("/platforms/{platform}/verify", dependencies=[owner])
async def verify_platform(platform: str) -> dict:
    """Checks a sandbox client ID against the vendor's live authorization server."""
    cfg = settings.platform_config(platform, "sandbox")
    if not cfg["client_id"]:
        return {"ok": False, "status": "not_configured", "detail": "No client ID configured."}
    if platform not in PROBES:
        return {"ok": None, "status": "unverifiable", "detail": "This vendor does not support a pre-flight check."}
    authorize, aud = PROBES[platform]
    params = {"response_type": "code", "client_id": cfg["client_id"], "redirect_uri": settings.get("relay.redirect_uri"),
              "scope": "openid launch/patient patient/Patient.read", "state": "probe", "aud": aud,
              "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM", "code_challenge_method": "S256"}
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
            resp = await client.get(authorize, params=params)
    except httpx.HTTPError:
        return {"ok": False, "status": "network_error", "detail": f"Couldn't reach {cfg['label']}'s sign-in server."}
    location = resp.headers.get("location", "")
    bad = any(s in (location + resp.text[:2000]).lower() for s in ("unknown-client", "invalid_client", "unauthorized_client", "invalid client"))
    if resp.status_code in (200, 302, 303) and not bad and "error=" not in location:
        return {"ok": True, "status": "recognized", "detail": "The authorization server accepted this client ID and redirect URI."}
    return {"ok": False, "status": "rejected", "detail": f"HTTP {resp.status_code}: the authorization server rejected this client ID or redirect URI."}
