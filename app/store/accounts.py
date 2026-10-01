"""Accounts: the people who sign in, what each can see, and invitations to join.

A profile is a person whose health data is kept here. An account is one of those people signing in with a password of
their own. It can always see and change its own data, and it sees the other people it's been given access to:

* ``view``: read their data (and ask about it), but change nothing;
* ``manage``: everything the person could do themselves (add sources, pair a phone, edit, delete).

People without an account (a child, a parent someone cares for) are managed by whoever added them. When such a person is
invited and joins, their data becomes theirs alone: every access others had is removed, and they choose whom to share
it with. The owner (who set the server up) changes the settings that affect everyone, but sees only what they're given
like anyone else; people nobody else can reach any more fall to the owners, so no one's data is ever stranded.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from app.core import security
from app.core.db import audit, db, new_id, read, rows_to_dicts

LEVELS = ("view", "manage")
ROLES = ("owner", "member")
INVITE_TTL = 7 * 24 * 3600


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

_COLUMNS = ("a.id, a.profile_id, a.role, a.terms_version, a.onboarding, a.prefs, a.created_at, a.last_login_at, "
            "a.password_hash IS NOT NULL AS has_password, p.name, p.color")


def _present(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["prefs"] = json.loads(d["prefs"]) if d.get("prefs") else {}
    d["prefs"] = {k: v for k, v in d["prefs"].items() if v is not None}
    d["has_password"] = bool(d.get("has_password"))
    d["onboarding"] = bool(d.get("onboarding"))
    return d


def get(account_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not account_id:
        return None
    with read() as conn:
        row = conn.execute(f"SELECT {_COLUMNS} FROM accounts a JOIN profiles p ON p.id = a.profile_id WHERE a.id = ?",
                           (account_id,)).fetchone()
    return _present(row) if row else None


def for_profile(profile_id: str) -> Optional[dict[str, Any]]:
    with read() as conn:
        row = conn.execute(f"SELECT {_COLUMNS} FROM accounts a JOIN profiles p ON p.id = a.profile_id WHERE a.profile_id = ?",
                           (profile_id,)).fetchone()
    return _present(row) if row else None


def list_accounts() -> list[dict[str, Any]]:
    with read() as conn:
        rows = conn.execute(f"SELECT {_COLUMNS} FROM accounts a JOIN profiles p ON p.id = a.profile_id "
                            "ORDER BY a.role = 'owner' DESC, a.created_at").fetchall()
    return [_present(r) for r in rows]


def any_accounts() -> bool:
    with read() as conn:
        return conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None


def any_password() -> bool:
    with read() as conn:
        return conn.execute("SELECT 1 FROM accounts WHERE password_hash IS NOT NULL LIMIT 1").fetchone() is not None


def first_owner() -> Optional[dict[str, Any]]:
    return next((a for a in list_accounts() if a["role"] == "owner"), None)


def create(profile_id: str, password: Optional[str], role: str = "member") -> dict[str, Any]:
    now = time.time()
    account_id = new_id("acc")
    with db() as conn:
        conn.execute("INSERT INTO accounts(id, profile_id, password_hash, role, onboarding, created_at, updated_at) "
                     "VALUES (?, ?, ?, ?, 1, ?, ?)",
                     (account_id, profile_id, _stored_hash(password), role if role in ROLES else "member", now, now))
        audit(conn, f"user:{account_id}", "account.created", {"role": role}, profile_id)
    return get(account_id)  # type: ignore[return-value]


def _stored_hash(password: Optional[str]) -> Optional[str]:
    # The same encrypted form as other secrets (see app.core.settings), so a copied database alone can't be brute-forced.
    return json.dumps(security.encrypt_json(security.hash_password(password))) if password else None


def _password_hash(account_id: str) -> Optional[str]:
    with read() as conn:
        row = conn.execute("SELECT password_hash FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not row or not row["password_hash"]:
        return None
    return security.decrypt_json(json.loads(row["password_hash"]))


def has_password(account_id: str) -> bool:
    return _password_hash(account_id) is not None


def verify_password(account_id: str, password: str) -> bool:
    stored = _password_hash(account_id)
    return bool(stored) and security.verify_password(password or "", stored)


def set_password(account_id: str, password: Optional[str]) -> None:
    """A new password (or none, only while developing). Signs the account out everywhere; the caller signs this
    browser back in."""
    with db() as conn:
        conn.execute("UPDATE accounts SET password_hash = ?, updated_at = ? WHERE id = ?",
                     (_stored_hash(password), time.time(), account_id))
        conn.execute("DELETE FROM user_sessions WHERE account_id = ? AND device_id IS NULL", (account_id,))


def find(ref: str) -> Optional[dict[str, Any]]:
    """An account by id, or by its person's name (ignoring case) when that name is unique."""
    ref = (ref or "").strip()
    if not ref:
        return None
    accounts = list_accounts()
    exact = next((a for a in accounts if a["id"] == ref), None)
    if exact:
        return exact
    named = [a for a in accounts if a["name"].strip().lower() == ref.lower()]
    return named[0] if len(named) == 1 else None


def set_role(account_id: str, role: str) -> None:
    if role not in ROLES:
        raise ValueError("Role must be owner or member.")
    with db() as conn:
        if role == "member":
            others = conn.execute("SELECT COUNT(*) FROM accounts WHERE role = 'owner' AND id != ?", (account_id,)).fetchone()[0]
            if not others:
                raise ValueError("The server needs at least one owner.")
        conn.execute("UPDATE accounts SET role = ?, updated_at = ? WHERE id = ?", (role, time.time(), account_id))
        audit(conn, "user", "account.role", {"account": account_id, "role": role})


def touch_login(account_id: str) -> None:
    with db() as conn:
        conn.execute("UPDATE accounts SET last_login_at = ? WHERE id = ?", (time.time(), account_id))


def set_pref(account_id: str, key: str, value: Any) -> dict[str, Any]:
    account = get(account_id)
    if not account:
        raise LookupError("Account not found")
    prefs = {**account["prefs"], key: value}
    with db() as conn:
        conn.execute("UPDATE accounts SET prefs = ?, updated_at = ? WHERE id = ?",
                     (json.dumps({k: v for k, v in prefs.items() if v is not None}), time.time(), account_id))
    return prefs


def set_flags(account_id: str, *, terms_version: Optional[int] = None, onboarding: Optional[bool] = None) -> None:
    with db() as conn:
        if terms_version is not None:
            conn.execute("UPDATE accounts SET terms_version = ? WHERE id = ?", (terms_version, account_id))
        if onboarding is not None:
            conn.execute("UPDATE accounts SET onboarding = ? WHERE id = ?", (1 if onboarding else 0, account_id))


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def access_for(account: dict[str, Any]) -> dict[str, str]:
    """{profile_id: 'view' | 'manage'} for everyone this account can see, itself included."""
    access = {account["profile_id"]: "manage"}
    with read() as conn:
        for r in conn.execute("SELECT profile_id, level FROM profile_access WHERE account_id = ?", (account["id"],)):
            if access.get(r["profile_id"]) != "manage":
                access[r["profile_id"]] = r["level"]
        if account["role"] == "owner":
            # People nobody can reach (their manager left): the owners look after them.
            for r in conn.execute("SELECT id FROM profiles p WHERE NOT EXISTS (SELECT 1 FROM accounts a WHERE a.profile_id = p.id) "
                                  "AND NOT EXISTS (SELECT 1 FROM profile_access g JOIN accounts a ON a.id = g.account_id "
                                  "WHERE g.profile_id = p.id AND g.level = 'manage')"):
                access.setdefault(r["id"], "manage")
    return access


def grant(account_id: str, profile_id: str, level: str) -> None:
    if level not in LEVELS:
        raise ValueError("Access must be view or manage.")
    with db() as conn:
        own = conn.execute("SELECT 1 FROM accounts WHERE id = ? AND profile_id = ?", (account_id, profile_id)).fetchone()
        if own:
            return
        conn.execute("INSERT INTO profile_access(account_id, profile_id, level, created_at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(account_id, profile_id) DO UPDATE SET level = excluded.level",
                     (account_id, profile_id, level, time.time()))
        audit(conn, "user", "access.granted", {"account": account_id, "level": level}, profile_id)


def revoke_access(account_id: str, profile_id: str) -> None:
    from app.store import devices

    with db() as conn:
        conn.execute("DELETE FROM profile_access WHERE account_id = ? AND profile_id = ?", (account_id, profile_id))
        paired = [r["id"] for r in conn.execute("SELECT id FROM devices WHERE account_id = ? AND profile_id = ?",
                                                (account_id, profile_id))]
        audit(conn, "user", "access.revoked", {"account": account_id}, profile_id)
    # A phone this account paired for that person (not the person's own) stops sending to them.
    for device_id in paired:
        devices.revoke(device_id)


def access_to(profile_id: str) -> list[dict[str, Any]]:
    """Who else can see this person, and how."""
    with read() as conn:
        rows = conn.execute("SELECT g.account_id, g.level, p.name, p.color FROM profile_access g "
                            "JOIN accounts a ON a.id = g.account_id JOIN profiles p ON p.id = a.profile_id "
                            "WHERE g.profile_id = ? ORDER BY p.name", (profile_id,)).fetchall()
    return rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------

def create_invite(profile_id: str, created_by: Optional[str]) -> dict[str, Any]:
    """A one-time code for this person to choose a password and sign in as themselves. Replaces any earlier one."""
    if for_profile(profile_id):
        raise ValueError("This person already signs in with their own password.")
    code = security.pairing_code()
    now = time.time()
    with db() as conn:
        conn.execute("DELETE FROM invites WHERE expires_at < ? OR profile_id = ?", (now, profile_id))
        conn.execute("INSERT INTO invites(code_hash, profile_id, created_by, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                     (security.hash_token(code), profile_id, created_by, now, now + INVITE_TTL))
        audit(conn, "user", "invite.created", None, profile_id)
    return {"code": f"{code[:4]}-{code[4:]}", "expires_at": now + INVITE_TTL}


def pending_invites() -> dict[str, float]:
    """{profile_id: expires_at} for invitations not used yet."""
    with read() as conn:
        rows = conn.execute("SELECT profile_id, expires_at FROM invites WHERE expires_at > ?", (time.time(),)).fetchall()
    return {r["profile_id"]: r["expires_at"] for r in rows}


def cancel_invite(profile_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM invites WHERE profile_id = ?", (profile_id,))


def _invite(code: str) -> Optional[dict[str, Any]]:
    normalized = security.normalize_pairing_code(code or "")
    if not normalized:
        return None
    with read() as conn:
        row = conn.execute("SELECT i.profile_id, i.expires_at, p.name, p.color FROM invites i JOIN profiles p ON p.id = i.profile_id "
                           "WHERE i.code_hash = ?", (security.hash_token(normalized),)).fetchone()
    if not row or row["expires_at"] < time.time():
        return None
    return {**dict(row), "code_hash": security.hash_token(normalized)}


def peek_invite(code: str) -> Optional[dict[str, Any]]:
    invite = _invite(code)
    return {"name": invite["name"], "color": invite["color"]} if invite else None


def accept_invite(code: str, password: str) -> Optional[dict[str, Any]]:
    """Makes the invited person's account. From now on their data is theirs: everyone else's access to it ends."""
    invite = _invite(code)
    if not invite:
        return None
    profile_id = invite["profile_id"]
    with db() as conn:
        if not conn.execute("DELETE FROM invites WHERE code_hash = ?", (invite["code_hash"],)).rowcount:
            return None      # used a moment ago
        if conn.execute("SELECT 1 FROM accounts WHERE profile_id = ?", (profile_id,)).fetchone():
            return None
        conn.execute("DELETE FROM profile_access WHERE profile_id = ?", (profile_id,))
        # Phones others paired for this person keep sending to them, but their screens no longer open this person's data.
        conn.execute("DELETE FROM user_sessions WHERE device_id IN (SELECT id FROM devices WHERE profile_id = ?)", (profile_id,))
    account = create(profile_id, password)
    with db() as conn:
        conn.execute("UPDATE devices SET account_id = ? WHERE profile_id = ?", (account["id"], profile_id))
        audit(conn, f"user:{account['id']}", "invite.accepted", None, profile_id)
    return account
