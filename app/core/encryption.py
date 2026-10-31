"""
Encrypting the database that's already here, on the first start of a version that can, and looking after the files around it.
``.github/SECURITY-MODEL.md`` says what this protects and what it can't.

The way this can go wrong that matters most is someone left without their data, so:

* Before the rename that puts the encrypted file in place, the original hasn't been touched. A crash or power cut just leaves
  a temporary file, which the next start removes. After the rename, the encrypted file is complete and has been read back.
* If anything isn't right (no room, another program using the file, a check that doesn't match), the database stays as it was
  and the app runs on it unencrypted, says so in Settings, and tries again later.
* The key is saved, and read back from where it was saved, before anything is encrypted with it.
* An update that fails goes back to the previous version, which can't read an encrypted database. So the original file stays,
  as that version's way back, until this version has shown it runs: ``finish_up``, ten minutes later. That wait is the one
  time an unencrypted copy is kept on purpose.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app.core import config, db, keystore, security
from app.core.sqlite import HAS_ENCRYPTION, sqlite3

log = logging.getLogger("syntropy.encryption")

TEMP_SUFFIX = ".encrypting"
ATTEMPT_FILE = "encryption-attempt.json"
RETRY_AFTER_FAILURE = 24 * 3600     # it can take minutes, so not at every start, unless it was only put off
KEEP_ORIGINAL_SECONDS = 600         # longer than the update scripts wait to hear a new version is running
RUNNING_BEFORE_FINISHING = 60
_ROOM_MARGIN = 64 * 1024 * 1024
_PAGE_OVERHEAD = 1.03               # SQLCipher keeps 80 bytes of each 4,096-byte page for itself
_SAMPLE_ROWS = 256

_progress: dict[str, Any] = {}
_progress_lock = threading.Lock()
_postponed: dict[str, str] = {}     # by database: why it isn't encrypted yet


def _step(name: str) -> None:
    """Named moments in the conversion, which the tests stop the process at."""


class _NotNow(Exception):
    """Why this can't happen today, for Settings. Nothing has changed."""


class _DidntMatch(Exception):
    """The encrypted copy isn't what it should be."""


def progress() -> Optional[dict[str, Any]]:
    """{"stage", "percent"} while a database is being encrypted, for the desktop app's start page."""
    with _progress_lock:
        return dict(_progress) if _progress else None


def _report(stage: str, percent: Optional[int] = None) -> None:
    with _progress_lock:
        _progress.update(stage=stage, percent=percent)


def _done_reporting() -> None:
    with _progress_lock:
        _progress.clear()


def enabled() -> bool:
    return db.encrypt_new_databases()


# ---------------------------------------------------------------------------
# Keeping files private
# ---------------------------------------------------------------------------

def _chmod(target: Path, most: int = 0o777) -> None:
    """Nothing for anyone but this account, whatever else the file's mode says: a program among what's kept stays one."""
    try:
        current = stat.S_IMODE(target.lstat().st_mode)
        wanted = current & 0o700 & most
        if not target.is_symlink() and current != wanted:
            target.chmod(wanted)
    except OSError as exc:        # e.g. a folder mounted in Docker that belongs to someone else
        log.warning("Couldn't make %s private: %s", target, exc)


def _windows_acl(folder: Path) -> None:
    """On Windows a folder in the profile is already private. One chosen elsewhere (C:\\Syntropy, or a second drive) inherits
    "Users can read" from its parent, so it gets its own list: this account, SYSTEM (which backup software runs as) and
    administrators. SIDs, because the names of those groups depend on the language of Windows."""
    profile = Path(os.environ.get("USERPROFILE") or "")
    try:
        if profile.parts and folder.resolve().is_relative_to(profile.resolve()):
            return
        who = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=30, check=True)
        sid = who.stdout.strip().split(",")[-1].strip('"')
        subprocess.run(["icacls", str(folder), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
                        "*S-1-5-32-544:(OI)(CI)F", "/T", "/C", "/Q"], capture_output=True, timeout=120, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.warning("Couldn't limit who can read %s: %s", folder, exc)


def make_private(path: Path) -> None:
    """The database and, when it's in the usual data folder, that folder and what's in it: logs, keys and the copies kept of
    the database. SQLite gives the -wal and -shm files the database's own permissions. Files are made private as they're
    created; this covers what an earlier version, or a restore done by hand, left open. ``updates/`` holds an installer on its
    way, which is nobody's health data and is run from there. For Windows, see at_start_up."""
    if os.name == "nt":
        return
    data = config.data_dir()
    _chmod(path, 0o600)
    if path.parent != data:
        return
    _chmod(data)
    try:
        for entry in data.iterdir():
            _chmod(entry)
            if entry.is_dir() and not entry.is_symlink() and entry.name != "updates":
                for inner, folders, files in os.walk(entry):
                    for name in (*folders, *files):
                        _chmod(Path(inner) / name)
    except OSError as exc:
        log.warning("Couldn't check who can read %s: %s", data, exc)


# ---------------------------------------------------------------------------
# Making sure things reach the disk, and go from it
# ---------------------------------------------------------------------------

def _durable(path: Path) -> None:
    """What os.fsync doesn't promise on a Mac, where only F_FULLFSYNC reaches the disk's platters or flash."""
    with open(path, "rb+") as fh:
        os.fsync(fh.fileno())
        if sys.platform == "darwin":
            import fcntl
            fcntl.fcntl(fh.fileno(), fcntl.F_FULLFSYNC)


def _sync_folder(folder: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def destroy(path: Path) -> None:
    """As far as a program can. Overwriting first helps on Windows and Linux with a disk that writes in place. On a Mac it
    would only use more space (APFS writes elsewhere, and the old blocks stay), and on an SSD the old blocks may survive
    either way. That's why the model still asks for FileVault or BitLocker."""
    if sys.platform != "darwin":
        try:
            size = path.stat().st_size
            with open(path, "rb+") as fh:
                zeros = bytes(1024 * 1024)
                for _ in range(0, size, len(zeros)):
                    fh.write(zeros)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            log.warning("Couldn't overwrite %s before removing it: %s", path, exc)
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Copying into an encrypted file
# ---------------------------------------------------------------------------

def _private_empty(path: Path) -> None:
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600))


def _export(source: sqlite3.Connection, target: Path, key: Optional[str], *, version: int, on_progress: Optional[Callable[[int], None]] = None,
            source_bytes: int = 0) -> None:
    """Writes everything in ``source``, which must not be in a transaction, to ``target``. SQLCipher's export copies the schema
    and rows but not these settings, and leaves free space behind, so what was deleted long ago doesn't come along. Without a key,
    the copy is ordinary SQLite."""
    _private_empty(target)
    source.execute(f"""ATTACH DATABASE ? AS converted KEY "{key or ''}" """, (str(target),))
    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(1.0):
            try:
                if on_progress and source_bytes:
                    on_progress(min(99, int(target.stat().st_size * 100 / (source_bytes * _PAGE_OVERHEAD))))
            except OSError:
                pass

    watcher = threading.Thread(target=watch, daemon=True, name="encryption-progress")
    watcher.start()
    try:
        if key:
            source.execute(f"PRAGMA converted.cipher_compatibility = {db.CIPHER_COMPATIBILITY}")
        source.execute("PRAGMA converted.auto_vacuum = INCREMENTAL")
        # The file is thrown away if this is interrupted, so a journal for it would only slow this down.
        source.execute("PRAGMA converted.journal_mode = OFF")
        source.execute("PRAGMA converted.synchronous = OFF")
        source.execute("SELECT sqlcipher_export('converted')").fetchall()
        source.execute(f"PRAGMA converted.user_version = {int(version)}")
    finally:
        stop.set()
        watcher.join(5)
        source.execute("DETACH DATABASE converted")
    Path(f"{target}-journal").unlink(missing_ok=True)
    _durable(target)


def _tables(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every table holding rows of its own, including the search index's internal ones."""
    rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND rootpage > 0").fetchall()
    return [{"name": r[0], "rowid": "WITHOUT ROWID" not in (r[1] or "").upper()} for r in rows]


def _sample_rows(conn: sqlite3.Connection, table: dict[str, Any]) -> list[tuple]:
    """The same rows from each side: evenly spaced through the table, so a copy that went wrong partway shows up. This reads
    values back through the cipher, and the page checks and row counts cover the rest of the file."""
    name = table["name"].replace('"', '""')
    if not table["rowid"]:
        return [tuple(r) for r in conn.execute(f'SELECT * FROM "{name}" LIMIT {_SAMPLE_ROWS}')]
    low, high = conn.execute(f'SELECT MIN(rowid), MAX(rowid) FROM "{name}"').fetchone()
    if low is None:
        return []
    out, seen = [], set()
    for i in range(_SAMPLE_ROWS):
        row = conn.execute(f'SELECT rowid, * FROM "{name}" WHERE rowid >= ? ORDER BY rowid LIMIT 1', (low + (high - low) * i // (_SAMPLE_ROWS - 1),)).fetchone()
        if row and row[0] not in seen:
            seen.add(row[0])
            out.append(tuple(row))
    return out


def _verify(source: sqlite3.Connection, copy_path: Path, key: Optional[str], version: int) -> None:
    """Raises _DidntMatch unless the copy opens with this key (and not without), every page checks out, and it holds the same
    tables, the same number of rows in each, and the same values in a sample of them."""
    if key and db.opens(copy_path, None):
        raise _DidntMatch("the copy opens without a key")
    conn = sqlite3.connect(str(copy_path))
    try:
        if key:
            db.use_key(conn, key)
            damaged = [r[0] for r in conn.execute("PRAGMA cipher_integrity_check")]
            if damaged:
                raise _DidntMatch(f"pages don't check out: {damaged[0]}")
        found = [r[0] for r in conn.execute("PRAGMA quick_check(5)")]
        if found != ["ok"]:
            raise _DidntMatch(f"it isn't consistent: {found[0]}")
        if conn.execute("PRAGMA user_version").fetchone()[0] != version:
            raise _DidntMatch("it has a different version")
        schema = "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name != 'sqlite_sequence' ORDER BY type, name"
        if [tuple(r) for r in source.execute(schema)] != [tuple(r) for r in conn.execute(schema)]:
            raise _DidntMatch("its tables are different")
        for table in _tables(source):
            name = table["name"].replace('"', '""')
            count = f'SELECT COUNT(*) FROM "{name}"'
            if source.execute(count).fetchone()[0] != conn.execute(count).fetchone()[0]:
                raise _DidntMatch(f"{table['name']} has a different number of rows")
            if _sample_rows(source, table) != _sample_rows(conn, table):
                raise _DidntMatch(f"{table['name']} has different rows")
    finally:
        conn.close()
        for extra in ("-wal", "-shm"):
            Path(f"{copy_path}{extra}").unlink(missing_ok=True)


def _leftovers(path: Path) -> None:
    for extra in (TEMP_SUFFIX, f"{TEMP_SUFFIX}-wal", f"{TEMP_SUFFIX}-shm", f"{TEMP_SUFFIX}-journal"):
        Path(f"{path}{extra}").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The database in use
# ---------------------------------------------------------------------------

def _room_for_copy(path: Path) -> None:
    size = path.stat().st_size
    wal = Path(f"{path}-wal")
    needed = int((size + (wal.stat().st_size if wal.exists() else 0)) * _PAGE_OVERHEAD) + _ROOM_MARGIN
    free = shutil.disk_usage(path.parent).free
    if free < needed:
        raise _NotNow(f"There isn't enough free disk space to encrypt your records yet: it takes about {needed // 1048576:,} MB, and "
                      f"{free // 1048576:,} MB is free. Free up some space and start Syntropy Health again.")


def _take_for_ourselves(path: Path) -> sqlite3.Connection:
    """In this mode the connection keeps the lock until it's closed, so nothing can write to the file while it's copied. If
    anything else has the file open (the server elsewhere, the command line, an AI app's MCP server), that fails, and it's
    best not to pull a file out from under them."""
    conn = sqlite3.connect(str(path), timeout=2.0, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout = 2000")
        conn.execute("PRAGMA locking_mode = EXCLUSIVE")
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("COMMIT")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return conn
    except sqlite3.OperationalError as exc:
        conn.close()
        if db.is_busy(exc) or "locked" in str(exc):
            raise _NotNow("Another program has the database open, such as an AI app using Syntropy Health. Your records will be "
                          "encrypted the next time Syntropy Health starts with that program closed.") from exc
        raise


def _attempt_file() -> Path:
    return config.data_dir() / ATTEMPT_FILE


REQUEST_FILE = "encrypt-at-next-start"
ASK_FIRST = "Records already here stay as they are until you choose to encrypt them, so you can make a backup first."


def _request_file() -> Path:
    return config.data_dir() / REQUEST_FILE


def asked_for() -> bool:
    return _request_file().exists()


def ask_for_encryption(on: bool = True) -> None:
    """The owner's choice, carried out at the next start. A new database is encrypted from the beginning without asking, but
    converting years of someone's records is theirs to time: the unencrypted original is only kept for minutes afterwards."""
    if on:
        _request_file().touch(mode=0o600)
    else:
        _request_file().unlink(missing_ok=True)


def _chosen() -> bool:
    return asked_for() or bool(config.env("SYNTROPY_DATABASE_ENCRYPTION"))


def _failed_recently() -> Optional[str]:
    try:
        attempt = json.loads(_attempt_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    same_version = attempt.get("version") == config.APP_VERSION
    return attempt.get("reason") if same_version and time.time() - float(attempt.get("at") or 0) < RETRY_AFTER_FAILURE else None


def _note_failure(reason: str) -> None:
    try:
        _attempt_file().write_text(json.dumps({"at": time.time(), "version": config.APP_VERSION, "reason": reason}), encoding="utf-8")
    except OSError:
        pass


def _keep_original(path: Path, version: int) -> Path:
    """Another name for the unencrypted file, ready for before it loses its own: the old version's way back. Its time is
    now, because going back looks for a copy made since the update began."""
    folder = path.parent / db.PRE_UPGRADE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    _chmod(folder, 0o700)
    db.same_owner(folder, path)
    kept = folder / f"syntropy-v{version}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.db"
    kept.unlink(missing_ok=True)
    try:
        os.link(path, kept)
    except OSError:     # a drive without hard links
        shutil.copy2(path, kept)
    now = time.time()
    os.utime(kept, (now, now))
    _sync_folder(folder)
    db._just_copied[str(path)] = (version, kept)
    return kept


def _encrypt_in_place(path: Path) -> None:
    key = security.database_key()
    # Finding out whether a key was needed may have read the database, and a connection kept for reuse holds it open.
    db.close_readers()
    _step("key-ready")
    if not db.opens(path, None):
        raise _NotNow("The database can't be read, so it hasn't been encrypted.")
    _room_for_copy(path)
    temp = Path(f"{path}{TEMP_SUFFIX}")
    source = _take_for_ourselves(path)
    try:
        _step("locked")
        _leftovers(path)        # only now that nothing else can be in the middle of this
        size = path.stat().st_size
        version = source.execute("PRAGMA user_version").fetchone()[0]
        _report("Encrypting your records", 0)
        started = time.monotonic()
        log.warning("Encrypting the database at %s (%d MB). This happens once.", path, size // 1048576)
        _step("before-export")
        _export(source, temp, key, version=version, source_bytes=size, on_progress=lambda p: _report("Encrypting your records", p))
        _step("exported")
        _report("Checking the encrypted copy", 99)
        _verify(source, temp, key, version)
        _step("verified")
        log.warning("Encrypted and checked the database in %.0f s", time.monotonic() - started)
    except BaseException:
        source.close()
        _leftovers(path)
        raise
    source.close()      # the last connection to close also removes the log, which the checkpoint emptied
    _step("original-closed")
    kept = _keep_original(path, version)
    _step("original-kept")
    try:
        for extra in ("-wal", "-shm"):      # a log beside the new file would be applied to it
            Path(f"{path}{extra}").unlink(missing_ok=True)
        db.same_owner(temp, path)
        _step("before-swap")
        os.replace(temp, path)
    except OSError:
        kept.unlink(missing_ok=True)
        _leftovers(path)
        raise
    _sync_folder(path.parent)
    _step("swapped")
    log.warning("Your records are now encrypted. The unencrypted original stays at %s for a few minutes, in case this version "
                "has to be undone.", kept)
    _attempt_file().unlink(missing_ok=True)


def at_start_up() -> None:
    """Before the database is opened for anything else, by the server only: the command line and the MCP server work with the
    database as they find it, and aren't a reason to wait minutes. Whatever happens here, the database still works."""
    path = config.db_path()
    db.close_readers()
    _postponed.pop(str(path), None)
    if os.name == "nt" and path.parent == config.data_dir():
        _windows_acl(path.parent)       # here only: it takes a moment, and the MCP server starts many times
    try:
        _keep_key_in_store()
        if not path.exists() or not path.stat().st_size or db.is_encrypted(path) or not enabled():
            if not db.is_encrypted(path) and path.exists() and path.stat().st_size:
                _postponed[str(path)] = "Encryption is turned off with SYNTROPY_DATABASE_ENCRYPTION."
            return
        if not _chosen():
            _postponed[str(path)] = ASK_FIRST
            return
        if earlier := _failed_recently():
            _postponed[str(path)] = earlier
            return
        _encrypt_in_place(path)
        _request_file().unlink(missing_ok=True)
    except _NotNow as exc:
        log.warning("Not encrypting the database yet: %s", exc)
        _postponed[str(path)] = str(exc)
    except (_DidntMatch, sqlite3.Error, OSError, db.StartupProblem) as exc:
        log.exception("Couldn't encrypt the database, so it stays as it was")
        reason = f"Encrypting your records didn't work, so they stay as they were, unencrypted and safe: {exc}. It will be tried again tomorrow."
        _postponed[str(path)] = reason
        _note_failure(reason)
    finally:
        _done_reporting()
        db.reset_migration_cache()


def _keep_key_in_store() -> None:
    """A key from before this version, or from a backup, is in secret.key. Where there's somewhere better it's put there, and
    read back, now. The file stays until finish_up: if this version has to be undone, the old one looks for it."""
    if security.explicit_source() or keystore.wanted_store() == keystore.FILE:
        return
    file = config.data_dir() / keystore.KEY_FILE
    if not file.exists():
        return
    key = security.key_from_file(file.read_bytes())
    if key is None:
        return
    try:
        if keystore.store_of_record() in keystore.PROTECTED_STORES and keystore.from_store() == key:
            return
    except keystore.Unreachable:
        return
    keystore.save_to_store(key)


# ---------------------------------------------------------------------------
# Afterwards: what's left unencrypted
# ---------------------------------------------------------------------------

def plain_copies(path: Optional[Path] = None) -> list[Path]:
    """Unencrypted copies of the database the app has kept beside it, from an earlier version or from just now."""
    path = path or config.db_path()
    parent = path.parent
    places = [parent / db.PRE_UPGRADE_DIR, parent / db.PRE_RESTORE_DIR, *parent.glob("set-aside-*")]
    found = []
    for place in places:
        for f in place.rglob("*.db") if place.is_dir() else []:
            if f.is_file() and f.stat().st_size and not db.is_encrypted(f):
                found.append(f)
    return found


def _convert_copy(copy: Path, key: str) -> None:
    """One kept copy, encrypted where it lies. It stays in place if anything goes wrong, or if there's no room."""
    temp = copy.with_name(copy.name + TEMP_SUFFIX)
    source = sqlite3.connect(str(copy), isolation_level=None, timeout=2.0)
    try:
        version = source.execute("PRAGMA user_version").fetchone()[0]
        _export(source, temp, key, version=version)
        _verify(source, temp, key, version)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    finally:
        source.close()
    aside = copy.with_name(copy.name + ".plain")
    os.link(copy, aside) if _can_link(copy, aside) else shutil.copy2(copy, aside)
    try:
        os.replace(temp, copy)
    finally:
        destroy(aside)
    for extra in ("-wal", "-shm"):
        Path(f"{copy}{extra}").unlink(missing_ok=True)


def _can_link(source: Path, target: Path) -> bool:
    try:
        probe = target.with_name(target.name + ".probe")
        os.link(source, probe)
        probe.unlink()
        return True
    except OSError:
        return False


def finish_up(path: Optional[Path] = None) -> dict[str, int]:
    """Once this version has shown it runs. Nothing here touches the database in use, so nothing waits for it. Each copy is
    encrypted where it lies; one that can't be (no room, or itself damaged) is removed if it's only the way back from this
    change, since the database in use now holds everything it does, and otherwise stays, and Settings says so."""
    path = path or config.db_path()
    out = {"converted": 0, "removed": 0, "left": 0}
    if not db.is_encrypted(path):
        return out
    key = security.database_key()
    for copy in plain_copies(path):
        try:
            if shutil.disk_usage(copy.parent).free < copy.stat().st_size * _PAGE_OVERHEAD + _ROOM_MARGIN:
                raise OSError("not enough free disk space")
            _convert_copy(copy, key)
            out["converted"] += 1
            log.info("Encrypted the copy at %s", copy)
        except (sqlite3.Error, OSError, _DidntMatch) as exc:
            if copy.parent.name == db.PRE_UPGRADE_DIR:
                destroy(copy)
                out["removed"] += 1
                log.warning("Removed the unencrypted copy at %s, since it couldn't be encrypted: %s", copy, exc)
            else:
                out["left"] += 1
                log.warning("Couldn't encrypt the copy at %s: %s", copy, exc)
    _retire_key_file()
    return out


def _retire_key_file() -> None:
    """Once the Keychain or DPAPI has the key, the file beside the data is only a weakness."""
    file = config.data_dir() / keystore.KEY_FILE
    if not file.exists() or security.explicit_source():
        return
    try:
        if keystore.store_of_record() in keystore.PROTECTED_STORES and keystore.from_store() == security.key_from_file(file.read_bytes()):
            destroy(file)
            log.info("The encryption key is now only in the system's key store")
    except (keystore.Unreachable, OSError) as exc:
        log.warning("Kept the encryption key file for now: %s", exc)


def finish_due(path: Path, running_for: float) -> bool:
    """Plain copies wait until this run has lasted a little and the newest of them is old enough."""
    if running_for < RUNNING_BEFORE_FINISHING or not db.is_encrypted(path):
        return False
    try:
        newest = max((c.stat().st_mtime for c in plain_copies(path)), default=0.0)
    except OSError:
        return False
    file = config.data_dir() / keystore.KEY_FILE
    if not newest and not (file.exists() and keystore.store_of_record() in keystore.PROTECTED_STORES and not security.explicit_source()):
        return False
    return time.time() - newest >= KEEP_ORIGINAL_SECONDS


# ---------------------------------------------------------------------------
# Out of encryption
# ---------------------------------------------------------------------------

def plain_copy(dest: Path, path: Optional[Path] = None) -> None:
    """An ordinary SQLite copy of the database, for a backup someone asked to have unencrypted, or for leaving. Taken in a
    read transaction, so the server can carry on."""
    path = path or config.db_path()
    conn = db.open_connection(path)
    try:
        conn.execute("BEGIN")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute("COMMIT")
        _export(conn, dest, None, version=version)
    finally:
        conn.close()


def encrypt_copy(database: Path, key: str) -> None:
    """A database file on its way in (from an older backup, say), before it takes the place of the one in use."""
    temp = database.with_name(database.name + TEMP_SUFFIX)
    source = sqlite3.connect(str(database), isolation_level=None)
    try:
        version = source.execute("PRAGMA user_version").fetchone()[0]
        _export(source, temp, key, version=version)
        _verify(source, temp, key, version)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    finally:
        source.close()
    os.replace(temp, database)
    for extra in ("-wal", "-shm"):
        Path(f"{database}{extra}").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# What's true right now, for Settings and the command line
# ---------------------------------------------------------------------------

STORE_WORDS = {
    keystore.KEYCHAIN: "this Mac's Keychain, protected by your login password",
    keystore.DPAPI: "Windows' protected storage, tied to your Windows account on this PC",
    keystore.FILE: "a file beside your data",
    "setting": "the SYNTROPY_SECRET_KEY setting",
    "key-file": "a key file you chose (SYNTROPY_KEY_FILE)",
}


def key_kind() -> str:
    explicit = security.explicit_source()
    if explicit:
        return explicit
    store = keystore.store_of_record()
    in_file = (config.data_dir() / keystore.KEY_FILE).exists()
    return store if store in keystore.PROTECTED_STORES and not (in_file and not _store_holds_key()) else keystore.FILE


def _store_holds_key() -> bool:
    try:
        return bool(keystore.from_store())
    except keystore.Unreachable:
        return False


def status() -> dict[str, Any]:
    """In words someone can act on. ``plain_copies`` counts what's still unencrypted beside the database."""
    path = config.db_path()
    encrypted = db.is_encrypted(path)
    kind = key_kind()
    cipher = None
    if encrypted and HAS_ENCRYPTION:
        conn = db.open_connection(path)
        try:
            cipher = conn.execute("PRAGMA cipher_version").fetchone()[0]
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return {
        "encrypted": encrypted,
        "available": HAS_ENCRYPTION,
        "cipher": f"AES-256 (SQLCipher {cipher.split()[0]})" if cipher else None,
        "why_not": None if encrypted else (_postponed.get(str(path)) or ("This copy of Syntropy Health can't encrypt its database on this computer: "
                                                                         "the part that does it (sqlcipher3) isn't installed." if not HAS_ENCRYPTION else None)),
        "key_kind": kind,
        "key_where": STORE_WORDS.get(kind, kind),
        "key_beside_data": kind == keystore.FILE,
        "key_file": str(keystore.explicit_file() or config.data_dir() / keystore.KEY_FILE) if kind in (keystore.FILE, "key-file") else None,
        "password_recovery": {
            "applies": security.recovery_applies(),
            "turned_off": keystore.recovery_turned_off(),
            "accounts": list(keystore.recovery_entries()),
        },
        "plain_copies": [str(c) for c in plain_copies(path)] if encrypted else [],
    }
