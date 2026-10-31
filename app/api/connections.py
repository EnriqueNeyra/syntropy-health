"""Connections: linking health record portals and wearables, OAuth callbacks, syncing."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
import time
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from app.api.deps import check_access, request_origin, resolve_profile
from app.connectors import smart
from app.connectors.smart import SmartError
from app.core import auth, context, security
from app.services import connect, sync
from app.store import accounts, biometrics, connections, devices, profiles, records

log = logging.getLogger("syntropy.api.connections")

router = APIRouter(tags=["connections"])
user = Depends(auth.require_user)


SYNC_NOW_WAIT = 25.0     # a sync that takes longer carries on; the page watches it, and a proxy doesn't time it out
_running: set[asyncio.Task] = set()


def _present(conn: dict[str, Any]) -> dict[str, Any]:
    conn["syncing"] = sync.is_running(conn["id"])
    runs = connections.recent_runs(conn["id"], 1)
    conn["last_run"] = runs[0] if runs else None
    if conn["kind"] == "ehr":
        conn["removed_count"] = records.removed_count(conn["id"])
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
    out["queries"] = sync.describe_queries(out["runs"])
    out["record_counts"] = records.counts_for_connection(connection_id)
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
    # An institution means the address the directory has for it: never one sent along with it. A saved address (for an
    # institution that has left the directory) only comes through a reconnect.
    custom = not req.institution_id
    try:
        return await connect.start_ehr(prof["id"], request_origin(request), institution_id=req.institution_id,
                                       fhir_base_url=req.fhir_base_url if custom else None, client_id=req.client_id,
                                       platform="custom" if custom else None)
    except SmartError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/connections/{connection_id}/reconnect", dependencies=[user])
async def reconnect(connection_id: str, request: Request) -> dict:
    conn = _mine(connection_id)
    try:
        if conn["kind"] == "ehr":
            return await connect.start_ehr(conn["profile_id"], request_origin(request), reconnect_id=connection_id)
        if conn["kind"] == "wearable" and conn["mode"] == "live":
            return await run_in_threadpool(connect.start_wearable, conn["profile_id"], conn["provider"], request_origin(request))
    except SmartError as exc:
        raise HTTPException(400, str(exc))
    raise HTTPException(400, "This source cannot be re-authorized.")


@router.post("/api/connections/{connection_id}/fetch-history", dependencies=[user])
def fetch_history(connection_id: str, background: BackgroundTasks) -> dict:
    """Asks a live wearable for everything it still has from before this source was connected."""
    conn = _mine(connection_id)
    if conn["kind"] != "wearable" or conn["mode"] != "live":
        raise HTTPException(400, "Only a connected wearable has older history to fetch.")
    sync.ask_for_history(conn)
    background.add_task(sync.fetch_whole_history, connection_id)
    return {"ok": True}


class WearableConnectRequest(BaseModel):
    profile_id: Optional[str] = None
    provider: str
    mode: str = "live"


@router.post("/api/connections/wearable", dependencies=[user])
def connect_wearable(req: WearableConnectRequest, request: Request, background: BackgroundTasks) -> dict:
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
    await run_in_threadpool(_mine, connection_id)
    task = asyncio.create_task(sync.sync_connection(connection_id, "manual"))
    _running.add(task)
    task.add_done_callback(_running.discard)
    await asyncio.wait({task}, timeout=SYNC_NOW_WAIT)
    if not task.done():
        return {"status": "running", "reason": "This is taking a while, so it's carrying on by itself. This page shows how it went."}
    try:
        return task.result()
    except LookupError:
        raise HTTPException(404, "Connection not found")


@router.get("/api/connections/{connection_id}/removed", dependencies=[user])
def removed_records(connection_id: str) -> dict:
    """Records this source stopped returning or took back. They're out of the record, and can be put back."""
    _mine(connection_id)
    return {"items": records.removed_for_connection(connection_id), "keep_days": records.REMOVED_KEEP_DAYS}


@router.post("/api/connections/{connection_id}/removed/{record_id}/restore", dependencies=[user])
def restore_record(connection_id: str, record_id: str) -> dict:
    _mine(connection_id)
    item = records.get_removed(record_id)
    if not item or item["connection_id"] != connection_id:
        raise HTTPException(404, "That record isn't among this source's removed records.")
    records.restore_removed(record_id)
    return {"ok": True}


class RenameRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=120)


@router.patch("/api/connections/{connection_id}", dependencies=[user])
def rename(connection_id: str, req: RenameRequest) -> dict:
    _mine(connection_id)
    connections.update(connection_id, display_name=req.display_name)
    return connections.get(connection_id)  # type: ignore[return-value]


@router.delete("/api/connections/{connection_id}", dependencies=[user])
def remove(connection_id: str, delete_data: bool = False) -> dict:
    conn = _mine(connection_id)
    if sync.is_running(connection_id):
        raise HTTPException(409, "This source is syncing right now. Try again in a minute.")
    if conn["kind"] == "device":
        devices.revoke_for_connection(connection_id)   # the phone stops sending to a source that's gone
    if delete_data:
        records.delete_for_connection(connection_id)
    connections.disconnect(connection_id, delete_data=delete_data)
    if delete_data:
        biometrics.delete_for_connection(connection_id, conn["profile_id"])
    return {"ok": True}


class DiscoverRequest(BaseModel):
    fhir_base_url: str = Field(..., max_length=500)


@router.post("/api/connections/discover", dependencies=[user])
async def discover(req: DiscoverRequest) -> dict:
    smart.GUARDED.set(not connect.acting_as_owner())
    try:
        await connect.check_custom_server(req.fhir_base_url)
        return await smart.discover(req.fhir_base_url)
    except SmartError as exc:
        raise HTTPException(400, str(exc))


# ---------------------------------------------------------------------------
# OAuth callbacks (public: authenticated by the one-time state)
# ---------------------------------------------------------------------------

STARTED_ELSEWHERE = "This sign-in was started by someone else on this server, so nothing was added. Start it from your own account."
EXPIRED = "This sign-in link has expired or was already used. Please start the connection again."
CHOICE_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
CHOICE_CSS = """body{font:15px/1.5 -apple-system,system-ui,sans-serif;background:#f6f7f9;color:#0b0b0e;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0;padding:16px;box-sizing:border-box}main{max-width:460px;background:#fff;border-radius:14px;
padding:24px;box-shadow:0 8px 30px rgba(20,30,60,.08)}h1{font-size:20px;margin:0 0 10px}form{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
button{border:0;border-radius:9px;padding:11px 16px;font:600 15px -apple-system,system-ui,sans-serif;cursor:pointer;background:#eef0f4;color:#1f2330}
button.go{background:#2f5bd3;color:#fff}@media (prefers-color-scheme:dark){body{background:#0b0b0e;color:#e7e7ea}main{background:#17171b}
button{background:#2a2a30;color:#e7e7ea}}"""


def _may_finish(request: Request, pending: dict[str, Any]) -> str:
    """Whether the browser a sign-in came back to may finish it. Signing in at a portal is what hands records over, so a
    link sent to someone else mustn't quietly put their records under another account.

    A browser signed in to an account that manages that person goes straight on: whoever started it, if they still do. It
    could have been started by someone who has since lost access, and a sign-in lasts a quarter of an hour. A browser signed
    in to another account is turned away. A browser with no session is ordinary: the Mac and Windows apps and the
    phone can come back through the system browser, or by another address. With one account on this server there's no one
    else it could be for; with several, it asks ('ask'), naming who the records will go to."""
    who = auth.current_user(request)
    if who is not None:
        return "go" if who.level(pending.get("profile_id")) == "manage" else "refuse"
    return "ask" if len(accounts.list_accounts()) > 1 else "go"


async def _ask(choice: dict[str, Any]) -> str:
    """Holds what's been received until the person decides; returns the address of the page that asks."""
    ticket = security.random_token(24)
    await run_in_threadpool(connections.save_pending, ticket, "choice", choice)
    return f"/callback/choose?state={quote(ticket)}"


def _choice_text(choice: dict[str, Any]) -> tuple[str, str, str, str]:
    if choice["action"] == "other_patient":
        new, was = choice.get("name") or "someone else", choice.get("was") or "the person it was connected for"
        return (f"This is {new}'s chart",
                f"You signed in to {new}'s chart at {choice['display_name']}, but this source holds {was}'s records. Nothing has "
                f"changed: {was}'s records are as they were. You can add {new} as a separate source, or go back and sign in "
                "again with the right chart.", f"Add {new} as a new source", "Leave it as it was")
    person = (profiles.get_profile(choice["pending"].get("profile_id", "")) or {}).get("name", "another person")
    return (f"Add these records to {person}?",
            f"This sign-in was started on this Syntropy Health server, for {person}. Continuing stores the records you just shared "
            f"there. If you didn't mean to share your records with {person}, cancel. Nothing has been saved yet.",
            f"Add to {person}", "Cancel")


@router.get("/callback/choose", include_in_schema=False)
def choose_page(state: str = "") -> HTMLResponse:
    held = connections.peek_pending(state)
    if not held or held[0] != "choice":
        return _done(error=EXPIRED)
    title, text, yes, no = _choice_text(held[1])
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{CHOICE_CSS}</style></head>'
            f'<body><main><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p><form method="post" action="/callback/choose">'
            f'<input type="hidden" name="state" value="{html.escape(state, quote=True)}">'
            f'<button class="go" name="decision" value="yes">{html.escape(yes)}</button>'
            f'<button name="decision" value="no">{html.escape(no)}</button></form></main></body></html>')
    return HTMLResponse(page, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "Content-Security-Policy": CHOICE_CSP})


@router.post("/callback/choose", include_in_schema=False)
async def choose(background: BackgroundTasks, state: str = Form(...), decision: str = Form(...)) -> RedirectResponse:
    held = await run_in_threadpool(connections.pop_pending, state)
    if not held or held[0] != "choice":
        return _done(error=EXPIRED)
    choice = held[1]
    context.ACTING_FOR.set(choice.get("actor"))
    if decision != "yes":
        return _done(error="Nothing was added, and nothing has changed.")
    try:
        if choice["action"] == "other_patient":
            result = await connect.add_other_patient(choice)
        else:
            result = await _complete(choice["kind"], choice["pending"], code=choice.get("code"), tokens=choice.get("tokens"))
    except SmartError as exc:
        return _done(error=str(exc))
    if result.get("ask"):
        return RedirectResponse(await _ask(result["ask"]), status_code=303)
    background.add_task(sync.sync_connection, result["connection_id"], "initial")
    return _done(result["connection_id"])
def _problem(message: str) -> str:
    """What went wrong, signed: the page shows only what this server says went wrong, not what a link says it should."""
    return security.sign({"message": message[:400], "exp": time.time() + 600}, "sign-in-problem")


def _done(connection_id: Optional[str] = None, error: Optional[str] = None) -> RedirectResponse:
    if error:
        return RedirectResponse(f"/#/sources?problem={quote(_problem(error))}", status_code=303)
    return RedirectResponse(f"/#/sources?connected={connection_id}", status_code=303)


@router.get("/api/sign-in-problem", dependencies=[user])
def sign_in_problem(token: str = Query(..., max_length=1000)) -> dict:
    """The words for a sign-in that didn't finish, from the address it came back to."""
    problem = security.unsign(token, "sign-in-problem")
    return {"message": problem["message"] if problem else None}


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
  if (!h.get("access_token") || !h.get("state")) { location.replace("/#/sources?failed=1"); return; }
  var body = {}; h.forEach(function(v,k){ body[k] = v; });
  fetch("/api/connections/oauth/relay", {method:"POST", headers:{"Content-Type":"application/json","X-Requested-With":"syntropy"},
    body: JSON.stringify(body), credentials: "same-origin"})
   .then(function(r){ return r.json().then(function(j){ return [r.ok, j]; }); })
   .then(function(res){ location.replace(res[1].choose ? res[1].choose
                                         : res[0] ? "/#/sources?connected=" + encodeURIComponent(res[1].connection_id)
                                                 : "/#/sources?" + (res[1].problem ? "problem=" + encodeURIComponent(res[1].problem) : "failed=1")); })
   .catch(function(){ location.replace("/#/sources?failed=1"); });
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
        # The provider's words only when it's answering a sign-in this server started: anyone can make a link to here.
        started = await run_in_threadpool(connections.pop_pending, state) if state else None
        if error == "access_denied":
            return _done(error="Access was not granted. No records were shared.")
        return _done(error=(error_description or desc or error) if started else "The sign-in didn't finish. Start the connection again.")
    if not code or not state:
        # Token hand-off from the relay arrives in the URL fragment, which only the browser can read.
        return HTMLResponse(RELAY_PAGE, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                                  "Content-Security-Policy": RELAY_CSP})
    held = await run_in_threadpool(connections.peek_pending, state)
    if not held or held[0] == "choice":
        return _done(error=EXPIRED)
    verdict = await run_in_threadpool(_may_finish, request, held[1])
    if verdict == "refuse":
        return _done(error=STARTED_ELSEWHERE)
    popped = await run_in_threadpool(connections.pop_pending, state)
    if not popped:
        return _done(error=EXPIRED)
    kind, pending = popped
    context.ACTING_FOR.set(pending.get("actor"))
    if verdict == "ask":
        return RedirectResponse(await _ask({"action": "finish", "kind": kind, "pending": pending, "code": code}), status_code=303)
    try:
        result = await _complete(kind, pending, code=code)
    except SmartError as exc:
        return _done(error=str(exc))
    if result.get("ask"):
        return RedirectResponse(await _ask(result["ask"]), status_code=303)
    background.add_task(sync.sync_connection, result["connection_id"], "initial")
    return _done(result["connection_id"])


class RelayTokens(BaseModel):
    state: str = Field(..., max_length=2000)
    access_token: str = Field(..., max_length=8000)
    refresh_token: Optional[str] = Field(None, max_length=8000)
    expires_in: Optional[float] = None
    scope: Optional[str] = Field(None, max_length=2000)
    relay_provider: Optional[str] = None

    # The relay sent an empty value when a provider left the lifetime out; relays that are still deployed may.
    @field_validator("expires_in", mode="before")
    @classmethod
    def _empty_means_unknown(cls, value: Any) -> Any:
        return None if value == "" else value


@router.post("/api/connections/oauth/relay", include_in_schema=False)
async def relay_complete(req: RelayTokens, request: Request, background: BackgroundTasks) -> Any:
    if request.headers.get(auth.CSRF_HEADER, "").lower() != auth.CSRF_VALUE:
        raise HTTPException(403, "Missing X-Requested-With: syntropy header.")
    held = await run_in_threadpool(connections.peek_pending, req.state)
    if not held or held[0] not in connect.WEARABLE_MODULES:
        return JSONResponse({"detail": EXPIRED, "problem": _problem(EXPIRED)}, status_code=400)
    verdict = await run_in_threadpool(_may_finish, request, held[1])
    if verdict == "refuse":
        return JSONResponse({"detail": STARTED_ELSEWHERE, "problem": _problem(STARTED_ELSEWHERE)}, status_code=403)
    popped = await run_in_threadpool(connections.pop_pending, req.state)
    if not popped or popped[0] not in connect.WEARABLE_MODULES:
        return JSONResponse({"detail": EXPIRED, "problem": _problem(EXPIRED)}, status_code=400)
    kind, pending = popped
    context.ACTING_FOR.set(pending.get("actor"))
    tokens = req.model_dump(exclude={"state", "relay_provider"})
    if verdict == "ask":
        return {"choose": await _ask({"action": "finish", "kind": kind, "pending": pending, "tokens": tokens})}
    try:
        result = await _complete(kind, pending, tokens=tokens)
    except SmartError as exc:
        return JSONResponse({"detail": str(exc), "problem": _problem(str(exc))}, status_code=400)
    background.add_task(sync.sync_connection, result["connection_id"], "initial")
    return result
