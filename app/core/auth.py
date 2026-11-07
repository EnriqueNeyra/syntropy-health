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

import hmac
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from fastapi import HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from app.core import config, context, security, settings
from app.core.db import audit, db, read
from app.services import network
from app.store import accounts

SESSION_COOKIE = "syntropy_session"
SESSION_TTL = 30 * 24 * 3600
# However often it's used, a browser signs in again after this. A phone's app does that by itself with its token, so
# its session (which the phone can always get anew) isn't held to it.
SESSION_MAX_AGE = 180 * 24 * 3600
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "syntropy"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Failed guesses, for each address and thing being guessed (one person's password, invitation codes...). Someone guessing
# only uses up their own tries: a refusal never reaches the right password from another address. Across all addresses
# (someone guessing from many at once) answers slow down instead of stopping, so nobody can lock the household out.
# One thing guessed at from everywhere at once does stop, after more tries than one address gets: addresses can be had
# by the thousand (on IPv6, or where a proxy passes on whatever address it's told), and that would otherwise be as
# many guesses as there are addresses. This computer, connecting directly, can still try then, so the person it belongs
# to can always sign in at the server itself.
_failed_logins: dict[str, deque[float]] = defaultdict(deque)
_all_failures: deque[float] = deque()
LOGIN_WINDOW, LOGIN_MAX_FAILURES, LOGIN_SLOW_AFTER, LOGIN_SLOW_S = 300, 8, 40, 1.0
LOGIN_MAX_FAILURES_ANYWHERE = 20
_ATTEMPTS_KEPT = 10000
_ANYWHERE = "*"         # the address part of the key counting every address's tries (an address is never "*")


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
    limited: bool = False                     # the screens of a phone paired for someone else: that person, nothing more

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


_setup_code: dict[str, str] = {}


def setup_code() -> str:
    """One for each run of the server, kept only in memory and shown only in its log: whoever can read that can already
    do anything to this server."""
    return _setup_code.setdefault("code", security.pairing_code())


def setup_code_matches(code: Optional[str]) -> bool:
    wanted = _setup_code.get("code")
    given = security.normalize_pairing_code(code or "")
    return bool(wanted and given and hmac.compare_digest(given.encode(), wanted.encode()))


def setup_code_used() -> None:
    """It works once, counted from when the password it let someone choose was accepted: only a restart makes another."""
    _setup_code.pop("code", None)


def _password_needed() -> None:
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Set a password to protect your health records first.")


def complete_setup(password: Optional[str], profile_id: str) -> dict:
    """The first person becomes the owner, signing in with this password."""
    if not password and not password_optional():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose a password to protect your health records.")
    if password:
        validate_password(password)
    owner = accounts.for_profile(profile_id)
    if not owner:
        owner = accounts.create(profile_id, password, role="owner")
    elif password:
        accounts.set_password(owner["id"], password)
    settings.set("auth.required", bool(password))
    settings.set("setup.completed", True)
    with db() as conn:
        audit(conn, f"user:{owner['id']}", "setup.completed", {"password": bool(password)})
    return owner


def confirm_password(request: Request, account_id: Optional[str], password: Optional[str],
                     wrong: str = "That password is incorrect.") -> None:
    """The signed-in person's password again, for what can't be undone. Counts toward the guessing limits: otherwise a
    borrowed session could try passwords as fast as it liked."""
    if not account_id or not accounts.has_password(account_id):
        return
    who = f"confirm:{account_id}"
    check_attempts(request, who)
    note_failure(request, who)
    if not accounts.verify_password(account_id, password or ""):
        raise HTTPException(status.HTTP_403_FORBIDDEN, wrong)
    forget_failure(request, who)


def confirm_from_phone(request: Request, principal: Optional[Principal], password: Optional[str],
                       needed: str = "From a phone, this takes your password.") -> None:
    """A phone's screens are signed in with its token (/api/devices/web-session), not a password, so whoever holds the
    phone holds them. What can't be undone, or hands everyone's records to whoever asks, takes the password there as
    well, with the same guessing limits. A browser or the desktop app signed in with it, and isn't asked again.
    ``needed`` says so when none was sent: nothing was guessed, so it isn't counted."""
    if principal is None or not principal.device_id:
        return
    if not password:
        if principal.account_id and accounts.has_password(principal.account_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, needed)
        return
    confirm_password(request, principal.account_id, password)


def change_password(request: Request, account_id: str, current: Optional[str], new: Optional[str]) -> None:
    confirm_password(request, account_id, current, "Current password is incorrect.")
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


# The ones long enough to pass the length check that every guessing list starts with.
_COMMON_PASSWORDS = frozenset({
    "password", "password1", "password12", "password123", "password1234", "passw0rd", "p@ssw0rd", "p@ssword", "12345678",
    "123456789", "1234567890", "0123456789", "87654321", "987654321", "11111111", "00000000", "qwertyui", "qwertyuiop",
    "qwerty123", "1q2w3e4r", "1q2w3e4r5t", "1qaz2wsx", "asdfghjk", "asdfghjkl", "zxcvbnm1", "abcd1234", "abc12345",
    "abcdefgh", "iloveyou", "iloveyou1", "sunshine", "princess", "football", "baseball", "superman", "trustno1",
    "welcome1", "letmein1", "letmein123", "admin123", "administrator", "changeme", "changeme1", "syntropy", "syntropy1",
    "healthrecord", "myhealth", "health123",
})


def validate_password(p: str) -> None:
    if len(p) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password must be at least 8 characters.")
    if p.lower() in _COMMON_PASSWORDS or len(set(p)) == 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That password is one of the first anyone would guess. Choose another.")


def _client_ip(request: Request) -> str:
    """The address to show (in the log, and where someone's signed in): what the request says, which behind a proxy
    trusting everyone (FORWARDED_ALLOW_IPS="*") can be anything. Limits go by _tries_from instead."""
    return request.client.host if request.client else "unknown"


def _tries_from(request: Request) -> str:
    """Whose tries these are: the connection's address, or behind a proxy named in FORWARDED_ALLOW_IPS, the address it
    says it's for. Where that's only what the sender wrote, everything forwarded counts as one (network.client_address)."""
    return network.client_address(request) or "forwarded"


def check_attempts(request: Request, what: str) -> None:
    """Guessing limits: tries from this address at ``what`` (an account, or "invite"), and from every address at once.
    They're counted before the answer is known (note_failure), so sending many at once doesn't get more tries than
    sending them one by one."""
    now = time.time()
    window = _failed_logins[f"{_tries_from(request)}|{what}"]
    anywhere = _failed_logins[f"{_ANYWHERE}|{what}"]
    for q in (window, anywhere, _all_failures):
        while q and now - q[0] > LOGIN_WINDOW:
            q.popleft()
    if len(window) >= LOGIN_MAX_FAILURES or (len(anywhere) >= LOGIN_MAX_FAILURES_ANYWHERE and not network.from_this_computer(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again in a few minutes.")
    if len(_all_failures) >= LOGIN_SLOW_AFTER:
        time.sleep(LOGIN_SLOW_S)


def note_failure(request: Request, what: str) -> None:
    now = time.time()
    if len(_failed_logins) > _ATTEMPTS_KEPT:      # addresses and names that haven't been back are all that's dropped
        for key in [k for k, q in _failed_logins.items() if not q or now - q[-1] > LOGIN_WINDOW]:
            del _failed_logins[key]
    _failed_logins[f"{_tries_from(request)}|{what}"].append(now)
    _failed_logins[f"{_ANYWHERE}|{what}"].append(now)
    _all_failures.append(now)


def forget_failure(request: Request, what: str) -> None:
    """It was right: the tries at this from this address start over (other addresses' tries aren't this one's to clear),
    and this one try no longer counts against everyone's."""
    _failed_logins.pop(f"{_tries_from(request)}|{what}", None)
    anywhere = _failed_logins.get(f"{_ANYWHERE}|{what}")
    if anywhere:
        anywhere.pop()
    if _all_failures:
        _all_failures.pop()


def login(request: Request, password: str, who: Optional[str] = None) -> str:
    """Signs an account in: ``who`` is its id or its person's name. Two people can share a name: the password says which
    of them this is. Without ``who`` (older iPhone apps), only while a single account has a password."""
    if who:
        candidates = accounts.matching(who)[:5]
    else:
        candidates = [a for a in accounts.list_accounts() if a["has_password"]]
        if len(candidates) > 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose who's signing in. In a phone app, use a pairing code from the web app instead.")
    what = candidates[0]["id"] if candidates else "unknown"
    check_attempts(request, what)
    unreadable = next((a for a in candidates if accounts.password_unreadable(a["id"])), None)
    if unreadable:
        # Not a wrong password, and saying so would send them guessing for one that can't work.
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Syntropy Health can't check this password, because its encryption key has changed. Whoever runs "
                            "this server needs to put the original secret.key file back, or set a new password with "
                            f"`syntropy-health accounts reset-password {unreadable['name']}`.")
    note_failure(request, what)
    account = next((a for a in candidates if accounts.verify_password(a["id"], password)), None)
    if not candidates:
        security.waste_a_password_check(password)
    if not account:
        with db() as conn:
            audit(conn, f"user:{candidates[0]['id']}" if candidates else "user", "auth.login_failed", {"ip": _client_ip(request)})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect name or password." if who else "Incorrect password.")
    forget_failure(request, what)
    accounts.upgrade_password(account["id"], password)
    if account["role"] == "owner":
        security.note_owner_password(account["id"], password)
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
    # Looked up without the write lock (every request does this); written only every few minutes, to slide the expiry.
    with read() as conn:
        row = conn.execute("SELECT account_id, device_id, created_at, expires_at, last_seen_at FROM user_sessions WHERE token_hash = ?",
                           (h,)).fetchone()
    if not row or row["expires_at"] < now:
        return None
    if not row["device_id"] and now - row["created_at"] > SESSION_MAX_AGE:
        with db() as conn:
            conn.execute("DELETE FROM user_sessions WHERE token_hash = ?", (h,))
        return None
    if now - row["last_seen_at"] > 300:  # sliding expiration, throttled writes
        with db() as conn:
            conn.execute(
                "UPDATE user_sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
                (now, now + SESSION_TTL, h),
            )
    return dict(row)


def session_handle(token_hash: str) -> str:
    """How a session is named to the person, in the list of where they're signed in: its stored hash would be enough
    to look it up, not to use it, but there's no need to hand that out either."""
    return security.hash_token(token_hash)[:20]


def _browser_sessions(account_id: Optional[str]) -> list[str]:
    with read() as conn:
        return [r[0] for r in conn.execute("SELECT token_hash FROM user_sessions WHERE account_id IS ? AND device_id IS NULL",
                                           (account_id,))]


def end_session(account_id: Optional[str], handle: str) -> bool:
    """Signs one of this account's browsers out. A phone's screens end by unpairing it (Sources): it would just ask again."""
    h = next((h for h in _browser_sessions(account_id) if hmac.compare_digest(session_handle(h), handle)), None)
    if not h:
        return False
    with db() as conn:
        conn.execute("DELETE FROM user_sessions WHERE token_hash = ?", (h,))
        audit(conn, "user", "auth.session_ended")
    return True


def end_other_sessions(request: Request, account_id: Optional[str]) -> int:
    """Signs this account's other browsers out."""
    token = request.cookies.get(SESSION_COOKIE)
    keep = security.hash_token(token) if token else ""
    others = [h for h in _browser_sessions(account_id) if h != keep]
    with db() as conn:
        conn.executemany("DELETE FROM user_sessions WHERE token_hash = ?", [(h,) for h in others])
        audit(conn, "user", "auth.sessions_ended", {"browsers": len(others)})
    return len(others)


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
        # Whoever holds it isn't signed in as the account behind it: no owner's powers, nothing that reaches beyond them.
        device = get_device(device_id)
        if device and device["profile_id"] != account["profile_id"]:
            level = access.get(device["profile_id"])
            return Principal(kind="user", account_id=account["id"], access={device["profile_id"]: level} if level else {},
                             home=device["profile_id"], device_id=device_id, limited=True)
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
    if not account or (session["device_id"] and not get_device(session["device_id"])):
        return None
    return _account_principal(account, session["device_id"])


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


def _signed_in(request: Request, *, password_set: bool = True) -> Principal:
    """Who's asking, for the dependencies below. It reads the database, so they run it in a worker thread: on the
    event loop, a slow read (or one waiting on the disk) would hold up every other request."""
    if not is_setup_complete():
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED, "Setup required.")
    _check_csrf(request)
    principal = current_user(request)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required.")
    if password_set and password_missing():
        _password_needed()
    return principal


async def require_user(request: Request) -> Principal:
    """Dependency for dashboard API routes."""
    return _enter(request, await run_in_threadpool(_signed_in, request))


async def require_owner(request: Request) -> Principal:
    """Settings that affect everyone on the server."""
    principal = await require_user(request)
    if not principal.owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the server's owner can change this.")
    return principal


def refuse_if_limited(principal: Optional[Principal]) -> None:
    if principal is not None and principal.limited:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This phone's app only opens the person it was paired for. "
                                                       "Sign in with a password in a browser to do this.")


async def require_whole_account(request: Request) -> Principal:
    """What belongs to the account as a whole, or passes someone's data on: not from a phone paired for someone else."""
    principal = await require_user(request)
    refuse_if_limited(principal)
    return principal


async def require_user_to_set_password(request: Request) -> Principal:
    """Like require_user, but also while the password still has to be chosen (the route that chooses it)."""
    return _enter(request, await run_in_threadpool(_signed_in, request, password_set=False))


def get_device(device_id: str) -> Optional[dict]:
    with read() as conn:
        row = conn.execute("SELECT * FROM devices WHERE id = ? AND revoked_at IS NULL", (device_id,)).fetchone()
    return dict(row) if row else None


DEVICE_SEEN_EVERY_S = 60


def device_from_token(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    with read() as conn:
        row = conn.execute(
            "SELECT * FROM devices WHERE token_hash = ? AND revoked_at IS NULL", (security.hash_token(token),)
        ).fetchone()
    if not row:
        return None
    device = dict(row)
    # When it was last heard from (Sources shows it), without a write on each of a sync's many requests.
    now = time.time()
    if now - (device.get("last_seen_at") or 0) > DEVICE_SEEN_EVERY_S:
        with db() as conn:
            conn.execute("UPDATE devices SET last_seen_at = ? WHERE id = ?", (now, device["id"]))
        device["last_seen_at"] = now
    return device


def _bearer(request: Request) -> Optional[str]:
    token = request.headers.get("x-syntropy-device-token")
    if token:
        return token.strip()
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        return authz[7:].strip()
    return None


def _device_or_user(request: Request) -> Principal:
    device = device_from_token(_bearer(request))
    if device:
        # A phone sends for one person; it reads only what its preferences need.
        return Principal(kind="device", device_id=device["id"], profile_id=device["profile_id"],
                         account_id=device.get("account_id"), access={device["profile_id"]: "manage"},
                         home=device["profile_id"])
    if is_setup_complete():
        principal = current_user(request)
        if principal is not None:
            _check_csrf(request)
            if password_missing():
                _password_needed()
            return principal
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "This phone isn't paired. Pair it again from Syntropy Health → Sources → Phones & watches.",
    )


async def require_device_or_user(request: Request) -> Principal:
    """Ingest endpoints accept a paired device token, or a logged-in dashboard session."""
    return _enter(request, await run_in_threadpool(_device_or_user, request))
