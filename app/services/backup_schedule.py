"""
Scheduled backups: inside the server, to a folder the owner chose, keeping the last N. Nothing leaves this computer
unless that folder does.

The passphrase isn't stored. What's stored is the key made from it, encrypted with this server's encryption key, so a
backup can run with nobody there to type anything. That has a cost the owner is told about: someone with this computer's
data folder could open these backups without the passphrase. It protects the copies that are elsewhere (a drive that's
lost, a folder that's synced), not against someone who already has this computer.
"""

from __future__ import annotations

import base64
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.core import db, maintenance, security, settings
from app.services import backup_file, backups
from app.services.backup_file import SealKey

log = logging.getLogger("syntropy.backups")

SCHEDULE_KEY = "backup.schedule"
STATE_KEY = "backup.schedule_state"
EVERY_HOURS = (24, 7 * 24)
KEEP_MIN, KEEP_MAX, KEEP_DEFAULT = 1, 100, 7
# A laptop that's been shut for a week isn't a failure until it's had a chance: one tick after waking, it has.
LATE_GRACE = 6 * 3600

_running = threading.Lock()


def _settings() -> Optional[dict[str, Any]]:
    return settings.get(SCHEDULE_KEY)


def _state() -> dict[str, Any]:
    return settings.get(STATE_KEY) or {}


def _try_folder(folder: Path) -> None:
    """Now, while the owner is here to fix it, rather than a day from now in an alert."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".syntropy-write-test-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise backups.BackupProblem(f"Syntropy Health can't write to {folder}: {exc.strerror or exc}. Choose a folder this server "
                                    "can write to.") from exc


def _same_disk(folder: Path) -> Optional[bool]:
    try:
        return os.stat(folder).st_dev == os.stat(backups.config.data_dir()).st_dev
    except OSError:
        return None


def configure(folder: str, every_hours: int, keep: int, passphrase: Optional[str]) -> dict[str, Any]:
    """Turns them on, or changes them. The passphrase is only needed the first time, or to change it."""
    path = Path(folder or "").expanduser()
    if not folder or not path.is_absolute():
        raise backups.BackupProblem("Enter the full path of a folder on the computer this server runs on.")
    if every_hours not in EVERY_HOURS:
        raise backups.BackupProblem("Choose daily or weekly.")
    if not KEEP_MIN <= keep <= KEEP_MAX:
        raise backups.BackupProblem(f"Keep between {KEEP_MIN} and {KEEP_MAX} backups.")
    current = _settings() or {}
    if passphrase:
        key = backup_file.new_seal_key(backups.validate_passphrase(passphrase))
        sealed = {"kdf": key.kdf, "key_enc": security.encrypt_json(base64.b64encode(key.material).decode())}
    elif current.get("key_enc"):
        sealed = {"kdf": current["kdf"], "key_enc": current["key_enc"]}
    else:
        raise backups.BackupProblem(f"Choose a passphrase of at least {backup_file.MIN_PASSPHRASE} characters.")
    _try_folder(path)
    settings.set(SCHEDULE_KEY, {"folder": str(path), "every_hours": every_hours, "keep": keep, **sealed, "enabled_at": time.time()})
    with db.db() as conn:
        db.audit(conn, "user", "backup.schedule_set", {"folder": str(path), "every_hours": every_hours, "keep": keep})
    return status()


def turn_off() -> dict[str, Any]:
    settings.set(SCHEDULE_KEY, None)
    settings.set(STATE_KEY, None)
    with db.db() as conn:
        db.audit(conn, "user", "backup.schedule_off")
    return status()


def status() -> dict[str, Any]:
    """For Settings. The key stays here."""
    plan = _settings()
    state = _state()
    if not plan:
        return {"enabled": False, "every_hours": EVERY_HOURS[0], "keep": KEEP_DEFAULT, "state": {}}
    folder = Path(plan["folder"])
    every = plan["every_hours"] * 3600
    last = state.get("last_success_at")
    return {"enabled": True, "folder": plan["folder"], "every_hours": plan["every_hours"], "keep": plan["keep"],
            "same_disk": _same_disk(folder), "state": state, "running": _running.locked(),
            "next_at": last + every if last else plan["enabled_at"],
            "scheduler_off": not backups.config.scheduler_enabled(), "files": _files(folder)}


def _files(folder: Path) -> list[dict[str, Any]]:
    try:
        return [{"name": f.name, "size_bytes": f.stat().st_size, "at": f.stat().st_mtime} for f in _ours(folder)][::-1]
    except OSError:
        return []


def _ours(folder: Path) -> list[Path]:
    """Backups this made, oldest first (the name holds the time). Nothing else in that folder is ever touched."""
    return sorted(folder.glob(f"{backups.NAME_PREFIX}*{backups.SEALED_SUFFIX}"), key=lambda f: f.name)


def _prune(folder: Path, keep: int) -> int:
    old = _ours(folder)[:-keep]
    for f in old:
        f.unlink(missing_ok=True)
    return len(old)


def _seal_key(plan: dict[str, Any]) -> SealKey:
    raw = security.decrypt_json(plan.get("key_enc"))
    if not raw:
        raise backups.BackupProblem("The key for scheduled backups can't be read, because this server's encryption key has changed. "
                                    "Enter the passphrase again in Settings → Your data.")
    return SealKey(base64.b64decode(raw), plan["kdf"])


def _due(plan: dict[str, Any], state: dict[str, Any], now: float) -> bool:
    last = state.get("last_attempt_at")
    if not last:
        return True
    if (state.get("last_error_at") or 0) >= (state.get("last_success_at") or 0):
        return now - last >= 3600       # a failure is tried again sooner than the next day, but not at every tick
    return now - (state.get("last_success_at") or 0) >= plan["every_hours"] * 3600


def run_now() -> dict[str, Any]:
    """One backup, whether or not it's due. Whatever goes wrong is saved for Settings and Alerts."""
    if not _running.acquire(blocking=False):
        return status()
    try:
        plan = _settings()
        if not plan:
            return status()
        state = _state()
        started = time.time()
        folder = Path(plan["folder"])
        try:
            # The folder must be there: a drive that isn't plugged in mustn't fill the disk it's mounted on.
            if not folder.is_dir():
                raise backups.BackupProblem(f"The backup folder {folder} isn't there. If it's on a drive, plug it in.")
            result = backups.create(folder, seal_key=_seal_key(plan), made_by="schedule")
            pruned = _prune(folder, plan["keep"])
            state = {**state, "last_attempt_at": started, "last_success_at": time.time(), "last_error": None, "last_error_at": None,
                     "last_file": result["filename"], "last_size_bytes": result["size_bytes"], "holds": result["holds"], "pruned": pruned}
        except db.StartupProblem as exc:
            if exc.kind == "restoring":
                return status()
            if exc.kind == "damaged":
                maintenance.note_damage(str(exc.__cause__ or exc))
            state = _failed(state, started, str(exc))
        except (backups.BackupProblem, OSError, sqlite3.Error) as exc:
            state = _failed(state, started, str(exc))
        settings.set(STATE_KEY, state)
        return status()
    finally:
        _running.release()


def _failed(state: dict[str, Any], started: float, why: str) -> dict[str, Any]:
    log.warning("A scheduled backup failed: %s", why)
    return {**state, "last_attempt_at": started, "last_error": why[:600], "last_error_at": time.time()}


def run_if_due() -> None:
    plan = _settings()
    if plan and _due(plan, _state(), time.time()):
        run_now()


def start_if_due() -> None:
    """From the scheduler's tick, which has syncs to get on with: a large backup takes minutes."""
    if not _running.locked():
        threading.Thread(target=_run_quietly, name="scheduled-backup", daemon=True).start()


def _run_quietly() -> None:
    try:
        backups.clean_leftovers()
        run_if_due()
    except Exception:  # noqa: BLE001 - the next tick tries again; a failure is reported by what stops being saved
        log.exception("Scheduled backup check failed")


def _ago(seconds: float) -> str:
    days = int(seconds // 86400)
    if days >= 2:
        return f"{days} days ago"
    return f"{max(1, int(seconds // 3600))} hours ago"


def problems() -> list[dict[str, str]]:
    """For Alerts: when they fail, and when they've quietly stopped."""
    plan = _settings()
    if not plan:
        return []
    state = _state()
    now = time.time()
    last = state.get("last_success_at")
    newest = f"The newest backup is from {_ago(now - last)}." if last else "No backup has been made yet."
    if (state.get("last_error_at") or 0) > (last or 0):
        return [{"id": "backups-failing", "title": "Scheduled backups are failing",
                 "text": f"{state['last_error']} {newest} Your records here aren't affected. Settings → Your data has the details."}]
    if now > (last or plan["enabled_at"]) + plan["every_hours"] * 3600 + LATE_GRACE:
        why = ("The server's scheduler is turned off (SYNTROPY_SCHEDULER), so they can't run." if not backups.config.scheduler_enabled()
               else "They run only while this server is on. Settings → Your data can run one now.")
        return [{"id": "backups-stopped", "title": "Scheduled backups have stopped", "text": f"{newest} {why}"}]
    return []

