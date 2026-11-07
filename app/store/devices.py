"""Companion-device pairing and device tokens."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Optional

from app.core import security
from app.core.db import audit, db, read, new_id, rows_to_dicts
from app.store import accounts, connections as conn_store

PAIRING_TTL = 10 * 60


def create_pairing_code(profile_id: str, account_id: Optional[str] = None) -> dict[str, Any]:
    """A code for a phone to send ``profile_id``'s data, made by ``account_id`` (whose sign-in its screens use)."""
    code = security.pairing_code()
    now = time.time()
    with db() as conn:
        conn.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (now,))
        conn.execute(
            "INSERT INTO pairing_codes(code_hash, profile_id, created_at, expires_at, account_id) VALUES (?, ?, ?, ?, ?)",
            (security.hash_token(code), profile_id, now, now + PAIRING_TTL, account_id),
        )
    return {"code": f"{code[:4]}-{code[4:]}", "expires_at": now + PAIRING_TTL, "ttl_seconds": PAIRING_TTL}


def claim(code: str, device_name: str, platform: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Redeems a pairing code. Returns the plaintext device token exactly once."""
    normalized = security.normalize_pairing_code(code)
    now = time.time()
    with db() as conn:
        row = conn.execute(
            "SELECT profile_id, expires_at, account_id FROM pairing_codes WHERE code_hash = ?", (security.hash_token(normalized),)
        ).fetchone()
        if not row or row["expires_at"] < now:
            return None
        conn.execute("DELETE FROM pairing_codes WHERE code_hash = ?", (security.hash_token(normalized),))
    profile_id, account_id = row["profile_id"], row["account_id"]
    if not _may_pair(account_id, profile_id):
        return None
    name = (device_name or ("Android phone" if platform == "android" else "iPhone")).strip()[:80]
    # The phone apps: Apple Health from the iPhone app, Health Connect from the Android app.
    store, provider = ("Health Connect", "health_connect") if platform == "android" else ("Apple Health", "apple_health")
    display = name if platform == "healthkit-export" else f"{store} · {name}"
    # Pairing the same phone again (after reinstalling the app, or signing out and back in) picks up its existing
    # source, so its data stays in one place rather than a new copy each time.
    connection = _existing_source(profile_id, display)
    if connection:
        conn_store.update(connection["id"], status="active", last_error=None, metadata={"platform": platform or "ios"})
    else:
        connection = conn_store.create(profile_id, "device", provider, display, mode="live",
                                       metadata={"platform": platform or "ios"})
    token = security.random_token()
    device_id = new_id("dev")
    with db() as conn:
        conn.execute(
            """INSERT INTO devices(id, profile_id, connection_id, name, platform, token_hash, created_at, last_seen_at, account_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (device_id, profile_id, connection["id"], name, platform or "ios", security.hash_token(token), now, now, account_id),
        )
        audit(conn, f"device:{device_id}", "device.paired", {"name": name}, profile_id)
        person = conn.execute("SELECT name FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    return {"device_id": device_id, "device_token": token, "profile_id": profile_id, "connection_id": connection["id"],
            "profile_name": person["name"] if person else None}


def _may_pair(account_id: Optional[str], profile_id: str) -> bool:
    """Whoever made a code may have lost the right to since (a code lasts a few minutes, and sharing can change)."""
    if not account_id:      # only while developing without a password: no one can be said to have lost anything
        return accounts.for_profile(profile_id) is None
    account = accounts.get(account_id)
    return bool(account) and accounts.access_for(account).get(profile_id) == "manage"


def _existing_source(profile_id: str, display_name: str) -> Optional[dict[str, Any]]:
    with read() as conn:
        row = conn.execute("SELECT id FROM connections WHERE profile_id = ? AND kind = 'device' AND display_name = ? "
                           "ORDER BY created_at DESC LIMIT 1", (profile_id, display_name)).fetchone()
    return conn_store.get(row["id"]) if row else None


def list_devices(profile_id: Optional[str] = None) -> list[dict[str, Any]]:
    q = "SELECT id, profile_id, connection_id, name, platform, created_at, last_seen_at, revoked_at FROM devices"
    params: list[Any] = []
    if profile_id:
        q += " WHERE profile_id = ?"
        params.append(profile_id)
    q += " ORDER BY created_at DESC"
    with read() as conn:
        return rows_to_dicts(conn.execute(q, params).fetchall())


def get_device(device_id: str) -> Optional[dict[str, Any]]:
    with read() as conn:
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return dict(row) if row else None


def proof(device_id: str, nonce: str) -> Optional[str]:
    """That this is the server the phone paired with: its nonce signed with what only this server keeps of its token
    (the hash, which the phone works out from the token itself). None for a phone that isn't paired here."""
    with read() as conn:
        row = conn.execute("SELECT token_hash FROM devices WHERE id = ? AND revoked_at IS NULL", (device_id,)).fetchone()
    if not row or not row["token_hash"]:
        return None
    return hmac.new(row["token_hash"].encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def revoke(device_id: str) -> None:
    """Unpairs a device: its token stops working and is forgotten. Its source and data stay, shown as not paired,
    until the phone pairs again or the source is removed."""
    with db() as conn:
        row = conn.execute("SELECT profile_id, connection_id, name FROM devices WHERE id = ?", (device_id,)).fetchone()
        if not row:
            return
        conn.execute("DELETE FROM user_sessions WHERE device_id = ?", (device_id,))   # its Syntropy tab too
        conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        audit(conn, "user", "device.revoked", {"id": device_id, "name": row["name"]}, row["profile_id"])
        still_paired = row["connection_id"] and conn.execute(
            "SELECT 1 FROM devices WHERE connection_id = ?", (row["connection_id"],)).fetchone()
    if row["connection_id"] and not still_paired:
        conn_store.update(row["connection_id"], status="disconnected")


def revoke_paired_in(conn: Any, profile_id: str, account_id: Optional[str] = None) -> list[str]:
    """Inside the caller's transaction, so nothing can slip in between: unpairs the phones sending for this person (only
    the ones ``account_id`` paired, when given), and any code still waiting. Returns the phones' names."""
    only = " AND account_id = ?" if account_id else ""
    params = (profile_id, *([account_id] if account_id else []))
    rows = conn.execute(f"SELECT id, connection_id, name FROM devices WHERE profile_id = ?{only}", params).fetchall()
    conn.execute(f"DELETE FROM pairing_codes WHERE profile_id = ?{only}", params)
    for row in rows:
        conn.execute("DELETE FROM user_sessions WHERE device_id = ?", (row["id"],))
        conn.execute("DELETE FROM devices WHERE id = ?", (row["id"],))
        audit(conn, "user", "device.revoked", {"id": row["id"], "name": row["name"]}, profile_id)
        if row["connection_id"]:
            conn.execute("UPDATE connections SET status = 'disconnected', updated_at = ? WHERE id = ? AND NOT EXISTS "
                         "(SELECT 1 FROM devices d WHERE d.connection_id = connections.id)", (time.time(), row["connection_id"]))
    return [r["name"] for r in rows]


def revoke_for_connection(connection_id: str) -> None:
    """Unpairs every device sending to a source (when the source itself is removed)."""
    with read() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM devices WHERE connection_id = ?", (connection_id,))]
    for device_id in ids:
        revoke(device_id)
