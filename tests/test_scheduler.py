"""The background work nobody watches: it syncs what's due, and deletes what's expired and nothing else."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest

from app.core import config, db, settings
from app.services import scheduler, sync
from app.store import connections, profiles


@pytest.fixture
def prof() -> str:
    return profiles.default_profile()["id"]


def _source(profile_id: str, kind: str = "wearable", provider: str = "oura", **fields) -> str:
    conn = connections.create(profile_id, kind, provider, f"{provider} {kind}", mode="simulated",
                              credentials={"access_token": "a", "refresh_token": "r"})
    if fields:
        connections.update(conn["id"], **fields)
    return conn["id"]


def _insert_expiring(profile_id: str, now: float) -> None:
    year = 365 * 86400
    with db.db() as c:
        c.executemany("INSERT INTO user_sessions(token_hash, created_at, expires_at, last_seen_at) VALUES (?, 0, ?, 0)",
                      [("expired", now - 1), ("live", now + 3600)])
        c.executemany("INSERT INTO oauth_pending(state, kind, payload_enc, created_at, expires_at) VALUES (?, 'smart', '', 0, ?)",
                      [("expired", now - 1), ("live", now + 3600)])
        c.executemany("INSERT INTO pairing_codes(code_hash, profile_id, created_at, expires_at) VALUES (?, ?, 0, ?)",
                      [("expired", profile_id, now - 1), ("live", profile_id, now + 3600)])
        c.executemany("INSERT INTO audit_log(at, actor, action) VALUES (?, 'test', ?)",
                      [(now - year - 60, "too-old"), (now - year + 60, "kept")])


def test_a_tick_deletes_exactly_what_has_expired(prof):
    _insert_expiring(prof, time.time())
    asyncio.run(scheduler.run_once())
    with db.read() as c:
        assert [r[0] for r in c.execute("SELECT token_hash FROM user_sessions")] == ["live"]
        assert [r[0] for r in c.execute("SELECT state FROM oauth_pending")] == ["live"]
        assert [r[0] for r in c.execute("SELECT code_hash FROM pairing_codes")] == ["live"]
        actions = [r[0] for r in c.execute("SELECT action FROM audit_log")]
    assert "too-old" not in actions and "kept" in actions


def test_which_sources_are_due(prof):
    now = time.time()
    hour = 3600
    fresh_wearable = _source(prof, last_sync_at=now - 1 * hour)
    stale_wearable = _source(prof, last_sync_at=now - 7 * hour)
    never_synced = _source(prof)
    fresh_ehr = _source(prof, "ehr", "epic", last_sync_at=now - 7 * hour)
    stale_ehr = _source(prof, "ehr", "cerner", last_sync_at=now - 25 * hour)
    failing_recently = _source(prof, last_sync_at=now - 5 * hour, status="error")     # due by its interval, not by the backoff
    failing_long_ago = _source(prof, last_sync_at=now - 7 * hour, status="error")
    needs_sign_in = _source(prof, last_sync_at=0, status="needs_reauth")
    pushes_by_itself = _source(prof, "device", "apple_health", last_sync_at=0)
    disconnected = _source(prof, last_sync_at=0, status="disconnected")
    due = {c["id"] for c in sync.due_connections(24, 4)}
    assert stale_wearable in due and never_synced in due and stale_ehr in due and failing_long_ago in due
    for not_due in (fresh_wearable, fresh_ehr, failing_recently, needs_sign_in, pushes_by_itself, disconnected):
        assert not_due not in due


def test_a_failing_source_does_not_stop_the_others(prof, monkeypatch):
    first, second, third = (_source(prof, last_sync_at=0) for _ in range(3))
    tried = []

    async def fake_sync(connection_id, trigger="manual"):
        tried.append((connection_id, trigger))
        if connection_id == second:
            raise RuntimeError("the vendor had a bad day")
        return {"status": "success"}
    monkeypatch.setattr(sync, "sync_connection", fake_sync)
    _insert_expiring(prof, time.time())
    assert set(asyncio.run(scheduler.run_once())) == {first, third}
    assert sorted(tried) == sorted((c, "scheduled") for c in (first, second, third))
    with db.read() as c:      # and the tidying-up still happens
        assert [r[0] for r in c.execute("SELECT token_hash FROM user_sessions")] == ["live"]


def test_a_scheduled_sync_runs_against_the_simulator(prof):
    source = _source(prof)
    assert asyncio.run(scheduler.run_once()) == [source]
    with db.read() as c:
        run = c.execute("SELECT trigger, status FROM sync_runs WHERE connection_id = ?", (source,)).fetchone()
    assert (run["trigger"], run["status"]) == ("scheduled", "success")
    assert connections.get(source)["last_sync_at"]


# ---------------------------------------------------------------------------
# Behind a long write
# ---------------------------------------------------------------------------

def _hold_write_lock(seconds: float) -> threading.Thread:
    """Another writer holding the lock, as a big upload or a time zone change does."""
    db.ensure_migrated()
    held = threading.Event()

    def hold():
        conn = sqlite3.connect(str(config.db_path()), isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        held.set()
        time.sleep(seconds)
        conn.execute("ROLLBACK")
        conn.close()
    thread = threading.Thread(target=hold)
    thread.start()
    held.wait(5)
    return thread


async def _longest_stall_while(work) -> float:
    """How long the event loop went without running anything while ``work`` waited."""
    longest = 0.0
    done = asyncio.Event()

    async def heartbeat():
        nonlocal longest
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            longest = max(longest, now - last)
            last = now
    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)      # its clock starts before the work does
    try:
        await work
    finally:
        done.set()
        await beat
    return longest


def test_the_event_loop_keeps_running_behind_a_held_write_lock(prof):
    source = _source(prof)

    async def run():
        holder = _hold_write_lock(1.0)
        try:
            return await _longest_stall_while(scheduler.run_once())
        finally:
            holder.join()
    stall = asyncio.run(run())
    assert stall < 0.5, f"the loop stalled for {stall:.2f}s"
    assert connections.get(source)["last_sync_status"] == "success"


def test_a_sync_that_cannot_start_reports_it_instead_of_raising(prof, monkeypatch):
    source = _source(prof)
    real_start = connections.start_run
    attempts = []

    def locked_once(connection_id, trigger):
        attempts.append(trigger)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_start(connection_id, trigger)
    monkeypatch.setattr(connections, "start_run", locked_once)
    result = asyncio.run(sync.sync_connection(source, "scheduled"))
    assert result["status"] == "error" and "locked" in result["error"]
    assert connections.get(source)["status"] == "error"


def test_saving_settings_behind_a_long_write_leaves_the_server_answering(client):
    holder = _hold_write_lock(1.0)
    results = {}

    def save():
        results["save"] = client.put("/api/settings/general", json={"ehr_interval_hours": 12}).status_code
    saving = threading.Thread(target=save)
    saving.start()
    time.sleep(0.1)
    started = time.monotonic()
    assert client.get("/health").status_code == 200
    assert time.monotonic() - started < 0.5
    saving.join()
    holder.join()
    assert results["save"] == 200 and settings.get("sync.ehr_interval_hours") == 12
