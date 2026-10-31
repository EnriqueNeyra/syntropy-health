"""Delete means delete: what was removed is gone from the file and stays gone, whatever was running at the time."""

from __future__ import annotations

import io
import sqlite3
import threading
import time
import zipfile

import pytest

from app.core import config, db, maintenance
from app.services import imports
from app.store import activity, biometrics, chats, connections, profiles


def _bytes_on_disk(marker: bytes) -> int:
    """How often it appears in the database file and its log, as anyone with `strings` would find it."""
    path = config.db_path()
    found = 0
    for f in (path, path.with_name(f"{path.name}-wal")):
        if f.exists():
            found += f.read_bytes().count(marker)
    return found


def _profile_id(client) -> str:
    return client.get("/api/profiles").json()["profiles"][0]["id"]


def _person_with_data(client, name: str = "PERSONZEBRA") -> str:
    pid = client.post("/api/profiles", json={"name": name, "relationship": "other"}).json()["id"]
    entry = {"event_type": "note", "category": "notes", "name": "Note", "start": "2024-01-02T03:04:05Z", "note": "JOURNALZEBRA private"}
    assert client.post("/api/journal", params={"profile": pid}, json=entry).status_code == 200
    chats.save_chat(pid, None, "chat_1", "CHATTITLEZEBRA", [{"role": "user", "content": "CHATMSGZEBRA"}])
    lab = {"profile_id": pid, "collected": "2024-01-02", "results": [{"test": "LABZEBRA", "value": 5.0, "unit": "mg/dL"}]}
    assert client.post("/api/labs", json=lab).status_code == 200
    return pid


def _gone_from_the_file(markers: list[bytes]) -> dict[bytes, int]:
    maintenance.reclaim(config.db_path())
    return {m: _bytes_on_disk(m) for m in markers if _bytes_on_disk(m)}


MARKERS = [b"PERSONZEBRA", b"JOURNALZEBRA", b"CHATMSGZEBRA", b"CHATTITLEZEBRA", b"LABZEBRA"]


def test_a_deleted_person_can_not_be_read_from_the_database_file(client):
    pid = _person_with_data(client)
    assert _bytes_on_disk(b"JOURNALZEBRA")
    assert client.delete(f"/api/profiles/{pid}").status_code == 200
    assert _gone_from_the_file(MARKERS) == {}


def test_wiped_data_can_not_be_read_from_the_database_file(client):
    pid = _person_with_data(client)
    assert client.delete(f"/api/profiles/{pid}/data").status_code == 200
    assert _gone_from_the_file([m for m in MARKERS if m != b"PERSONZEBRA"]) == {}


def test_single_deletes_clear_the_file_too(client):
    pid = _person_with_data(client)
    entry = activity.list_events(pid, None, manual=True)[0]
    assert client.delete(f"/api/journal/{entry['id']}", params={"profile": pid}).status_code == 200
    chats.delete_chat(pid, None, "chat_1")
    assert _gone_from_the_file([b"JOURNALZEBRA", b"CHATMSGZEBRA"]) == {}


def test_what_an_older_version_freed_is_cleared_once(client):
    pid = _person_with_data(client)
    path = config.db_path()
    db.close_readers()
    # As an older version left it: freed space not overwritten, and no way to reclaim it in short steps.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA secure_delete = OFF")
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA auto_vacuum = 0")
    conn.execute("VACUUM")
    conn.execute("DELETE FROM health_events WHERE profile_id = ?", (pid,))
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    conn.execute("PRAGMA journal_mode = WAL")
    conn.close()
    assert path.read_bytes().count(b"JOURNALZEBRA")
    assert maintenance.reclaim(path) == {"checkpointed": True, "vacuumed": True, "freed_pages": 0}
    assert not path.read_bytes().count(b"JOURNALZEBRA")
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    assert check.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    check.close()
    assert maintenance.reclaim(path)["vacuumed"] is False        # from now on, in short steps


def test_new_databases_can_reclaim_space_in_short_steps(client):
    conn = sqlite3.connect(config.db_path())
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    conn.close()
    with db.db() as c:
        assert c.execute("PRAGMA secure_delete").fetchone()[0] == 1


def test_reclaiming_gives_space_back_without_holding_the_lock(client):
    pid = _profile_id(client)
    biometrics.insert_samples(pid, None, [
        {"id": f"s{i}", "metric_type": "heart_rate", "value": 60 + i % 40, "unit": "count/min",
         "start_date": f"2024-01-{1 + i % 28:02d}T00:{i % 60:02d}:{(i // 60) % 60:02d}Z",
         "end_date": f"2024-01-{1 + i % 28:02d}T00:{i % 60:02d}:{(i // 60) % 60:02d}Z", "source_name": f"Watch {i}",
         "metadata": {"padding": "x" * 300}} for i in range(4000)])
    path = config.db_path()
    db.checkpoint(path)
    before = path.stat().st_size
    with db.db() as conn:
        conn.execute("DELETE FROM biometric_samples")
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] > 100
    # Another write gets in during the reclaiming: it's done in small transactions, not one long one.
    waits = []
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            started = time.monotonic()
            with db.db() as conn:
                conn.execute("INSERT INTO app_settings(key, value, updated_at) VALUES ('t', '1', 0) "
                             "ON CONFLICT(key) DO UPDATE SET updated_at = updated_at + 1")
            waits.append(time.monotonic() - started)
            time.sleep(0.001)
    t = threading.Thread(target=writer)
    t.start()
    try:
        result = maintenance.reclaim(path, step_pages=64)
    finally:
        stop.set()
        t.join()
    assert result["vacuumed"] is False and result["freed_pages"] > 100
    db.checkpoint(path)
    assert path.stat().st_size < before / 2
    assert waits and max(waits) < 2


def test_a_full_disk_does_not_start_a_rewrite_of_the_whole_file(client, monkeypatch):
    path = config.db_path()
    db.close_readers()
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA auto_vacuum = 0")
    conn.execute("VACUUM")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.close()

    class Tiny:
        free = 1024
    monkeypatch.setattr(maintenance.shutil, "disk_usage", lambda p: Tiny)
    assert maintenance.reclaim(path)["vacuumed"] is False


def test_the_thread_reclaims_soon_after_a_delete(client, monkeypatch):
    monkeypatch.setattr(maintenance, "TICK_SECONDS", 0.01)
    called = threading.Event()
    monkeypatch.setattr(maintenance, "reclaim", lambda path=None, **k: called.set())
    maintenance.stop()
    maintenance.start()
    pid = _person_with_data(client)
    assert client.delete(f"/api/profiles/{pid}").status_code == 200
    assert called.wait(5)


# ---------------------------------------------------------------------------
# A person or source deleted while their data is on its way (F109)
# ---------------------------------------------------------------------------

def _rows(table: str, profile_id: str) -> int:
    with db.read() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE profile_id = ?", (profile_id,)).fetchone()[0]


def _sample(i: int) -> dict:
    return {"id": f"s{i}", "metric_type": "step_count", "value": 1.0, "unit": "count",
            "start_date": f"2024-02-01T00:00:{i:02d}Z", "end_date": f"2024-02-01T00:00:{i:02d}Z", "source_name": "Watch"}


def test_samples_for_someone_deleted_are_not_stored(client):
    pid = _person_with_data(client)
    source = connections.create(pid, "import", "apple_health_export", "Watch")
    assert client.delete(f"/api/profiles/{pid}").status_code == 200
    for store in (lambda: biometrics.insert_samples(pid, source["id"], [_sample(1)]),
                  lambda: biometrics.insert_samples(pid, None, [_sample(1)]),
                  lambda: activity.insert_workouts(pid, source["id"], [{"id": "w", "start": "2024-02-01T00:00:00Z"}]),
                  lambda: activity.insert_events(pid, source["id"], [{"id": "e", "event_type": "x", "start": "2024-02-01T00:00:00Z"}])):
        with pytest.raises(biometrics.SourceGone):
            store()
    assert biometrics.rebuild_daily(pid) == 0
    for table in ("biometric_samples", "workouts", "health_events", "biometric_daily", "daily_revisions"):
        assert _rows(table, pid) == 0, table


def test_samples_for_a_wiped_source_are_not_stored(client):
    pid = _profile_id(client)
    source = connections.create(pid, "import", "apple_health_export", "Watch")
    biometrics.insert_samples(pid, source["id"], [_sample(1)])
    assert client.delete(f"/api/profiles/{pid}/data").status_code == 200
    with pytest.raises(biometrics.SourceGone):
        biometrics.insert_samples(pid, source["id"], [_sample(2)])
    assert _rows("biometric_samples", pid) == 0
    assert client.get("/api/biometrics/metrics", params={"profile": pid}).json()["metrics"] == []


def test_a_phone_uploading_after_its_source_went_is_told_so(client):
    from app.api import biometrics as api
    from fastapi import HTTPException
    pid = _profile_id(client)
    source = connections.create(pid, "device", "apple_health", "Phone")
    with db.db() as conn:
        conn.execute("DELETE FROM connections WHERE id = ?", (source["id"],))
    with pytest.raises(HTTPException) as caught:
        api._store(pid, source["id"], [_sample(1)], [], [], {"device_id": "d", "device_name": "Phone"})
    assert caught.value.status_code == 410


def _apple_export(samples: int) -> bytes:
    rows = "".join(f'<Record type="HKQuantityTypeIdentifierStepCount" sourceName="Watch" unit="count" value="5" '
                   f'startDate="2024-03-01 {i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d} +0000" '
                   f'endDate="2024-03-01 {i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d} +0000"/>' for i in range(samples))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("apple_health_export/export.xml", f"<HealthData>{rows}</HealthData>")
    return buf.getvalue()


@pytest.mark.parametrize("how", ["person", "wipe"])
def test_an_import_stops_when_what_it_imports_into_is_deleted(client, how, monkeypatch):
    pid = _person_with_data(client)
    client.delete(f"/api/profiles/{pid}/data")
    deleted = threading.Event()
    real = biometrics.insert_samples
    calls = []

    def after_first_batch(*args, **kwargs):
        out = real(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            if how == "person":
                profiles.delete_profile(pid)
            else:
                assert client.delete(f"/api/profiles/{pid}/data").status_code == 200
            deleted.set()
        return out
    monkeypatch.setattr(imports.biometrics, "insert_samples", after_first_batch)
    monkeypatch.setattr(imports, "BATCH", 50)
    r = client.post("/api/imports/apple-health", files={"file": ("export.zip", _apple_export(400), "application/zip")},
                    data={"profile_id": pid})
    job_id = r.json()["job_id"]
    for _ in range(200):
        job = imports.get_job(job_id)
        if job["finished_at"]:
            break
        time.sleep(0.05)
    assert deleted.is_set() and job["status"] == "error" and "deleted" in job["error"]
    assert len(calls) == 1 or len(calls) == 2
    assert _rows("biometric_samples", pid) == 0 and _rows("biometric_daily", pid) == 0


def _orphans(pid: str) -> None:
    now = time.time()
    with db.db() as conn:
        for i in range(25):
            conn.execute("INSERT INTO biometric_samples(id, profile_id, metric_type, value, unit, start_date, end_date, created_at) "
                         "VALUES (?, ?, 'step_count', 1, 'count', '2024-01-01', '2024-01-01', ?)", (f"o{pid}{i}", pid, now))
        conn.execute("INSERT INTO biometric_daily(profile_id, day, metric_type, source_name, value, sample_count) "
                     "VALUES (?, '2024-01-01', 'step_count', 'x', 1, 1)", (pid,))
        conn.execute("INSERT INTO workouts(id, profile_id, name, start_date, end_date, created_at) "
                     "VALUES (?, ?, 'w', '2024-01-01', '2024-01-01', ?)", (f"w{pid}", pid, now))
        conn.execute("INSERT INTO health_events(id, profile_id, event_type, category, name, start_date, end_date, created_at) "
                     "VALUES (?, ?, 't', 'c', 'n', '2024-01-01', '2024-01-01', ?)", (f"e{pid}", pid, now))
        conn.execute("INSERT INTO sync_batches(batch_id, profile_id, device_id, device_name, sync_trigger, sample_count, synced_at) "
                     "VALUES (?, ?, 'd', 'd', 'manual', 1, ?)", (f"b{pid}", pid, now))
        conn.execute("INSERT INTO daily_revisions(profile_id, rev) VALUES (?, 1)", (pid,))


def test_rows_of_people_who_are_gone_are_cleared_at_start_up(client):
    keep = _profile_id(client)
    biometrics.insert_samples(keep, None, [_sample(1)])
    _orphans("prf_gone_a")
    _orphans("prf_gone_b")
    profiles.finish_deletes()
    for pid in ("prf_gone_a", "prf_gone_b"):
        for table in profiles.LOOSE_TABLES:
            assert _rows(table, pid) == 0, table
    assert _rows("biometric_samples", keep) == 1


class _Crash(Exception):
    pass


def test_an_interrupted_delete_finishes_at_the_next_start(client, monkeypatch):
    pid = _profile_id(client)
    source = connections.create(pid, "import", "apple_health_export", "Watch")
    biometrics.insert_samples(pid, source["id"], [_sample(i) for i in range(30)])
    real = db.write_in_batches
    monkeypatch.setattr(profiles.db_batches, "write_in_batches", lambda *a, **k: (_ for _ in ()).throw(_Crash()))
    with pytest.raises(_Crash):
        profiles.delete_all_data(pid)
    assert _rows("biometric_samples", pid) == 30
    assert not connections.get(source["id"])
    monkeypatch.setattr(profiles.db_batches, "write_in_batches", real)
    profiles.finish_deletes()
    assert _rows("biometric_samples", pid) == 0


def test_an_interrupted_source_removal_finishes_at_the_next_start(client, monkeypatch):
    pid = _profile_id(client)
    source = connections.create(pid, "import", "apple_health_export", "Watch")
    biometrics.insert_samples(pid, source["id"], [_sample(i) for i in range(30)])
    real = db.write_in_batches
    monkeypatch.setattr(profiles.db_batches, "write_in_batches", lambda *a, **k: (_ for _ in ()).throw(_Crash()))
    with pytest.raises(_Crash):
        client.delete(f"/api/connections/{source['id']}", params={"delete_data": True})
    monkeypatch.setattr(profiles.db_batches, "write_in_batches", real)
    profiles.finish_deletes()
    assert _rows("biometric_samples", pid) == 0


def test_removing_a_source_removes_only_its_samples(client):
    pid = _profile_id(client)
    a = connections.create(pid, "import", "apple_health_export", "Watch")
    b = connections.create(pid, "import", "apple_health_export", "Ring")
    biometrics.insert_samples(pid, a["id"], [_sample(1)])
    biometrics.insert_samples(pid, b["id"], [{**_sample(2), "source_name": "Ring"}])
    assert client.delete(f"/api/connections/{a['id']}", params={"delete_data": True}).status_code == 200
    with db.read() as conn:
        left = [r[0] for r in conn.execute("SELECT connection_id FROM biometric_samples WHERE profile_id = ?", (pid,))]
    assert left == [b["id"]]
    assert biometrics.available_metrics(pid)


# ---------------------------------------------------------------------------
# What "all data" covers (F254)
# ---------------------------------------------------------------------------

def test_deleting_all_data_includes_goals_layouts_and_alerts(client):
    pid = _profile_id(client)
    goal = {"metric": "step_count", "target": 9123, "direction": "min"}
    assert client.put("/api/goals", params={"profile": pid}, json=goal).status_code == 200
    assert client.get("/api/goals", params={"profile": pid}).json()["goals"]
    assert client.post(f"/api/profiles/{pid}/alerts", json={"seen": ["a"], "dismissed": ["b"]}).status_code == 200
    from app.core import settings
    settings.set(f"overview.layout.{pid}", [{"id": "sleep", "visible": True}])
    assert client.delete(f"/api/profiles/{pid}/data").status_code == 200
    assert client.get("/api/goals", params={"profile": pid}).json()["goals"] == {}
    alerts = client.get(f"/api/profiles/{pid}/alerts").json()
    assert alerts["seen"] == [] and alerts["dismissed"] == []
    with db.read() as conn:
        keys = [r[0] for r in conn.execute("SELECT key FROM app_settings WHERE key LIKE ?", (f"%{pid}%",))]
    assert keys == []
    # The person stays.
    assert client.get("/api/profiles").json()["profiles"][0]["id"] == pid


def test_a_wipe_moves_the_rollup_revision_forward(client):
    """What's cached from the old rows is keyed by this number, so it mustn't start again from where it once was."""
    pid = _profile_id(client)
    biometrics.insert_samples(pid, None, [_sample(1)])
    before = biometrics.daily_revision(pid)
    assert before
    assert client.delete(f"/api/profiles/{pid}/data").status_code == 200
    assert biometrics.daily_revision(pid) > before


def test_a_file_that_stays_busy_is_tried_again_for_a_while_only(client, monkeypatch):
    monkeypatch.setattr(maintenance, "TICK_SECONDS", 0.001)
    monkeypatch.setattr(maintenance, "RECLAIM_TRIES", 5)
    calls = []
    monkeypatch.setattr(maintenance, "reclaim", lambda path=None, **k: calls.append(1) or {"checkpointed": False})
    maintenance.stop()
    db.take_deleted()
    maintenance.start()
    db.note_deleted()
    for _ in range(200):
        if len(calls) >= 5:
            break
        time.sleep(0.01)
    time.sleep(0.05)
    assert len(calls) == 5
