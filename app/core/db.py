"""
SQLite access layer.

* One short-lived connection per unit of work (``with db() as conn``), which is the
  safest pattern for SQLite under an async server with background tasks.
* WAL journaling + busy timeout so the scheduler, ingest and UI never block each other.
* Versioned migrations (``PRAGMA user_version``) from ``app/core/migrations``,
  including an upgrade path for databases created by the pre-1.0 prototype.
"""

from __future__ import annotations

import json
import logging
import os
import math
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from app.core import config

log = logging.getLogger("syntropy.db")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_migrated_paths: set[str] = set()
_migrate_lock = threading.Lock()


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """Yields a connection wrapped in a transaction (committed on success).

    Transactions start IMMEDIATE (write lock up front). Most blocks read and then write, and a
    deferred transaction that read an older snapshot cannot be upgraded to a writer once another
    connection has committed: SQLite fails that instantly with "database is locked", bypassing the
    busy timeout. Taking the lock first makes concurrent writers wait their turn instead. In WAL
    mode readers outside a transaction are never blocked.
    """
    path = config.db_path()
    ensure_migrated(path)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


@contextmanager
def read() -> Iterator[sqlite3.Connection]:
    """A connection for queries that only read. It takes a consistent snapshot without the write lock, so in WAL mode
    it never waits for the writer or other readers, and page requests run side by side. Writes are refused."""
    path = config.db_path()
    ensure_migrated(path)
    conn = _connect(path)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    finally:
        conn.close()


def _make_private(path: Path) -> None:
    """Health records: only this account may read the database (SQLite gives its -wal and -shm files the same
    permissions) and, when it's the usual data folder, that folder."""
    if os.name == "nt":          # the per-user profile folders are already private
        return
    targets = [(path, 0o600)]
    if path.parent == config.data_dir():
        targets.append((path.parent, 0o700))
    for target, mode in targets:
        try:
            if target.stat().st_mode & 0o777 != mode:
                target.chmod(mode)
        except OSError as exc:        # e.g. a folder mounted in Docker that belongs to someone else
            log.warning("Couldn't make %s private: %s", target, exc)


def ensure_migrated(path: Optional[Path] = None) -> None:
    path = path or config.db_path()
    key = str(path.resolve())
    if key in _migrated_paths:
        return
    with _migrate_lock:
        if key in _migrated_paths:
            return
        conn = _connect(path)
        _make_private(path)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            _run_migrations(conn)
        finally:
            conn.close()
        _migrated_paths.add(key)


def reset_migration_cache() -> None:
    _migrated_paths.clear()


def _run_migrations(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    for f in files:
        num = int(f.name.split("_", 1)[0])
        if num <= version:
            continue
        log.info("Applying migration %s", f.name)
        conn.execute("BEGIN")
        try:
            for statement in _split_sql(f.read_text(encoding="utf-8")):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {num}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def _split_sql(script: str) -> list[str]:
    """Splits a migration script into statements (no triggers/procedures are used)."""
    statements, buf = [], []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        buf.append(line)
        if stripped.endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    if buf:
        statements.append("\n".join(buf))
    return statements


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:20]
    return f"{prefix}_{raw}" if prefix else raw


def row_to_dict(row: Optional[sqlite3.Row], json_fields: Iterable[str] = ()) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    d = dict(row)
    for f in json_fields:
        raw = d.pop(f, None)
        key = f[:-5] if f.endswith("_json") else f
        if raw:
            try:
                d[key] = json.loads(raw)
            except (TypeError, ValueError):
                d[key] = None
        else:
            d[key] = None
    return d


def rows_to_dicts(rows: Iterable[sqlite3.Row], json_fields: Iterable[str] = ()) -> list[dict[str, Any]]:
    fields = list(json_fields)
    return [row_to_dict(r, fields) for r in rows]  # type: ignore[misc]


def finite(value: Any) -> Any:
    """Replaces NaN/Infinity (not valid JSON) with None, recursively."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    return value


def dumps(value: Any) -> Optional[str]:
    """JSON for storage. NaN/Infinity become null: they are not valid JSON and would break API responses."""
    if value is None:
        return None
    return json.dumps(finite(value), separators=(",", ":"), default=str)


def audit(conn: sqlite3.Connection, actor: str, action: str, detail: Any = None, profile_id: Optional[str] = None) -> None:
    if actor == "user":         # the account signed in for this request
        from app.core import context
        actor = context.actor()
    conn.execute(
        "INSERT INTO audit_log(at, actor, action, profile_id, detail) VALUES (?, ?, ?, ?, ?)",
        (time.time(), actor, action, profile_id, detail if isinstance(detail, str) or detail is None else dumps(detail)),
    )
