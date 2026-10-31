"""Profiles: the people whose health data this instance manages."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from app.core import db as db_batches, settings
from app.core.db import audit, db, discard_pre_upgrade_copies, read, new_id, row_to_dict, rows_to_dicts
from app.store import sample_counts

log = logging.getLogger("syntropy.profiles")

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
    # None clears a birth date or sex; the rest can only be changed.
    allowed = {k: v for k, v in fields.items()
               if k in ("name", "relationship", "birth_date", "sex", "color", "timezone")
               and (v is not None or k in ("birth_date", "sex", "timezone"))}
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


# Wearable data hasn't a foreign key to anyone, and there can be millions of rows: it goes after whoever it belongs to,
# in short transactions (biometrics.check_source stops anything new arriving first).
LOOSE_TABLES = ("biometric_samples", "biometric_daily", "workouts", "health_events", "sync_batches", "daily_revisions",
                "data_sources", "sample_tombstones", "daily_rebuilds", "import_jobs")
_UNFINISHED = "data.unfinished_deletes"


def _person_settings(conn: Any, profile_id: str) -> None:
    for prefix in (f"overview.layout.{profile_id}", f"alerts.seen.{profile_id}", f"alerts.dismissed.{profile_id}", f"goals.{profile_id}",
                   f"insights.hidden.{profile_id}", f"training.max_hr.{profile_id}", f"report.preferences.{profile_id}", f"records.seen.{profile_id}"):
        settings.delete_prefix_in(conn, prefix)


def note_unfinished(conn: Any, item: str, done: bool = False) -> None:
    """Saved in the transaction that starts a delete, so a crash partway is finished by the next start."""
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (_UNFINISHED,)).fetchone()
    items = [i for i in (json.loads(row[0]) if row else []) if i != item]
    if not done:
        items.append(item)
    if items:
        conn.execute("INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     (_UNFINISHED, json.dumps(items), time.time()))
    else:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (_UNFINISHED,))


def delete_rows(table: str, column: str, value: str) -> None:
    def step(conn: Any, done: Any, size: int) -> Any:
        n = conn.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {column} = ? LIMIT ?)",
                         (value, size)).rowcount
        return None if n < size else (done or 0) + n
    db_batches.write_in_batches(step)


def _delete_loose(profile_id: str, keep_revision: bool = False) -> None:
    for table in LOOSE_TABLES:
        if not (keep_revision and table == "daily_revisions"):
            delete_rows(table, "profile_id", profile_id)


def _rollups_changed(conn: Any, profile_id: str) -> None:
    """As biometrics.mark_daily_changed: the person stays, so what's cached from their old rows has to go."""
    conn.execute("INSERT INTO daily_revisions(profile_id, rev) VALUES (?, 1) ON CONFLICT(profile_id) DO UPDATE SET rev = rev + 1", (profile_id,))


def delete_profile(profile_id: str) -> None:
    """Deletes a profile and all of its data (records, samples, connections, devices)."""
    from app.store import devices

    with db() as conn:
        conn.execute("DELETE FROM user_sessions WHERE device_id IN (SELECT id FROM devices WHERE profile_id = ?)", (profile_id,))
        # Their sign-in, if they had one: its sessions and agent tokens end with it (the account row goes with the
        # profile, as do the access it was given and any invitation), and so do phones it paired for other people,
        # codes still waiting, and the conversations it had with an AI about anyone: nobody else could open them.
        account = conn.execute("SELECT id FROM accounts WHERE profile_id = ?", (profile_id,)).fetchone()
        conn.execute("UPDATE agent_tokens SET revoked_at = ? WHERE revoked_at IS NULL AND profile_id = ?", (time.time(), profile_id))
        if account:
            conn.execute("DELETE FROM user_sessions WHERE account_id = ?", (account["id"],))
            conn.execute("UPDATE agent_tokens SET revoked_at = ? WHERE revoked_at IS NULL AND account_id = ?", (time.time(), account["id"]))
            for row in conn.execute("SELECT DISTINCT profile_id FROM devices WHERE account_id = ?", (account["id"],)).fetchall():
                devices.revoke_paired_in(conn, row["profile_id"], account["id"])
            conn.execute("DELETE FROM pairing_codes WHERE account_id = ?", (account["id"],))
            conn.execute("DELETE FROM chats WHERE account_id = ?", (account["id"],))
        _person_settings(conn, profile_id)
        # What the log said about them can name their doctors and what an AI was asked. That it happened, and when, stays.
        conn.execute("UPDATE audit_log SET detail = NULL WHERE profile_id = ?", (profile_id,))
        audit(conn, "user", "profile.removed")      # that it happened and who did it, not their name: they're gone from the file
        conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        remaining = conn.execute("SELECT id FROM profiles ORDER BY created_at LIMIT 1").fetchone()
        if remaining and not conn.execute("SELECT 1 FROM profiles WHERE is_default = 1").fetchone():
            conn.execute("UPDATE profiles SET is_default = 1 WHERE id = ?", (remaining["id"],))
    # With no profile row, finish_deletes picks up where this stops.
    _delete_loose(profile_id)
    sample_counts.forget()
    discard_pre_upgrade_copies()


def delete_all_data(profile_id: str) -> None:
    """Everything kept about this person, but not the person: sources and what they brought in, wearable data, the
    journal, everyone's conversations about them, goals, layouts and alert state, and paired phones."""
    with db() as conn:
        note_unfinished(conn, f"profile:{profile_id}")
        # The paired phones' Syntropy tabs are signed out along with their tokens.
        conn.execute("DELETE FROM user_sessions WHERE device_id IN (SELECT id FROM devices WHERE profile_id = ?)", (profile_id,))
        conn.execute("DELETE FROM devices WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM pairing_codes WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM chats WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM connections WHERE profile_id = ?", (profile_id,))      # records, identities and runs with them
        conn.execute("DELETE FROM clinical_records_removed WHERE profile_id = ?", (profile_id,))
        _person_settings(conn, profile_id)
        audit(conn, "user", "profile.data_deleted", None, profile_id)
    _delete_loose(profile_id, keep_revision=True)
    with db() as conn:
        _rollups_changed(conn, profile_id)
        note_unfinished(conn, f"profile:{profile_id}", done=True)
    sample_counts.forget()
    discard_pre_upgrade_copies()


def _people_in(conn: Any, table: str) -> list[str]:
    """Who has rows here. By hopping along the index where there is one: the sample tables can have millions of rows."""
    if table == "sync_batches":
        return [r[0] for r in conn.execute("SELECT DISTINCT profile_id FROM sync_batches WHERE profile_id IS NOT NULL")]
    out, last = [], ""
    while (row := conn.execute(f"SELECT MIN(profile_id) FROM {table} WHERE profile_id > ?", (last,)).fetchone()) and row[0]:
        out.append(row[0])
        last = row[0]
    return out


def finish_deletes() -> None:
    """At each start: finishes a delete a crash interrupted, and removes rows of people who are gone, which an import
    running during a delete could leave before the check that stops that (no page can reach them)."""
    with read() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (_UNFINISHED,)).fetchone()
        known = {r[0] for r in conn.execute("SELECT id FROM profiles")}
        gone = {p for table in LOOSE_TABLES for p in _people_in(conn, table)} - known
    for item in json.loads(row[0]) if row else []:
        kind, _, value = item.partition(":")
        if kind == "profile":
            _delete_loose(value, keep_revision=value in known)
        elif kind == "connection":
            delete_rows("biometric_samples", "connection_id", value)
        with db() as conn:
            if kind == "profile" and value in known:
                _rollups_changed(conn, value)
            note_unfinished(conn, item, done=True)
    for profile_id in gone:
        log.info("Removing data left behind for a person who was deleted")
        _delete_loose(profile_id)
    if gone or row:
        sample_counts.forget()
        discard_pre_upgrade_copies()


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
