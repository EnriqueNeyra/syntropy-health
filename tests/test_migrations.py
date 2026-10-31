"""Every old schema, with data in it, upgraded to today's: nothing lost, and the app works on the result. Add to this when
adding a migration that changes what's stored. Migrations that have shipped never change: MIGRATION_HASHES below."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import BASE, CSRF, LOCAL_CLIENT, PASSPHRASE

# A file listed here is on main, and people's databases have run it, so editing it would leave them behind: write a new
# migration instead. A newly merged migration can be added here at any time; until then CI's check on pull requests
# (.github/workflows/ci.yml) still refuses edits to anything on main.
MIGRATION_HASHES = {
    "001_initial.sql": "cbf4d6900cfe3d6bbcbeedcc827e81afe275ccf0f3345bb1d8fa9c65aaea04a7",
    "002_workouts_events.sql": "448ec4384636dfd8bde3e71a8ece644b433d3ebbea2c590feb20aa831baa6bdb",
    "003_agent_tokens.sql": "33c610ae0d6b4cb1d6d268d8f8a7f1a26f5a1d21d3576c540767b6cb7c0b503a",
    "004_device_sessions.sql": "2fefaae8260d1dffcf6326f08d74cfce03833a216f991cea6b93f06771903017",
    "005_sample_indexes.sql": "a9ec027a65a5fdf569a803013daf36c1fdff341c83642e2a8f75d1961bf02c2d",
    "006_device_sources.sql": "198dda009f6f5d5f5d3ab83d4a203bf325e14422b0ce7c2440e39e479174e4ad",
    "007_journal.sql": "4ea1ddacae921784a76e9994b095f90887306f86068831a3067a86e261f702da",
    "008_agent_last_call.sql": "72fc611d4a1a6e41940c833553ba0a026bed970eb1790ce5e9b9428e24fb4275",
    "009_accounts.sql": "07eca07bcffe5e14b919ebffc6953fbd6bdf047ee710992ae460dabff96063da",
    "010_chats.sql": "aa7e2c2a1a91d2fb45f6bd97d0830b7780bb003e01a24d5542967426119e8c65",
    "011_healthkit_export_platform.sql": "019c3c1d7fa357f5c2a148f091a0fcd0be768aeec59adf178b3dc7dd03a4a46f",
    "012_workout_humidity.sql": "35f0dff42fa87d9252f9311efb742eb4c3a0d5f86e1fe512f9597e7c662c2d03",
    "013_daily_revisions.sql": "81aa57eaeaddd9ac7e7504e7e8a811e23936a7bc0274f7988a5414cf90cf7279",
    "020_records_without_a_standard_code.sql": "43dc59c069ba81fc8e97eb45d67af7cd4449e941078c224d73aabab56b325538",
    "025_sync_query_state.sql": "89b2ae046caae627a8ad9385cb35fbe4a7e2a8a0d00742cbd5fa888c3f7e8ff7",
    "026_removed_records.sql": "fcef2b3cc2a95d41d33544209356bbde629235071d2591c14dac9e7381ffba27",
    "027_sync_query_progress.sql": "52db3f9056b00235f3bcaeeb657433d84562ce417c8b3a8cead7339d25604be5",
    "030_wearable_corrections.sql": "8636c6871551e03987123b41bf6f811ad663c937bdfd5018f1da82faa56eedea",
    "037_record_search.sql": "a777ac07ffb9d2eb0312c6d0c2ddb8f338875c0c7eaa4e53114f4cd955d51734",
    "038_agent_token_expiry.sql": "b0d3469439632f45c9c4d34b4e86794bb04fb1e18fc2f2bfa083dee54f2e1d94",
    "040_phones_paired_by_others.sql": "c2f08699dd8d015be4aea8ed37e529ddcce5a42bae76111ef356029b4b7b3bd5",
    "041_cleared_what_no_one_can_reach.sql": "7165e2d472680397ea6d05e91ddca84c3163ddabdcb3a430c318c81afcd517bb",
    "045_fhir_references.sql": "47c03487e3b28ce143b31f8cc4cbfe28a44ff248bf1e6f4ea65880cc2b29a152",
}


def _migrations() -> list[Path]:
    from app.core import db
    return sorted(db.MIGRATIONS_DIR.glob("*.sql"))


def test_shipped_migrations_never_change():
    present = {f.name: hashlib.sha256(f.read_bytes().replace(b"\r\n", b"\n")).hexdigest() for f in _migrations()}
    changed = [name for name, digest in MIGRATION_HASHES.items() if present.get(name) != digest]
    assert not changed, f"Shipped migrations must stay as they are; add a new one instead of changing {changed}"


def test_migration_numbers_are_unique():
    numbers = [int(f.name.split("_", 1)[0]) for f in _migrations()]
    assert len(numbers) == len(set(numbers)), f"Two migrations share a number: {sorted(numbers)}"
    assert numbers[0] == 1


def test_migration_files_only_use_what_the_runner_supports():
    """They all run inside one transaction, split on lines ending in a semicolon."""
    for f in _migrations():
        text = "\n".join(line for line in f.read_text(encoding="utf-8").splitlines() if not line.strip().startswith("--"))
        upper = text.upper()
        for word in ("BEGIN", "COMMIT", "ROLLBACK", "VACUUM", "PRAGMA", "CREATE TRIGGER"):
            assert word not in upper.replace("BEGINNING", ""), f"{f.name} uses {word}"


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

def _build(path: Path, version: int) -> sqlite3.Connection:
    from app.core import db
    conn = sqlite3.connect(str(path), isolation_level=None)
    for f in _migrations():
        if int(f.name.split("_", 1)[0]) > version:
            break
        for statement in db._split_sql(f.read_text(encoding="utf-8")):
            conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {version}")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}


def _seed(conn: sqlite3.Connection, version: int, stored_hash: str) -> None:
    settings = [("setup.completed", True), ("auth.required", True), ("terms.accepted", {"version": 1, "at": 1}),
                ("display.units", "metric"), ("display.accent", "teal"), ("ai.cloud_ack", 1.0), ("onboarding.pending", True),
                ("overview.layout.prf_a", [{"id": "sleep", "visible": True, "options": {}}]),
                ("alerts.seen.prf_a", ["x"]), ("alerts.dismissed.prf_a", ["y"])]
    if version < 9:
        settings.append(("auth.password_hash", stored_hash))
    conn.executemany("INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, 0)", [(k, json.dumps(v)) for k, v in settings])
    conn.executemany("INSERT INTO profiles(id, name, relationship, is_default, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0)",
                     [("prf_a", "Alex", "self", 1, 1), ("prf_b", "Sam", "child", 0, 2)])
    conn.execute("INSERT INTO connections(id, profile_id, kind, provider, display_name, mode, status, created_at, updated_at, metadata_json) "
                 "VALUES ('con_ehr', 'prf_a', 'ehr', 'epic', 'Stanford', 'simulated', 'active', 1, 1, '{}')")
    # The same phone twice, from before sources were folded together (006).
    conn.execute("INSERT INTO connections(id, profile_id, kind, provider, display_name, mode, status, created_at, updated_at, last_sync_at, metadata_json) "
                 "VALUES ('con_ph1', 'prf_a', 'device', 'apple_health', 'Apple Health · iPhone', 'live', 'disconnected', 1, 1, 100, '{\"platform\":\"hae\"}')")
    conn.execute("INSERT INTO connections(id, profile_id, kind, provider, display_name, mode, status, created_at, updated_at, last_sync_at, metadata_json) "
                 "VALUES ('con_ph2', 'prf_a', 'device', 'apple_health', 'Apple Health · iPhone', 'live', 'active', 2, 2, 200, '{\"platform\":\"hae\"}')")
    conn.execute("INSERT INTO connections(id, profile_id, kind, provider, display_name, mode, status, created_at, updated_at, metadata_json) "
                 "VALUES ('con_oura', 'prf_b', 'wearable', 'oura', 'Oura', 'simulated', 'active', 1, 1, 'not json')")
    conn.execute("INSERT INTO sync_runs(connection_id, trigger, started_at, status) VALUES ('con_ph1', 'ingest', 1, 'success')")
    conn.execute("INSERT INTO clinical_records(id, profile_id, connection_id, resource_type, resource_id, category, title, code, "
                 "effective_at, value_num, unit, first_seen_at, updated_at) "
                 "VALUES ('r1', 'prf_a', 'con_ehr', 'Observation', 'o1', 'labs', 'A1c', '4548-4', '2025-01-01', 6.1, '%', 1, 1)")
    for i, source in enumerate(["con_ph1", "con_ph2", "con_ph1"]):
        conn.execute("INSERT INTO biometric_samples(id, profile_id, connection_id, metric_type, value, unit, start_date, end_date, "
                     "source_name, created_at) VALUES (?, 'prf_a', ?, 'step_count', ?, 'count', ?, ?, 'iPhone', 1)",
                     (f"s{i}", source, 100 + i, f"2025-01-0{i + 1}T10:00:00Z", f"2025-01-0{i + 1}T10:01:00Z"))
    conn.execute("INSERT INTO biometric_daily(profile_id, day, metric_type, source_name, value, sample_count, unit) "
                 "VALUES ('prf_a', '2025-01-01', 'step_count', 'iPhone', 100, 1, 'count')")
    conn.execute("INSERT INTO sync_batches(batch_id, profile_id, device_id, device_name, sync_trigger, sample_count, synced_at) "
                 "VALUES ('b1', 'prf_a', 'd', 'iPhone', 'manual', 3, 1)")
    conn.execute("INSERT INTO user_sessions(token_hash, created_at, expires_at, last_seen_at) VALUES ('h', 1, 99999999999, 1)")
    conn.execute("INSERT INTO devices(id, profile_id, connection_id, name, platform, token_hash, created_at, revoked_at) "
                 "VALUES ('dev1', 'prf_a', 'con_ph1', 'iPhone', 'hae', 't1', 1, 1)")
    conn.execute("INSERT INTO devices(id, profile_id, connection_id, name, platform, token_hash, created_at) "
                 "VALUES ('dev2', 'prf_a', 'con_ph2', 'iPhone', 'hae', 't2', 2)")
    if "workouts" in _tables(conn):
        conn.execute("INSERT INTO workouts(id, profile_id, connection_id, name, start_date, end_date, humidity_pct, created_at) "
                     "VALUES ('w1', 'prf_a', 'con_ph1', 'Run', '2025-01-01T10:00:00Z', '2025-01-01T10:30:00Z', 6500, 1)")
        conn.execute("INSERT INTO health_events(id, profile_id, connection_id, event_type, category, name, start_date, end_date, created_at) "
                     "VALUES ('e1', 'prf_a', 'con_ph1', 'headache', 'symptoms', 'Headache', '2025-01-01T10:00:00Z', '2025-01-01T10:00:00Z', 1)")
    if "agent_tokens" in _tables(conn):
        conn.execute("INSERT INTO agent_tokens(id, name, token_hash, created_at) VALUES ('ag', 'claude', 'th', 1)")


def _shipped_versions() -> list[int]:
    """Every version a database can be at that still has an upgrade ahead of it."""
    return [int(f.name.split("_", 1)[0]) for f in _migrations()][:-1]


@pytest.mark.parametrize("version", _shipped_versions())
def test_every_old_schema_upgrades_with_its_data(version):
    from app.core import config, db, security
    if version == db.newest_schema():
        pytest.skip('the newest schema has nothing to upgrade to')
    path = config.db_path()
    stored_hash = security.encrypt_json(security.hash_password(PASSPHRASE))
    conn = _build(path, version)
    _seed(conn, version, stored_hash)
    conn.close()
    db.reset_migration_cache()
    db.ensure_migrated(path)

    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.newest_schema()
    assert c.execute("SELECT COUNT(*) FROM clinical_records").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM biometric_samples").fetchone()[0] == 3
    assert c.execute("SELECT COUNT(*) FROM biometric_daily").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 2
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert c.execute("SELECT COUNT(*) FROM fhir_references").fetchone()[0] == 0     # what a sync brings next fills it
    if version < 9:
        accounts = c.execute("SELECT * FROM accounts").fetchall()
        assert len(accounts) == 1 and accounts[0]["role"] == "owner" and accounts[0]["password_hash"]
        assert json.loads(accounts[0]["prefs"]) == {"units": "metric", "accent": "teal"}
        assert c.execute("SELECT COUNT(*) FROM profile_access").fetchone()[0] == 1
        assert c.execute("SELECT account_id FROM user_sessions").fetchone()[0] == "acc_owner"
    if version < 6:
        assert c.execute("SELECT COUNT(*) FROM connections WHERE kind = 'device'").fetchone()[0] == 1
        assert {r[0] for r in c.execute("SELECT DISTINCT connection_id FROM biometric_samples")} == {"con_ph2"}
        assert c.execute("SELECT id FROM devices").fetchall()[0][0] == "dev2"
        assert c.execute("SELECT last_sync_at FROM connections WHERE id = 'con_ph2'").fetchone()[0] == 200
    if version < 11:
        assert c.execute("SELECT platform FROM devices WHERE id = 'dev2'").fetchone()[0] == "healthkit-export"
    if 2 <= version < 12:
        assert c.execute("SELECT humidity_pct FROM workouts").fetchone()[0] == 65
    if version < 30:
        assert c.execute("SELECT COUNT(*) FROM biometric_samples WHERE flag IS NOT NULL OR tz_offset_min IS NOT NULL").fetchone()[0] == 0
        assert c.execute("SELECT timezone FROM profiles").fetchone()[0] is None
        assert {"data_sources", "sample_tombstones", "daily_rebuilds", "import_jobs"} <= _tables(c)
    c.close()

    if version == db.newest_schema():
        return      # already up to date: nothing was upgraded, so nothing was copied first
    copies = db.pre_upgrade_copies(path)
    if version < db.newest_schema():      # already the newest: nothing to upgrade, so no copy from before
        assert len(copies) == 1 and copies[0]["version"] == version
        kept = sqlite3.connect(copies[0]["path"])
        assert kept.execute("SELECT COUNT(*) FROM biometric_samples").fetchone()[0] == 3
        kept.close()

    db.reset_migration_cache()
    from app.main import create_app
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT) as client:
        assert client.get("/health").json()["status"] == "ok"
        # The search index is built from what was already there, and old tokens carry on without an end.
        from app.core import data_fixes
        from app.store import agents, records, search
        data_fixes.run_pending()
        assert search.ready(), data_fixes.status()
        assert records.list_records("prf_a", q="a1c")["total"] == 1
        if version >= 3:
            assert [t["expires_at"] for t in agents.list_tokens()] == [None]
        if version >= 9:      # from here the seed has no account: the password came from the settings above
            return
        client.headers.update(CSRF)
        assert client.post("/api/auth/login", json={"password": PASSPHRASE}).status_code == 200
        assert client.get("/api/status").json()["account"]["owner"]
        for url in ("/api/records?category=labs", "/api/biometrics/overview", "/api/connections", "/api/devices",
                    "/api/biometrics/workouts?days=0", "/api/biometrics/events?days=0", "/api/insights", "/api/profiles", "/api/audit"):
            assert client.get(url).status_code == 200, url
