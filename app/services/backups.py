"""
Backups and restoring them. One kind of backup, from Settings, the command line and the schedule alike: a zip holding
manifest.json, syntropy.db (a consistent copy taken while the server runs), secret.key and uploads, so it brings back
everything on any computer, sign-ins included. It's encrypted with a passphrase (``app.services.backup_file``) unless
someone explicitly chooses otherwise.

Restoring is two steps. ``check`` unpacks a file into a private folder and tests everything without touching anything
else; ``apply`` then swaps it in, keeps what it replaces in ``pre-restore/``, and puts that back if the result doesn't
start. Older backups still work: a zip without a manifest, and a bare database as Settings used to download (which needs
the key from elsewhere, and says so).
"""

from __future__ import annotations

import calendar
import errno
import json
import logging
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from app.core import config, db, maintenance, security, settings
from app.services import backup_file
from app.services.backup_file import BackupFileError, Damaged, SealKey

log = logging.getLogger("syntropy.backups")

FORMAT = 1
SEALED_SUFFIX = ".syntropy"
PLAIN_SUFFIX = ".zip"
NAME_PREFIX = "syntropy-backup-"
LAST_KEY = "backup.last"
SAFETY_COPIES_KEPT = 2
_WORK_PREFIX = ".backup-work-"
_DOWNLOAD_PREFIX, _RESTORE_PREFIX = db.WAITING_COPY_PREFIXES
_STAGED_LIFETIME = 3600
DOWNLOAD_GRACE = 300.0
WORK_LIFETIME = 6 * 3600
_MARGIN = 16 * 1024 * 1024
_SQLITE_MAGIC = b"SQLite format 3\0"
# Files on their way into an import, gone once it's processed: restored, nothing would ever finish them or clear them.
_TRANSIENT_UPLOAD = "upload_"

_lock = threading.Lock()
_staged: dict[str, "Staged"] = {}
_downloads: dict[str, dict[str, Any]] = {}


class BackupProblem(Exception):
    """Why a backup or restore can't go ahead, in words for the person."""


class NothingHere(BackupProblem):
    """Pointed at a folder with no records in it: an empty backup that looks like a real one is worse than an error."""


def validate_passphrase(passphrase: Optional[str]) -> str:
    if not passphrase or len(passphrase) < backup_file.MIN_PASSPHRASE:
        raise BackupProblem(f"Choose a passphrase of at least {backup_file.MIN_PASSPHRASE} characters.")
    return passphrase


def key_source() -> str:
    """Where this computer's encryption key lives: included in backups when it's the file."""
    if config.env("SYNTROPY_SECRET_KEY"):
        return "setting"
    return "file" if (config.data_dir() / "secret.key").exists() else "none"


def _private_folder(prefix: str) -> Path:
    data = config.data_dir()
    folder = Path(tempfile.mkdtemp(dir=data, prefix=prefix))       # 0700, on the disk that has room for a copy of the database
    db.same_owner(folder, data)
    return folder


def clean_leftovers() -> None:
    """What a crash left behind in the data folder may hold everything in the clear. Nothing in use is this old."""
    now = time.time()
    data = config.data_dir()
    for folder in data.iterdir():
        if not folder.is_dir():
            continue
        limit = WORK_LIFETIME if folder.name.startswith(_WORK_PREFIX) else (
            _STAGED_LIFETIME if folder.name.startswith((_DOWNLOAD_PREFIX, _RESTORE_PREFIX)) else None)
        try:
            if limit is not None and now - folder.stat().st_mtime > limit:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass
    with _lock:
        for token, item in list(_downloads.items()):
            if now - item["created_at"] > _STAGED_LIFETIME or not Path(item["path"]).exists():
                del _downloads[token]
        for staged_id, staged in list(_staged.items()):
            if now - staged.created_at > _STAGED_LIFETIME or not staged.folder.exists():
                del _staged[staged_id]


# ---------------------------------------------------------------------------
# What's in a database
# ---------------------------------------------------------------------------

def _count(conn: sqlite3.Connection, table: str) -> int:
    """An older backup may not have every table yet."""
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def _describe(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counting samples reads an index, about a second per million rows: fine for something this rare."""
    names = [r[0] for r in conn.execute("SELECT name FROM profiles ORDER BY is_default DESC, created_at")]
    try:
        accounts = [{"name": r[0], "has_password": bool(r[1]), "owner": r[2] == "owner"} for r in conn.execute(
            "SELECT p.name, a.password_hash, a.role FROM accounts a JOIN profiles p ON p.id = a.profile_id ORDER BY a.created_at")]
    except sqlite3.OperationalError:
        accounts = []
    setup = conn.execute("SELECT value FROM app_settings WHERE key = 'setup.completed'").fetchone()
    return {"people": names, "accounts": accounts, "setup_completed": bool(setup and json.loads(setup[0])),
            "counts": {"people": len(names), "sources": _count(conn, "connections"), "records": _count(conn, "clinical_records"),
                       "samples": _count(conn, "biometric_samples"), "workouts": _count(conn, "workouts"),
                       "journal": _count(conn, "health_events"), "chats": _count(conn, "chats")}}


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def holds_text(info: dict[str, Any]) -> str:
    counts = info["counts"]
    return ", ".join((_plural(counts["people"], "person") if counts["people"] == 1 else f"{counts['people']:,} people",
                      _plural(counts["records"], "record"), _plural(counts["samples"], "wearable sample")))


# ---------------------------------------------------------------------------
# Making one
# ---------------------------------------------------------------------------

def _room_for(folder: Path, size: int, what: str) -> None:
    free = shutil.disk_usage(folder).free
    if free < size + _MARGIN:
        raise BackupProblem(f"There isn't enough room in {folder} for {what}: it needs about {(size + _MARGIN) // 1048576:,} MB "
                            f"and {free // 1048576:,} MB is free.")


def _write_zip(out: Any, data: Path, snapshot: Path, manifest: dict[str, Any]) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.write(snapshot, "syntropy.db")
        if manifest["key"] == "included":
            z.write(data / "secret.key", "secret.key")
        uploads = data / "uploads"
        for f in sorted(uploads.rglob("*")) if uploads.exists() else []:
            if f.is_file() and not f.name.startswith(_TRANSIENT_UPLOAD):
                try:
                    z.write(f, (Path("uploads") / f.relative_to(uploads)).as_posix())
                except FileNotFoundError:
                    pass


def _sync_folder(folder: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create(folder: Path, *, passphrase: Optional[str] = None, seal_key: Optional[SealKey] = None, unencrypted: bool = False,
           made_by: str = "settings", progress: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
    """A whole backup in ``folder``. It appears under its final name only once it's complete and has been read back, so a
    backup that exists is one that was written in full. Raises BackupProblem, OSError or StartupProblem (damage)."""
    if not unencrypted and seal_key is None:
        seal_key = backup_file.new_seal_key(validate_passphrase(passphrase))
    path = config.db_path()
    data = config.data_dir()
    clean_leftovers()
    folder.mkdir(parents=True, exist_ok=True)
    work = _private_folder(_WORK_PREFIX)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    final = folder / f"{NAME_PREFIX}{stamp}{PLAIN_SUFFIX if unencrypted else SEALED_SUFFIX}"
    partial = final.with_name(final.name + ".partial")
    try:
        size = path.stat().st_size if path.exists() else 0
        _room_for(data, size, "a copy of the database")
        snapshot = work / "syntropy.db"
        if progress:
            progress("Copying the database…")
        db.snapshot(snapshot)
        conn = sqlite3.connect(snapshot)
        try:
            info = _describe(conn)
            schema = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        if not info["setup_completed"] and not info["counts"]["records"] and not info["counts"]["samples"]:
            raise NothingHere(f"There's nothing to back up in {data}: it hasn't been set up and holds no records. If your "
                              "data is somewhere else, say where with --data-dir.")
        source = key_source()
        created_at = time.time()
        manifest = {"format": FORMAT, "app_version": config.APP_VERSION, "schema_version": schema,
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created_at)), "made_by": made_by,
                    "key": {"file": "included", "setting": "setting", "none": "none"}[source], **info}
        if progress:
            progress("Writing the backup…")
        _room_for(folder, snapshot.stat().st_size, "the backup")
        # Private from the moment it exists: not created with the default permissions and tightened afterwards.
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            if unencrypted:
                _write_zip(out, data, snapshot, manifest)
            else:
                sealer = backup_file.Sealer(out, seal_key)      # type: ignore[arg-type]
                _write_zip(sealer, data, snapshot, manifest)
                sealer.close()
            out.flush()
            os.fsync(out.fileno())
        _read_back(partial, seal_key, unencrypted)
        os.replace(partial, final)
        _sync_folder(folder)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        if exc.errno == errno.ENOSPC:
            raise BackupProblem(f"The disk ran out of room while writing the backup to {folder}. Free up some space and try again.") from exc
        raise
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    result = {"path": str(final), "filename": final.name, "size_bytes": final.stat().st_size, "encrypted": not unencrypted,
              "created_at": created_at, "made_by": made_by, "key": manifest["key"], "holds": holds_text(info), "counts": info["counts"]}
    _remember(result)
    return result


def _read_back(path: Path, seal_key: Optional[SealKey], unencrypted: bool) -> None:
    if unencrypted:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
        if bad:
            raise BackupProblem(f"The backup didn't read back correctly ({bad}). The disk may be failing. Try another folder.")
        return
    try:
        with open(path, "rb") as fh:
            for _ in backup_file.unseal(fh, key=seal_key):
                pass
    except Damaged as exc:
        raise BackupProblem("The backup didn't read back correctly. The disk may be failing. Try another folder.") from exc


def _remember(result: dict[str, Any]) -> None:
    """A database too busy to note it doesn't undo the backup."""
    try:
        settings.set(LAST_KEY, {k: result[k] for k in ("created_at", "filename", "size_bytes", "encrypted", "made_by", "key")})
        with db.db() as conn:
            db.audit(conn, "scheduler" if result["made_by"] == "schedule" else "cli" if result["made_by"] == "command" else "user",
                     "export.backup", {"file": result["filename"], "encrypted": result["encrypted"]})
    except (sqlite3.Error, db.StartupProblem) as exc:
        log.warning("Couldn't note the backup that was just made: %s", exc)


def last() -> Optional[dict[str, Any]]:
    return settings.get(LAST_KEY)


# ---------------------------------------------------------------------------
# Downloading one (Settings)
# ---------------------------------------------------------------------------

def prepare_download(passphrase: Optional[str], unencrypted: bool) -> dict[str, Any]:
    """Built on disk, then fetched with a one-time address, so a large backup isn't held in memory by the browser, and
    the passphrase never goes in an address."""
    folder = _private_folder(_DOWNLOAD_PREFIX)
    try:
        result = create(folder, passphrase=passphrase, unencrypted=unencrypted)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    token = secrets.token_urlsafe(24)
    with _lock:
        _downloads[token] = {"path": result["path"], "filename": result["filename"], "created_at": time.time()}
    return {**{k: v for k, v in result.items() if k != "path"}, "download": f"/api/backups/download/{token}"}


def download(token: str) -> Optional[dict[str, Any]]:
    with _lock:
        item = _downloads.get(token)
    return item if item and Path(item["path"]).exists() else None


def finish_download(token: str) -> None:
    """Not gone at once: the Android app's web view asks for it and gives up on seeing a download, then the app fetches it
    itself, and a dropped connection looks the same as a finished one."""
    with _lock:
        item = _downloads.get(token)
        if not item or item.get("ending"):
            return
        item["ending"] = True
    timer = threading.Timer(DOWNLOAD_GRACE, _end_download, (token,))
    timer.daemon = True
    timer.start()


def _end_download(token: str) -> None:
    with _lock:
        item = _downloads.pop(token, None)
    if item:
        shutil.rmtree(Path(item["path"]).parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Checking one
# ---------------------------------------------------------------------------

@dataclass
class Staged:
    id: str
    folder: Path
    report: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    key_only: Optional[bytes] = None


def _copy_limited(source: Any, target: Path, limit: int) -> None:
    written = 0
    with open(target, "wb") as out:
        while chunk := source.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                raise Damaged("a file in it is larger than it says")
            out.write(chunk)


def _safe_member(name: str) -> bool:
    parts = name.split("/")
    return not ("\\" in name or ":" in name or ".." in parts or "" in parts)


def _unzip(archive: Path, folder: Path) -> dict[str, Any]:
    """Only what a backup holds, and only inside ``folder``. Reading each file to its end checks its checksum."""
    try:
        with zipfile.ZipFile(archive) as z:
            members = [m for m in z.infolist() if not m.is_dir()]
            names = {m.filename for m in members}
            if "syntropy.db" not in names:
                raise BackupFileError("This zip isn't a Syntropy Health backup: it has no syntropy.db in it.")
            _room_for(folder, sum(m.file_size for m in members), "unpacking this backup")
            for m in members:
                name = m.filename
                allowed = name in ("manifest.json", "syntropy.db", "secret.key") or (name.startswith("uploads/") and _safe_member(name))
                if not allowed:
                    raise BackupFileError(f"This zip isn't a Syntropy Health backup: it holds something unexpected ({name}).")
                target = folder / Path(*name.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as source:
                    _copy_limited(source, target, m.file_size)
            return _read_manifest(folder) if "manifest.json" in names else {}
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, zlib.error) as exc:
        raise Damaged(str(exc)) from exc
    except (RuntimeError, NotImplementedError) as exc:      # a zip with its own password, or an odd compression
        raise BackupFileError("This zip isn't a Syntropy Health backup: it can't be read as one.") from exc


def _read_manifest(folder: Path) -> dict[str, Any]:
    target = folder / "manifest.json"
    try:
        manifest = json.loads(target.read_text(encoding="utf-8"))
        newer = int(manifest.get("format") or 0) > FORMAT
    except (ValueError, TypeError, AttributeError) as exc:
        raise Damaged("its description can't be read") from exc
    if newer:
        raise BackupFileError("This backup was made by a newer version of Syntropy Health. Install the latest version from "
                              f"{db.RELEASES_URL} to restore it.")
    target.unlink()
    # Only what's shown or relied on, and only as what it should be: this came from a file.
    return {k: manifest[k] for k in ("created_at", "app_version", "key") if isinstance(manifest.get(k), str)} | {"format": FORMAT}


def _unpack(file: Path, folder: Path, passphrase: Optional[str]) -> tuple[str, bool, dict[str, Any]]:
    """Returns its kind (current, older-zip or database-only), whether it was encrypted, and its manifest."""
    with open(file, "rb") as fh:
        first = fh.read(32)
        fh.seek(0)
        if backup_file.is_sealed(first):
            if not passphrase:
                raise PassphraseNeeded()
            archive = folder / "archive.zip"
            with open(archive, "wb") as out:
                for chunk in backup_file.unseal(fh, passphrase):
                    out.write(chunk)
            manifest = _unzip(archive, folder)
            archive.unlink()
            return ("current" if manifest else "older-zip"), True, manifest
    if first.startswith(_SQLITE_MAGIC):
        _room_for(folder, file.stat().st_size, "unpacking this backup")
        shutil.copyfile(file, folder / "syntropy.db")
        return "database-only", False, {}
    if zipfile.is_zipfile(file):
        manifest = _unzip(file, folder)
        return ("current" if manifest else "older-zip"), False, manifest
    raise BackupFileError("This isn't a Syntropy Health backup. It should be a .syntropy file, or a .zip or .db from an older version.")


class PassphraseNeeded(BackupFileError):
    def __init__(self) -> None:
        super().__init__("This backup is encrypted. Enter the passphrase it was made with.")


def _test_database(path: Path) -> dict[str, Any]:
    conn = None
    try:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA trusted_schema = OFF")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not version or not {"profiles", "app_settings"} <= tables:
            raise BackupFileError("The database in this file isn't a Syntropy Health database.")
        db.refuse_if_newer(version, db.newest_schema())
        found = "; ".join(r[0] for r in conn.execute("PRAGMA integrity_check(5)").fetchall())
        if found != "ok":
            raise Damaged(found[:200])
        return {"schema_version": version, **_describe(conn)}
    except db.StartupProblem as exc:
        raise BackupFileError(str(exc)) from exc
    except sqlite3.DatabaseError as exc:
        if db.is_damage(exc):
            raise Damaged(str(exc)) from exc
        raise BackupFileError(f"The database in this file can't be read: {exc}") from exc
    except UnicodeDecodeError as exc:       # what should be a name in its own list of tables isn't text
        raise Damaged(str(exc)) from exc
    finally:
        if conn:
            conn.close()
        for extra in ("-wal", "-shm"):      # never to travel with it
            Path(f"{path}{extra}").unlink(missing_ok=True)


def _key_report(folder: Path, manifest: dict[str, Any], key_file: Optional[Path]) -> dict[str, Any]:
    """Will the restored data's passwords and sign-ins open? ``ok`` false means they won't, and restoring takes an explicit yes."""
    database = folder / "syntropy.db"
    staged_key = folder / "secret.key"
    if key_file:
        raw = key_file.read_bytes()
        if security.key_from_file(raw) is None:
            raise BackupFileError("That key file isn't valid: it should hold a 32-byte key.")
        staged_key.write_bytes(raw)
    in_backup = security.key_from_file(staged_key.read_bytes()) if staged_key.exists() else None
    if staged_key.exists() and in_backup is None:
        raise Damaged("the encryption key in it isn't valid")
    env = config.env("SYNTROPY_SECRET_KEY")
    here = None
    if env:
        here = security.key_from_setting(env)
    elif (config.data_dir() / "secret.key").exists():
        try:
            here = security.key_from_file((config.data_dir() / "secret.key").read_bytes())
        except OSError:
            here = None
    needed = security.opens(database, here) if here else None
    has_secrets = security.has_secrets(database)
    if not has_secrets:
        return {"status": "not-needed", "ok": True, "included": in_backup is not None,
                "message": "Nothing in this backup needs its encryption key."}
    if env:
        if needed:
            return {"status": "setting", "ok": True, "included": in_backup is not None,
                    "message": "The SYNTROPY_SECRET_KEY setting on this computer opens the passwords and sign-ins in this backup."}
        return {"status": "setting-differs", "ok": False, "included": in_backup is not None,
                "message": "This computer's SYNTROPY_SECRET_KEY setting takes priority and doesn't open the passwords and sign-ins "
                           "in this backup. Set it to the value used when the backup was made, or remove it"
                           + (" to use the key inside the backup" if in_backup else "") + ", then check again."}
    if in_backup is not None:
        if security.opens(database, in_backup):
            return {"status": "included", "ok": True, "included": True,
                    "message": "The encryption key is in this backup, so passwords and saved sign-ins come back too."}
        return {"status": "wrong-key", "ok": False, "included": True,
                "message": "The encryption key with this backup doesn't open the passwords and sign-ins in it."}
    if needed:
        return {"status": "here", "ok": True, "included": False,
                "message": "The encryption key already on this computer opens the passwords and sign-ins in this backup."}
    if manifest.get("key") == "setting":
        return {"status": "missing", "ok": False, "included": False,
                "message": "This backup was made with the SYNTROPY_SECRET_KEY setting, so its key isn't inside it. Set "
                           "SYNTROPY_SECRET_KEY to the same value on this computer, then check again."}
    return {"status": "missing", "ok": False, "included": False,
            "message": "This older backup holds only the database, not the encryption key (secret.key). Without that file nobody can "
                       "sign in with their old password and saved sign-ins are lost: everyone would set a new password with "
                       "`syntropy-health accounts reset-password NAME` after `syntropy-health key new`. If you still have the "
                       "secret.key file, add it and check again."}


def check(file: Path, passphrase: Optional[str] = None, key_file: Optional[Path] = None) -> Staged:
    """Unpacks and tests a backup, changing nothing. The result stays ready for ``apply`` until it's used, discarded or an
    hour has passed."""
    clean_leftovers()
    folder = _private_folder(_RESTORE_PREFIX)
    try:
        kind, encrypted, manifest = _unpack(file, folder, passphrase)
        info = _test_database(folder / "syntropy.db")
        key = _key_report(folder, manifest, key_file)
        uploads = sum(1 for f in (folder / "uploads").rglob("*") if f.is_file()) if (folder / "uploads").exists() else 0
        report = {
            "id": secrets.token_urlsafe(16), "kind": kind, "encrypted": encrypted, "file": file.name,
            "created_at": manifest.get("created_at"), "app_version": manifest.get("app_version"),
            "schema_version": info["schema_version"], "upgrade_needed": info["schema_version"] < db.newest_schema(),
            "people": info["people"], "accounts": info["accounts"], "counts": info["counts"], "uploads": uploads,
            "holds": holds_text(info), "key": key, "replaces": _what_is_here(), "sign_in": _sign_in_text(info, manifest),
        }
    except OSError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        if exc.errno == errno.ENOSPC:
            raise BackupProblem("The disk ran out of room while unpacking this backup. Free up some space and try again.") from exc
        raise
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    item = Staged(report["id"], folder, report)
    with _lock:
        _staged[item.id] = item
    return item


def _what_is_here() -> Optional[dict[str, Any]]:
    path = config.db_path()
    if not path.exists() or not path.stat().st_size:
        return None
    conn = None
    try:
        conn = db.open_connection(path, timeout=2.0)
        info = _describe(conn)
        return None if not info["setup_completed"] and not info["counts"]["records"] else {"holds": holds_text(info), **info}
    except sqlite3.Error:
        return {"holds": "a database that can't be read", "people": [], "accounts": [], "counts": {}}
    finally:
        if conn:
            conn.close()


def _local_date(created_at: Optional[str]) -> Optional[str]:
    """The manifest says UTC. In the evening in America that's already tomorrow, beside a report showing today."""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(calendar.timegm(time.strptime(created_at or "", "%Y-%m-%dT%H:%M:%SZ"))))
    except ValueError:
        return None


def _sign_in_text(info: dict[str, Any], manifest: dict[str, Any]) -> str:
    made = _local_date(manifest.get("created_at"))
    when = f" ({made})" if made else ""
    if not any(a["has_password"] for a in info["accounts"]):
        return "Nobody needed a password when this backup was made."
    return (f"After restoring, sign in with the password that was current when this backup was made{when}, not one you've set "
            "since. Each person uses their own.")


def staged(staged_id: str) -> Optional[Staged]:
    with _lock:
        item = _staged.get(staged_id)
    return item if item and item.folder.exists() else None


def discard(staged_id: str) -> None:
    with _lock:
        item = _staged.pop(staged_id, None)
    if item:
        shutil.rmtree(item.folder, ignore_errors=True)


# ---------------------------------------------------------------------------
# Applying one
# ---------------------------------------------------------------------------

def _link_or_copy(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _prune_safety_copies(folder: Path) -> None:
    copies = sorted((f for f in folder.iterdir() if f.is_dir()), key=lambda f: f.name)
    for old in copies[:-SAFETY_COPIES_KEPT]:
        shutil.rmtree(old, ignore_errors=True)


def _swap(item: Staged) -> Optional[Path]:
    """Puts the staged files in place. There's a database under the real name at every moment (a crash here must not leave
    the next start to make an empty one), and the old log is never left beside the new file: SQLite would replay it onto
    the wrong database, which is how unzipping a backup over a folder corrupted it. An OSError means nothing has changed."""
    data, path = config.data_dir(), config.db_path()
    siblings = [Path(f"{path}{extra}") for extra in ("-wal", "-shm")]
    key, uploads = data / "secret.key", data / "uploads"
    aside = None
    if path.exists() or key.exists() or uploads.exists():
        if path.exists():
            db.checkpoint(path)      # so the copy kept is complete by itself, where possible
        root = path.parent / db.PRE_RESTORE_DIR
        root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            root.chmod(0o700)
        db.same_owner(root, data)
        aside = Path(tempfile.mkdtemp(dir=root, prefix=time.strftime("%Y%m%d-%H%M%S-", time.gmtime())))
        db.same_owner(aside, data)
        for f in (path, *siblings):
            if f.exists():
                _link_or_copy(f, aside / f.name)
        if key.exists():
            shutil.copy2(key, aside / key.name)
    try:
        for f in siblings:
            f.unlink(missing_ok=True)
        restored = item.folder / "syntropy.db"
        if os.name != "nt":
            restored.chmod(0o600)
        db.same_owner(restored, data)
        with open(restored, "rb+") as fh:
            os.fsync(fh.fileno())
        os.replace(restored, path)
        staged_key = item.folder / "secret.key"
        if staged_key.exists():
            if os.name != "nt":
                staged_key.chmod(0o600)
            db.same_owner(staged_key, data)
            os.replace(staged_key, key)
        if aside and uploads.exists():
            os.replace(uploads, aside / "uploads")
        if (item.folder / "uploads").exists():
            os.replace(item.folder / "uploads", uploads)
        _sync_folder(path.parent)
    except OSError:
        _put_back(aside)
        raise
    if aside:
        _prune_safety_copies(aside.parent)
    return aside


def _put_back(aside: Optional[Path]) -> None:
    """Back to what was there, from the copy kept, or to an empty folder if nothing was."""
    data, path = config.data_dir(), config.db_path()
    for extra in ("-wal", "-shm"):
        Path(f"{path}{extra}").unlink(missing_ok=True)
    if aside is None:
        path.unlink(missing_ok=True)
        (data / "secret.key").unlink(missing_ok=True)
        shutil.rmtree(data / "uploads", ignore_errors=True)
        return
    if (aside / path.name).exists():
        _link_or_copy(aside / path.name, path.with_name(path.name + ".back"))
        os.replace(path.with_name(path.name + ".back"), path)
    else:
        path.unlink(missing_ok=True)
    for extra in ("-wal", "-shm"):
        if (aside / f"{path.name}{extra}").exists():
            _link_or_copy(aside / f"{path.name}{extra}", Path(f"{path}{extra}"))
    if (aside / "secret.key").exists():
        shutil.copy2(aside / "secret.key", data / "secret.key")
    else:
        (data / "secret.key").unlink(missing_ok=True)
    if (aside / "uploads").exists():
        shutil.rmtree(data / "uploads", ignore_errors=True)
        os.replace(aside / "uploads", data / "uploads")
    shutil.rmtree(aside, ignore_errors=True)


def forget_everything_kept() -> None:
    """What's kept in memory describes the database that just went: the next read of each starts afresh."""
    from app.store import biometrics, sample_counts

    db.reset_migration_cache()
    security.forget_key()
    sample_counts.forget()
    biometrics.forget_derived()
    maintenance.forget_damage()


def _settle(report: dict[str, Any], accept_key_problem: bool, actor: str) -> None:
    """Brings it up to this version and signs everyone in the browser out: sessions saved in an old copy may have been ended
    on purpose since. Phones keep their tokens, so they carry on syncing."""
    db.ensure_migrated()
    with db.db() as conn:
        conn.execute("DELETE FROM user_sessions WHERE device_id IS NULL")
        db.audit(conn, actor, "backup.restored", {"file": report["file"], "made": report.get("created_at"), "key": report["key"]["status"]})
    try:
        security.check_key()
    except db.StartupProblem as exc:
        if exc.kind != "key" or not accept_key_problem:
            raise


def apply(item: Staged, accept_key_problem: bool = False, *, actor: str = "cli",
          when_swapped: Optional[Callable[[], None]] = None) -> dict[str, Any]:
    """Swaps it in. If the result doesn't open, what was there goes back and this raises. The server must not be using the
    database: the command line checks that first, and in the server ``restore_in_server`` stops everything else."""
    report = item.report
    if not report["key"]["ok"] and not accept_key_problem:
        raise BackupProblem(report["key"]["message"])
    if uses := in_use_elsewhere():
        raise BackupProblem(uses)
    aside = _swap(item)
    try:
        forget_everything_kept()
        if when_swapped:
            when_swapped()
        _settle(report, accept_key_problem, actor)
    except (db.StartupProblem, sqlite3.Error, OSError) as exc:
        log.exception("The restored database didn't open")
        _put_back(aside)
        forget_everything_kept()
        raise BackupProblem(f"The restored data wouldn't open, so everything has been put back as it was: {exc}") from exc
    with _lock:
        _staged.pop(item.id, None)
    shutil.rmtree(item.folder, ignore_errors=True)
    return {**report, "safety_copy": str(aside) if aside else None}


def in_use_elsewhere() -> Optional[str]:
    """Another program with the database open. In WAL mode every open connection holds a shared lock on the file, so
    an exclusive lock can only be had when nothing else has it open, however idle."""
    path = config.db_path()
    if not path.exists() or not path.stat().st_size:
        return None
    conn = None
    try:
        conn = db.open_connection(path, timeout=0.2)
        conn.execute("PRAGMA busy_timeout = 200")
        conn.execute("PRAGMA locking_mode = EXCLUSIVE")
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        if db.is_busy(exc):
            return ("Another program is using the database: the Syntropy Health server, the command line, or an AI app using its "
                    "MCP server. Close it, then try again.")
    except sqlite3.DatabaseError:       # damaged is exactly when this is wanted
        pass
    finally:
        if conn:
            conn.close()
    return None


RESTORING_TEXT = "Syntropy Health is putting a backup back. This usually takes a minute, then you can sign in again."
_BUSY_SECONDS = 30.0


def restore_in_server(app: Any, item: Staged, accept_key_problem: bool = False) -> dict[str, Any]:
    """From Settings or the first-run screen: every other request is told to wait, background work stops, and only when
    nothing is touching the database does the swap happen. If that can't be had, nothing has changed."""
    from app import main
    from app.core import data_fixes
    from app.services import imports

    if not item.report["key"]["ok"] and not accept_key_problem:
        raise BackupProblem(item.report["key"]["message"])
    app.state.problem = RESTORING_TEXT
    imports.stop_running()      # before the pause, which an import's next batch would wait out and then write to what's restored
    db.pause(RESTORING_TEXT)
    try:
        data_fixes.stop()
        maintenance.stop()
        if not db.wait_until_idle(_BUSY_SECONDS):
            raise BackupProblem("Syntropy Health is busy with a long task, such as another backup or a download. Try again when it has finished.")
        db.close_readers()
        return apply(item, accept_key_problem, actor="user", when_swapped=db.resume)
    finally:
        db.resume()
        try:
            main._start_up()
            app.state.problem = None
            maintenance.start()
            data_fixes.start()
        except db.StartupProblem as exc:
            app.state.problem = str(exc)
        except (sqlite3.Error, OSError) as exc:
            log.exception("Couldn't start again after restoring")
            app.state.problem = f"Syntropy Health couldn't start again after restoring: {exc}. Restart it."
