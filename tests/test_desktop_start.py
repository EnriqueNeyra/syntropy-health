"""The whole Mac app starting up (tests/desktop_app_harness.py): what a person sees when it can't, and the way out."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

if sys.platform != "darwin":
    pytest.skip("the Mac app's window", allow_module_level=True)
pytest.importorskip("webview")
pytest.importorskip("WebKit")

HARNESS = Path(__file__).with_name("desktop_app_harness.py")


def _run(scenario: str, folder: Path, seconds: int = 200) -> dict:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    data = folder / "data"
    data.mkdir(exist_ok=True)
    (data / "desktop.json").write_text(json.dumps({"port": port}))
    out = folder / "result.json"
    env = {**os.environ, "SYNTROPY_DATA_DIR": str(data), "SYNTROPY_SCHEDULER": "false", "SYNTROPY_MDNS": "0",
           "SYNTROPY_UPDATE_CHECK": "false", "SYNTROPY_NO_DOTENV": "1", "PYWEBVIEW_LOG": "ERROR"}
    proc = subprocess.run([sys.executable, str(HARNESS), scenario, str(out)], capture_output=True, text=True, timeout=seconds,
                          check=False, env=env, cwd=folder)
    assert out.exists(), f"The app's test run ended early:\n{proc.stdout}\n{proc.stderr}\n{_log(data)}"
    return json.loads(out.read_text())


def _log(data: Path) -> str:
    log = data / "desktop.log"
    return log.read_text() if log.exists() else ""


def _good_database(path: Path) -> None:
    """As an older version would have left it: a backup can be from long ago, and needs no key to open."""
    from app.core import db
    os.environ["SYNTROPY_DATABASE_ENCRYPTION"] = "0"
    try:
        db.ensure_migrated(path)
    finally:
        del os.environ["SYNTROPY_DATABASE_ENCRYPTION"]
    db.reset_migration_cache()


def test_the_window_opens_the_app_when_all_is_well(tmp_path):
    seen = _run("normal", tmp_path)
    assert seen["first"]["url"].startswith(seen["own_home"])
    assert seen["first"]["away"] is False and seen["health"] == "ok"


@pytest.mark.parametrize("scenario", ["damaged", "damaged-at-login"])
def test_a_damaged_database_says_so_in_the_window_and_a_backup_gets_it_going(tmp_path, scenario):
    # Opened at login there's no window to see, so this has to bring one up.
    data = tmp_path / "data"
    data.mkdir()
    (data / "syntropy.db").write_bytes(os.urandom(200_000))
    (tmp_path / "not-a-backup.db").write_bytes(b"not a database at all" * 100)
    _good_database(tmp_path / "good-backup.db")

    seen = _run(scenario, tmp_path)
    first = seen["first"]
    assert first["visible"] is True
    assert "damaged" in first["text"] and str(data) in first["text"]
    assert first["away"] is False
    assert {"Try Again", "Restore from a Backup…", "Set It Aside and Start Fresh…", "Show Data Folder", "Quit"} <= set(seen["buttons"])
    assert seen["health"] == 503

    # A bad backup changes nothing and says why; the damaged file stays where it was until a good one replaces it.
    assert "backup" in seen["messages"][0][1] and "Nothing has been changed" in seen["messages"][0][1]
    assert "damaged" in seen["after_bad_backup"]["text"]

    assert seen["health_after"] == "ok"
    assert seen["after_good_backup"]["url"].startswith("http://localhost")
    kept = list(data.glob("set-aside-*/syntropy.db"))
    assert len(kept) == 1 and kept[0].stat().st_size == 200_000
    sqlite3.connect(data / "syntropy.db").execute("SELECT count(*) FROM profiles").fetchone()


def test_starting_fresh_keeps_the_damaged_file(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "syntropy.db").write_bytes(os.urandom(200_000))
    seen = _run("fresh", tmp_path)
    assert seen["health_after"] == "ok"
    assert seen["after_fresh"]["url"].startswith("http://localhost")
    kept = list(data.glob("set-aside-*/syntropy.db"))
    assert len(kept) == 1 and kept[0].stat().st_size == 200_000


def test_quitting_stays_responsive_and_ends(tmp_path):
    # Cmd-Q waited for the server on the main thread: a beachball for up to 15 seconds.
    seen = _run("quit", tmp_path, seconds=25)      # the harness waits 30 seconds unless the app really ends
    assert seen["main_thread_free_while_stopping"] is True


def test_the_window_says_its_starting_during_a_long_start(tmp_path):
    # The first start after an update copies and upgrades the database first: minutes, with several GB.
    seen = _run("slow", tmp_path)
    assert "Starting Syntropy Health" in seen["while_starting"]["text"]
    assert seen["while_starting"]["visible"] is True
    assert seen["first"]["url"].startswith("http://localhost") and seen["health"] == "ok"
