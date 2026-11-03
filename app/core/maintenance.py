"""
Looking after the database file while the server runs, on one background thread:

* The write-ahead log. SQLite's automatic checkpoint can't reset the log while readers and writers overlap, so a phone's
  first backfill grew it to gigabytes, and it stayed that size. Above ``db.WAL_LIMIT`` it's truncated here.
* Damage. ``PRAGMA quick_check`` takes about a second per GB on an SSD (measured on a 1 GB file; a spinning disk or
  a cold cache is many times that), so it runs before serving only on small files, otherwise a little after start-up,
  and then daily. It reads pages, not the indexes against their tables: ``integrity_check`` took 30 times longer.
* Statistics, for the query planner: ``PRAGMA optimize`` every few hours.
* Deleted data. Connections overwrite what they delete (``secure_delete``), but the log still holds the old pages until
  it's emptied and the file stays its old size, so soon after a permanent delete ``reclaim`` does both, a little at a time.

``health()`` is what /health reports, and ``problems()`` what owners are told about in Alerts.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.core import config, db, security, settings
from app.core.sqlite import sqlite3

log = logging.getLogger("syntropy.maintenance")

TICK_SECONDS = 5.0
INTEGRITY_EVERY = 24 * 3600
OPTIMIZE_EVERY = 6 * 3600
SMALL_FILE = 256 * 1024 * 1024
RECLAIM_STEP_PAGES = 2048       # 8 MB at a time: a few milliseconds of holding the write lock
RECLAIM_TRIES = 60              # ticks: five minutes of a busy file, then the next delete or the shutdown empties the log
QUIET_HOURS = range(1, 5)       # when a large file written by an older version gets its one rewrite, unprompted
LARGE_FILE_DELAY = 120          # not while the first pages are loading
MIN_FREE_DISK = 1024 * 1024     # SQLite can't commit without room
LOW_DISK = 1024 ** 3            # worth saying something while there's still room to act
_INTEGRITY_KEY = "db.integrity"

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_checking: Optional[sqlite3.Connection] = None
_damage: dict[str, str] = {}     # by file, for when the result can't be saved in the damaged file itself
_last_reported: Optional[str] = None
# For whoever asks from another device, before signing in: what's wrong can name folders and an account on this computer.
FROM_ELSEWHERE = ("Syntropy Health can't do its job right now because of a problem on the computer it runs on. To see what's "
                  "wrong, open it on that computer or look at its log.")


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
        conn = db.open_connection(path, timeout=5.0, check_same_thread=False)
        _checking = conn
        rows = [r[0] for r in conn.execute("PRAGMA quick_check(5)").fetchall()]
        detail = None if rows == ["ok"] else "; ".join(rows)
    except sqlite3.DatabaseError as exc:
        if not db.is_damage(exc):       # busy, stopped, or the disk's fault: the check didn't get to say anything
            log.info("Couldn't check the database for damage right now: %s", exc)
            return integrity(path) or {"ok": None, "checked_at": None, "detail": None}
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


def forget_damage(path: Optional[Path] = None) -> None:
    """The file it was noticed in has been replaced."""
    _damage.pop(str(path or config.db_path()), None)


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
    return (not last or not last.get("ok") or not last.get("checked_at")
            or time.time() - last["checked_at"] >= INTEGRITY_EVERY)


def check_at_start_up(path: Optional[Path] = None) -> None:
    """Only a small file, where it takes no time; the background thread gets to a large one. Until it does, damage it
    found before is still damage."""
    path = path or config.db_path()
    if _size(path) <= SMALL_FILE:
        check_integrity(path)
        return
    last = integrity(path)
    if last and last.get("ok") is False:
        note_damage(last.get("detail") or "found earlier", path)


# ---------------------------------------------------------------------------
# Deleted data
# ---------------------------------------------------------------------------

def _rewrite(path: Path, conn: sqlite3.Connection) -> bool:
    """The one long job: a file from before this version may hold deleted text in space freed then, which only a rewrite
    clears, and can't give space back a little at a time until it has had one. It holds the write lock throughout, so it
    happens once per database. It needs room for the new file and for the log. Its working copy goes on disk rather than
    in memory (where the rest of the app keeps temporary data): it's as large as the database, and SQLCipher encrypts this
    one with the database's key."""
    global _checking
    size = _size(path)
    free = shutil.disk_usage(path.parent).free
    if free < 2 * size + 64 * 1024 * 1024:
        log.warning("Not enough disk space to compact the database at %s (%d MB free): trying again later", path, free // 1048576)
        return False
    log.info("Compacting the database at %s (%d MB); this happens once", path, size // 1048576)
    _checking = conn
    try:
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("PRAGMA temp_store = FILE")
        conn.execute("VACUUM")
    finally:
        _checking = None
    return True


def _merge_search_index(conn: sqlite3.Connection) -> None:
    """The search index keeps a deleted record's words in its old blocks until they're merged, and neither VACUUM nor a
    backup's VACUUM INTO rewrites those. If the file is busy, the next delete merges them."""
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'record_search_fts'").fetchone():
            conn.execute("INSERT INTO record_search_fts(record_search_fts) VALUES('optimize')")
    except sqlite3.Error as exc:
        log.info("Couldn't merge the search index yet: %s", exc)


def reclaim(path: Optional[Path] = None, *, step_pages: int = RECLAIM_STEP_PAGES) -> dict[str, Any]:
    """Empties the log and gives free space back to the disk, in short transactions between other writes. Nothing here
    raises: if the file is busy, the next delete or start tries again."""
    path = path or config.db_path()
    out: dict[str, Any] = {"checkpointed": False, "vacuumed": False, "freed_pages": 0}
    conn = None
    try:
        out["checkpointed"] = db.checkpoint(path)
        conn = db.open_connection(path, timeout=2.0, check_same_thread=False)
        conn.execute("PRAGMA secure_delete = ON")
        _merge_search_index(conn)
        if conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
            if _rewrite(path, conn):
                out["vacuumed"] = True
                out["checkpointed"] = db.checkpoint(path, wait_seconds=10.0)
            return out
        conn.execute("PRAGMA busy_timeout = 2000")
        while not _stop.is_set() and (free := conn.execute("PRAGMA freelist_count").fetchone()[0]):
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(f"PRAGMA incremental_vacuum({int(step_pages)})").fetchall()
            finally:
                conn.execute("COMMIT")
            out["freed_pages"] += min(free, step_pages)
            db.checkpoint_if_large(path)
            time.sleep(0.02)      # a writer waiting on SQLite's busy handler needs to find the lock free
        out["checkpointed"] = db.checkpoint(path)
    except sqlite3.Error as exc:
        log.info("Couldn't reclaim space in the database right now: %s", exc)
    finally:
        if conn:
            conn.close()
    return out


def _rewrite_due(path: Path) -> bool:
    """A file an older version made: straight away when small, and at night when large, unless someone deletes something
    first (they've just asked for it, so reclaim doesn't wait)."""
    if _size(path) > SMALL_FILE and time.localtime().tm_hour not in QUIET_HOURS:
        return False
    conn = db.open_connection(path, timeout=1.0)
    try:
        return conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# What's reported
# ---------------------------------------------------------------------------

def health(path: Optional[Path] = None) -> dict[str, Any]:
    """For /health: fast, and honest about whether this database can be read and written. ``detail`` says what's wrong;
    a long write in progress isn't wrong."""
    out: dict[str, Any] = {"journal_mode": None, "database": "ok", "schema_version": None, "detail": None}
    try:
        path = path or config.db_path()
        db.ensure_migrated(path)
        state = db.probe(path)
        out.update(journal_mode=state["journal_mode"], schema_version=state["schema_version"], database=state["database"])
    except db.StartupProblem as exc:
        return {**out, "database": exc.kind, "detail": str(exc)}
    except sqlite3.DatabaseError as exc:
        if db.is_damage(exc):
            return {**out, "database": "damaged", "detail": _damage_text(path, str(exc))}
        return {**out, "database": "unavailable", "detail": db.unavailable_text(exc, path)}
    except OSError as exc:
        return {**out, "database": "unavailable", "detail": f"Syntropy Health can't use its data folder: {exc}. Check that "
                                                            "the disk is connected and has room, and that this account may "
                                                            "write to that folder."}
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


def report(detail: Optional[str]) -> None:
    """A health check repeats every 30 seconds. Each new kind of trouble goes in the log once."""
    global _last_reported
    if detail and detail != _last_reported:
        log.error("Health check failed: %s", detail)
    _last_reported = detail


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
    try:
        if shutil.disk_usage(path.parent).free < LOW_DISK:
            out.append({"id": "disk-low", "title": "The disk is nearly full",
                        "text": "Less than 1 GB is free on the disk that holds your data. When it runs out, phones can't sync and nothing new "
                                "can be saved, though what's stored stays safe. Free up some space."})
    except OSError:
        pass
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
    try:
        conn = db.open_connection(path, timeout=2.0)
    except sqlite3.Error as exc:
        log.info("Skipped updating the database's statistics: %s", exc)
        return
    try:
        conn.execute("PRAGMA analysis_limit = 400")      # a sample, so it takes milliseconds on any size of table
        conn.execute("PRAGMA optimize = 0x10002")        # every table, not only those this connection has read
    except sqlite3.Error as exc:
        log.info("Skipped updating the database's statistics: %s", exc)
    finally:
        conn.close()


def _run(path: Path) -> None:
    from app.core import encryption

    started = time.monotonic()
    next_check = time.monotonic() + (0 if _size(path) <= SMALL_FILE else LARGE_FILE_DELAY)
    next_optimize = time.monotonic() + 300
    next_rewrite = time.monotonic() + 300
    next_finish = time.monotonic()
    tries = 0
    while not _stop.wait(TICK_SECONDS):
        try:
            db.checkpoint_if_large(path)
            if db.take_deleted():
                tries = RECLAIM_TRIES
            if tries:
                tries = 0 if (reclaim(path) or {}).get("checkpointed") else tries - 1
            now = time.monotonic()
            if now >= next_finish:
                next_finish = now + 60
                if encryption.finish_due(path, now - started):
                    encryption.finish_up(path)
            if now >= next_rewrite:
                next_rewrite = now + 3600
                if _rewrite_due(path):
                    reclaim(path)
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
