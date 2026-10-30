"""
Looking after the database file while the server runs, on one background thread:

* The write-ahead log. SQLite's automatic checkpoint can't reset the log while readers and writers overlap, so a phone's
  first backfill grew it to gigabytes, and it stayed that size. Above ``db.WAL_LIMIT`` it's truncated here.
* Damage. ``PRAGMA quick_check`` takes about a second per GB on an SSD (measured on a 1 GB file; a spinning disk or
  a cold cache is many times that), so it runs before serving only on small files, otherwise a little after start-up,
  and then daily. It reads pages, not the indexes against their tables: ``integrity_check`` took 30 times longer.
* Statistics, for the query planner: ``PRAGMA optimize`` every few hours.

``health()`` is what /health reports, and ``problems()`` what owners are told about in Alerts.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.core import config, db, security, settings

log = logging.getLogger("syntropy.maintenance")

TICK_SECONDS = 5.0
INTEGRITY_EVERY = 24 * 3600
OPTIMIZE_EVERY = 6 * 3600
SMALL_FILE = 256 * 1024 * 1024
LARGE_FILE_DELAY = 120          # not while the first pages are loading
MIN_FREE_DISK = 1024 * 1024     # SQLite can't commit without room
_INTEGRITY_KEY = "db.integrity"

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_checking: Optional[sqlite3.Connection] = None
_damage: dict[str, str] = {}     # by file, for when the result can't be saved in the damaged file itself


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Damage
# ---------------------------------------------------------------------------

def check_integrity(path: Optional[Path] = None) -> dict[str, Any]:
    """Reads the whole file, without blocking anyone. Saves and returns what it found."""
    global _checking
    path = path or config.db_path()
    started = time.monotonic()
    conn = None
    try:
        conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False)
        _checking = conn
        rows = [r[0] for r in conn.execute("PRAGMA quick_check(5)").fetchall()]
        detail = None if rows == ["ok"] else "; ".join(rows)
    except sqlite3.OperationalError as exc:
        if db.is_busy(exc) or "interrupted" in str(exc):
            return integrity(path) or {"ok": None, "checked_at": None, "detail": None}
        detail = str(exc)
    except sqlite3.DatabaseError as exc:
        detail = str(exc)
    finally:
        _checking = None
        if conn:
            conn.close()
    result = {"ok": detail is None, "checked_at": time.time(), "detail": detail}
    log.info("Checked the database for damage in %.1f s: %s", time.monotonic() - started, detail or "none found")
    if detail:
        note_damage(detail, path)
    else:
        _damage.pop(str(path), None)
    try:
        settings.set(_INTEGRITY_KEY, result)
    except (sqlite3.Error, db.StartupProblem) as exc:
        log.warning("Couldn't save the result of the database check: %s", exc)
    return result


def note_damage(detail: str, path: Optional[Path] = None) -> None:
    path = path or config.db_path()
    if str(path) not in _damage:
        log.error("The database at %s is damaged: %s", path, detail)
    _damage[str(path)] = detail


def integrity(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """The last result: damage noticed since then, else what was saved."""
    path = path or config.db_path()
    if str(path) in _damage:
        return {"ok": False, "checked_at": None, "detail": _damage[str(path)]}
    try:
        return settings.get(_INTEGRITY_KEY)
    except (sqlite3.Error, db.StartupProblem, ValueError):
        return None


def integrity_due(path: Optional[Path] = None) -> bool:
    last = integrity(path)
    return not last or not last.get("checked_at") or time.time() - last["checked_at"] >= INTEGRITY_EVERY


def check_at_start_up(path: Optional[Path] = None) -> None:
    """Only a small file, where it takes no time; the background thread gets to a large one."""
    path = path or config.db_path()
    if _size(path) <= SMALL_FILE:
        check_integrity(path)


# ---------------------------------------------------------------------------
# What's reported
# ---------------------------------------------------------------------------

def health(path: Optional[Path] = None) -> dict[str, Any]:
    """For /health: fast, and honest about whether this database can be read and written. ``detail`` says what's wrong;
    a long write in progress isn't wrong."""
    path = path or config.db_path()
    out: dict[str, Any] = {"journal_mode": None, "database": "ok", "schema_version": None, "detail": None}
    try:
        db.ensure_migrated(path)
        state = db.probe(path)
        out.update(journal_mode=state["journal_mode"], schema_version=state["schema_version"], database=state["database"])
    except db.StartupProblem as exc:
        return {**out, "database": exc.kind, "detail": str(exc)}
    except sqlite3.DatabaseError as exc:
        return {**out, "database": "damaged", "detail": _damage_text(path, str(exc))}
    newest = db.newest_schema()
    if out["schema_version"] is not None and out["schema_version"] != newest:
        try:
            db.refuse_if_newer(out["schema_version"], newest)
            detail = f"The database is at version {out['schema_version']}, but this version expects {newest}. Start Syntropy Health again."
        except db.StartupProblem as exc:
            detail = str(exc)
        return {**out, "database": "version", "detail": detail}
    if out["database"] == "read-only":
        return {**out, "detail": f"Syntropy Health can't write to its database at {path}. Check that this account may write "
                                 "to that file and folder, and that the disk isn't mounted read-only."}
    if shutil.disk_usage(path.parent).free < MIN_FREE_DISK:
        return {**out, "database": "disk-full", "detail": f"The disk holding {path.parent} is full, so nothing new can be "
                                                          "saved. Free up some space."}
    if str(path) in _damage:
        return {**out, "database": "damaged", "detail": _damage_text(path, _damage[str(path)])}
    return out


def _damage_text(path: Path, detail: str) -> str:
    copies = db.pre_upgrade_copies(path)
    copy = f" A copy from before the last upgrade is at {copies[0]['path']}." if copies else ""
    return (f"Syntropy Health found damage in its database file at {path} ({detail[:200]}). Some records may not open, and new "
            f"data may not save. Keep your older backups: they may be the last good copies.{copy}")


def problems() -> list[dict[str, str]]:
    """What the server's owner should hear about, for Alerts."""
    out = []
    path = config.db_path()
    if str(path) in _damage:
        out.append({"id": "database-damaged", "title": "The database file has damage", "text": _damage_text(path, _damage[str(path)])})
    key = security.key_problem()
    if key:
        out.append({"id": "encryption-key", "title": "Saved sign-ins can't be read", "text": key})
    return out


def details() -> dict[str, Any]:
    """For the owner: /api/health."""
    path = config.db_path()
    state = health(path)
    return {
        "schema_version": state["schema_version"],
        "newest_schema": db.newest_schema(),
        "state": state["database"],
        "detail": state["detail"],
        "path": str(path),
        "size_bytes": _size(path),
        "wal_bytes": _size(Path(f"{path}-wal")),
        "integrity": integrity(path),
        "pre_upgrade_copies": db.pre_upgrade_copies(path),
    }


# ---------------------------------------------------------------------------
# The thread
# ---------------------------------------------------------------------------

def _optimize(path: Path) -> None:
    conn = sqlite3.connect(str(path), timeout=2.0, isolation_level=None)
    try:
        conn.execute("PRAGMA analysis_limit = 400")      # a sample, so it takes milliseconds on any size of table
        conn.execute("PRAGMA optimize = 0x10002")        # every table, not only those this connection has read
    except sqlite3.Error as exc:
        log.info("Skipped updating the database's statistics: %s", exc)
    finally:
        conn.close()


def _run(path: Path) -> None:
    next_check = time.monotonic() + (0 if _size(path) <= SMALL_FILE else LARGE_FILE_DELAY)
    next_optimize = time.monotonic() + 300
    while not _stop.wait(TICK_SECONDS):
        try:
            db.checkpoint_if_large(path)
            now = time.monotonic()
            if now >= next_check:
                next_check = now + 3600
                if integrity_due(path):
                    check_integrity(path)
            if now >= next_optimize:
                next_optimize = now + OPTIMIZE_EVERY
                _optimize(path)
        except Exception:  # noqa: BLE001 - this thread must outlive whatever went wrong once
            log.exception("Database maintenance failed")


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, args=(config.db_path(),), daemon=True, name="db-maintenance")
    _thread.start()


def stop() -> None:
    """Ends the thread, and leaves the log empty: a stopped server's data is all in the one file, ready to copy."""
    global _thread
    path = config.db_path()
    if _thread:
        _stop.set()
        checking = _checking
        if checking:
            checking.interrupt()
        _thread.join(10)
        _thread = None
    db.close_readers()
    db.checkpoint(path)
