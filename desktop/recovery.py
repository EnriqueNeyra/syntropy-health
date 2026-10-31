"""
Getting going again when the database can't be used: setting it aside, or putting a backup in its place. Started by a
person from the page the app shows when it can't start, with the server stopped. Nothing is ever deleted here: what's
replaced moves to a set-aside-<time> folder beside the database, for them to keep or remove.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional

from app.core import db

log = logging.getLogger("syntropy.desktop")

KEY_FILE = "secret.key"
ENCRYPTED = b"SYNTROPY-BACKUP\n"     # how app.services.backup_file begins


class RestoreRefused(Exception):
    """Why this file wasn't restored, in words for the person. Nothing has changed."""


def _aside_folder(path: Path) -> Path:
    folder = path.parent / f"set-aside-{time.strftime('%Y%m%d-%H%M%S')}"
    folder.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        folder.chmod(0o700)
    return folder


def _database_files(path: Path) -> list[Path]:
    return [f for f in (path, Path(f"{path}-wal"), Path(f"{path}-shm")) if f.exists()]


def set_aside(path: Path) -> Path:
    """Moves the database out of the way, so the next start makes a new empty one. Returns where it went."""
    folder = _aside_folder(path)
    moved: list[Path] = []
    try:
        for f in _database_files(path):
            os.replace(f, folder / f.name)
            moved.append(f)
    except OSError:
        for f in moved:
            os.replace(folder / f.name, f)
        raise
    log.warning("Moved the database to %s", folder)
    return folder


def _check(candidate: Path) -> None:
    """Opens the copy the way the app will, since a backup that looks fine and isn't is worse than the damage."""
    try:
        conn = sqlite3.connect(str(candidate))
        try:
            found = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            is_ours = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'profiles'").fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        log.warning("%s can't be restored: %s", candidate, exc)
        raise RestoreRefused("That file isn't a Syntropy Health backup, or it's damaged too. Nothing has been changed.") from exc
    if found != "ok":
        raise RestoreRefused("That backup is damaged too, so it wasn't restored. Nothing has been changed. Try an older one.")
    if not is_ours:
        raise RestoreRefused("That file isn't a Syntropy Health backup. Nothing has been changed.")
    if version > db.newest_schema():
        raise RestoreRefused("That backup was made by a newer version of Syntropy Health. Install the latest version, then "
                             "restore it. Nothing has been changed.")


def _copy_in(source: Path, dest: Path) -> None:
    if shutil.disk_usage(dest.parent).free < source.stat().st_size + 50_000_000:
        raise RestoreRefused("There isn't room on the disk for that backup. Free up some space and try again. Nothing has "
                             "been changed.")
    shutil.copyfile(source, dest)


def _take_from_zip(archive: zipfile.ZipFile, name: str, dest: Path) -> None:
    if shutil.disk_usage(dest.parent).free < archive.getinfo(name).file_size + 50_000_000:
        raise RestoreRefused("There isn't room on the disk for that backup. Free up some space and try again. Nothing has "
                             "been changed.")
    with archive.open(name) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out, 1 << 20)


def _restore_uploads(archive: zipfile.ZipFile, folder: Path) -> None:
    """Files a report was read from, say. Only ones that aren't here already, and never outside the folder."""
    for name in archive.namelist():
        parts = PurePosixPath(name).parts
        if len(parts) < 2 or parts[0] != "uploads" or ".." in parts or name.endswith("/") or ":" in name or "\\" in name:
            continue
        target = folder.joinpath(*parts)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)


def restore(backup: Path, path: Path) -> Path:
    """Puts a backup (a .db file, a copy from pre-upgrade/, or the .zip from `syntropy-health backup`) in the database's
    place, and its key if it has one. Returns where the replaced files went; raises RestoreRefused with nothing changed."""
    incoming = path.with_name(f"{path.name}.restoring")
    key_in: Optional[Path] = None
    archive: Optional[zipfile.ZipFile] = None
    try:
        with open(backup, "rb") as file:
            if file.read(len(ENCRYPTED)) == ENCRYPTED:
                raise RestoreRefused("That backup is encrypted, so it can't be restored from here. Nothing has been changed. Set this "
                                     "database aside to start Syntropy Health, then restore it from the first screen, or use the "
                                     "command: syntropy-health restore FILE.")
        if zipfile.is_zipfile(backup):
            archive = zipfile.ZipFile(backup)
            if path.name not in archive.namelist():
                raise RestoreRefused("That zip file doesn't hold a Syntropy Health backup. Nothing has been changed.")
            _take_from_zip(archive, path.name, incoming)
            if KEY_FILE in archive.namelist():
                key_in = path.with_name(f"{KEY_FILE}.restoring")
                _take_from_zip(archive, KEY_FILE, key_in)
        else:
            _copy_in(backup, incoming)
        _check(incoming)
        for leftover in (Path(f"{incoming}-wal"), Path(f"{incoming}-shm")):
            leftover.unlink(missing_ok=True)

        folder = _aside_folder(path)
        moved: list[Path] = []
        key = path.with_name(KEY_FILE)
        try:
            for f in _database_files(path) + ([key] if key_in and key.exists() else []):
                os.replace(f, folder / f.name)
                moved.append(f)
            os.replace(incoming, path)
            if key_in:
                os.replace(key_in, key)
        except OSError:
            for f in moved:     # with no database here at all, the next start would make an empty one
                os.replace(folder / f.name, f)
            raise
        if archive:
            _restore_uploads(archive, path.parent)
        log.warning("Restored the database from %s; what it replaced is in %s", backup, folder)
        return folder
    except (OSError, zipfile.BadZipFile) as exc:
        log.exception("Couldn't restore from %s", backup)
        raise RestoreRefused(f"That backup couldn't be restored: {exc}. Nothing has been changed.") from exc
    finally:
        if archive:
            archive.close()
        for leftover in (incoming, Path(f"{incoming}-wal"), Path(f"{incoming}-shm"), key_in):
            if leftover:
                leftover.unlink(missing_ok=True)
