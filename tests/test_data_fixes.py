"""Fixes to stored data: once per database, in the background, resuming after a restart, and small writes throughout."""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.core import data_fixes, db
from tests.conftest import BASE, LOCAL_CLIENT


@pytest.fixture(autouse=True)
def no_other_fixes(monkeypatch):
    monkeypatch.setattr(data_fixes, "_fixes", {})
    data_fixes._stop.clear()
    yield
    data_fixes.stop()
    data_fixes._stop.clear()


def _rows(count: int) -> None:
    with db.db() as conn:
        conn.executemany("INSERT INTO app_settings(key, value, updated_at) VALUES (?, '1', 0)",
                         ((f"row.{i:06d}",) for i in range(count)))


def _count_changed() -> int:
    with db.read() as conn:
        return conn.execute("SELECT COUNT(*) FROM app_settings WHERE key LIKE 'row.%' AND value = '2'").fetchone()[0]


def _doubler(after_step=None):
    def step(conn, cursor, size):
        rows = conn.execute("SELECT key FROM app_settings WHERE key LIKE 'row.%' AND key > ? ORDER BY key LIMIT ?",
                            (cursor or "", size)).fetchall()
        for row in rows:
            conn.execute("UPDATE app_settings SET value = '2' WHERE key = ?", (row["key"],))
        if after_step:
            after_step(rows)
        return rows[-1]["key"] if rows else None
    return step


def _state(name: str) -> dict:
    return next(f for f in data_fixes.status() if f["name"] == name)


def test_a_fix_runs_to_the_end_once():
    _rows(2500)
    calls = []
    data_fixes.register("double", _doubler(lambda rows: calls.append(len(rows))))
    assert _state("double")["state"] == "pending"
    data_fixes.run_pending()
    assert _count_changed() == 2500
    state = _state("double")
    assert state["state"] == "done" and state["finished_at"] and state["steps"] >= 1
    taken = len(calls)
    data_fixes.run_pending()
    assert len(calls) == taken        # never again on this database


def test_a_restart_carries_on_where_it_stopped():
    _rows(3000)
    seen: list[str] = []

    def stop_after_first(rows):
        seen.extend(r["key"] for r in rows)
        data_fixes._stop.set()
    data_fixes.register("double", _doubler(stop_after_first))
    data_fixes.run_pending()
    first = len(seen)
    assert 0 < first < 3000 and _count_changed() == first and _state("double")["state"] == "running"
    data_fixes._stop.clear()
    data_fixes._fixes["double"] = _doubler(lambda rows: seen.extend(r["key"] for r in rows))
    data_fixes.run_pending()
    assert _state("double")["state"] == "done" and _count_changed() == 3000
    assert len(seen) == len(set(seen)) == 3000      # each row handled exactly once


def test_a_crash_inside_a_step_loses_that_step_only():
    _rows(1000)
    steps = []

    def fails_second_time(conn, cursor, size):
        steps.append(cursor)
        rows = conn.execute("SELECT key FROM app_settings WHERE key LIKE 'row.%' AND key > ? ORDER BY key LIMIT 100",
                            (cursor or "",)).fetchall()
        for row in rows:
            conn.execute("UPDATE app_settings SET value = '2' WHERE key = ?", (row["key"],))
        if len(steps) == 2:
            raise RuntimeError("the disk hiccuped")
        return rows[-1]["key"] if rows else None
    data_fixes.register("flaky", fails_second_time)
    other = []
    data_fixes.register("other", lambda conn, cursor, size: other.append(1))
    data_fixes.run_pending()
    state = _state("flaky")
    assert state["state"] == "failed" and "disk hiccuped" in state["error"]
    assert _count_changed() == 100        # the failed step's writes went with it
    assert other == [1] and _state("other")["state"] == "done"        # the others still ran
    data_fixes.run_pending()              # the next start
    state = _state("flaky")
    assert state["state"] == "done" and state["attempts"] == 2 and state["error"] is None
    assert _count_changed() == 1000


def test_a_fix_that_makes_no_progress_is_stopped():
    data_fixes.register("stuck", lambda conn, cursor, size: 1)
    data_fixes.run_pending()
    assert _state("stuck")["state"] == "failed" and "never finish" in _state("stuck")["error"]


def test_the_same_name_cannot_be_two_fixes():
    def one(conn, cursor, size):
        return None
    data_fixes.register("name", one)
    data_fixes.register("name", one)
    with pytest.raises(ValueError):
        data_fixes.register("name", lambda conn, cursor, size: None)


def test_the_server_runs_them_in_the_background_and_answers_meanwhile():
    release = threading.Event()
    running = threading.Event()

    def slow(conn, cursor, size):
        running.set()
        release.wait(5)
        return None
    data_fixes.register("slow", slow)
    from app.main import create_app
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT) as c:
        assert running.wait(5)
        started = time.monotonic()
        assert c.get("/api/status").status_code == 200
        assert c.get("/health").status_code == 200
        assert time.monotonic() - started < 1
        assert _state("slow")["state"] == "running"
        release.set()
        deadline = time.monotonic() + 5
        while _state("slow")["state"] != "done" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _state("slow")["state"] == "done"


def test_shutting_down_stops_between_steps():
    _rows(200)

    def slowly(conn, cursor, size):
        time.sleep(0.01)
        row = conn.execute("SELECT key FROM app_settings WHERE key LIKE 'row.%' AND key > ? ORDER BY key LIMIT 1",
                           (cursor or "",)).fetchone()
        return row["key"] if row else None
    data_fixes.register("slowly", slowly)
    data_fixes.start()
    time.sleep(0.05)
    started = time.monotonic()
    data_fixes.stop()
    assert time.monotonic() - started < 2
    assert _state("slowly")["state"] == "running"


# ---------------------------------------------------------------------------
# db.write_in_batches
# ---------------------------------------------------------------------------

def test_batches_shrink_until_each_holds_the_lock_briefly():
    db.ensure_migrated()
    sizes = []
    left = [40_000]

    def costly(conn, cursor, size):
        sizes.append(size)
        time.sleep(size * 0.0002)        # 5,000 rows a second
        left[0] -= size
        return (cursor or 0) + 1 if left[0] > 0 else None
    db.write_in_batches(costly, size=10_000, pause=0)
    assert sizes[0] == 10_000
    assert all(s * 0.0002 < 1.0 for s in sizes[2:]), sizes
    assert max(s * 0.0002 for s in sizes[3:]) <= 2 * db.BATCH_TARGET_SECONDS + 0.05, sizes


def test_batches_grow_when_they_are_cheap():
    db.ensure_migrated()
    sizes = []

    def cheap(conn, cursor, size):
        sizes.append(size)
        return (cursor or 0) + 1 if len(sizes) < 8 else None
    db.write_in_batches(cheap, size=100, pause=0)
    assert sizes[-1] > sizes[0] and sizes[-1] <= db.BATCH_MAX


def test_other_writers_get_their_turn_during_a_long_rewrite():
    _rows(5000)
    db.ensure_migrated()
    stop = threading.Event()
    waits: list[float] = []

    def other_writer():
        while not stop.is_set():
            started = time.monotonic()
            with db.db() as conn:
                conn.execute("INSERT OR REPLACE INTO app_settings(key, value, updated_at) VALUES ('other', '1', 0)")
            waits.append(time.monotonic() - started)
            time.sleep(0.02)
    thread = threading.Thread(target=other_writer)
    thread.start()

    def rewrite(conn, cursor, size):
        time.sleep(0.2)     # holds the lock
        return (cursor or 0) + 1 if (cursor or 0) < 8 else None
    try:
        db.write_in_batches(rewrite, size=10)
    finally:
        stop.set()
        thread.join()
    assert len(waits) >= 5 and max(waits) < 1.5, waits


def test_a_stop_request_ends_it_and_returns_where_it_got_to():
    db.ensure_migrated()
    stop = threading.Event()

    def step(conn, cursor, size):
        if (cursor or 0) == 2:
            stop.set()
        return (cursor or 0) + 1
    assert db.write_in_batches(step, size=10, pause=0, should_stop=stop.is_set) == 3


def test_writes_of_a_failed_batch_are_undone():
    db.ensure_migrated()

    def step(conn, cursor, size):
        conn.execute("INSERT INTO app_settings(key, value, updated_at) VALUES ('half-done', '1', 0)")
        raise sqlite3.IntegrityError("nope")
    with pytest.raises(sqlite3.IntegrityError):
        db.write_in_batches(step)
    with db.read() as conn:
        assert not conn.execute("SELECT 1 FROM app_settings WHERE key = 'half-done'").fetchone()
