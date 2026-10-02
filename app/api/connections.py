"""Connections: linking health record portals and wearables, OAuth callbacks, syncing."""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.api.deps import check_access, request_origin, resolve_profile
from app.connectors import smart
from app.connectors.smart import SmartError
from app.core import auth
from app.services import connect, sync
from app.store import biometrics, connections, devices, records

log = logging.getLogger("syntropy.api.connections")

router = APIRouter(tags=["connections"])
user = Depends(auth.require_user)


def _present(conn: dict[str, Any]) -> dict[str, Any]:
    conn["syncing"] = sync.is_running(conn["id"])
    runs = connections.recent_runs(conn["id"], 1)
    conn["last_run"] = runs[0] if runs else None
    return conn


@router.get("/api/connections", dependencies=[user])
def list_connections(profile: Optional[str] = None, include_disconnected: bool = False) -> dict:
    prof = resolve_profile(profile)
    items = connections.list_for_profile(prof["id"], include_disconnected=include_disconnected)
    return {"profile_id": prof["id"], "connections": [_present(c) for c in items]}


def _mine(connection_id: str) -> dict:
    """A source of someone the signed-in account can see (and, to change it, manage)."""
    conn = connections.get(connection_id)
    if not conn:
        raise HTTPException(404, "Connection not found")
    check_access(conn["profile_id"])
    return conn


@router.get("/api/connections/{connection_id}", dependencies=[user])
def get_connection(connection_id: str) -> dict:
    conn = _mine(connection_id)
    out = _present(conn)
    out["runs"] = connections.recent_runs(connection_id, 20)
    return out


class EhrConnectRequest(BaseModel):
    profile_id: Optional[str] = None
    institution_id: Optional[str] = None
    fhir_base_url: Optional[str] = Field(None, max_length=500)
    client_id: Optional[str] = Field(None, max_length=200)
    platform: Optional[str] = None


@router.post("/api/connections/ehr", dependencies=[user])
async def connect_ehr(req: EhrConnectRequest, request: Request) -> dict:
    prof = resolve_profile(req.profile_id)
    if not req.institution_id and not req.fhir_base_url:
        raise HTTPException(400, "Choose an institution or enter a FHIR server URL.")
    try:
        return await connect.start_ehr(prof["id"], request_origin(request), institution_id=req.institution_id,
                                       fhir_base_url=req.fhir_base_url, client_id=req.client_id,
                                       platform="custom" if req.fhir_base_url and not req.institution_id else req.platform)
    except SmartError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/connections/{connection_id}/reconnect", dependencies=[user])
async def reconnect(connection_id: str, request: Request) -> dict:
    conn = _mine(connection_id)
    try:
        if conn["kind"] == "ehr":
            return await connect.start_ehr(conn["profile_id"], request_origin(request), reconnect_id=connection_id)
        if conn["kind"] == "wearable" and conn["mode"] == "live":
            return connect.start_wearable(conn["profile_id"], conn["provider"], request_origin(request))
    except SmartError as exc:
        raise HTTPException(400, str(exc))
    raise HTTPException(400, "This source cannot be re-authorized.")


class WearableConnectRequest(BaseModel):
    profile_id: Optional[str] = None
    provider: str
    mode: str = "live"


@router.post("/api/connections/wearable", dependencies=[user])
async def connect_wearable(req: WearableConnectRequest, request: Request, background: BackgroundTasks) -> dict:
    prof = resolve_profile(req.profile_id)
    try:
        result = connect.start_wearable(prof["id"], req.provider, request_origin(request),
                                        "simulated" if req.mode == "simulated" else "live")
    except SmartError as exc:
        raise HTTPException(400, str(exc))
    if result.get("connection_id"):
        background.add_task(sync.sync_connection, result["connection_id"], "initial")
    return result


@router.post("/api/connections/{connection_id}/sync", dependencies=[user])
async def sync_now(connection_id: str) -> dict:
    _mine(connection_id)
    try:
        return await sync.sync_connection(connection_id, "manual")
    except LookupError:
        raise HTTPException(404, "Connection not found")


class RenameRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=120)


@router.patch("/api/connections/{connection_id}", dependencies=[user])
async def rename(connection_id: str, req: RenameRequest) -> dict:
    _mine(connection_id)
    connections.update(connection_id, display_name=req.display_name)
    return connections.get(connection_id)  # type: ignore[return-value]


@router.delete("/api/connections/{connection_id}", dependencies=[user])
async def remove(connection_id: str, delete_data: bool = False) -> dict:
    conn = _mine(connection_id)
    if conn["kind"] == "device":
        devices.revoke_for_connection(connection_id)   # the phone stops sending to a source that's gone
    if delete_data:
        records.delete_for_connection(connection_id)
        biometrics.delete_for_connection(connection_id)
    connections.disconnect(connection_id, delete_data=delete_data)
    return {"ok": True}


class DiscoverRequest(BaseModel):
    fhir_base_url: str = Field(..., max_length=500)


@router.post("/api/connections/discover", dependencies=[user])
async def discover(req: DiscoverRequest) -> dict:
    if not req.fhir_base_url.startswith("https://"):
        raise HTTPException(400, "FHIR servers must use https.")
    try:
        return await smart.discover(req.fhir_base_url)
    except SmartError as exc:
        raise HTTPException(400, str(exc))


# ---------------------------------------------------------------------------
# OAuth callbacks (public: authenticated by the one-time state)
# ---------------------------------------------------------------------------

def _done(connection_id: Optional[str] = None, error: Optional[str] = None) -> RedirectResponse:
    if error:
        return RedirectResponse(f"/#/sources?error={quote(error[:400])}", status_code=303)
    return RedirectResponse(f"/#/sources?connected={connection_id}", status_code=303)


async def _complete(pending_kind: str, pending: dict[str, Any], *, code: Optional[str] = None,
                    tokens: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if pending_kind == "smart":
        return await connect.complete_ehr(pending, code or "")
    return await connect.complete_wearable(pending, code=code, tokens=tokens)


RELAY_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Finishing connection…</title>
<link rel="icon" href="/static/img/icon.svg" type="image/svg+xml">
<link rel="icon" href="/static/img/favicon-32.png" sizes="32x32" type="image/png" media="(prefers-color-scheme: light)">
<link rel="icon" href="/static/img/favicon-32-dark.png" sizes="32x32" type="image/png" media="(prefers-color-scheme: dark)">
<style>body{font:15px -apple-system,system-ui,sans-serif;background:#f6f7f9;color:#0b0b0e;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0}p{opacity:.8}
@media (prefers-color-scheme:dark){body{background:#0b0b0e;color:#e7e7ea}}</style></head><body><p id="m">Finishing connection…</p>
<script>
(function(){
  var h = new URLSearchParams(location.hash.slice(1));
  history.replaceState(null, "", location.pathname);
  if (!h.get("access_token") || !h.get("state")) { location.replace("/#/sources?error=" + encodeURIComponent("The relay did not return any credentials.")); return; }
  var body = {}; h.forEach(function(v,k){ body[k] = v; });
  fetch("/api/connections/oauth/relay", {method:"POST", headers:{"Content-Type":"application/json","X-Requested-With":"syntropy"},
    body: JSON.stringify(body), credentials: "same-origin"})
   .then(function(r){ return r.json().then(function(j){ return [r.ok, j]; }); })
   .then(function(res){ location.replace(res[0] ? "/#/sources?connected=" + encodeURIComponent(res[1].connection_id)
                                                 : "/#/sources?error=" + encodeURIComponent(res[1].detail || "Connection failed")); })
   .catch(function(e){ location.replace("/#/sources?error=" + encodeURIComponent(String(e))); });
})();
</script></body></html>"""
# Only the page's own script may run, and it may only talk to this server.
_RELAY_SCRIPT = RELAY_PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
RELAY_CSP = ("default-src 'none'; script-src 'sha256-" + base64.b64encode(hashlib.sha256(_RELAY_SCRIPT.encode()).digest()).decode()
             + "'; style-src 'unsafe-inline'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
             "form-action 'none'")


@router.get("/callback", include_in_schema=False)
async def oauth_callback(request: Request, background: BackgroundTasks, code: Optional[str] = None,
                         state: Optional[str] = None, error: Optional[str] = None,
                         error_description: Optional[str] = None, desc: Optional[str] = None):
    if error:
        if state:
            connections.pop_pending(state)
        message = error_description or desc or error
        if error == "access_denied":
            message = "Access was not granted. No records were shared."
        return _done(error=message)
    if not code or not state:
        # Token hand-off from the relay arrives in the URL fragment, which only the browser can read.
        return HTMLResponse(RELAY_PAGE, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                                  "Content-Security-Policy": RELAY_CSP})
    popped = connections.pop_pending(state)
    if not popped:
        return _done(error="This sign-in link has expired or was already used. Please start the connection again.")
    kind, pending = popped
    try:
        result = await _complete(kind, pending, code=code)
    except SmartError as exc:
        return _done(error=str(exc))
    background.add_task(sync.sync_connection, result["connection_id"], "initial")
    return _done(result["connection_id"])


class RelayTokens(BaseModel):
    state: str = Field(..., max_length=2000)
    access_token: str = Field(..., max_length=8000)
    refresh_token: Optional[str] = Field(None, max_length=8000)
    expires_in: Optional[float] = None
    scope: Optional[str] = Field(None, max_length=2000)
    relay_provider: Optional[str] = None


@router.post("/api/connections/oauth/relay", include_in_schema=False)
async def relay_complete(req: RelayTokens, request: Request, background: BackgroundTasks) -> dict:
    if request.headers.get(auth.CSRF_HEADER, "").lower() != auth.CSRF_VALUE:
        raise HTTPException(403, "Missing X-Requested-With: syntropy header.")
    popped = connections.pop_pending(req.state)
    if not popped or popped[0] not in connect.WEARABLE_MODULES:
        raise HTTPException(400, "This sign-in link has expired or was already used. Please start the connection again.")
    kind, pending = popped
    try:
        result = await _complete(kind, pending, tokens=req.model_dump(exclude={"state", "relay_provider"}))
    except SmartError as exc:
        raise HTTPException(400, str(exc))
    background.add_task(sync.sync_connection, result["connection_id"], "initial")
    return result
