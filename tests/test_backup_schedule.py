"""Scheduled backups: to a chosen folder, keeping the last few, restorable with the passphrase, and loud when they stop."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.core import config, db, security, settings
from app.services import backup_file, backup_schedule, backups, scheduler

SECRET = "a passphrase for this backup"


@pytest.fixture(autouse=True)
def quick_passphrases(monkeypatch):
    monkeypatch.setitem(backup_file._SCRYPT, "n", 2**10)


def _turn_on(client, folder, **kw):
    return client.put("/api/backups/schedule", json={"folder": str(folder), "every_hours": 24, "keep": 3, "passphrase": SECRET, **kw})


def _shift(seconds: float) -> None:
    """As if everything saved happened this long ago."""
    state = settings.get(backup_schedule.STATE_KEY) or {}
    settings.set(backup_schedule.STATE_KEY, {k: (v - seconds if k.endswith("_at") and v else v) for k, v in state.items()})
    plan = settings.get(backup_schedule.SCHEDULE_KEY)
    settings.set(backup_schedule.SCHEDULE_KEY, {**plan, "enabled_at": plan["enabled_at"] - seconds})


def test_a_scheduled_backup_restores_with_the_passphrase_alone(client, tmp_path):
    assert _turn_on(client, tmp_path / "drive").status_code == 200
    assert client.post("/api/backups/schedule/run").status_code == 200
    made = list((tmp_path / "drive").glob("*.syntropy"))
    assert len(made) == 1
    item = backups.check(made[0], SECRET)
    assert item.report["key"]["ok"] and item.report["people"] == ["Alex"]
    backups.discard(item.id)
    with pytest.raises(backup_file.WrongPassphrase):
        backups.check(made[0], "the wrong passphrase")
    status = client.get("/api/backups").json()["schedule"]
    assert status["state"]["last_success_at"] and status["state"]["last_file"] == made[0].name
    assert client.get("/api/backups").json()["last"]["made_by"] == "schedule"


def test_the_passphrase_is_not_stored(client, tmp_path):
    _turn_on(client, tmp_path / "drive")
    with db.read() as conn:
        everything = " ".join(str(tuple(r)) for r in conn.execute("SELECT * FROM app_settings"))
    assert SECRET not in everything
    stored = settings.get(backup_schedule.SCHEDULE_KEY)
    assert "key_enc" in stored and "salt" in stored["kdf"]
    # The key is unreadable as stored, and doesn't go out to the page.
    answer = client.get("/api/backups").text
    assert "key_enc" not in answer and "salt" not in answer


def test_only_the_last_few_are_kept_and_nothing_else_is_touched(client, tmp_path, monkeypatch):
    folder = tmp_path / "drive"
    _turn_on(client, folder, keep=2)
    (folder / "my-holiday-photos.zip").write_bytes(b"not ours")
    (folder / "syntropy-backup-19990101-000000.zip").write_bytes(b"a plain one someone keeps on purpose")
    clock = iter(range(5))
    real = backups.time.strftime
    monkeypatch.setattr(backups.time, "strftime", lambda f, *a: f"2026010{next(clock)}-000000" if f == "%Y%m%d-%H%M%S" else real(f, *a))
    for _ in range(4):
        backup_schedule.run_now()
    names = sorted(f.name for f in folder.iterdir())
    assert names == ["my-holiday-photos.zip", "syntropy-backup-19990101-000000.zip",
                     "syntropy-backup-20260102-000000.syntropy", "syntropy-backup-20260103-000000.syntropy"]


def test_when_it_is_due(client, tmp_path):
    _turn_on(client, tmp_path / "drive")
    backup_schedule.run_if_due()
    assert len(list((tmp_path / "drive").glob("*.syntropy"))) == 1
    backup_schedule.run_if_due()       # not yet
    assert len(list((tmp_path / "drive").glob("*.syntropy"))) == 1
    _shift(25 * 3600)
    time.sleep(1.1)                    # names are by the second
    backup_schedule.run_if_due()
    assert len(list((tmp_path / "drive").glob("*.syntropy"))) == 2


def test_the_schedule_runs_from_the_scheduler_without_holding_it_up(client, tmp_path, monkeypatch):
    _turn_on(client, tmp_path / "drive")
    monkeypatch.setattr(scheduler, "_due", list)
    asyncio.run(scheduler.run_once())
    for _ in range(100):
        if list((tmp_path / "drive").glob("*.syntropy")) and not backup_schedule._running.locked():
            break
        time.sleep(0.05)
    assert list((tmp_path / "drive").glob("*.syntropy"))


def test_a_missing_drive_is_reported_and_does_not_write_to_what_is_underneath(client, tmp_path):
    folder = tmp_path / "drive"
    _turn_on(client, folder)
    folder.rmdir()
    client.post("/api/backups/schedule/run")
    assert not folder.exists()
    status = client.get("/api/status").json()
    problem = next(p for p in status["problems"] if p["id"] == "backups-failing")
    assert "plug it in" in problem["text"] and "No backup has been made yet" in problem["text"]
    # And a failure is tried again in an hour, not every tick.
    state = settings.get(backup_schedule.STATE_KEY)
    assert not backup_schedule._due(settings.get(backup_schedule.SCHEDULE_KEY), state, time.time() + 60)
    assert backup_schedule._due(settings.get(backup_schedule.SCHEDULE_KEY), state, time.time() + 3700)
    # Back again, the alert goes.
    folder.mkdir()
    client.post("/api/backups/schedule/run")
    assert not [p for p in client.get("/api/status").json()["problems"] if p["id"].startswith("backups")]


def test_backups_that_quietly_stop_are_reported(client, tmp_path):
    _turn_on(client, tmp_path / "drive")
    client.post("/api/backups/schedule/run")
    assert not backup_schedule.problems()
    _shift(3 * 86400)
    problem = backup_schedule.problems()[0]
    assert problem["id"] == "backups-stopped" and "3 days ago" in problem["text"] and "SYNTROPY_SCHEDULER" in problem["text"]


def test_a_new_encryption_key_asks_for_the_passphrase_again(client, tmp_path):
    _turn_on(client, tmp_path / "drive")
    security.start_new_key()
    client.post("/api/backups/schedule/run")
    problem = next(p for p in backup_schedule.problems() if p["id"] == "backups-failing")
    assert "passphrase again" in problem["text"]
    assert _turn_on(client, tmp_path / "drive").status_code == 200
    client.post("/api/backups/schedule/run")
    assert not backup_schedule.problems()


def test_settings_checks_what_it_is_given(client, tmp_path):
    good = {"folder": str(tmp_path / "d"), "every_hours": 24, "keep": 3, "passphrase": SECRET}
    for change, words in (({"folder": "relative/path"}, "full path"), ({"folder": ""}, "full path"), ({"every_hours": 5}, "daily"),
                          ({"keep": 0}, "Keep"), ({"passphrase": "short"}, "passphrase"), ({"passphrase": None}, "passphrase")):
        r = client.put("/api/backups/schedule", json={**good, **change})
        assert r.status_code == 400 and words in r.json()["detail"], change
    blocked = tmp_path / "a-file"
    blocked.write_text("x")
    r = client.put("/api/backups/schedule", json={**good, "folder": str(blocked / "inside")})
    assert r.status_code == 400 and "can't write" in r.json()["detail"]
    assert not client.get("/api/backups").json()["schedule"]["enabled"]


def test_changing_the_plan_keeps_the_passphrase_and_stopping_keeps_the_files(client, tmp_path):
    _turn_on(client, tmp_path / "drive")
    assert client.put("/api/backups/schedule", json={"folder": str(tmp_path / "drive"), "every_hours": 168, "keep": 9}).status_code == 200
    client.post("/api/backups/schedule/run")
    made = list((tmp_path / "drive").glob("*.syntropy"))
    backups.discard(backups.check(made[0], SECRET).id)
    status = client.get("/api/backups").json()["schedule"]
    assert status["every_hours"] == 168 and status["keep"] == 9 and status["files"][0]["name"] == made[0].name
    assert client.delete("/api/backups/schedule").json()["schedule"]["enabled"] is False
    assert list((tmp_path / "drive").glob("*.syntropy")) == made
    assert not backup_schedule.problems()


def test_it_says_when_the_folder_is_on_the_same_disk(client, tmp_path):
    _turn_on(client, config.data_dir() / "backups")
    assert client.get("/api/backups").json()["schedule"]["same_disk"] is True


def test_only_the_owner_sets_the_schedule(client, tmp_path):
    from tests.test_accounts import _join
    pid = client.post("/api/profiles", json={"name": "Maria", "relationship": "other"}).json()["id"]
    member, _ = _join(client, pid)
    assert _turn_on(member, tmp_path / "x").status_code == 403
    assert member.post("/api/backups/schedule/run").status_code == 403
    assert member.get("/api/status").json().get("problems") is None


def test_damage_found_by_a_scheduled_backup_is_noted(client, tmp_path, monkeypatch):
    _turn_on(client, tmp_path / "drive")

    def damaged(*a, **k):
        raise db.StartupProblem("damaged", "The backup was stopped because the file has damage") from __import__("sqlite3").DatabaseError("found")
    monkeypatch.setattr(backups, "create", damaged)
    backup_schedule.run_now()
    assert backup_schedule.problems()[0]["id"] == "backups-failing"
    from app.core import maintenance
    assert maintenance.integrity()["ok"] is False
    maintenance.forget_damage()
