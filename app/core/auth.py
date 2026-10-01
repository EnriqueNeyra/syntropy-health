"""
Local authentication.

* Each person who signs in has an account with a password of their own (app.store.accounts); the first, chosen during
  setup, is the owner's. It holds health records, so a password is required: an instance set up without one (before
  this was required) asks for one before anything else opens. Developers can allow no password with
  SYNTROPY_ALLOW_NO_PASSWORD=1 or `syntropy-health dev no-password on`.
  Successful login issues an opaque session cookie (HttpOnly, SameSite=Lax) for that account;
  only its SHA-256 hash is stored.
* Every request knows which people its account can see (``Principal.access``); app.api.deps checks it.
* State-changing browser requests must carry ``X-Requested-With: syntropy``.
  Browsers cannot attach custom headers cross-origin without a CORS preflight,
  which this server never grants, so this blocks CSRF.
* The companion app authenticates with a per-device bearer token obtained by
  redeeming a short-lived pairing code.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from fastapi import HTTPException, Request, status

from app.core import config, context, security, settings
from app.core.db import audit, db
from app.store import accounts

SESSION_COOKIE = "syntropy_session"
SESSION_TTL = 30 * 24 * 3600
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "syntropy"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_failed_logins: dict[str, deque[float]] = defaultdict(deque)
_all_failures: deque[float] = deque()
# Per address, and across all addresses (someone guessing from many addresses at once).
LOGIN_WINDOW, LOGIN_MAX_FAILURES, LOGIN_MAX_FAILURES_ALL = 300, 8, 40


@dataclass
class Principal:
    kind: str                     # 'user' (a signed-in account) or 'device' (a paired phone's token)
    device_id: Optional[str] = None
    profile_id: Optional[str] = None          # a device: the person it sends data for
    account_id: Optional[str] = None
    owner: bool = False
    access: dict[str, str] = field(default_factory=dict)     # {profile_id: 'view' | 'manage'}
    home: Optional[str] = None                # the person shown first: the account's own (or the device's person)
    writing: bool = False                     # the request changes something (not GET)

    @property
    def actor(self) -> str:
        if self.kind == "device":
            return f"device:{self.device_id}"
        return f"user:{self.account_id}" if self.account_id else "user"

    def level(self, profile_id: Optional[str]) -> Optional[str]:
        return self.access.get(profile_id or "")

    def can_manage(self, profile_id: Optional[str]) -> bool:
        return self.level(profile_id) == "manage"


# ---------------------------------------------------------------------------
# Setup & passwords
# ---------------------------------------------------------------------------

def is_setup_complete() -> bool:
    return bool(settings.get("setup.completed"))


def auth_required() -> bool:
    return bool(settings.get("auth.required")) and accounts.any_password()


def password_optional() -> bool:
    """Only for development: lets an instance run without a password. Set in the environment, or in the database by
    the command line (someone at the command line can read the database anyway); never through the API."""
    return config.env_bool("SYNTROPY_ALLOW_NO_PASSWORD") or bool(settings.get("dev.allow_no_password"))


def password_missing() -> bool:
    """Set up without a password while one is required: the password has to be chosen before anything else."""
    return is_setup_complete() and not auth_required() and not password_optional()


def _password_needed() -> None:
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Set a password to protect your health records first.")


def complete_setup(password: Optional[str], profile_id: str) -> dict:
    """The first person becomes the owner, signing in with this password."""
    if not password and not password_optional():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose a password to protect your health records.")
    if password:
        validate_password(password)
    owner = accounts.for_profile(profile_id) or accounts.create(profile_id, password, role="owner")
    if password:
        accounts.set_password(owner["id"], password)
    settings.set("auth.required", bool(password))
    settings.set("setup.completed", True)
    with db() as conn:
        audit(conn, f"user:{owner['id']}", "setup.completed", {"password": bool(password)})
    return owner


def change_password(account_id: str, current: Optional[str], new: Optional[str]) -> None:
    if accounts.has_password(account_id) and not accounts.verify_password(account_id, current or ""):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect.")
    account = accounts.get(account_id)
    if not new and not (password_optional() and account and account["role"] == "owner"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A password is required to protect your health records.")
    if new:
        validate_password(new)
    accounts.set_password(account_id, new)
    if account and account["role"] == "owner" and (new or not accounts.any_password()):
        settings.set("auth.required", bool(new) or accounts.any_password())
    with db() as conn:
        audit(conn, f"user:{account_id}", "auth.password_changed", {"enabled": bool(new)})


def validate_password(p: str) -> None:
    if len(p) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password must be at least 8 characters.")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def check_attempts(request: Request) -> None:
    """Guessing limits (sign-in and invitation codes): per address, and across all addresses (someone guessing from
    many at once)."""
    now = time.time()
    window = _failed_logins[_client_ip(request)]
    for q in (window, _all_failures):
        while q and now - q[0] > LOGIN_WINDOW:
            q.popleft()
    if len(window) >= LOGIN_MAX_FAILURES or len(_all_failures) >= LOGIN_MAX_FAILURES_ALL:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again in a few minutes.")


def note_failure(request: Request) -> None:
    now = time.time()
    _failed_logins[_client_ip(request)].append(now)
    _all_failures.append(now)


def login(request: Request, password: str, who: Optional[str] = None) -> str:
    """Signs an account in: ``who`` is its id or its person's name. Without it (older iPhone apps), only while a
    single account has a password."""
    if who:
        account = accounts.find(who)
    else:
        with_password = [a for a in accounts.list_accounts() if a["has_password"]]
        if len(with_password) > 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose who's signing in.")
        account = with_password[0] if with_password else None
    check_attempts(request)
    if not account or not accounts.verify_password(account["id"], password):
        note_failure(request)
        with db() as conn:
            audit(conn, f"user:{account['id']}" if account else "user", "auth.login_failed", {"ip": _client_ip(request)})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect name or password." if who else "Incorrect password.")
    _failed_logins.pop(_client_ip(request), None)
    accounts.touch_login(account["id"])
    return create_session(request, account["id"])


def create_session(request: Request, account_id: Optional[str], device_id: Optional[str] = None) -> str:
    """A browser session for an account. With device_id it belongs to that paired app, which keeps at most one."""
    token = security.random_token()
    now = time.time()
    with db() as conn:
        conn.execute("DELETE FROM user_sessions WHERE expires_at < ?", (now,))
        if device_id:
            conn.execute("DELETE FROM user_sessions WHERE device_id = ?", (device_id,))
        conn.execute(
            "INSERT INTO user_sessions(token_hash, created_at, expires_at, last_seen_at, user_agent, ip, device_id, account_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (security.hash_token(token), now, now + SESSION_TTL, now,
             (request.headers.get("user-agent") or "")[:200], _client_ip(request), device_id, account_id),
        )
        audit(conn, f"device:{device_id}" if device_id else f"user:{account_id}", "auth.login", {"ip": _client_ip(request)})
    return token


def logout(request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with db() as conn:
            conn.execute("DELETE FROM user_sessions WHERE token_hash = ?", (security.hash_token(token),))


def _session(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    now = time.time()
    h = security.hash_token(token)
    with db() as conn:
        row = conn.execute("SELECT account_id, device_id, expires_at, last_seen_at FROM user_sessions WHERE token_hash = ?",
                           (h,)).fetchone()
        if not row or row["expires_at"] < now:
            return None
        if now - row["last_seen_at"] > 300:  # sliding expiration, throttled writes
            conn.execute(
                "UPDATE user_sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
                (now, now + SESSION_TTL, h),
            )
    return dict(row)


def session_valid(token: Optional[str]) -> bool:
    return _session(token) is not None


def set_session_cookie(response, token: str, request: Request) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax",
        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
        path="/",
    )


# ---------------------------------------------------------------------------
# Who is asking
# ---------------------------------------------------------------------------

def _account_principal(account: dict, device_id: Optional[str] = None) -> Principal:
    access = accounts.access_for(account)
    home = account["profile_id"]
    if device_id:
        # The web screens of a phone paired for someone else (a child's phone the parent paired) open only that person.
        device = get_device(device_id)
        if device and device["profile_id"] != account["profile_id"]:
            level = access.get(device["profile_id"])
            access = {device["profile_id"]: level} if level else {}
            home = device["profile_id"]
    return Principal(kind="user", account_id=account["id"], owner=account["role"] == "owner", access=access,
                     home=home, device_id=device_id)


def current_user(request: Request) -> Optional[Principal]:
    """The signed-in account, or None. Without a password (only while developing) whoever opens it is the owner."""
    if not auth_required():
        owner = accounts.first_owner()
        return _account_principal(owner) if owner else Principal(kind="user", owner=True)
    session = _session(request.cookies.get(SESSION_COOKIE))
    if not session:
        return None
    account = accounts.get(session["account_id"])
    return _account_principal(account, session["device_id"]) if account else None


def _enter(request: Request, principal: Principal) -> Principal:
    principal.writing = request.method not in SAFE_METHODS
    context.PRINCIPAL.set(principal)
    return principal


# ---------------------------------------------------------------------------
# FastAPI dependencies
#
# These are async so the principal they set (app.core.context) is seen by the endpoint and what it calls: FastAPI runs
# a plain function dependency in a worker thread, whose context changes stay there.
# ---------------------------------------------------------------------------

def _check_csrf(request: Request) -> None:
    if request.method in SAFE_METHODS:
        return
    if request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing X-Requested-With: syntropy header.")


async def require_user(request: Request) -> Principal:
    """Dependency for dashboard API routes."""
    if not is_setup_complete():
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED, "Setup required.")
    _check_csrf(request)
    principal = current_user(request)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required.")
    if password_missing():
        _password_needed()
    return _enter(request, principal)


async def require_owner(request: Request) -> Principal:
    """Settings that affect everyone on the server."""
    principal = await require_user(request)
    if not principal.owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the server's owner can change this.")
    return principal


async def require_user_to_set_password(request: Request) -> Principal:
    """Like require_user, but also while the password still has to be chosen (the route that chooses it)."""
    if not is_setup_complete():
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED, "Setup required.")
    _check_csrf(request)
    principal = current_user(request)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required.")
    return _enter(request, principal)


def get_device(device_id: str) -> Optional[dict]:
    with db() as conn:
        row = conn.execute("SELECT * FROM devices WHERE id = ? AND revoked_at IS NULL", (device_id,)).fetchone()
    return dict(row) if row else None


def device_from_token(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM devices WHERE token_hash = ? AND revoked_at IS NULL", (security.hash_token(token),)
        ).fetchone()
        if row:
            conn.execute("UPDATE devices SET last_seen_at = ? WHERE id = ?", (time.time(), row["id"]))
    return dict(row) if row else None


def _bearer(request: Request) -> Optional[str]:
    token = request.headers.get("x-syntropy-device-token")
    if token:
        return token.strip()
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        return authz[7:].strip()
    return None


async def require_device_or_user(request: Request) -> Principal:
    """Ingest endpoints accept a paired device token, or a logged-in dashboard session."""
    device = device_from_token(_bearer(request))
    if device:
        # A phone sends for one person; it reads only what its preferences need.
        return _enter(request, Principal(kind="device", device_id=device["id"], profile_id=device["profile_id"],
                                         account_id=device.get("account_id"), access={device["profile_id"]: "manage"},
                                         home=device["profile_id"]))
    if is_setup_complete():
        principal = current_user(request)
        if principal is not None:
            _check_csrf(request)
            if password_missing():
                _password_needed()
            return _enter(request, principal)
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Device not paired. Generate a pairing code in Syntropy Health → Sources → iPhone companion.",
    )
