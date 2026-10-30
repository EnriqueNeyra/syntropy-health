"""Connections (linked data sources) and their encrypted credentials."""

from __future__ import annotations

import time
from typing import Any, Optional

from app.core import security
from app.core.db import audit, db, discard_pre_upgrade_copies, read, dumps, new_id, row_to_dict, rows_to_dicts
from app.store import sample_counts

PUBLIC_FIELDS = (
    "id", "profile_id", "kind", "provider", "institution_id", "display_name", "mode",
    "fhir_base_url", "patient_ref", "status", "scopes", "last_sync_at", "last_sync_status",
    "last_error", "next_retry_at", "sync_failures", "created_at", "updated_at",
)

# A temporary failure tries again soon, then less often. It only reads as "Sync failed" once it has lasted a while: a
# laptop waking up before its Wi-Fi isn't worth an alert.
RETRY_DELAYS = (15 * 60, 30 * 60, 3600, 2 * 3600, 4 * 3600, 6 * 3600)
FAILURES_BEFORE_ALERT = 3
UNFINISHED_RETRIES = 5        # a history that won't finish goes back to the usual interval after this many early tries


def _present(row: Optional[dict[str, Any]], include_credentials: bool = False) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    creds = row.pop("credentials_enc", None)
    c = security.decrypt_json(creds) or {}
    if creds and not c and row.get("status") in ("active", "error", "needs_reauth"):
        # Saved, but this encryption key can't read it. It stays saved: with the right key back, it works again. Until
        # then it asks to be signed in again, and isn't mistaken for a finished one-time import.
        row["credentials_unreadable"] = True
        row["status"] = "needs_reauth"
    if include_credentials:
        row["credentials"] = c
    else:
        row["token_expires_at"] = c.get("expires_at")
        row["has_refresh_token"] = bool(c.get("refresh_token") or c.get("dynamic_client"))  # either renews access
    if row.get("kind") == "ehr":
        row["access_ended"] = not row.get("credentials_unreadable") and _access_ended(row, c)
    if "record_count" in row:   # listings: how much each source brought in
        row["sample_count"] = sample_counts.for_connection(row["id"])
    return row


def _access_ended(row: dict[str, Any], creds: dict[str, Any]) -> bool:
    """A health record connection whose access ran out and can't be renewed: a one-time import, such as healow's
    (which gives public apps no refresh token). That's expected, not a fault: signing in again brings in what's new."""
    if row.get("status") == "disconnected" or creds.get("refresh_token") or creds.get("dynamic_client"):
        return False
    expires_at = creds.get("expires_at")
    return (row.get("status") == "needs_reauth" or not creds.get("access_token")
            or bool(expires_at and expires_at <= time.time()))


def create(
    profile_id: str,
    kind: str,
    provider: str,
    display_name: str,
    *,
    mode: str = "live",
    institution_id: Optional[str] = None,
    fhir_base_url: Optional[str] = None,
    patient_ref: Optional[str] = None,
    scopes: Optional[str] = None,
    credentials: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
    status: str = "active",
) -> dict[str, Any]:
    now = time.time()
    cid = new_id("con")
    with db() as conn:
        conn.execute(
            """INSERT INTO connections(id, profile_id, kind, provider, institution_id, display_name, mode,
                   fhir_base_url, patient_ref, status, scopes, credentials_enc, metadata_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cid, profile_id, kind, provider, institution_id, display_name, mode, fhir_base_url, patient_ref,
             status, scopes, security.encrypt_json(credentials) if credentials else None, dumps(metadata or {}), now, now),
        )
        audit(conn, "user", "connection.created", {"id": cid, "provider": provider, "name": display_name}, profile_id)
    return get(cid)  # type: ignore[return-value]


def find_existing(profile_id: str, provider: str, *, institution_id: Optional[str] = None,
                  fhir_base_url: Optional[str] = None, patient_ref: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Finds a connection that should be re-used on reconnect (same source, same patient)."""
    q = "SELECT * FROM connections WHERE profile_id = ? AND provider = ?"
    params: list[Any] = [profile_id, provider]
    if institution_id:
        q += " AND institution_id = ?"
        params.append(institution_id)
    if fhir_base_url:
        q += " AND fhir_base_url = ?"
        params.append(fhir_base_url)
    if patient_ref:
        q += " AND patient_ref = ?"
        params.append(patient_ref)
    q += " ORDER BY created_at DESC LIMIT 1"
    with read() as conn:
        row = conn.execute(q, params).fetchone()
    return _present(row_to_dict(row, ["metadata_json"])) if row else None


def get(connection_id: str, include_credentials: bool = False) -> Optional[dict[str, Any]]:
    with read() as conn:
        row = conn.execute(
            """SELECT c.*,
                  (SELECT COUNT(*) FROM clinical_records r WHERE r.connection_id = c.id) AS record_count
               FROM connections c WHERE c.id = ?""",
            (connection_id,),
        ).fetchone()
    return _present(row_to_dict(row, ["metadata_json"]), include_credentials) if row else None


def list_for_profile(profile_id: Optional[str] = None, kind: Optional[str] = None,
                     include_disconnected: bool = False) -> list[dict[str, Any]]:
    q = """SELECT c.*,
              (SELECT COUNT(*) FROM clinical_records r WHERE r.connection_id = c.id) AS record_count
           FROM connections c WHERE 1=1"""
    params: list[Any] = []
    if profile_id:
        q += " AND c.profile_id = ?"
        params.append(profile_id)
    if kind:
        q += " AND c.kind = ?"
        params.append(kind)
    if not include_disconnected:
        q += " AND c.status != 'disconnected'"
    q += " ORDER BY c.created_at"
    with read() as conn:
        rows = conn.execute(q, params).fetchall()
    return [_present(r) for r in rows_to_dicts(rows, ["metadata_json"])]  # type: ignore[misc]


def list_active_all() -> list[dict[str, Any]]:
    with read() as conn:
        rows = conn.execute("SELECT * FROM connections WHERE status IN ('active', 'error')").fetchall()
    return [_present(r) for r in rows_to_dicts(rows, ["metadata_json"])]  # type: ignore[misc]


def update(connection_id: str, **fields: Any) -> None:
    cols: dict[str, Any] = {}
    for k, v in fields.items():
        if k == "credentials":
            cols["credentials_enc"] = security.encrypt_json(v) if v else None
        elif k == "metadata":
            cols["metadata_json"] = dumps(v or {})
        elif k in PUBLIC_FIELDS:
            cols[k] = v
    if not cols:
        return
    cols["updated_at"] = time.time()
    sets = ", ".join(f"{k} = ?" for k in cols)
    with db() as conn:
        conn.execute(f"UPDATE connections SET {sets} WHERE id = ?", (*cols.values(), connection_id))


def save_renewed(connection_id: str, had: dict[str, Any], renewed: dict[str, Any]) -> bool:
    """Saves tokens renewed during a sync, the moment they arrive: a vendor that rotates refresh tokens has already
    retired the old one. Unless the person has signed in again meanwhile: those tokens are newer than these."""
    with db() as conn:
        row = conn.execute("SELECT credentials_enc FROM connections WHERE id = ?", (connection_id,)).fetchone()
        if not row or _replaced(security.decrypt_json(row["credentials_enc"]) or {}, had):
            return False
        conn.execute("UPDATE connections SET credentials_enc = ?, updated_at = ? WHERE id = ?",
                     (security.encrypt_json(renewed), time.time(), connection_id))
    return True


def _replaced(stored: dict[str, Any], had: dict[str, Any]) -> bool:
    return any(stored.get(k) != had.get(k) for k in ("access_token", "refresh_token"))


def credentials_replaced(connection_id: str, had: dict[str, Any]) -> bool:
    """A sync's failure says nothing about a sign-in that happened after it started."""
    with read() as conn:
        row = conn.execute("SELECT credentials_enc FROM connections WHERE id = ?", (connection_id,)).fetchone()
    return bool(row) and _replaced(security.decrypt_json(row["credentials_enc"]) or {}, had)


def forget_query_state(connection_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM sync_query_state WHERE connection_id = ?", (connection_id,))


def query_state(connection_id: str) -> dict[str, dict[str, Any]]:
    with read() as conn:
        rows = conn.execute("SELECT * FROM sync_query_state WHERE connection_id = ?", (connection_id,)).fetchall()
    return {r["query"]: dict(r) for r in rows}


def save_query_state(connection_id: str, states: dict[str, dict[str, Any]]) -> None:
    now = time.time()
    with db() as conn:
        for query, s in states.items():
            answered = 1 if s["status"] in ("ok", "empty") else 0
            conn.execute(
                """INSERT INTO sync_query_state(connection_id, query, status, record_count, next_url, alternative, no_sort,
                       ever_answered, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(connection_id, query) DO UPDATE SET status = excluded.status,
                       record_count = excluded.record_count, next_url = excluded.next_url, alternative = excluded.alternative,
                       no_sort = excluded.no_sort, ever_answered = MAX(ever_answered, excluded.ever_answered),
                       updated_at = excluded.updated_at""",
                (connection_id, query, s["status"], s.get("record_count") or 0, s.get("next_url"), s.get("alternative") or 0,
                 1 if s.get("no_sort") else 0, answered, now),
            )


def disconnect(connection_id: str, delete_data: bool = False) -> None:
    with db() as conn:
        row = conn.execute("SELECT profile_id, display_name FROM connections WHERE id = ?", (connection_id,)).fetchone()
        if not row:
            return
        if delete_data:
            conn.execute("DELETE FROM biometric_samples WHERE connection_id = ?", (connection_id,))
            conn.execute("DELETE FROM workouts WHERE connection_id = ?", (connection_id,))
            conn.execute("DELETE FROM health_events WHERE connection_id = ?", (connection_id,))
            conn.execute("DELETE FROM connections WHERE id = ?", (connection_id,))
        else:
            conn.execute(
                "UPDATE connections SET status = 'disconnected', credentials_enc = NULL, updated_at = ? WHERE id = ?",
                (time.time(), connection_id),
            )
        audit(conn, "user", "connection.removed" if delete_data else "connection.disconnected",
              {"id": connection_id, "name": row["display_name"]}, row["profile_id"])
    if delete_data:
        sample_counts.forget()
        discard_pre_upgrade_copies()


# ---------------------------------------------------------------------------
# Sync runs
# ---------------------------------------------------------------------------

def start_run(connection_id: str, trigger: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO sync_runs(connection_id, trigger, started_at) VALUES (?, ?, ?)",
            (connection_id, trigger, time.time()),
        )
        return int(cur.lastrowid)


def _after_run(row: Any, status: str, stats: dict[str, Any], now: float) -> tuple[str, int, Optional[float]]:
    """How a connection stands after a sync: its status, how many tries in a row have fallen short, and when to try next
    if that's sooner than usual."""
    kind = stats.get("failure_kind")
    failures = row["sync_failures"] or 0
    if row["status"] == "disconnected" or kind == "superseded":
        return row["status"], failures, row["next_retry_at"]
    if kind == "auth":
        return "needs_reauth", 0, None
    if kind == "network":
        delay = max(RETRY_DELAYS[min(failures, len(RETRY_DELAYS) - 1)], stats.get("retry_after") or 0)
        return ("error" if failures + 1 >= FAILURES_BEFORE_ALERT else "active"), failures + 1, now + delay
    if status == "error":
        return "error", failures + 1, None
    if stats.get("more_to_fetch"):
        if failures >= UNFINISHED_RETRIES:
            return "active", failures, None
        return "active", failures + 1, now + max(RETRY_DELAYS[failures], stats.get("retry_after") or 0)
    return "active", 0, None


def finish_run(run_id: int, connection_id: str, status: str, stats: dict[str, Any], error: Optional[str] = None) -> None:
    now = time.time()
    with db() as conn:
        conn.execute(
            "UPDATE sync_runs SET finished_at = ?, status = ?, stats_json = ?, error = ? WHERE id = ?",
            (now, status, dumps(stats), error, run_id),
        )
        row = conn.execute("SELECT status, sync_failures, next_retry_at FROM connections WHERE id = ?", (connection_id,)).fetchone()
        if row:
            state, failures, retry_at = _after_run(row, status, stats, now)
            conn.execute(
                """UPDATE connections SET last_sync_at = ?, last_sync_status = ?, last_error = ?, updated_at = ?, status = ?,
                       sync_failures = ?, next_retry_at = ? WHERE id = ?""",
                (now, status, error, now, state, failures, retry_at, connection_id),
            )
        conn.execute(
            "DELETE FROM sync_runs WHERE connection_id = ? AND id NOT IN "
            "(SELECT id FROM sync_runs WHERE connection_id = ? ORDER BY started_at DESC LIMIT 50)",
            (connection_id, connection_id),
        )


def last_success_at(connection: dict[str, Any]) -> Optional[float]:
    """When this connection last synced without an error. ``last_sync_at`` moves on failed syncs too (it paces the
    retries), so it can't say what has been fetched. Only the last 50 runs are kept: past those, a connection whose
    latest sync failed has no known success, and the caller fetches its whole first window again."""
    with read() as conn:
        row = conn.execute("SELECT MAX(started_at) FROM sync_runs WHERE connection_id = ? AND status IN ('success', 'partial')",
                           (connection["id"],)).fetchone()
    if row and row[0]:
        return row[0]
    return connection.get("last_sync_at") if connection.get("last_sync_status") in ("success", "partial") else None


def recent_runs(connection_id: str, limit: int = 10) -> list[dict[str, Any]]:
    with read() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_runs WHERE connection_id = ? ORDER BY started_at DESC LIMIT ?",
            (connection_id, limit),
        ).fetchall()
    return rows_to_dicts(rows, ["stats_json"])


# ---------------------------------------------------------------------------
# Pending OAuth state
# ---------------------------------------------------------------------------

def save_pending(state: str, kind: str, payload: dict[str, Any], ttl: int = 900) -> None:
    from app.core import context
    payload = {**payload, "actor": context.actor()}      # who the sign-in returns to (see context.ACTING_FOR)
    now = time.time()
    with db() as conn:
        conn.execute("DELETE FROM oauth_pending WHERE expires_at < ?", (now,))
        conn.execute(
            "INSERT OR REPLACE INTO oauth_pending(state, kind, payload_enc, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (state, kind, security.encrypt_json(payload), now, now + ttl),
        )


def peek_pending(state: str) -> Optional[tuple[str, dict[str, Any]]]:
    """What a sign-in link stands for, leaving it usable."""
    with read() as conn:
        row = conn.execute("SELECT kind, payload_enc, expires_at FROM oauth_pending WHERE state = ?", (state,)).fetchone()
    if not row or row["expires_at"] < time.time():
        return None
    return row["kind"], security.decrypt_json(row["payload_enc"]) or {}


def pop_pending(state: str) -> Optional[tuple[str, dict[str, Any]]]:
    with db() as conn:
        row = conn.execute(
            "SELECT kind, payload_enc, expires_at FROM oauth_pending WHERE state = ?", (state,)
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM oauth_pending WHERE state = ?", (state,))
    if row["expires_at"] < time.time():
        return None
    return row["kind"], security.decrypt_json(row["payload_enc"]) or {}
