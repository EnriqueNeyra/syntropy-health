"""Setup, authentication and instance status."""

from __future__ import annotations

import ipaddress
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.core import auth, config, context, settings
from app.core.db import audit, db, rows_to_dicts
from app.services import network, updates
from app.store import accounts, profiles

router = APIRouter(tags=["system"])


@router.api_route("/health", methods=["GET", "HEAD"], include_in_schema=False)
def health() -> dict:
    with db() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    return {"status": "ok", "service": "syntropy-health", "version": config.APP_VERSION, "journal_mode": mode}


@router.get("/api/status")
def status(request: Request) -> dict:
    setup = auth.is_setup_complete()
    required = auth.auth_required()
    principal = auth.current_user(request) if setup else None
    authenticated = principal is not None
    out = {"version": config.APP_VERSION, "setup_complete": setup, "auth_required": required,
           "authenticated": authenticated, "password_optional": auth.password_optional(),
           "password_missing": auth.password_missing()}
    if setup and not authenticated and _nearby(request):
        # Who can sign in, to choose from on this computer and the home network (from farther away, type the name).
        out["accounts"] = [{"id": a["id"], "name": a["name"], "color": a["color"]}
                           for a in accounts.list_accounts() if a["has_password"]]
    if authenticated:
        from app.api.profiles import visible_profiles

        token = context.PRINCIPAL.set(principal)
        try:
            out["profiles"] = visible_profiles()
        finally:
            context.PRINCIPAL.reset(token)
        account = accounts.get(principal.account_id)
        out["account"] = _account_summary(account, principal)
        out["preferences"] = preferences(account)
        out["onboarding"] = bool(account and account["onboarding"])
        out["terms_accepted"] = terms_accepted(account)
        if principal.owner and updates.checks_on():
            update = updates.status()
            if update["available"]:
                out["update"] = {"version": update["latest"]["version"], "installs": update["method"]["installs"]}
    return out


def _account_summary(account: Optional[dict], principal: auth.Principal) -> Optional[dict]:
    if not account:
        return None
    everyone = accounts.list_accounts()
    return {"id": account["id"], "name": account["name"], "profile_id": account["profile_id"], "owner": principal.owner,
            "has_password": account["has_password"], "members": len(everyone),
            "owner_name": next((a["name"] for a in everyone if a["role"] == "owner"), None)}


# What the person agrees to at setup (and once more when this changes): their records are on their own computer and
# theirs to secure and back up; AI providers they choose see what they ask about; it isn't medical advice. The full
# text is on health.syntropylabs.io/terms. Bump the version when that summary changes in substance.
TERMS_VERSION = 1


def terms_accepted(account: Optional[dict]) -> bool:
    """Each person agrees for themselves (with no account, only while developing, the agreement isn't asked)."""
    return account is None or account.get("terms_version") == TERMS_VERSION


def _accept_terms(account_id: Optional[str]) -> None:
    if not account_id:
        return
    accounts.set_flags(account_id, terms_version=TERMS_VERSION)
    with db() as conn:
        audit(conn, f"user:{account_id}", "terms.accepted", {"version": TERMS_VERSION})


@router.post("/api/terms/accept")
def accept_terms(principal: auth.Principal = Depends(auth.require_user_to_set_password)) -> dict:
    _accept_terms(principal.account_id)
    return {"ok": True}


def _nearby(request: Request) -> bool:
    """A request from this computer, the home network or Tailscale (not the internet)."""
    try:
        ip = ipaddress.ip_address((request.client.host if request.client else "").split("%")[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip in network.TAILSCALE_NET


# The accent color, chosen once for the web app, the desktop apps and the iPhone app. "heart" matches the logo.
ACCENTS = ("heart", "orange", "green", "teal", "blue", "indigo", "purple", "graphite")


def preferences(account: Optional[dict]) -> dict:
    """Each person's own display choices, followed on every device they use."""
    prefs = (account or {}).get("prefs") or {}
    return {"units": prefs.get("units"), "accent": prefs.get("accent") or "heart"}


def _principal_account(principal: auth.Principal) -> Optional[dict]:
    return accounts.get(principal.account_id) if principal.account_id else None


@router.get("/api/preferences")
def get_preferences(principal: auth.Principal = Depends(auth.require_device_or_user)) -> dict:
    """Display choices every device follows. The paired iPhone app reads them with its device token (the choices of
    whoever paired it)."""
    return {**preferences(_principal_account(principal)), "accents": list(ACCENTS)}


class PreferencesRequest(BaseModel):
    accent: Optional[str] = None
    units: Optional[str] = None     # "metric", "us", or "" to follow the device's region


@router.put("/api/preferences")
def put_preferences(req: PreferencesRequest, principal: auth.Principal = Depends(auth.require_device_or_user)) -> dict:
    if not principal.account_id:
        raise HTTPException(400, "Sign in to change your preferences.")
    if req.accent is not None:
        if req.accent not in ACCENTS:
            raise HTTPException(400, f"Accent must be one of: {', '.join(ACCENTS)}.")
        accounts.set_pref(principal.account_id, "accent", None if req.accent == "heart" else req.accent)
    if req.units is not None:
        if req.units not in ("", "metric", "us"):
            raise HTTPException(400, "Units must be metric or us.")
        accounts.set_pref(principal.account_id, "units", req.units or None)
    return {**preferences(_principal_account(principal)), "accents": list(ACCENTS)}


class SetupRequest(BaseModel):
    password: Optional[str] = Field(None, max_length=256)
    passphrase: Optional[str] = Field(None, max_length=256)     # earlier name, still accepted
    profile_name: str = Field("Me", max_length=80)
    birth_date: Optional[str] = None
    timezone: Optional[str] = None
    accept_terms: bool = False


@router.post("/api/setup")
async def setup(req: SetupRequest, request: Request, response: Response) -> dict:
    if auth.is_setup_complete():
        raise HTTPException(409, "Setup has already been completed.")
    if request.headers.get(auth.CSRF_HEADER, "").lower() != auth.CSRF_VALUE:
        raise HTTPException(403, "Missing X-Requested-With: syntropy header.")
    # Until setup, whoever reaches the server could claim it by choosing the password: only from nearby.
    if not _nearby(request):
        raise HTTPException(403, "Finish setting up from this computer or your home network.")
    password = req.password or req.passphrase or None
    if not password and not auth.password_optional():
        raise HTTPException(400, "Choose a password to protect your health records.")
    prof = profiles.default_profile()
    profiles.update_profile(prof["id"], name=req.profile_name or "Me", birth_date=req.birth_date)
    if req.timezone:
        settings.set("display.timezone", req.timezone)
    owner = auth.complete_setup(password, prof["id"])
    accounts.set_flags(owner["id"], onboarding=True)     # the rest of first-run: other devices, data, AI
    if req.accept_terms:
        _accept_terms(owner["id"])
    if password:
        auth.set_session_cookie(response, auth.create_session(request, owner["id"]), request)
    return {"ok": True}


@router.post("/api/onboarding/done")
def onboarding_done(principal: auth.Principal = Depends(auth.require_user)) -> dict:
    if principal.account_id:
        accounts.set_flags(principal.account_id, onboarding=False)
    return {"ok": True}


@router.post("/api/onboarding/restart")
def onboarding_restart(principal: auth.Principal = Depends(auth.require_user)) -> dict:
    """Settings → General: walk through other devices, data and AI again."""
    if principal.account_id:
        accounts.set_flags(principal.account_id, onboarding=True)
    return {"ok": True}


@router.get("/api/system/network", dependencies=[Depends(auth.require_user)])
async def network_status(request: Request) -> dict:
    """Where this server can be opened from, and whether it listens beyond this computer."""
    port = network.listen_port() or request.url.port
    info = await run_in_threadpool(network.describe, port)
    return {**info, "password_set": auth.auth_required(), "password_optional": auth.password_optional()}


class NetworkRequest(BaseModel):
    enabled: bool
    without_password: bool = False


@router.put("/api/system/network", dependencies=[Depends(auth.require_owner)])
def set_network(req: NetworkRequest) -> dict:
    """The Mac and Windows apps: listen on the home network and Tailscale too, or only on this computer. The server
    restarts on the new address a moment after this answers."""
    if not network.managed():
        raise HTTPException(409, "This server's network access is set where it's started (for example "
                                 "`syntropy-health serve --host 127.0.0.1`), not here.")
    password = auth.auth_required()
    if req.enabled and not password and not (req.without_password and auth.password_optional()):
        raise HTTPException(400, "Set a password first, so other people on your network can't open your records.")
    with db() as conn:
        audit(conn, "user", "network.enabled" if req.enabled else "network.disabled", {"password": password})
    changing = req.enabled != network.on_network()
    if changing:
        network.set_on_network(req.enabled)
    return {"ok": True, "restarting": changing}


# ---------------------------------------------------------------------------
# Updates (for the server's owner)
# ---------------------------------------------------------------------------

@router.get("/api/system/updates", dependencies=[Depends(auth.require_owner)])
def update_status() -> dict:
    return updates.status()


@router.post("/api/system/updates/check", dependencies=[Depends(auth.require_owner)])
async def check_for_updates() -> dict:
    if not updates.checks_allowed():
        raise HTTPException(409, "Update checks are turned off where this server is started (SYNTROPY_UPDATE_CHECK).")
    return await run_in_threadpool(updates.check)


class UpdateSettings(BaseModel):
    check: Optional[bool] = None
    auto: Optional[bool] = None


@router.put("/api/system/updates", dependencies=[Depends(auth.require_owner)])
def set_update_settings(req: UpdateSettings) -> dict:
    if req.check is not None:
        settings.set("updates.check", req.check)
    if req.auto is not None:
        if req.auto and not updates.method()["automatic"]:
            raise HTTPException(409, "This installation can't install updates by itself.")
        settings.set("updates.auto", req.auto)
    with db() as conn:
        audit(conn, "user", "updates.settings", req.model_dump(exclude_none=True))
    return updates.status()


@router.post("/api/system/updates/install", dependencies=[Depends(auth.require_owner)])
def install_update() -> dict:
    """The Mac and Windows apps: download and install the latest release, then open again."""
    try:
        result = updates.install()
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    with db() as conn:
        audit(conn, "user", "updates.install", {"version": (result["latest"] or {}).get("version")})
    return result


class LoginRequest(BaseModel):
    password: Optional[str] = Field(None, max_length=256)
    passphrase: Optional[str] = Field(None, max_length=256)     # earlier name, still accepted (older iPhone app)
    account: Optional[str] = Field(None, max_length=120)        # who: an account id or the person's name


@router.post("/api/auth/login")
async def login(req: LoginRequest, request: Request, response: Response) -> dict:
    if request.headers.get(auth.CSRF_HEADER, "").lower() != auth.CSRF_VALUE:
        raise HTTPException(403, "Missing X-Requested-With: syntropy header.")
    token = auth.login(request, req.password or req.passphrase or "", req.account)
    auth.set_session_cookie(response, token, request)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Invitations: a person someone added chooses their own password and signs in as themselves
# ---------------------------------------------------------------------------

class InviteCode(BaseModel):
    code: str = Field(..., max_length=40)


@router.post("/api/invites/check")
async def check_invite(req: InviteCode, request: Request) -> dict:
    """Whom an invitation is for, before choosing a password. Counts toward the guessing limits like a sign-in."""
    if request.headers.get(auth.CSRF_HEADER, "").lower() != auth.CSRF_VALUE:
        raise HTTPException(403, "Missing X-Requested-With: syntropy header.")
    auth.check_attempts(request)
    invite = accounts.peek_invite(req.code)
    if not invite:
        auth.note_failure(request)
        raise HTTPException(404, "That invitation code isn't valid. Codes last a week and work once; ask for a new one.")
    return invite


class JoinRequest(BaseModel):
    code: str = Field(..., max_length=40)
    password: str = Field(..., max_length=256)
    accept_terms: bool = False


@router.post("/api/invites/accept")
async def accept_invite(req: JoinRequest, request: Request, response: Response) -> dict:
    if request.headers.get(auth.CSRF_HEADER, "").lower() != auth.CSRF_VALUE:
        raise HTTPException(403, "Missing X-Requested-With: syntropy header.")
    if not auth.is_setup_complete():
        raise HTTPException(428, "Setup required.")
    auth.validate_password(req.password)
    if not req.accept_terms:
        raise HTTPException(400, "Agree to the terms to continue.")
    auth.check_attempts(request)
    account = accounts.accept_invite(req.code, req.password)
    if not account:
        auth.note_failure(request)
        raise HTTPException(404, "That invitation code isn't valid. Codes last a week and work once; ask for a new one.")
    _accept_terms(account["id"])
    auth.set_session_cookie(response, auth.create_session(request, account["id"]), request)
    return {"ok": True, "account": {"id": account["id"], "name": account["name"]}}


@router.post("/api/auth/logout")
async def logout(request: Request, response: Response) -> dict:
    auth.logout(request)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


class PasswordRequest(BaseModel):
    current: Optional[str] = None
    new: Optional[str] = Field(None, max_length=256)
    accept_terms: bool = False          # choosing the password an older setup didn't have


@router.post("/api/auth/password")
@router.post("/api/auth/passphrase", include_in_schema=False)     # earlier address
async def change_password(req: PasswordRequest, request: Request, response: Response,
                          principal: auth.Principal = Depends(auth.require_user_to_set_password)) -> dict:
    if not principal.account_id:
        raise HTTPException(400, "No account to change the password of.")
    auth.change_password(principal.account_id, req.current, req.new)
    if req.accept_terms:
        _accept_terms(principal.account_id)
    if req.new:
        auth.set_session_cookie(response, auth.create_session(request, principal.account_id), request)
    return {"ok": True, "auth_required": auth.auth_required()}


@router.get("/api/auth/sessions")
def list_sessions(principal: auth.Principal = Depends(auth.require_user)) -> dict:
    """Where this account is signed in (its browsers and the screens of phones it paired)."""
    with db() as conn:
        rows = conn.execute("SELECT s.created_at, s.last_seen_at, s.expires_at, s.user_agent, s.ip, d.name AS device_name "
                            "FROM user_sessions s LEFT JOIN devices d ON d.id = s.device_id WHERE s.account_id IS ? "
                            "ORDER BY s.last_seen_at DESC", (principal.account_id,)).fetchall()
    return {"sessions": rows_to_dicts(rows)}


@router.get("/api/audit")
def audit_log(limit: int = 100, principal: auth.Principal = Depends(auth.require_user)) -> dict:
    """What happened to the people this account can see, and what it did itself. Owners also see what happened to
    the server (sign-ins, settings)."""
    visible = list(principal.access)
    marks = ",".join("?" * len(visible)) or "NULL"
    where = f"(a.profile_id IN ({marks}) OR a.actor = ?"
    params: list = [*visible, principal.actor]
    if principal.owner:
        where += " OR a.profile_id IS NULL"
    where += ")"
    with db() as conn:
        rows = conn.execute(f"SELECT a.* FROM audit_log a WHERE {where} ORDER BY a.at DESC LIMIT ?",
                            (*params, max(1, min(limit, 500)))).fetchall()
        names = {r["id"]: r["name"] for r in conn.execute("SELECT a.id, p.name FROM accounts a JOIN profiles p ON p.id = a.profile_id")}
        devices = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM devices")}
    events = rows_to_dicts(rows)
    for e in events:
        kind, _, ref = (e["actor"] or "").partition(":")
        e["who"] = (names.get(ref) if kind == "user" else devices.get(ref) if kind == "device" else None) or e["actor"]
    return {"events": events}


