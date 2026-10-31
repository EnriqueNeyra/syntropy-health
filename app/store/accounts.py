"""Accounts: the people who sign in, what each can see, and invitations to join.

A profile is a person whose health data is kept here. An account is one of those people signing in with a password of
their own. It can always see and change its own data, and it sees the other people it's been given access to:

* ``view``: read their data (and ask about it), but change nothing;
* ``manage``: looks after their data: adds and removes sources, pairs a phone, edits. For someone who signs in, that stops
  short of what stays theirs alone: sharing it onward, and deleting all of it (see app.api.profiles).

People without an account (a child, a parent someone cares for) are managed by whoever added them. When such a person is
invited and joins, their data becomes theirs alone: every access others had is removed, and they choose whom to share
it with. The owner (who set the server up) changes the settings that affect everyone, but sees only what they're given
like anyone else; people nobody else can reach any more fall to the owners, so no one's data is ever stranded.
"""

from __future__ import annotations

import json
import logging
from app.core.sqlite import sqlite3
import time
from typing import Any, Optional

from app.core import security
from app.core.db import audit, db, new_id, read, rows_to_dicts

log = logging.getLogger("syntropy.accounts")

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
    stored = _stored_hash(password)     # slow on purpose: not while holding the write lock
    with db() as conn:
        account_id = _insert(conn, profile_id, stored, role)
    if role == "owner" and password:
        security.note_owner_password(account_id, password, changed=True)
    return get(account_id)  # type: ignore[return-value]


def _insert(conn: Any, profile_id: str, stored_hash: Optional[str], role: str = "member", prefs: Optional[dict[str, Any]] = None) -> str:
    now = time.time()
    account_id = new_id("acc")
    conn.execute("INSERT INTO accounts(id, profile_id, password_hash, role, onboarding, prefs, created_at, updated_at) "
                 "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                 (account_id, profile_id, stored_hash, role if role in ROLES else "member",
                  json.dumps(prefs) if prefs else None, now, now))
    audit(conn, f"user:{account_id}", "account.created", {"role": role}, profile_id)
    return account_id


def _stored_hash(password: Optional[str]) -> Optional[str]:
    # The same encrypted form as other secrets (see app.core.settings), so a copied database alone can't be brute-forced.
    return json.dumps(security.encrypt_json(security.hash_password(password))) if password else None


def _password_hash(account_id: str) -> Optional[str]:
    with read() as conn:
        row = conn.execute("SELECT password_hash FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not row or not row["password_hash"]:
        return None
    return security.decrypt_json(json.loads(row["password_hash"]))


def password_unreadable(account_id: str) -> bool:
    """A password was saved, but this encryption key can't open it."""
    return has_password(account_id) and _password_hash(account_id) is None


def has_password(account_id: str) -> bool:
    with read() as conn:
        row = conn.execute("SELECT password_hash FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return bool(row and row["password_hash"])


def verify_password(account_id: str, password: str) -> bool:
    stored = _password_hash(account_id)
    return bool(stored) and security.verify_password(password or "", stored)


def upgrade_password(account_id: str, password: str) -> None:
    """After a good sign-in, when the saved hash uses weaker settings than a new one would. Never signs anyone out, and
    a password changed in the meantime isn't overwritten. Failing only means trying again at the next sign-in."""
    old = _password_hash(account_id)
    if not old or not security.password_needs_upgrade(old):
        return
    with read() as conn:
        row = conn.execute("SELECT password_hash FROM accounts WHERE id = ?", (account_id,)).fetchone()
    new = _stored_hash(password)
    try:
        with db() as conn:
            conn.execute("UPDATE accounts SET password_hash = ? WHERE id = ? AND password_hash = ?", (new, account_id, row["password_hash"]))
    except sqlite3.Error:
        log.exception("Couldn't upgrade a saved password")


def set_password(account_id: str, password: Optional[str]) -> None:
    """A new password (or none, only while developing). Signs the account's browsers out; the caller signs this one back
    in. Phones and AI apps have tokens of their own that a password doesn't open, and keep working: a lost phone is
    unpaired in Sources, which is what ends its access."""
    stored = _stored_hash(password)
    with db() as conn:
        conn.execute("UPDATE accounts SET password_hash = ?, updated_at = ? WHERE id = ?", (stored, time.time(), account_id))
        conn.execute("DELETE FROM user_sessions WHERE account_id = ? AND device_id IS NULL", (account_id,))
    account = get(account_id)
    if account and account["role"] == "owner":
        if password:
            security.note_owner_password(account_id, password, changed=True)
        else:
            security.forget_owner_password(account_id)


def matching(ref: str) -> list[dict[str, Any]]:
    """The accounts this could mean: the one with this id, or everyone with this name (ignoring case)."""
    ref = (ref or "").strip()
    if not ref:
        return []
    accounts = list_accounts()
    exact = [a for a in accounts if a["id"] == ref]
    return exact or [a for a in accounts if a["name"].strip().lower() == ref.lower()]


def find(ref: str) -> Optional[dict[str, Any]]:
    """An account by id, or by its person's name when only one person has it."""
    found = matching(ref)
    return found[0] if len(found) == 1 else None


def name_taken(name: str, profile_id: Optional[str] = None) -> bool:
    """Someone else already signs in with this name."""
    wanted = (name or "").strip().lower()
    return any(a["name"].strip().lower() == wanted and a["profile_id"] != profile_id for a in list_accounts())


def set_role(account_id: str, role: str, step_down: Optional[str] = None) -> None:
    """Makes someone an owner or a member. ``step_down``, an owner handing over: that account becomes a member in the same
    step, so the server is never without an owner and never has one too many. Actors: whoever's request this is, or
    the command line."""
    if role not in ROLES:
        raise ValueError("Role must be owner or member.")
    now = time.time()
    with db() as conn:
        conn.execute("UPDATE accounts SET role = ?, updated_at = ? WHERE id = ?", (role, now, account_id))
        audit(conn, "user", "account.role", {"account": account_id, "role": role})
        if step_down and step_down != account_id:
            conn.execute("UPDATE accounts SET role = 'member', updated_at = ? WHERE id = ?", (now, step_down))
            audit(conn, "user", "account.role", {"account": step_down, "role": "member"})
        if not conn.execute("SELECT 1 FROM accounts WHERE role = 'owner' LIMIT 1").fetchone():
            raise ValueError("The server needs at least one owner.")
        for changed in {account_id, step_down} - {None}:
            _end_phones_out_of_reach(conn, "account_id", changed)
    # Only an owner's password opens the key. A new owner's starts to at their next sign-in, when it's known.
    for changed, now_role in ((account_id, role), (step_down, "member")):
        if changed and now_role != "owner":
            security.forget_owner_password(changed)


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
    with read() as conn:
        return _access_in(conn, account)


def _access_in(conn: Any, account: Any) -> dict[str, str]:
    access = {account["profile_id"]: "manage"}
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


def _end_phones_out_of_reach(conn: Any, column: str, value: str) -> None:
    """Inside the caller's transaction: a phone sends for a person only while whoever paired it may manage them. What an
    owner reached by being an owner ends with that, or when someone else comes to manage the person."""
    from app.store import devices

    paired = conn.execute(f"SELECT account_id, profile_id FROM devices WHERE {column} = ? AND account_id IS NOT NULL UNION "
                          f"SELECT account_id, profile_id FROM pairing_codes WHERE {column} = ? AND account_id IS NOT NULL",
                          (value, value)).fetchall()
    for row in paired:
        account = conn.execute("SELECT id, profile_id, role FROM accounts WHERE id = ?", (row["account_id"],)).fetchone()
        if account and _access_in(conn, account).get(row["profile_id"]) != "manage":
            devices.revoke_paired_in(conn, row["profile_id"], row["account_id"])


def grant(account_id: str, profile_id: str, level: str) -> None:
    from app.store import devices

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
        if level != "manage":       # someone who may only read can't keep sending numbers into the record
            devices.revoke_paired_in(conn, profile_id, account_id)
        _end_phones_out_of_reach(conn, "profile_id", profile_id)


def revoke_access(account_id: str, profile_id: str) -> None:
    from app.store import devices

    with db() as conn:
        conn.execute("DELETE FROM profile_access WHERE account_id = ? AND profile_id = ?", (account_id, profile_id))
        audit(conn, "user", "access.revoked", {"account": account_id}, profile_id)
        # A phone this account paired for that person (not the person's own) stops sending to them, and what it kept
        # about them goes: an AI app limited to them, and conversations about them no one could open any more.
        devices.revoke_paired_in(conn, profile_id, account_id)
        conn.execute("UPDATE agent_tokens SET revoked_at = ? WHERE revoked_at IS NULL AND account_id = ? AND profile_id = ?",
                     (time.time(), account_id, profile_id))
        conn.execute("DELETE FROM chats WHERE account_id = ? AND profile_id = ?", (account_id, profile_id))


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
        row = conn.execute("SELECT i.profile_id, i.expires_at, i.created_by, p.name, p.color FROM invites i JOIN profiles p ON p.id = i.profile_id "
                           "WHERE i.code_hash = ?", (security.hash_token(normalized),)).fetchone()
    if not row or row["expires_at"] < time.time():
        return None
    # Redeeming one takes the person over and shuts everyone else out, so it's only good while whoever made it still looks
    # after them: someone removed yesterday mustn't be able to use a code they kept. None is an invite from before this was noted.
    maker = get(row["created_by"]) if row["created_by"] else None
    if row["created_by"] and (not maker or access_for(maker).get(row["profile_id"]) != "manage"):
        return None
    return {**dict(row), "code_hash": security.hash_token(normalized)}


def peek_invite(code: str) -> Optional[dict[str, Any]]:
    invite = _invite(code)
    return {"name": invite["name"], "color": invite["color"]} if invite else None


def accept_invite(code: str, password: str, name: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Makes the invited person's account. From now on their data is theirs: everyone else's access to it ends, along
    with whatever those people set up to reach it. A phone may well be the person's own, but nothing shows whose hand
    it's in: it pairs again, once, with their own sign-in. What's left for them to look over is kept with their account
    until they've seen it (``join_review``)."""
    from app.store import devices

    invite = _invite(code)
    if not invite:
        return None
    profile_id = invite["profile_id"]
    stored = _stored_hash(password)
    now = time.time()
    with db() as conn:
        if not conn.execute("DELETE FROM invites WHERE code_hash = ?", (invite["code_hash"],)).rowcount:
            return None      # used a moment ago
        if conn.execute("SELECT 1 FROM accounts WHERE profile_id = ?", (profile_id,)).fetchone():
            return None
        carers = [r["name"] for r in conn.execute(
            "SELECT p.name FROM profile_access g JOIN accounts a ON a.id = g.account_id JOIN profiles p ON p.id = a.profile_id "
            "WHERE g.profile_id = ? ORDER BY p.name", (profile_id,))]
        conn.execute("DELETE FROM profile_access WHERE profile_id = ?", (profile_id,))
        phones = devices.revoke_paired_in(conn, profile_id)
        conn.execute("UPDATE agent_tokens SET revoked_at = ? WHERE revoked_at IS NULL AND profile_id = ?", (now, profile_id))
        # Those who looked after them can't open what they asked an AI about this person any more, so nor could anyone.
        conn.execute("DELETE FROM chats WHERE profile_id = ?", (profile_id,))
        sources = [r["display_name"] for r in conn.execute(
            "SELECT display_name FROM connections WHERE profile_id = ? AND status != 'disconnected' AND kind != 'device' "
            "ORDER BY display_name", (profile_id,))]
        conn.execute("UPDATE profiles SET relationship = 'self', updated_at = ?" + (", name = ?" if name else "") + " WHERE id = ?",
                     (now, *([name] if name else []), profile_id))
        review = {"carers": carers, "phones": phones, "sources": sources} if carers or phones or sources else None
        account_id = _insert(conn, profile_id, stored, prefs={"join_review": review} if review else None)
        audit(conn, f"user:{account_id}", "invite.accepted", None, profile_id)
    return get(account_id)
