"""Backups from Settings, and restoring one: from Settings for the owner, or from the first-run screen."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.core import auth, config, db, maintenance
from app.services import backup_file, backup_schedule, backups

router = APIRouter(prefix="/api", tags=["backups"])
owner = Depends(auth.require_owner)


class BackupRequest(BaseModel):
    passphrase: Optional[str] = Field(None, max_length=1024)
    unencrypted: bool = False


def _problem(exc: Exception) -> HTTPException:
    return HTTPException(400, str(exc))


def status_of() -> dict:
    backups.clean_leftovers()
    return {"last": backups.last(), "key_source": backups.key_source(), "min_passphrase": backup_file.MIN_PASSPHRASE,
            "data_dir": str(config.data_dir()), "schedule": backup_schedule.status(), **backup_schedule.folder_advice()}


@router.get("/backups", dependencies=[owner])
def backup_status() -> dict:
    return status_of()


@router.post("/backups", dependencies=[owner])
def make_backup(req: BackupRequest) -> dict:
    """Makes the file. The browser then fetches it from the address in the answer."""
    try:
        return backups.prepare_download(req.passphrase, req.unencrypted)
    except backups.BackupProblem as exc:
        raise _problem(exc) from exc
    except db.StartupProblem as exc:
        if exc.kind == "damaged":
            maintenance.note_damage(str(exc.__cause__ or exc))
        raise


@router.get("/backups/download/{token}", dependencies=[owner])
def download_backup(token: str, background: BackgroundTasks) -> FileResponse:
    item = backups.download(token)
    if not item:
        raise HTTPException(404, "This backup has already been downloaded, or has expired. Make another one.")
    background.add_task(backups.finish_download, token)
    return FileResponse(item["path"], media_type="application/octet-stream", filename=item["filename"])


class ScheduleRequest(BaseModel):
    folder: str = Field(..., max_length=1024)
    every_hours: int = 24
    keep: int = backup_schedule.KEEP_DEFAULT
    passphrase: Optional[str] = Field(None, max_length=1024)     # needed the first time, or to change it


@router.put("/backups/schedule", dependencies=[owner])
def set_schedule(req: ScheduleRequest) -> dict:
    try:
        backup_schedule.configure(req.folder, req.every_hours, req.keep, req.passphrase)
    except backups.BackupProblem as exc:
        raise _problem(exc) from exc
    return status_of()


@router.delete("/backups/schedule", dependencies=[owner])
def stop_schedule() -> dict:
    """Backups already made stay where they are."""
    backup_schedule.turn_off()
    return status_of()


@router.post("/backups/schedule/run", dependencies=[owner])
def run_schedule() -> dict:
    """One now, to see that it works. Waits for it: a failure comes back in the answer."""
    backup_schedule.run_now()
    return status_of()


@router.get("/export/backup", dependencies=[owner])
def old_backup_address() -> None:
    """For a page still open from before an upgrade."""
    raise HTTPException(410, "Backups are made from Settings → Your data now. Reload this page.")


# ---------------------------------------------------------------------------
# Restoring
# ---------------------------------------------------------------------------

async def _may_restore(request: Request) -> None:
    """The owner, or before setup, whoever could also finish setting up: from nearby, as for /api/setup. A backup of
    their own is no more than the password they could choose there."""
    from app.api.system import _nearby

    if await run_in_threadpool(auth.is_setup_complete):
        await auth.require_owner(request)
        return
    auth._check_csrf(request)
    if not _nearby(request):
        raise HTTPException(403, "Restore from this computer or your home network.")


def _save(upload: UploadFile, folder: Path, name: str) -> Path:
    target = folder / name
    with open(target, "wb") as out:
        shutil.copyfileobj(upload.file, out, 1024 * 1024)
    return target


def _check(file: UploadFile, passphrase: Optional[str], key_file: Optional[UploadFile]) -> dict:
    data = config.data_dir()
    holding = Path(tempfile.mkdtemp(dir=data, prefix=".restore-upload-"))
    try:
        saved = _save(file, holding, Path(file.filename or "backup").name or "backup")
        key = _save(key_file, holding, "given.key") if key_file else None
        return backups.check(saved, passphrase, key).report
    except backups.BackupFileError as exc:
        raise HTTPException(400, str(exc)) from exc
    except backups.BackupProblem as exc:
        raise _problem(exc) from exc
    finally:
        shutil.rmtree(holding, ignore_errors=True)


@router.post("/restore/check", dependencies=[Depends(_may_restore)])
async def restore_check(file: UploadFile = File(...), passphrase: Optional[str] = Form(None),
                        key_file: Optional[UploadFile] = File(None)) -> dict:
    """Unpacks and tests the file and says what's in it. Nothing is changed. Takes a while with a large backup."""
    return await run_in_threadpool(_check, file, passphrase, key_file)


class RestoreApply(BaseModel):
    id: str = Field(..., max_length=100)
    accept_key_problem: bool = False


@router.post("/restore/apply", dependencies=[Depends(_may_restore)])
def restore_apply(req: RestoreApply, request: Request) -> dict:
    item = backups.staged(req.id)
    if not item:
        raise HTTPException(404, "That check has expired. Choose the backup again.")
    try:
        done = backups.restore_in_server(request.app, item, req.accept_key_problem)
    except backups.BackupProblem as exc:
        raise _problem(exc) from exc
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(500, f"Couldn't restore: {exc}. Nothing was changed.") from exc
    return {"ok": True, "holds": done["holds"], "sign_in": done["sign_in"], "safety_copy": done["safety_copy"], "accounts": done["accounts"]}


@router.delete("/restore/{staged_id}", dependencies=[Depends(_may_restore)])
def restore_discard(staged_id: str) -> dict:
    backups.discard(staged_id)
    return {"ok": True}
