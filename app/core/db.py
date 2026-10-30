"""
SQLite access layer.

* One short-lived connection per unit of work that writes (``with db() as conn``), which is the
  safest pattern for SQLite under an async server with background tasks. Reads (``with read() as conn``)
  borrow a kept connection from a small pool instead, since a page's requests read many times.
* WAL journaling + busy timeout so the scheduler, ingest and UI never block each other.
* Versioned migrations (``PRAGMA user_version``) from ``app/core/migrations``,
  including an upgrade path for databases created by the pre-1.0 prototype. Before an upgrade a copy goes to
  ``pre-upgrade/`` beside the database; a database this build can't use safely raises ``StartupProblem``.
"""

from __future__ import annotations

import json
import logging
import os
import math
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

from app.core import config

log = logging.getLogger("syntropy.db")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_migrated_paths: set[str] = set()
_migrate_lock = threading.Lock()

PRE_UPGRADE_DIR = "pre-upgrade"
PRE_UPGRADE_KEPT = 3            # versions; the newest copy of each
_COPY_NAME = re.compile(r"^syntropy-v(\d+)-(\d{8}-\d{6})\.db$")
_COPY_MARGIN = 16 * 1024 * 1024
# Another process may be copying and upgrading a large database. It finishes by itself, or its lock ends with it.
_UPGRADE_WAIT_SECONDS = 1800
RELEASES_URL = "https://github.com/EnriqueNeyra/syntropy-health/releases"
# Past this the log is truncated (app.core.maintenance), and it's what SQLite cuts it back to after any reset.
WAL_LIMIT = 64 * 1024 * 1024
_BIG_WRITE = 1000       # rows changed by one transaction


class StartupProblem(Exception):
    """Why this data can't be used as it is, in words for the person. ``kind``: too_new, no_copy, upgrade_failed,
    damaged, unavailable, or (from app.core.security) key."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _connect(path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=15.0, isolation_level=None, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA journal_size_limit = {WAL_LIMIT}")
    return conn


def _writer(path: Path) -> sqlite3.Connection:
    """For db(). A write can wait up to the busy timeout for the lock, so never on the event loop: that would stop every
    request (tests/conftest.py fails any test that does).

    FULL because in WAL mode NORMAL doesn't sync at each commit, so a power cut can undo what was already acknowledged:
    a phone moves on after its upload's 200 and never sends those samples again. It costs one sync per commit: about 5 ms
    on a Mac, where fullfsync is what actually reaches the disk (elsewhere that setting does nothing)."""
    conn = _connect(path)
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA fullfsync = ON")
    return conn


def is_busy(exc: sqlite3.Error) -> bool:
    return getattr(exc, "sqlite_errorcode", 0) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


# Read connections are kept and reused: opening one costs about as much as a small query, and a page's request reads a
# dozen times (who's asking, settings, then the data). Each read takes one for itself (so nested reads, and a
# generator that reads across yields, never share one), and gives it back when done; a connection whose read failed
# is closed instead. Kept per database file, so tests that switch folders start afresh (see reset_migration_cache).
_READ_POOL_MAX = 16
_read_pool: dict[str, list[sqlite3.Connection]] = {}
_read_pool_lock = threading.Lock()


def _take_reader(path: Path) -> sqlite3.Connection:
    with _read_pool_lock:
        idle = _read_pool.get(str(path))
        if idle:
            return idle.pop()
    # Handed between threads (a streamed response's generator resumes in whichever worker is free), but only ever
    # used by one at a time: the pool gives each connection to one read.
    conn = _connect(path, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA cache_size = -8000")      # 8 MB of pages, kept between reads
    return conn


def _return_reader(path: Path, conn: sqlite3.Connection) -> None:
    with _read_pool_lock:
        idle = _read_pool.setdefault(str(path), [])
        if len(idle) < _READ_POOL_MAX:
            idle.append(conn)
            return
    conn.close()


def close_readers() -> None:
    with _read_pool_lock:
        pools = list(_read_pool.values())
        _read_pool.clear()
    for idle in pools:
        for conn in idle:
            try:
                conn.close()
            except sqlite3.Error:
                pass


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
    conn = _writer(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
        big = conn.total_changes >= _BIG_WRITE
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    if big:
        # Right after letting go of the lock it has the best chance of a checkpoint: from another thread it kept losing
        # to the next batch and the log reached gigabytes. And what fills the log waits for it to empty; a small
        # write never waits for this.
        checkpoint_if_large(path, wait_seconds=1.0)


@contextmanager
def read() -> Iterator[sqlite3.Connection]:
    """A connection for queries that only read. It takes a consistent snapshot without the write lock, so in WAL mode
    it never waits for the writer or other readers, and page requests run side by side. Writes are refused."""
    path = config.db_path()
    ensure_migrated(path)
    conn = _take_reader(path)
    done = False
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
        done = True
    finally:
        if done:
            _return_reader(path, conn)
        else:
            try:
                conn.close()          # closing ends any transaction left open
            except sqlite3.Error:
                pass


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
        conn = None
        try:
            conn = _connect(path)
            _make_private(path)
            _use_wal(conn)
            _run_migrations(conn, path)
        except sqlite3.Error as exc:
            raise unusable(exc, path) from exc
        finally:
            if conn:
                conn.close()
        _migrated_paths.add(key)


def reset_migration_cache() -> None:
    _migrated_paths.clear()
    close_readers()


def unusable(exc: sqlite3.Error, path: Path) -> StartupProblem:
    if isinstance(exc, sqlite3.DatabaseError) and not isinstance(exc, sqlite3.OperationalError):
        log.error("%s can't be read: %s", path, exc)
        return StartupProblem("damaged", f"The database file at {path} is damaged and can't be opened. It hasn't been changed "
                                         f"or moved. Restore it from a backup{_copy_hint(path)}, then start Syntropy Health again.")
    log.error("%s can't be used: %s", path, exc)
    return StartupProblem("unavailable", f"Syntropy Health can't use its database at {path}: {exc}. Check that the disk "
                                         "has room and that this account may write to that folder, then start it again.")


def _copy_hint(path: Path) -> str:
    copies = pre_upgrade_copies(path)
    return f", or from the copy made before the last upgrade ({copies[0]['path']})" if copies else ""


def _use_wal(conn: sqlite3.Connection) -> None:
    """Changing the journal needs the file to itself, and that wait doesn't honor the busy timeout."""
    deadline = time.monotonic() + _UPGRADE_WAIT_SECONDS
    while True:
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if not is_busy(exc) or time.monotonic() > deadline:
                raise
            time.sleep(0.1)


def _migration_files() -> list[tuple[int, Path]]:
    return sorted((int(f.name.split("_", 1)[0]), f) for f in MIGRATIONS_DIR.glob("*.sql"))


def newest_schema() -> int:
    return _migration_files()[-1][0]


def refuse_if_newer(version: int, newest: int) -> None:
    if version > newest:
        raise StartupProblem("too_new", f"This data was last used by a newer version of Syntropy Health (its database is at "
                                        f"version {version}; this version knows up to {newest}). Nothing has been changed. "
                                        f"Install the latest version from {RELEASES_URL} and start it again.")


def _begin_upgrade(conn: sqlite3.Connection) -> None:
    deadline = time.monotonic() + _UPGRADE_WAIT_SECONDS
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if not is_busy(exc) or time.monotonic() > deadline:
                raise
            log.info("Waiting for another Syntropy Health process using the database")


def _run_migrations(conn: sqlite3.Connection, path: Path) -> None:
    """Every pending migration in one transaction, under the write lock from before anything is read: another process
    waits here, then finds the work done. The copy is taken under that lock too, so only one of them takes it."""
    files = _migration_files()
    newest = files[-1][0]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    refuse_if_newer(version, newest)
    if version == newest:
        return
    _begin_upgrade(conn)
    copy = None
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        refuse_if_newer(version, newest)
        pending = [(num, f) for num, f in files if num > version]
        if pending and conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
            copy = _copy_before_upgrade(path, version, newest)
        for num, f in pending:
            log.info("Applying migration %s", f.name)
            for statement in _split_sql(f.read_text(encoding="utf-8")):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {num}")
        conn.execute("COMMIT")
    except BaseException as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if isinstance(exc, sqlite3.Error) and copy and not is_busy(exc):
            log.exception("The upgrade from database version %s failed", version)
            raise StartupProblem("upgrade_failed", f"Syntropy Health couldn't upgrade its database, so it has been left "
                                                   f"unchanged. A copy from before the attempt is at {copy}. Install the "
                                                   f"previous version, or report this at {RELEASES_URL}.") from exc
        raise


def _copy_before_upgrade(path: Path, version: int, newest: int) -> Path:
    """An online copy beside the database, or StartupProblem: without one, a migration that's valid SQL and wrong would
    leave no way back."""
    folder = path.parent / PRE_UPGRADE_DIR
    try:
        copy = _save_copy(path, folder, version)
    except (OSError, sqlite3.Error) as exc:
        log.exception("Couldn't copy the database before upgrading it")
        raise StartupProblem("no_copy", f"Syntropy Health needs to upgrade its database and keeps a copy first, but couldn't "
                                        f"make one in {folder}: {exc}. Nothing has been changed. Free up some disk space "
                                        "and start it again.") from exc
    log.warning("Saved a copy of the database to %s before upgrading it from version %s to %s", copy, version, newest)
    _prune_copies(folder)
    return copy


def _save_copy(path: Path, folder: Path, version: int) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(path), timeout=15.0)
    try:
        needed = src.execute("PRAGMA page_count").fetchone()[0] * src.execute("PRAGMA page_size").fetchone()[0]
        free = shutil.disk_usage(folder).free
        if free < needed + _COPY_MARGIN:
            raise StartupProblem("no_copy", f"Syntropy Health needs to upgrade its database and keeps a copy first, but there "
                                            f"isn't room in {folder}: it needs about {_mb(needed + _COPY_MARGIN)} and "
                                            f"{_mb(free)} is free. Nothing has been changed. Free up some disk space and "
                                            "start it again.")
        if os.name != "nt":
            folder.chmod(0o700)
        final = folder / f"syntropy-v{version}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.db"
        tmp = final.with_name(final.name + ".tmp")
        os.close(os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600))
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
                if dst.execute("PRAGMA user_version").fetchone()[0] != version or \
                        dst.execute("PRAGMA page_count").fetchone()[0] * dst.execute("PRAGMA page_size").fetchone()[0] < needed:
                    raise sqlite3.DatabaseError("the copy doesn't match the database")
            finally:
                dst.close()
            with open(tmp, "rb+") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp, final)
        finally:
            for leftover in (tmp, Path(f"{tmp}-wal"), Path(f"{tmp}-shm")):
                leftover.unlink(missing_ok=True)
    finally:
        src.close()
    return final


def _mb(size: int) -> str:
    return f"{size / 1024 / 1024:,.0f} MB"


def _prune_copies(folder: Path) -> None:
    """A failed upgrade retried at every start copies the same version again: that mustn't push out older ones."""
    newest_of: dict[int, tuple[str, Path]] = {}
    for f in folder.iterdir():
        match = _COPY_NAME.match(f.name)
        if not match:
            if f.name.endswith((".tmp", ".tmp-wal", ".tmp-shm")):
                f.unlink(missing_ok=True)
            continue
        version, stamp = int(match[1]), match[2]
        if version not in newest_of or stamp > newest_of[version][0]:
            if version in newest_of:
                newest_of[version][1].unlink(missing_ok=True)
            newest_of[version] = (stamp, f)
        else:
            f.unlink(missing_ok=True)
    for version in sorted(newest_of, reverse=True)[PRE_UPGRADE_KEPT:]:
        newest_of[version][1].unlink(missing_ok=True)


def pre_upgrade_copies(path: Optional[Path] = None) -> list[dict[str, Any]]:
    """The copies kept from before upgrades, newest first."""
    folder = (path or config.db_path()).parent / PRE_UPGRADE_DIR
    out = []
    for f in folder.glob("*.db") if folder.is_dir() else []:
        if match := _COPY_NAME.match(f.name):
            info = f.stat()
            out.append({"path": str(f), "version": int(match[1]), "created_at": info.st_mtime, "size_bytes": info.st_size})
    return sorted(out, key=lambda c: -c["created_at"])


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
# Long rewrites
# ---------------------------------------------------------------------------

BATCH_TARGET_SECONDS = 0.25
BATCH_MAX = 100_000


def write_in_batches(step: Callable[[sqlite3.Connection, Any, int], Any], *, cursor: Any = None, size: int = 1000,
                     pause: Optional[float] = None, should_stop: Optional[Callable[[], bool]] = None) -> Any:
    """The one way to rewrite a lot of rows: each batch is its own transaction, sized to hold the write lock for a
    fraction of a second, with a gap after it so phone uploads and page saves get their turn. Never on the event loop.

    ``step(conn, cursor, size)`` does up to ``size`` rows in the open transaction and returns where to carry on next, or
    ``None`` when there's nothing left. ``size`` adapts to how long batches take. A batch that raises is rolled back and
    the exception comes out here, so what earlier batches did stays done: make steps safe to run again from their cursor.
    Returns ``None`` when finished, or the cursor to resume from if ``should_stop`` ended it first."""
    while not (should_stop and should_stop()):
        with db() as conn:
            started = time.monotonic()      # waiting for the lock doesn't count
            cursor = step(conn, cursor, size)
        held = time.monotonic() - started
        if cursor is None:
            return None
        if held > 2 * BATCH_TARGET_SECONDS:
            size = max(1, int(size * BATCH_TARGET_SECONDS / held))
        elif held < BATCH_TARGET_SECONDS / 2:
            size = min(BATCH_MAX, size * 2)
        checkpoint_if_large()
        # A writer waiting on SQLite's busy handler retries every 100 ms or so, and needs to find the lock free.
        time.sleep(pause if pause is not None else max(0.12, held / 2))
    return cursor


# ---------------------------------------------------------------------------
# Keeping the file healthy (the thread that uses these is in app.core.maintenance)
# ---------------------------------------------------------------------------

def checkpoint(path: Optional[Path] = None, wait_seconds: float = 2.0) -> bool:
    """Moves the write-ahead log into the database and empties it. True if it did; if writers or readers are busy
    (``wait_seconds``), the next try will. Most of the copying happens first, without holding up writers."""
    path = path or config.db_path()
    try:
        conn = sqlite3.connect(str(path), timeout=wait_seconds, isolation_level=None)
    except sqlite3.Error:
        return False
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(wait_seconds * 1000)}")
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        return conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    except sqlite3.Error as exc:
        log.info("Couldn't check-point the database right now: %s", exc)
        return False
    finally:
        conn.close()


def checkpoint_if_large(path: Optional[Path] = None, wait_seconds: float = 2.0) -> bool:
    path = path or config.db_path()
    try:
        if Path(f"{path}-wal").stat().st_size <= WAL_LIMIT:
            return False
    except OSError:
        return False
    return checkpoint(path, wait_seconds)



def probe(path: Path) -> dict[str, Any]:
    """Reads the database and starts a write that changes nothing, on a connection of its own that doesn't wait: a long
    write in progress is "busy", not a failure. BEGIN IMMEDIATE alone isn't enough, since on a read-only file SQLite
    quietly makes it a read. The statement that matches no rows writes no pages, so this leaves the disk alone."""
    out: dict[str, Any] = {"journal_mode": None, "schema_version": None, "database": "ok"}
    conn = sqlite3.connect(str(path), timeout=0.2, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout = 200")
        out["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
        out["schema_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE app_settings SET updated_at = updated_at WHERE 0")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        if is_busy(exc):
            out["database"] = "busy"
        elif exc.sqlite_errorcode & 0xff == sqlite3.SQLITE_READONLY:
            out["database"] = "read-only"
        else:
            raise
    finally:
        conn.close()
    return out


def snapshot(dest: Path, path: Optional[Path] = None) -> None:
    """A consistent copy of the database made while it's in use, read back before anyone relies on it. A damaged file
    raises StartupProblem: a backup that looks fine and isn't is worse than none."""
    path = path or config.db_path()
    src = sqlite3.connect(str(path), timeout=15.0)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
        found = [r[0] for r in dst.execute("PRAGMA quick_check(5)").fetchall()]
        if found != ["ok"]:
            raise sqlite3.DatabaseError("; ".join(found))
    except sqlite3.DatabaseError as exc:
        if isinstance(exc, sqlite3.OperationalError):
            raise
        raise StartupProblem("damaged", f"The backup was stopped because the database file at {path} has damage ({str(exc)[:200]}), "
                                        "so the copy couldn't be relied on. Keep your older backups: they may be the "
                                        "last good copies.") from exc
    finally:
        dst.close()
        src.close()


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
