"""`syntropy-health update`, which the Linux timer runs at night with nobody watching: it checks the server came back,
and goes back to what worked if it didn't. GitHub, pip and systemd are stood in for."""

from __future__ import annotations

import errno
import http.server
import json
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from app import cli
from app.core import config, db
from app.services import updates
from tests.test_database_safety import _build


@pytest.fixture
def machine(monkeypatch, tmp_path):
    """A Linux server install, one release behind, recording what the updater does."""
    actions: list[str] = []
    prefix = tmp_path / "venv"
    prefix.mkdir()
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(config, "is_source_checkout", lambda: False)
    monkeypatch.setattr(updates, "fetch_latest", lambda: {"version": "99.0.0", "assets": {
        "syntropy_health-99.0.0-py3-none-any.whl": "https://example.test/new.whl"}})
    monkeypatch.setattr(updates, "fetch_checksums", lambda version: {f"syntropy_health-{version}-py3-none-any.whl": "ab12"})
    monkeypatch.setattr(cli, "_service_state", lambda: "active")
    monkeypatch.setattr(cli, "service_control", lambda action: actions.append(f"service {action}"))
    monkeypatch.setattr(cli, "_pip_install", lambda source, constraints=None: actions.append(f"install {source}"))
    monkeypatch.setattr(cli, "UPDATE_WAIT_SECONDS", 1)
    return actions


def test_a_good_update_is_reported_once_the_server_answers(machine, monkeypatch, capsys):
    updates.note_failed_update("98.0.0", "an earlier failure")
    monkeypatch.setattr(cli, "_wait_for_server", lambda port, version, seconds: machine.append(f"wait {version}"))
    cli.update()
    assert machine == ["install syntropy-health @ https://example.test/new.whl#sha256=ab12", "service restart", "wait 99.0.0"]
    assert "the server is running" in capsys.readouterr().out
    assert updates.failed_update() is None


def _upgrade_went_wrong() -> Path:
    """The new version upgraded the database, keeping a copy, and then wouldn't start."""
    path = config.db_path()
    conn = _build(path, 13)
    conn.execute("INSERT INTO app_settings(key, value, updated_at) VALUES ('from', '\"before\"', 0)")
    conn.close()
    db.reset_migration_cache()
    db.ensure_migrated(path)
    db.reset_migration_cache()
    return path


def test_a_release_that_will_not_start_is_rolled_back(machine, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_last_port", lambda: 8123)
    answers = iter(["It didn't answer.", None])

    def wait(port, version, seconds):
        machine.append(f"wait {port} {version}")
        return next(answers)
    monkeypatch.setattr(cli, "_wait_for_server", wait)
    real_restart = machine.append

    def control(action):
        real_restart(f"service {action}")
        if action == "restart":
            _upgrade_went_wrong()
    monkeypatch.setattr(cli, "service_control", control)
    with pytest.raises(SystemExit) as caught:
        cli.update()
    assert machine == ["install syntropy-health @ https://example.test/new.whl#sha256=ab12", "service restart",
                       "wait 8123 99.0.0", "service stop",
                       f"install syntropy-health @ https://github.com/{updates.REPO}/releases/download/v{config.APP_VERSION}/"
                       f"syntropy_health-{config.APP_VERSION}-py3-none-any.whl#sha256=ab12",
                       "service start", f"wait 8123 {config.APP_VERSION}"]
    assert f"Version {config.APP_VERSION} is running again" in str(caught.value.code)
    path = config.db_path()
    conn = sqlite3.connect(str(path))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 13         # what the old version knows how to run
    assert conn.execute("SELECT value FROM app_settings WHERE key = 'from'").fetchone()[0] == '"before"'
    conn.close()
    kept = list((path.parent / "pre-upgrade").glob("replaced-*/syntropy.db"))
    assert len(kept) == 1      # what it replaced is never thrown away
    assert updates.failed_update()["version"] == "99.0.0"


def _going_back_after_an_upgrade(machine, monkeypatch) -> float:
    started = time.time() - 2
    _upgrade_went_wrong()
    machine.clear()
    monkeypatch.setattr(cli, "_wait_for_server", lambda port, version, seconds: machine.append(f"wait {version}"))
    return started


def _version() -> int:
    conn = sqlite3.connect(str(config.db_path()))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_a_full_disk_does_not_stop_the_database_going_back(machine, monkeypatch):
    """No room for a second copy, as when the new version stopped because the disk is full: the one copy moves instead."""
    started = _going_back_after_an_upgrade(machine, monkeypatch)
    path = config.db_path()

    def full(*_a, **_k):
        Path(f"{path}.restoring").write_bytes(b"as far as it got")
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(db.shutil, "copyfile", full)
    with pytest.raises(SystemExit) as caught:
        cli._update_failed("The disk is full.", "99.0.0", config.APP_VERSION, started, 8000)
    assert [a.split(" @")[0] for a in machine][:3] == ["service stop", "install syntropy-health", "service start"]
    assert f"Version {config.APP_VERSION} is running again" in str(caught.value.code)
    assert _version() == 13
    assert not Path(f"{path}.restoring").exists()
    assert len(list((path.parent / "pre-upgrade").glob("replaced-*/syntropy.db"))) == 1


def test_a_rollback_that_cannot_restore_still_starts_the_service_and_says_so(machine, monkeypatch):
    started = _going_back_after_an_upgrade(machine, monkeypatch)
    path = config.db_path()
    newest = _version()

    def broken(*_a, **_k):
        raise OSError(errno.EIO, "Input/output error")
    monkeypatch.setattr(db.shutil, "copyfile", broken)
    with pytest.raises(SystemExit) as caught:
        cli._update_failed("It crashed.", "99.0.0", config.APP_VERSION, started, 8000)
    assert "service start" in machine and machine.index("service start") > machine.index("service stop")
    message = str(caught.value.code)
    assert "couldn't be put back" in message and "Input/output error" in message and "pre-upgrade" in message
    assert _version() == newest and path.exists()
    assert not list(path.parent.glob("*.restoring"))


def test_a_restore_that_fails_halfway_leaves_the_database_in_place(machine, monkeypatch):
    started = _going_back_after_an_upgrade(machine, monkeypatch)
    path = config.db_path()
    newest = _version()
    real = db.os.replace

    def refuse_the_last_step(src, dst):
        if Path(dst) == path and Path(src).name.endswith(".restoring"):
            raise OSError(errno.EACCES, "Permission denied")
        real(src, dst)
    monkeypatch.setattr(db.os, "replace", refuse_the_last_step)
    with pytest.raises(SystemExit):
        cli._update_failed("It crashed.", "99.0.0", config.APP_VERSION, started, 8000)
    assert "service start" in machine
    assert path.exists() and _version() == newest


def test_a_rollback_goes_on_when_the_failure_cannot_be_noted(machine, monkeypatch, capsys):
    started = _going_back_after_an_upgrade(machine, monkeypatch)

    def full(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(updates, "note_failed_update", full)
    with pytest.raises(SystemExit):
        cli._update_failed("The disk is full.", "99.0.0", config.APP_VERSION, started, 8000)
    assert "service start" in machine and _version() == 13
    assert "may try it again" in capsys.readouterr().out


def test_a_reinstall_that_cannot_even_run_still_starts_the_service(machine, monkeypatch):
    def no_room(_source, _constraints=None):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(cli, "_pip_install", no_room)
    with pytest.raises(SystemExit) as caught:
        cli._update_failed("The disk is full.", "99.0.0", config.APP_VERSION, time.time(), 8000)
    assert machine == ["service stop", "service start"] and "Couldn't reinstall" in str(caught.value.code)


def test_files_the_updater_puts_back_as_root_keep_their_owner(machine, monkeypatch):
    started = _going_back_after_an_upgrade(machine, monkeypatch)
    path = config.db_path()
    owner = path.stat()
    given = []
    monkeypatch.setattr(db.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(db.os, "chown", lambda p, uid, gid: given.append((Path(p).name, uid, gid)), raising=False)
    assert db.restore_pre_upgrade(started) is not None
    assert ("syntropy.db.restoring", owner.st_uid, owner.st_gid) in given


def test_a_copy_made_as_root_stays_usable_by_the_service(monkeypatch):
    """An older install's `sudo syntropy-health accounts` can be what upgrades the database."""
    path = config.db_path()
    conn = _build(path, 13)
    conn.close()
    db.reset_migration_cache()
    owner = path.stat()
    given = []
    monkeypatch.setattr(db.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(db.os, "chown", lambda p, uid, gid: given.append((Path(p).name, uid, gid)), raising=False)
    db.ensure_migrated(path)
    names = {name for name, uid, gid in given if (uid, gid) == (owner.st_uid, owner.st_gid)}
    assert "pre-upgrade" in names and any(n.endswith(".db.tmp") for n in names)


def test_the_nightly_timer_does_not_retry_a_release_that_failed(machine, monkeypatch, capsys):
    monkeypatch.setattr(updates, "auto_on", lambda: True)
    cli.update_due()      # exits 0: the timer goes on to install it
    assert "is available" in capsys.readouterr().out
    updates.note_failed_update("99.0.0", "it crashed")
    with pytest.raises(SystemExit) as caught:
        cli.update_due()
    assert caught.value.code == 1
    assert "won't be tried again" in capsys.readouterr().out


def test_the_owner_is_told(client):
    updates.note_failed_update("99.0.0", "it crashed")
    problems = client.get("/api/status").json()["problems"]
    assert problems[0]["id"] == "update-failed-99.0.0" and "it crashed" in problems[0]["text"]
    updates.note_failed_update(config.APP_VERSION, "this one is installed now, so that's over")
    assert client.get("/api/status").json()["problems"] == []


def test_without_a_release_to_go_back_to_it_says_what_to_do(machine, monkeypatch, tmp_path):
    (Path(sys.prefix) / "syntropy-source.txt").write_text("/some/where/own.whl")
    monkeypatch.setattr(cli, "_wait_for_server", lambda port, version, seconds: "It didn't answer.")
    with pytest.raises(SystemExit) as caught:
        cli.update()
    assert machine == ["install /some/where/own.whl", "service restart"]
    assert config.APP_VERSION in str(caught.value.code) and updates.RELEASES_URL in str(caught.value.code)


def test_with_no_service_there_is_nothing_to_wait_for(machine, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_service_state", lambda: "not installed")
    cli.update()
    assert machine == ["install syntropy-health @ https://example.test/new.whl#sha256=ab12"]
    assert "Updated" in capsys.readouterr().out


def test_a_copy_older_than_this_update_is_not_put_back(machine):
    db.ensure_migrated()
    path = config.db_path()
    folder = path.parent / "pre-upgrade"
    folder.mkdir()
    (folder / "syntropy-v13-20200101-000000.db").write_bytes(b"old")
    since = time.time() + 60
    assert db.restore_pre_upgrade(since) is None
    assert path.read_bytes()[:15] == b"SQLite format 3"


# ---------------------------------------------------------------------------
# Asking the server
# ---------------------------------------------------------------------------

@pytest.fixture
def health_server():
    replies: list[tuple[int, dict]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = replies.pop(0) if len(replies) > 1 else replies[0]
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_port, replies
    server.shutdown()
    server.server_close()


def test_waiting_goes_on_until_the_new_version_answers(health_server, monkeypatch):
    port, replies = health_server
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    replies += [(200, {"status": "ok", "version": "1.0.0"}), (200, {"status": "ok", "version": "2.0.0"})]
    assert cli._wait_for_server(port, "2.0.0", 5) is None


def test_a_503_is_the_servers_own_explanation(health_server):
    port, replies = health_server
    replies.append((503, {"status": "error", "version": "2.0.0", "detail": "The disk is full."}))
    assert cli._wait_for_server(port, "2.0.0", 5) == "The disk is full."


def test_a_server_that_never_answers_runs_out_of_time(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert "didn't answer" in cli._wait_for_server(port, "2.0.0", 0.05)


# ---------------------------------------------------------------------------
# Tidying up
# ---------------------------------------------------------------------------

def test_deleting_someone_removes_them_from_the_kept_copies(client):
    other = client.post("/api/profiles", json={"name": "Sam"}).json()["id"]
    folder = config.db_path().parent / "pre-upgrade"
    folder.mkdir()
    (folder / "syntropy-v13-20260101-000000.db").write_bytes(b"has Sam in it")
    assert client.delete(f"/api/profiles/{other}").status_code == 200
    assert not folder.exists()


def test_deleting_someones_data_removes_it_from_the_kept_copies(client):
    prof = client.get("/api/profiles").json()["profiles"][0]["id"]
    folder = config.db_path().parent / "pre-upgrade"
    folder.mkdir()
    (folder / "syntropy-v13-20260101-000000.db").write_bytes(b"x")
    assert client.delete(f"/api/profiles/{prof}/data").status_code == 200
    assert not folder.exists()


def test_a_deleted_persons_settings_go_with_them(client):
    from app.core import settings
    from app.store import profiles
    other = client.post("/api/profiles", json={"name": "Sam"}).json()["id"]
    for key in (f"overview.layout.{other}.acc", f"alerts.seen.{other}.acc", f"alerts.dismissed.{other}.acc", f"goals.{other}"):
        settings.set(key, ["kept until now"])
    settings.set("overview.layout.someone-else.acc", ["stays"])
    profiles.delete_profile(other)     # by any caller, not only the API
    assert settings.get(f"overview.layout.{other}.acc") is None and settings.get(f"alerts.seen.{other}.acc") is None
    assert settings.get(f"alerts.dismissed.{other}.acc") is None and settings.get(f"goals.{other}") is None
    assert settings.get("overview.layout.someone-else.acc") == ["stays"]


def test_the_access_log_skips_routine_health_checks():
    import logging

    from app.main import _QuietHealthChecks
    quiet = _QuietHealthChecks()

    def record(*args):
        return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s %s %s %s %s", args, None)
    assert not quiet.filter(record("127.0.0.1:5000", "GET", "/health", "1.1", 200))
    assert quiet.filter(record("127.0.0.1:5000", "GET", "/health", "1.1", 503))
    assert quiet.filter(record("10.0.0.5:5000", "GET", "/health", "1.1", 200))
    assert quiet.filter(record("127.0.0.1:5000", "GET", "/api/status", "1.1", 200))
