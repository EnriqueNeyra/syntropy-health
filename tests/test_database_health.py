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

from app.core import config, db, maintenance
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
    with db.db() as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2      # FULL: an acknowledged upload survives a power cut
    with db.read() as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1


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
        return real(f"file:{path}?mode=ro", *a, uri=True, **k) if str(target) == str(path) else real(target, *a, **k)
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
    page = conn.execute("PRAGMA page_size").fetchone()[0]
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


def test_a_read_that_hits_damage_says_so(client, monkeypatch):
    from app.api import system

    def malformed(*_a, **_k):
        raise sqlite3.DatabaseError("database disk image is malformed")
    c = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT, raise_server_exceptions=False)
    c.headers.update(client.headers)
    c.cookies.update(client.cookies)
    monkeypatch.setattr(system.accounts, "set_flags", malformed)
    r = c.post("/api/onboarding/done")
    assert r.status_code == 500 and "damage" in r.json()["detail"]
    assert c.get("/health").status_code == 503


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
    r = c.get("/api/export/backup")
    assert r.status_code == 503 and "damage" in r.json()["detail"]
    assert maintenance.integrity(path)["ok"] is False


def test_a_good_backup_is_served(client):
    r = client.get("/api/export/backup")
    assert r.status_code == 200 and r.content[:15] == b"SQLite format 3"


def test_the_command_line_backup_refuses_a_damaged_database(tmp_path):
    from app import cli
    db.ensure_migrated()
    _damage(config.db_path())
    out = tmp_path / "backups"
    with pytest.raises(SystemExit) as caught:
        cli.main(["backup", str(out)])
    assert "damage" in str(caught.value.code)
    assert not list(out.glob("*.zip")) if out.exists() else True


def test_a_healthy_database_is_only_checked_daily(monkeypatch):
    db.ensure_migrated()
    path = config.db_path()
    assert maintenance.integrity_due(path) is True
    maintenance.check_integrity(path)
    assert maintenance.integrity_due(path) is False
    real = time.time
    monkeypatch.setattr(maintenance.time, "time", lambda: real() + maintenance.INTEGRITY_EVERY + 1)
    assert maintenance.integrity_due(path) is True


def test_a_large_file_is_checked_in_the_background_not_at_start_up(monkeypatch):
    db.ensure_migrated()
    checked = []
    monkeypatch.setattr(maintenance, "SMALL_FILE", 0)
    monkeypatch.setattr(maintenance, "check_integrity", lambda p=None: checked.append(p) or {"ok": True})
    maintenance.check_at_start_up()
    assert not checked



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
