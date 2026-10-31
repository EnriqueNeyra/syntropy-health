"""Noticing trouble while the server runs: the write-ahead log's size, /health, damage to the file, and backups."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import config, db, maintenance, settings
from tests.conftest import BASE, LOCAL_CLIENT


def _wal_bytes(path: Path) -> int:
    wal = Path(f"{path}-wal")
    return wal.stat().st_size if wal.exists() else 0


def _grow_wal(path: Path, rows: int = 2000) -> None:
    """Writes that stay in the log: a reader's snapshot stops the automatic checkpoint from resetting it, and a kept
    connection (as the server has) stops the last one to close from cleaning up."""
    with db.read() as kept:
        kept.execute("SELECT 1").fetchone()
    reader = sqlite3.connect(str(path), isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM app_settings").fetchone()
    with db.db() as conn:
        conn.executemany("INSERT OR REPLACE INTO app_settings(key, value, updated_at) VALUES (?, ?, 0)",
                         ((f"filler.{i}", '"' + "x" * 2000 + '"') for i in range(rows)))
    reader.execute("ROLLBACK")
    reader.close()


# ---------------------------------------------------------------------------
# The write-ahead log
# ---------------------------------------------------------------------------

def test_connections_limit_the_log_size():
    db.ensure_migrated()
    with db.db() as conn:
        assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == db.WAL_LIMIT
    with db.read() as conn:
        assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == db.WAL_LIMIT


def test_a_large_log_is_truncated(monkeypatch):
    db.ensure_migrated()
    path = config.db_path()
    _grow_wal(path)
    grown = _wal_bytes(path)
    assert grown > 1_000_000
    monkeypatch.setattr(db, "WAL_LIMIT", 10 * 1024 * 1024)
    assert db.checkpoint_if_large(path) is False        # under the limit: left alone
    assert _wal_bytes(path) == grown
    monkeypatch.setattr(db, "WAL_LIMIT", 1024 * 1024)
    assert db.checkpoint_if_large(path) is True
    assert _wal_bytes(path) == 0
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM app_settings WHERE key LIKE 'filler.%'").fetchone()[0] == 2000


def test_a_checkpoint_gives_way_to_a_writer():
    db.ensure_migrated()
    path = config.db_path()
    _grow_wal(path)
    writer = sqlite3.connect(str(path), isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    assert db.checkpoint(path, wait_seconds=0.1) is False
    assert time.monotonic() - started < 2
    writer.execute("ROLLBACK")
    writer.close()
    assert db.checkpoint(path) is True and _wal_bytes(path) == 0


def test_the_background_thread_shrinks_the_log_and_shutdown_leaves_none(monkeypatch):
    monkeypatch.setattr(maintenance, "TICK_SECONDS", 0.02)
    monkeypatch.setattr(db, "WAL_LIMIT", 1024 * 1024)
    db.ensure_migrated()
    path = config.db_path()
    maintenance.start()
    try:
        _grow_wal(path)
        deadline = time.monotonic() + 5
        while _wal_bytes(path) > db.WAL_LIMIT and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _wal_bytes(path) <= db.WAL_LIMIT
        with db.db() as conn:
            conn.execute("INSERT OR REPLACE INTO app_settings(key, value, updated_at) VALUES ('last', '1', 0)")
    finally:
        maintenance.stop()
    assert _wal_bytes(path) == 0


def test_writers_during_checkpoints_never_fail(monkeypatch):
    """A phone's upload, a journal entry and page loads at once, with the log kept small."""
    monkeypatch.setattr(maintenance, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(db, "WAL_LIMIT", 256 * 1024)
    db.ensure_migrated()
    errors: list[Exception] = []
    stop = threading.Event()

    def write():
        try:
            for i in range(60):
                with db.db() as conn:
                    conn.executemany("INSERT OR REPLACE INTO app_settings(key, value, updated_at) VALUES (?, ?, 0)",
                                     ((f"w.{threading.get_ident()}.{i}.{j}", '"' + "x" * 500 + '"') for j in range(50)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def look():
        try:
            while not stop.is_set():
                with db.read() as conn:
                    conn.execute("SELECT COUNT(*) FROM app_settings").fetchone()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    maintenance.start()
    try:
        writers = [threading.Thread(target=write) for _ in range(3)]
        reader = threading.Thread(target=look)
        reader.start()
        for w in writers:
            w.start()
        for w in writers:
            w.join()
        stop.set()
        reader.join()
    finally:
        maintenance.stop()
    assert not errors


def test_the_server_stops_cleanly_with_no_log_left(raw_client):
    path = config.db_path()
    raw_client.__exit__(None, None, None)
    assert _wal_bytes(path) == 0
    raw_client.__enter__()


def test_writes_are_durable_and_reads_stay_light():
    db.ensure_migrated()
    conn = db._connect(config.db_path())
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    conn.close()
    with db.db() as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2      # FULL: an acknowledged upload survives a power cut
    with db.read() as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_a_big_write_empties_the_log_itself(monkeypatch):
    monkeypatch.setattr(db, "WAL_LIMIT", 1024 * 1024)
    db.ensure_migrated()
    path = config.db_path()
    with db.read() as kept:
        kept.execute("SELECT 1").fetchone()
    with db.db() as conn:
        conn.executemany("INSERT OR REPLACE INTO app_settings(key, value, updated_at) VALUES (?, ?, 0)",
                         ((f"filler.{i}", '"' + "x" * 2000 + '"') for i in range(2000)))
    assert _wal_bytes(path) == 0


def test_a_small_write_never_waits_for_a_checkpoint(monkeypatch):
    db.ensure_migrated()
    monkeypatch.setattr(db, "checkpoint_if_large", lambda *a, **k: pytest.fail("not for a small write"))
    with db.db() as conn:
        conn.execute("INSERT OR REPLACE INTO app_settings(key, value, updated_at) VALUES ('a', '1', 0)")


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

def test_health_keeps_its_fields_and_reports_the_database(raw_client):
    r = raw_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["service"] == "syntropy-health" and body["version"] == config.APP_VERSION
    assert body["journal_mode"] == "wal" and body["database"] == "ok" and body["schema_version"] == db.newest_schema()
    assert raw_client.head("/health").status_code == 200


def test_a_long_write_is_busy_not_broken(raw_client):
    conn = sqlite3.connect(str(config.db_path()), isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    r = raw_client.get("/health")
    assert time.monotonic() - started < 2
    conn.execute("ROLLBACK")
    conn.close()
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["database"] == "busy"


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="file permissions")
def test_health_fails_when_the_files_cannot_be_written(raw_client):
    path = config.db_path()
    assert raw_client.get("/health").status_code == 200
    files = [p for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")) if p.exists()]
    for f in files:
        f.chmod(0o400)
    path.parent.chmod(0o500)
    try:
        r = raw_client.get("/health")
    finally:
        path.parent.chmod(0o700)
        for f in files:
            f.chmod(0o600)
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "error" and body["database"] == "read-only" and "write" in body["detail"]
    assert body["service"] == "syntropy-health" and body["version"] == config.APP_VERSION
    assert raw_client.get("/health").status_code == 200       # and it recovers by itself


def test_health_fails_on_a_read_only_mount(raw_client, monkeypatch):
    path = config.db_path()
    real = sqlite3.connect

    def read_only(target, *a, **k):      # SQLite treats BEGIN IMMEDIATE on such a file as a plain read
        if path.name in str(target):
            return real(f"file:{path}?mode=ro", *a, **{**k, "uri": True})
        return real(target, *a, **k)
    monkeypatch.setattr(db.sqlite3, "connect", read_only)
    r = raw_client.get("/health")
    assert r.status_code == 503 and r.json()["database"] == "read-only"


def test_health_fails_when_the_disk_is_full(raw_client, monkeypatch):
    monkeypatch.setattr(maintenance.shutil, "disk_usage", lambda p: type("U", (), {"free": 1000})())
    r = raw_client.get("/health")
    assert r.status_code == 503 and r.json()["database"] == "disk-full" and "disk" in r.json()["detail"]


def test_health_fails_when_the_database_changed_underneath(raw_client):
    conn = sqlite3.connect(str(config.db_path()))
    conn.execute("PRAGMA user_version = 999")
    conn.close()
    r = raw_client.get("/health")
    assert r.status_code == 503 and "newer version" in r.json()["detail"]


def test_owners_get_the_details(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["database"]["schema_version"] == db.newest_schema()
    assert body["database"]["size_bytes"] > 0 and body["disk"]["free_bytes"] > 0
    assert body["database"]["pre_upgrade_copies"] == []
    assert "last_tick_at" in body["scheduler"] and "data_fixes" in body


def test_only_owners_get_the_details(raw_client):
    from tests.test_accounts import _join, _person, _signed_in_owner

    owner = _signed_in_owner(raw_client)
    theirs, _ = _join(owner, _person(owner, "Maria"))
    assert theirs.get("/api/health").status_code == 403
    assert owner.get("/api/health").status_code == 200


# ---------------------------------------------------------------------------
# Damage
# ---------------------------------------------------------------------------

def _damage(path: Path, table: str = "biometric_samples") -> None:
    """Overwrites the middle of a real table's pages, as a failing disk would."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO biometric_samples(id, profile_id, connection_id, metric_type, value, unit, start_date, "
                     "end_date, source_name, created_at) VALUES (?, 'p', 'c', 'heart_rate', 60, 'bpm', "
                     "'2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z', 'W', 1)",
                     ((uuid.uuid4().hex,) for _ in range(20000)))
    conn.execute("COMMIT")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    page = int(conn.execute("PRAGMA page_size").fetchone()[0])
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    conn.close()
    db.close_readers()
    with open(path, "r+b") as fh:
        for n in range(pages // 2, min(pages // 2 + 60, pages)):
            fh.seek(n * page)
            fh.write(os.urandom(page))


def test_the_check_finds_damage_and_says_so():
    db.ensure_migrated()
    path = config.db_path()
    assert maintenance.check_integrity(path)["ok"] is True
    _damage(path)
    found = maintenance.check_integrity(path)
    assert found["ok"] is False and found["detail"]
    assert maintenance.integrity(path)["ok"] is False
    problems = maintenance.problems()
    assert problems and "damage" in problems[0]["text"]


def test_a_damaged_database_at_start_up_is_reported_in_words(tmp_path):
    db.ensure_migrated()
    path = config.db_path()
    _damage(path)
    db.reset_migration_cache()
    from app.main import create_app
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT, raise_server_exceptions=False) as c:
        r = c.get("/health")
        assert r.status_code == 503 and "damage" in r.json()["detail"]


def _sqlite_error(kind: type, code: int, text: str) -> sqlite3.Error:
    exc = kind(text)
    exc.sqlite_errorcode = code
    return exc


def _not_raising(client) -> TestClient:
    c = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT, raise_server_exceptions=False)
    c.headers.update(client.headers)
    c.cookies.update(client.cookies)
    return c


@pytest.mark.parametrize("code, text", [(sqlite3.SQLITE_CORRUPT, "database disk image is malformed"),
                                        (sqlite3.SQLITE_NOTADB, "file is not a database")])
def test_a_read_that_hits_damage_says_so(client, monkeypatch, code, text):
    from app.api import system

    def malformed(*_a, **_k):
        raise _sqlite_error(sqlite3.DatabaseError, code, text)
    c = _not_raising(client)
    monkeypatch.setattr(system.accounts, "set_flags", malformed)
    r = c.post("/api/onboarding/done")
    assert r.status_code == 500 and "damage" in r.json()["detail"]
    assert c.get("/health").status_code == 503


def _broken_constraint(*_a, **_k):
    from app.store import connections
    connections.create("nobody-has-this-id", "wearable", "oura", "Oura")


def _mistaken_query(*_a, **_k):
    with db.read() as conn:
        conn.execute("SELECT ?")


def _too_big(*_a, **_k):
    raise _sqlite_error(sqlite3.DataError, sqlite3.SQLITE_TOOBIG, "string or blob too big")


def _internal(*_a, **_k):
    raise sqlite3.InternalError("something inside SQLite")


@pytest.mark.parametrize("bug", [_broken_constraint, _mistaken_query, _too_big, _internal])
def test_a_bug_is_not_called_damage(client, monkeypatch, bug):
    from app.api import system

    c = _not_raising(client)
    monkeypatch.setattr(system.accounts, "set_flags", bug)
    r = c.post("/api/onboarding/done")
    assert r.status_code == 500 and "damage" not in r.json()["detail"]
    assert c.get("/health").status_code == 200
    assert c.get("/api/status").json()["problems"] == []


def test_a_sign_in_finishing_for_someone_just_deleted_is_not_called_damage(client):
    from tests.conftest import simulated_login
    other = client.post("/api/profiles", json={"name": "Sam"}).json()["id"]
    r = client.post("/api/connections/ehr", json={"institution_id": "epic-duke-health", "profile_id": other})
    callback = simulated_login(client, r.json()["auth_url"], "alex.rivera")
    assert client.delete(f"/api/profiles/{other}").status_code == 200
    c = _not_raising(client)
    r = c.get(callback[len(BASE):], follow_redirects=False)
    assert "damage" not in r.text
    assert c.get("/health").status_code == 200
    assert c.get("/api/status").json()["problems"] == []


def test_only_damage_is_reported_as_damage_at_start_up():
    path = config.db_path()
    assert db.unusable(_sqlite_error(sqlite3.DatabaseError, sqlite3.SQLITE_NOTADB, "file is not a database"), path).kind == "damaged"
    for exc in (_sqlite_error(sqlite3.IntegrityError, sqlite3.SQLITE_CONSTRAINT, "FOREIGN KEY constraint failed"),
                _sqlite_error(sqlite3.OperationalError, sqlite3.SQLITE_CANTOPEN, "unable to open database file"),
                sqlite3.ProgrammingError("a mistake")):
        problem = db.unusable(exc, path)
        assert problem.kind == "unavailable" and "is damaged" not in str(problem)


@pytest.mark.parametrize("code, text", [(sqlite3.SQLITE_FULL, "database or disk is full"),
                                        (sqlite3.SQLITE_IOERR, "disk I/O error"),
                                        (sqlite3.SQLITE_CANTOPEN, "unable to open database file")])
def test_a_check_that_could_not_run_finds_no_damage(monkeypatch, code, text):
    db.ensure_migrated()
    path = config.db_path()
    real = sqlite3.connect

    class Stopped(sqlite3.Connection):
        def execute(self, sql, *a):
            if "quick_check" in sql:
                raise _sqlite_error(sqlite3.OperationalError, code, text)
            return super().execute(sql, *a)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: real(*a, factory=Stopped, **k))
    assert maintenance.check_integrity(path)["ok"] is not False
    assert maintenance.health(path)["detail"] is None and maintenance.problems() == []


def test_a_probe_failure_that_is_not_damage_is_reported_as_what_it_is(monkeypatch):
    db.ensure_migrated()
    path = config.db_path()

    def stuck(_path):
        raise _sqlite_error(sqlite3.OperationalError, sqlite3.SQLITE_IOERR, "disk I/O error")
    monkeypatch.setattr(db, "probe", stuck)
    state = maintenance.health(path)
    assert state["database"] == "unavailable" and "disk I/O error" in state["detail"] and "damage" not in state["detail"]


def test_a_data_folder_moved_away_is_not_called_damage_and_no_empty_database_appears(raw_client):
    path = config.db_path()
    folder = path.parent
    moved = folder.with_name(folder.name + "-moved")
    with db.db() as conn:
        conn.execute("INSERT OR REPLACE INTO app_settings(key, value, updated_at) VALUES ('mine', '1', 0)")
    folder.rename(moved)
    try:
        r = raw_client.get("/health")
        assert r.status_code == 503 and r.json()["database"] == "unavailable" and "damage" not in r.json()["detail"]
        assert not path.exists()
        with pytest.raises(sqlite3.OperationalError):
            with db.db():
                pass
        with pytest.raises(sqlite3.OperationalError):
            db.snapshot(moved / "copy.db", path)
        assert maintenance.check_integrity(path)["ok"] is not False
        assert db.checkpoint(path) is False
        assert not path.exists() and maintenance.problems() == []
    finally:
        for leftover in (folder.glob("*") if folder.exists() else []):
            leftover.unlink()
        if folder.exists():
            folder.rmdir()
        moved.rename(folder)
    assert raw_client.get("/health").status_code == 200       # and it recovers once the folder is back


def test_a_folder_that_cannot_be_made_is_explained(monkeypatch):
    def refused():
        raise PermissionError(13, "Permission denied", "/data")
    monkeypatch.setattr(config, "db_path", refused)
    state = maintenance.health()
    assert state["database"] == "unavailable" and "data folder" in state["detail"]


def test_requests_explain_a_database_that_cannot_be_reached(client, monkeypatch):
    from app.api import system

    def gone(*_a, **_k):
        raise _sqlite_error(sqlite3.OperationalError, sqlite3.SQLITE_CANTOPEN, "unable to open database file")
    c = _not_raising(client)
    monkeypatch.setattr(system.accounts, "set_flags", gone)
    r = c.post("/api/onboarding/done")
    assert r.status_code == 503 and "folder" in r.json()["detail"] and "damage" not in r.json()["detail"]
    assert c.get("/health").status_code == 200


def test_every_way_of_emptying_the_log_reaches_the_disk(monkeypatch):
    """Without fullfsync a Mac's plain sync stops at the drive's cache, and truncating drops the log that held the only
    durable copy of an acknowledged upload."""
    db.ensure_migrated()
    path = config.db_path()
    seen = []
    real = db.sqlite3.connect

    class Spy(db.sqlite3.Connection):
        def execute(self, sql, *a):
            if "wal_checkpoint(TRUNCATE)" in sql:
                seen.append(super().execute("PRAGMA fullfsync").fetchone()[0])
            if "fullfsync = OFF" in sql:      # only conftest does this, for speed
                sql = "SELECT 1"
            return super().execute(sql, *a)

        def close(self):
            seen.append(super().execute("PRAGMA fullfsync").fetchone()[0])     # the last connection to close checkpoints too
            super().close()
    monkeypatch.setattr(db.sqlite3, "connect", lambda *a, **k: real(*a, factory=Spy, **k))
    db.close_readers()
    assert db.checkpoint(path) is True
    db.probe(path)
    maintenance._optimize(path)
    maintenance.check_integrity(path)
    with db.read():
        pass
    db.close_readers()
    with db.db():
        pass
    assert seen and all(v == 1 for v in seen)


def test_a_busy_database_is_not_called_a_bug(client):
    c = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT, raise_server_exceptions=False)
    c.headers.update(client.headers)
    c.cookies.update(client.cookies)
    from app.api import system

    def locked(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")
    real = system.accounts.set_flags
    system.accounts.set_flags = locked
    try:
        r = c.post("/api/onboarding/done")
    finally:
        system.accounts.set_flags = real
    assert r.status_code == 503 and "busy" in r.json()["detail"]


def test_a_backup_of_a_damaged_database_is_refused(client):
    path = config.db_path()
    _damage(path)
    c = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT, raise_server_exceptions=False)
    c.headers.update(client.headers)
    c.cookies.update(client.cookies)
    r = c.post("/api/backups", json={"unencrypted": True})
    assert r.status_code == 503 and "damage" in r.json()["detail"]
    assert maintenance.integrity(path)["ok"] is False


def test_a_data_folder_that_cannot_be_used_is_explained(monkeypatch):
    import app.main as main

    def no_folder():
        raise PermissionError(13, "Permission denied", "/data")
    monkeypatch.setattr(main, "_start_up", no_folder)
    with TestClient(main.create_app(), base_url=BASE, client=LOCAL_CLIENT) as c:
        r = c.get("/health")
        assert r.status_code == 503 and "data folder" in r.json()["detail"]


def test_a_good_backup_is_served(client):
    made = client.post("/api/backups", json={"unencrypted": True}).json()
    r = client.get(made["download"])
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_the_command_line_backup_refuses_a_damaged_database(tmp_path):
    from app import cli
    db.ensure_migrated()
    _damage(config.db_path())
    out = tmp_path / "backups"
    with pytest.raises(SystemExit) as caught:
        cli.main(["backup", str(out), "--no-encrypt"])
    assert "damage" in str(caught.value.code)
    assert not list(out.glob("*.zip")) if out.exists() else True


def test_the_command_line_backup_does_not_make_an_empty_database_to_back_up(tmp_path):
    from app import cli
    out = tmp_path / "backups"
    with pytest.raises(SystemExit) as caught:
        cli.main(["backup", str(out)])
    assert "couldn't use its database" in str(caught.value.code)
    assert not config.db_path().exists() and not list(out.glob("*.zip"))


def test_a_healthy_database_is_only_checked_daily(monkeypatch):
    db.ensure_migrated()
    path = config.db_path()
    assert maintenance.integrity_due(path) is True
    maintenance.check_integrity(path)
    assert maintenance.integrity_due(path) is False
    real = time.time
    monkeypatch.setattr(maintenance.time, "time", lambda: real() + maintenance.INTEGRITY_EVERY + 1)
    assert maintenance.integrity_due(path) is True


def test_damage_found_earlier_counts_until_a_large_file_is_checked_again(monkeypatch):
    db.ensure_migrated()
    path = config.db_path()
    settings.set(maintenance._INTEGRITY_KEY, {"ok": False, "checked_at": time.time(), "detail": "Tree 28 page 277"})
    monkeypatch.setattr(maintenance, "SMALL_FILE", 0)
    assert maintenance.integrity_due(path) is True
    maintenance.check_at_start_up(path)
    assert "Tree 28" in maintenance.health(path)["detail"]
    assert maintenance.check_integrity(path)["ok"] is True      # the file was repaired or restored
    assert maintenance.health(path)["detail"] is None


def test_a_large_file_is_checked_in_the_background_not_at_start_up(monkeypatch):
    db.ensure_migrated()
    checked = []
    monkeypatch.setattr(maintenance, "SMALL_FILE", 0)
    monkeypatch.setattr(maintenance, "check_integrity", lambda p=None: checked.append(p) or {"ok": True})
    maintenance.check_at_start_up()
    assert not checked



# ---------------------------------------------------------------------------
# What a stranger on the network can learn
# ---------------------------------------------------------------------------

ELSEWHERE = ("192.168.1.20", 50000)


def _visit_all(app, client_address) -> dict:
    c = TestClient(app, base_url=BASE, client=client_address, raise_server_exceptions=False)
    return {url: c.get(url) for url in ("/health", "/api/status", "/mcp", "/")}


def test_a_garbage_database_shows_paths_only_on_this_computer():
    from app.main import create_app
    folder = str(config.data_dir())
    config.db_path().write_bytes(b"this is not a database, " * 400)
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT) as c:
        here = _visit_all(c.app, LOCAL_CLIENT)
        away = _visit_all(c.app, ELSEWHERE)
    assert folder in here["/health"].json()["detail"] and folder in here["/"].text
    for url, r in away.items():
        assert r.status_code == 503, url
        assert folder not in r.text and "syntropy.db" not in r.text, url
    assert away["/health"].json()["status"] == "error" and away["/health"].json()["detail"]
    assert "that computer" in away["/"].text and "that computer" in away["/api/status"].json()["detail"]


def test_a_key_problem_shows_its_path_only_on_this_computer():
    from app.core import security
    from app.main import create_app
    from app.store import connections, profiles
    connections.create(profiles.default_profile()["id"], "wearable", "oura", "Oura", credentials={"access_token": "a"})
    security._key_cache.clear()
    (config.data_dir() / "secret.key").write_bytes(b"")
    folder = str(config.data_dir())
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT) as c:
        assert folder in _visit_all(c.app, LOCAL_CLIENT)["/health"].json()["detail"]
        for url, r in _visit_all(c.app, ELSEWHERE).items():
            assert r.status_code == 503 and folder not in r.text and "secret.key" not in r.text, url


def test_trouble_while_running_shows_paths_only_on_this_computer(raw_client, monkeypatch, caplog):
    folder = str(config.data_dir())
    monkeypatch.setattr(maintenance.shutil, "disk_usage", lambda p: type("U", (), {"free": 1000})())
    away = TestClient(raw_client.app, base_url=BASE, client=ELSEWHERE)
    with caplog.at_level("ERROR", logger="syntropy.maintenance"):
        r = away.get("/health")
        away.get("/health")
    assert r.status_code == 503 and r.json()["database"] == "disk-full" and folder not in r.text
    assert len([m for m in caplog.messages if folder in m]) == 1      # the log has it, once, for whoever can read the log
    assert folder in raw_client.get("/health").json()["detail"]


def test_an_owner_elsewhere_still_gets_the_details(client):
    owner_elsewhere = TestClient(client.app, base_url=BASE, client=ELSEWHERE)
    owner_elsewhere.headers.update(client.headers)
    owner_elsewhere.cookies.update(client.cookies)
    assert str(config.data_dir()) in owner_elsewhere.get("/api/health").json()["database"]["path"]


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

def _plan(sql: str, params: tuple) -> str:
    with db.read() as conn:
        return " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_newest_samples_use_an_index_instead_of_sorting_them_all():
    from app.store import biometrics, profiles
    prof = profiles.default_profile()["id"]
    plan = _plan("SELECT id FROM biometric_samples WHERE profile_id = ? ORDER BY start_date DESC LIMIT ?", (prof, 100))
    assert "idx_samples_profile_start" in plan and "TEMP B-TREE" not in plan
    assert biometrics.recent_samples(prof) == []
    with db.read() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'idx_samples_start'").fetchone()


@pytest.mark.parametrize("stats", [False, True])
def test_a_few_metrics_over_years_still_read_only_those_metrics(stats):
    """The date index mustn't tempt the planner to walk through every sample in the range, as with sleep over years."""
    from app.store import profiles
    prof = profiles.default_profile()["id"]
    if stats:
        with db.db() as conn:
            conn.executemany("INSERT INTO biometric_samples(id, profile_id, metric_type, value, unit, start_date, end_date, created_at) "
                             "VALUES (?, ?, ?, 1, '', ?, ?, 0)",
                             ((uuid.uuid4().hex, prof, f"m{i % 20}", f"2025-01-{i % 28 + 1:02d}", f"2025-01-{i % 28 + 1:02d}") for i in range(2000)))
            conn.execute("ANALYZE")
        db.close_readers()
    plan = _plan("SELECT value FROM biometric_samples WHERE profile_id = ? AND metric_type IN (?, ?, ?, ?, ?) AND start_date >= ? AND start_date < ?",
                 (prof, "a", "b", "c", "d", "e", "2020-01-01", "2026-01-01"))
    assert "idx_samples_profile_metric" in plan, plan
    plan = _plan("SELECT value FROM biometric_samples WHERE profile_id = ? AND metric_type IN (?, ?) AND start_date >= ? AND start_date < ?",
                 (prof, "a", "b", "2020-01-01", "2026-01-01"))
    assert "idx_samples_profile_metric" in plan, plan


def test_one_tests_results_read_from_an_index_in_date_order():
    from app.store import profiles
    prof = profiles.default_profile()["id"]
    plan = _plan("SELECT id FROM clinical_records WHERE profile_id = ? AND code = ? AND category = ? ORDER BY effective_at",
                 (prof, "4548-4", "labs"))
    assert "idx_records_code_date" in plan and "TEMP B-TREE" not in plan
