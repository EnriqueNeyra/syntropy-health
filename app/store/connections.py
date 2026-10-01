"""Connections (linked data sources) and their encrypted credentials."""

from __future__ import annotations

import time
from typing import Any, Optional

from app.core import security
from app.core.db import audit, db, read, dumps, new_id, row_to_dict, rows_to_dicts
from app.store import sample_counts

PUBLIC_FIELDS = (
    "id", "profile_id", "kind", "provider", "institution_id", "display_name", "mode",
    "fhir_base_url", "patient_ref", "status", "scopes", "last_sync_at", "last_sync_status",
    "last_error", "created_at", "updated_at",
)


def _present(row: Optional[dict[str, Any]], include_credentials: bool = False) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    creds = row.pop("credentials_enc", None)
    if include_credentials:
        row["credentials"] = security.decrypt_json(creds) or {}
    else:
        c = security.decrypt_json(creds) or {}
        row["token_expires_at"] = c.get("expires_at")
        row["has_refresh_token"] = bool(c.get("refresh_token") or c.get("dynamic_client"))  # either renews access
    if "record_count" in row:   # listings: how much each source brought in
        row["sample_count"] = sample_counts.for_connection(row["id"])
    return row


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


def update_credentials(connection_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    current = (get(connection_id, include_credentials=True) or {}).get("credentials") or {}
    current.update({k: v for k, v in patch.items() if v is not None})
    update(connection_id, credentials=current)
    return current


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


def finish_run(run_id: int, connection_id: str, status: str, stats: dict[str, Any], error: Optional[str] = None) -> None:
    now = time.time()
    with db() as conn:
        conn.execute(
            "UPDATE sync_runs SET finished_at = ?, status = ?, stats_json = ?, error = ? WHERE id = ?",
            (now, status, dumps(stats), error, run_id),
        )
        conn.execute(
            """UPDATE connections SET last_sync_at = ?, last_sync_status = ?, last_error = ?, updated_at = ?,
                   status = CASE WHEN status = 'disconnected' THEN status
                                 WHEN ? = 'auth' THEN 'needs_reauth'
                                 WHEN ? = 'error' THEN 'error' ELSE 'active' END
               WHERE id = ?""",
            (now, status, error, now, stats.get("failure_kind"), status, connection_id),
        )
        conn.execute(
            "DELETE FROM sync_runs WHERE connection_id = ? AND id NOT IN "
            "(SELECT id FROM sync_runs WHERE connection_id = ? ORDER BY started_at DESC LIMIT 50)",
            (connection_id, connection_id),
        )


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
    now = time.time()
    with db() as conn:
        conn.execute("DELETE FROM oauth_pending WHERE expires_at < ?", (now,))
        conn.execute(
            "INSERT OR REPLACE INTO oauth_pending(state, kind, payload_enc, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (state, kind, security.encrypt_json(payload), now, now + ttl),
        )


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
