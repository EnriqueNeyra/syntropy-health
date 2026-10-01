"""Profiles: the people whose health data this instance manages."""

from __future__ import annotations

import time
from typing import Any, Optional

from app.core.db import db, read, new_id, row_to_dict, rows_to_dicts
from app.store import sample_counts

RELATIONSHIPS = ("self", "partner", "child", "parent", "sibling", "other")
PALETTE = ["#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#ec4899", "#8b5cf6", "#14b8a6", "#ef4444"]


def list_profiles() -> list[dict[str, Any]]:
    with read() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM connections c WHERE c.profile_id = p.id AND c.status != 'disconnected') AS connection_count,
                   (SELECT COUNT(*) FROM clinical_records r WHERE r.profile_id = p.id) AS record_count
            FROM profiles p ORDER BY p.is_default DESC, p.created_at
            """
        ).fetchall()
    return [_present(r) for r in rows_to_dicts(rows)]


def get_profile(profile_id: str) -> Optional[dict[str, Any]]:
    with read() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    return _present(row_to_dict(row)) if row else None


def default_profile() -> dict[str, Any]:
    """Returns the default profile, creating 'Me' on first use."""
    with read() as conn:
        row = conn.execute("SELECT * FROM profiles ORDER BY is_default DESC, created_at LIMIT 1").fetchone()
        if row:
            return _present(row_to_dict(row))
    return create_profile("Me", relationship="self", is_default=True)


def create_profile(
    name: str,
    relationship: str = "self",
    birth_date: Optional[str] = None,
    sex: Optional[str] = None,
    is_default: bool = False,
) -> dict[str, Any]:
    now = time.time()
    pid = new_id("prf")
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
        if count == 0:
            is_default = True
        if is_default:
            conn.execute("UPDATE profiles SET is_default = 0")
        conn.execute(
            """INSERT INTO profiles(id, name, relationship, birth_date, sex, color, is_default, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pid, name.strip() or "Unnamed", relationship if relationship in RELATIONSHIPS else "other",
             birth_date, sex, PALETTE[count % len(PALETTE)], 1 if is_default else 0, now, now),
        )
    return get_profile(pid)  # type: ignore[return-value]


def update_profile(profile_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    allowed = {k: v for k, v in fields.items() if k in ("name", "relationship", "birth_date", "sex", "color") and v is not None}
    with db() as conn:
        if fields.get("is_default"):
            conn.execute("UPDATE profiles SET is_default = 0")
            conn.execute("UPDATE profiles SET is_default = 1 WHERE id = ?", (profile_id,))
        if allowed:
            sets = ", ".join(f"{k} = ?" for k in allowed)
            conn.execute(
                f"UPDATE profiles SET {sets}, updated_at = ? WHERE id = ?",
                (*allowed.values(), time.time(), profile_id),
            )
    return get_profile(profile_id)


def delete_profile(profile_id: str) -> None:
    """Deletes a profile and all of its data (records, samples, connections, devices)."""
    with db() as conn:
        conn.execute("DELETE FROM biometric_samples WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM biometric_daily WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM sync_batches WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM workouts WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM health_events WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM user_sessions WHERE device_id IN (SELECT id FROM devices WHERE profile_id = ?)", (profile_id,))
        # Their sign-in, if they had one: its sessions and agent tokens end with it (the account row goes with the
        # profile, as do the access it was given and any invitation).
        conn.execute("DELETE FROM user_sessions WHERE account_id IN (SELECT id FROM accounts WHERE profile_id = ?)", (profile_id,))
        conn.execute("UPDATE agent_tokens SET revoked_at = ? WHERE revoked_at IS NULL AND (profile_id = ? OR account_id IN "
                     "(SELECT id FROM accounts WHERE profile_id = ?))", (time.time(), profile_id, profile_id))
        conn.execute("DELETE FROM devices WHERE account_id IN (SELECT id FROM accounts WHERE profile_id = ?)", (profile_id,))
        conn.execute("DELETE FROM app_settings WHERE key = ?", (f"overview.layout.{profile_id}",))
        conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        remaining = conn.execute("SELECT id FROM profiles ORDER BY created_at LIMIT 1").fetchone()
        if remaining and not conn.execute("SELECT 1 FROM profiles WHERE is_default = 1").fetchone():
            conn.execute("UPDATE profiles SET is_default = 1 WHERE id = ?", (remaining["id"],))
    sample_counts.forget()


def resolve(profile_id: Optional[str]) -> dict[str, Any]:
    if profile_id:
        prof = get_profile(profile_id)
        if prof:
            return prof
        raise LookupError(f"Profile '{profile_id}' not found")
    return default_profile()


def _present(d: Optional[dict[str, Any]]) -> dict[str, Any]:
    assert d is not None
    d["is_default"] = bool(d.get("is_default"))
    return d
