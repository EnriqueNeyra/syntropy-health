"""What protects the one database file: the copy before an upgrade, refusing what can't be used safely, and telling the
person what happened in words."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import BASE, LOCAL_CLIENT


def _build(path: Path, version: int) -> sqlite3.Connection:
    """A database as version ``version`` of the schema left it."""
    from app.core import db
    conn = sqlite3.connect(str(path), isolation_level=None)
    for f in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
        if int(f.name.split("_", 1)[0]) > version:
            break
        for statement in db._split_sql(f.read_text(encoding="utf-8")):
            conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {version}")
    return conn


def _old_database(version: int = 13) -> Path:
    from app.core import config, db
    path = config.db_path()
    conn = _build(path, version)
    conn.execute("INSERT INTO app_settings(key, value, updated_at) VALUES ('kept', '\"yes\"', 0)")
    conn.close()
    db.reset_migration_cache()
    return path


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def _copies(path: Path) -> list[Path]:
    return sorted((path.parent / "pre-upgrade").glob("*.db"))


def _newest_known() -> int:
    from app.core import db
    return max(int(f.name.split("_", 1)[0]) for f in db.MIGRATIONS_DIR.glob("*.sql"))


# ---------------------------------------------------------------------------
# The copy before an upgrade
# ---------------------------------------------------------------------------

def test_upgrading_keeps_a_copy_of_the_database_as_it_was():
    from app.core import db
    path = _old_database(13)
    db.ensure_migrated(path)
    assert _user_version(path) == _newest_known()
    copies = _copies(path)
    assert len(copies) == 1 and copies[0].name.startswith("syntropy-v13-")
    conn = sqlite3.connect(str(copies[0]))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
    assert conn.execute("SELECT value FROM app_settings WHERE key = 'kept'").fetchone()[0] == '"yes"'
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()
    assert not [p.name for p in path.parent.joinpath("pre-upgrade").iterdir() if not p.name.endswith(".db")]
    if os.name != "nt":
        assert stat.S_IMODE(copies[0].stat().st_mode) == 0o600
        assert stat.S_IMODE(copies[0].parent.stat().st_mode) == 0o700


def test_no_copy_for_a_new_database_or_when_nothing_is_pending():
    from app.core import config, db
    db.ensure_migrated()
    path = config.db_path()
    assert not (path.parent / "pre-upgrade").exists()
    db.reset_migration_cache()
    db.ensure_migrated(path)
    assert not (path.parent / "pre-upgrade").exists()


def test_the_newest_copy_of_the_last_three_versions_is_kept():
    from app.core import db
    path = _old_database(13)
    folder = path.parent / "pre-upgrade"
    folder.mkdir()
    for name in ("syntropy-v9-20260101-000000.db", "syntropy-v10-20260201-000000.db", "syntropy-v11-20260301-000000.db",
                 "syntropy-v11-20260302-000000.db", "syntropy-v12-20260401-000000.db", "syntropy-v13-20260501-000000.db"):
        (folder / name).write_bytes(b"x")
    (folder / "syntropy-v13-20260501-000000.db.tmp").write_bytes(b"left by a crash")
    db.ensure_migrated(path)
    names = sorted(p.name for p in folder.iterdir())
    assert len(names) == 3
    assert [n for n in names if n.startswith("syntropy-v13-") and n != "syntropy-v13-20260501-000000.db"]
    assert "syntropy-v12-20260401-000000.db" in names and "syntropy-v11-20260302-000000.db" in names


def test_without_room_for_the_copy_nothing_is_upgraded(monkeypatch):
    from app.core import db
    path = _old_database(13)
    monkeypatch.setattr(db.shutil, "disk_usage", lambda p: type("U", (), {"free": 4096, "total": 1, "used": 1})())
    with pytest.raises(db.StartupProblem) as caught:
        db.ensure_migrated(path)
    assert _user_version(path) == 13
    assert "room" in str(caught.value) and "Nothing has been changed" in str(caught.value)
    assert not _copies(path)


def test_a_failed_copy_stops_the_upgrade(monkeypatch):
    from app.core import db
    path = _old_database(13)

    def full(*_a, **_k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(db, "_save_copy", full)
    with pytest.raises(db.StartupProblem) as caught:
        db.ensure_migrated(path)
    assert _user_version(path) == 13
    assert "Nothing has been changed" in str(caught.value)


def test_a_failing_migration_leaves_the_database_as_it_was(monkeypatch, tmp_path):
    from app.core import db
    path = _old_database(13)
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for f in db.MIGRATIONS_DIR.glob("*.sql"):
        (migrations / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    (migrations / "900_good.sql").write_text("CREATE TABLE from_900(id TEXT);\n")
    (migrations / "901_bad.sql").write_text("CREATE TABLE from_901(id TEXT);\nINSERT INTO no_such_table VALUES (1);\n")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    with pytest.raises(db.StartupProblem) as caught:
        db.ensure_migrated(path)
    assert _user_version(path) == 13
    conn = sqlite3.connect(str(path))
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name IN ('from_900', 'from_901')").fetchall()
    conn.close()
    assert "unchanged" in str(caught.value) and str(_copies(path)[0]) in str(caught.value)


# ---------------------------------------------------------------------------
# A database this build doesn't know
# ---------------------------------------------------------------------------

@pytest.fixture
def too_new_database():
    from app.core import config, db
    db.ensure_migrated()
    path = config.db_path()
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA user_version = 999")
    conn.close()
    db.reset_migration_cache()
    return path


def test_a_newer_database_is_refused(too_new_database):
    from app.core import db
    with pytest.raises(db.StartupProblem) as caught:
        db.ensure_migrated(too_new_database)
    assert caught.value.kind == "too_new"
    assert "newer version" in str(caught.value)
    assert _user_version(too_new_database) == 999 and not _copies(too_new_database)


def test_the_server_explains_instead_of_running_on_a_newer_database(too_new_database):
    from app.main import create_app
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT) as c:
        health = c.get("/health")
        assert health.status_code == 503
        body = health.json()
        assert body["status"] == "error" and body["service"] == "syntropy-health" and body["version"]
        assert "newer version" in body["detail"]
        api = c.get("/api/status")
        assert api.status_code == 503 and "newer version" in api.json()["detail"]
        page = c.get("/")
        assert page.status_code == 503 and "text/html" in page.headers["content-type"] and "newer version" in page.text
        assert "<script" not in page.text and page.headers.get("content-security-policy")
    assert _user_version(too_new_database) == 999


def test_a_file_that_is_not_a_database_is_left_alone_and_explained():
    from app.core import config, db
    path = config.db_path()
    path.write_bytes(b"this is not a database at all, and never was" * 200)
    before = path.read_bytes()
    with pytest.raises(db.StartupProblem) as caught:
        db.ensure_migrated(path)
    assert caught.value.kind == "damaged"
    assert str(path) in str(caught.value)
    db.reset_migration_cache()
    from app.main import create_app
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT) as c:
        assert c.get("/health").status_code == 503
        assert "damaged" in c.get("/api/status").json()["detail"]
    assert path.read_bytes() == before


def test_the_command_line_says_it_in_words(too_new_database):
    from app import cli
    with pytest.raises(SystemExit) as caught:
        cli.main(["accounts"])
    assert "newer version" in str(caught.value.code)


# ---------------------------------------------------------------------------
# Two processes at once
# ---------------------------------------------------------------------------

def test_processes_upgrading_at_once_all_succeed(tmp_path):
    """The server and an AI app's MCP server can start together right after an update."""
    from app.core import config, db
    path = config.db_path()
    conn = _build(path, 3)
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO biometric_samples(id, profile_id, connection_id, metric_type, value, unit, start_date, "
                     "end_date, source_name, created_at) VALUES (?, 'p', 'c', 'heart_rate', 60, 'bpm', "
                     "'2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z', 'W', 1)", ((f"s{i}",) for i in range(40_000)))
    conn.execute("COMMIT")
    conn.close()
    start = tmp_path / "go"
    child = textwrap.dedent(f"""
        import pathlib, sys, time
        while not pathlib.Path({str(start)!r}).exists():
            time.sleep(0.002)
        from app.core import db
        db.ensure_migrated(pathlib.Path({str(path)!r}))
        print("ok")
    """)
    env = {**os.environ, "SYNTROPY_DATA_DIR": str(path.parent), "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    procs = [subprocess.Popen([sys.executable, "-c", child], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(4)]
    start.write_text("go")
    results = [p.communicate(timeout=120) for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0, 0], results
    assert _user_version(path) == _newest_known()
    assert len(_copies(path)) == 1      # one of them took it; the others found the work done


def _locked() -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError("database is locked")
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return exc


def test_turning_on_wal_waits_for_another_process(monkeypatch):
    from app.core import db
    calls = []

    class Busy:
        def execute(self, sql):
            calls.append(sql)
            if len(calls) <= 2:
                raise _locked()
            return type("R", (), {"fetchone": lambda self: ["delete"]})()
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    db._use_wal(Busy())
    assert calls[-1] == "PRAGMA journal_mode = WAL"


def test_status_lists_the_copies():
    from app.core import db
    path = _old_database(13)
    db.ensure_migrated(path)
    copies = db.pre_upgrade_copies(path)
    assert len(copies) == 1 and copies[0]["version"] == 13 and copies[0]["size_bytes"] > 0
    assert json.dumps(copies)
