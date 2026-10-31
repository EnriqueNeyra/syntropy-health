"""The database encrypted on disk: what's on the disk, moving a database from before encryption across without anyone being
left without their data (including being stopped at every step), the key's life, and backups. test_encryption_key covers a
database that isn't encrypted, and test_keystore where the key is kept."""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import config, db, encryption, maintenance, security
from app.core.sqlite import HAS_ENCRYPTION, sqlite3
from app.services import backups
from tests.conftest import BASE, CSRF, LOCAL_CLIENT, PASSPHRASE

pytestmark = pytest.mark.skipif(not HAS_ENCRYPTION, reason="needs sqlcipher3, which CI and every release install")

MARKER = "ZEBRAQUARTZ-DIAGNOSIS"
SECRET = "correct horse battery"


def _start():
    from app.main import create_app
    return TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT)


def _set_up(client) -> None:
    client.headers.update(CSRF)
    r = client.post("/api/setup", json={"password": PASSPHRASE, "profile_name": "Alex"})
    assert r.status_code == 200, r.text


def _with_record() -> str:
    from app.store import connections, profiles, records
    pid = profiles.default_profile()["id"]
    source = connections.create(pid, "import", "file", "A file", mode="live")["id"]
    records.upsert_records(pid, source, "A file", [{"resource_type": "Condition", "resource_id": "c1", "category": "conditions", "title": MARKER,
                                          "narrative": f"{MARKER} notes", "raw": {"resourceType": "Condition", "id": "c1"}}])
    return pid


def _plaintext_database(path: Path, version_only: bool = False) -> None:
    """A database as the version before this one left it: unencrypted, in WAL mode, with secrets saved under a secret.key."""
    with pytest.MonkeyPatch.context() as m:
        m.setenv("SYNTROPY_DATABASE_ENCRYPTION", "0")
        db.reset_migration_cache()
        security.forget_key()
        db.ensure_migrated(path)
        if not version_only:
            with TestClient(_app(), base_url=BASE, client=LOCAL_CLIENT) as c:
                _set_up(c)
                _with_record()
                from app.core import settings
                settings.set("ai.key.openai", "sk-saved-key")
        db.reset_migration_cache()
    db.reset_migration_cache()
    security.forget_key()
    assert not db.is_encrypted(path) and path.read_bytes()[:15] == b"SQLite format 3"


def _app():
    from app.main import create_app
    return create_app()


def _on_disk(folder: Path, marker: str = MARKER) -> list[str]:
    """Every file under the folder that holds this in the clear."""
    found = []
    for f in folder.rglob("*"):
        if f.is_file() and marker.encode() in f.read_bytes():
            found.append(str(f.relative_to(folder)))
    return found


# ---------------------------------------------------------------------------
# What's on the disk
# ---------------------------------------------------------------------------

def test_a_new_database_is_encrypted_from_the_start_and_nothing_on_disk_gives_it_away(tmp_path):
    path = config.db_path()
    with _start() as c:
        _set_up(c)
        _with_record()
        assert path.read_bytes()[:15] != b"SQLite format 3"
        assert Path(f"{path}-wal").exists()         # with the server up and the log not yet emptied
        assert _on_disk(tmp_path) == []
        db.checkpoint(path)
    assert _on_disk(tmp_path) == []


def test_the_file_can_not_be_opened_without_the_key(tmp_path):
    with _start() as c:
        _set_up(c)
    path = config.db_path()
    bare = sqlite3.connect(":memory:")
    bare.close()
    assert not db.opens(path, None)
    assert not db.opens(path, security.database_key(os.urandom(32)))
    assert db.opens(path, security.database_key())


def test_temporary_tables_and_sorts_stay_in_memory():
    """SQLCipher doesn't encrypt temporary files, so every connection is told not to make them."""
    db.ensure_migrated()
    with db.read() as conn:
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2
    with db.db() as conn:
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2


def test_a_copy_before_an_upgrade_is_encrypted(tmp_path):
    db.ensure_migrated()
    path = config.db_path()
    conn = db.open_connection(path)
    conn.execute("PRAGMA user_version = 40")
    conn.execute("INSERT OR REPLACE INTO app_settings(key, value, updated_at) VALUES ('marker', ?, 0)", (json.dumps(MARKER),))
    conn.close()
    copy = db._save_copy(path, path.parent / db.PRE_UPGRADE_DIR, 40)
    assert db.is_encrypted(copy) and MARKER.encode() not in copy.read_bytes()
    assert db.opens(copy, security.database_key())
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600 or os.name == "nt"


def test_the_snapshot_a_backup_is_made_from_is_encrypted(tmp_path):
    db.ensure_migrated()
    with db.db() as conn:
        conn.execute("INSERT OR REPLACE INTO app_settings(key, value, updated_at) VALUES ('marker', ?, 0)", (json.dumps(MARKER),))
    target = tmp_path / "snap.db"
    db.snapshot(target)
    assert db.is_encrypted(target) and MARKER.encode() not in target.read_bytes()


# ---------------------------------------------------------------------------
# A database from before: the normal way through
# ---------------------------------------------------------------------------

def test_starting_this_version_encrypts_an_existing_database_and_everything_is_still_there(tmp_path):
    path = config.db_path()
    _plaintext_database(path)
    key_file = tmp_path / "secret.key"
    assert key_file.exists()
    with _start() as c:
        assert db.is_encrypted(path)
        assert db.opens(path, security.database_key())
        assert MARKER.encode() not in path.read_bytes() and not Path(f"{path}{encryption.TEMP_SUFFIX}").exists()
        r = c.post("/api/auth/login", json={"password": PASSPHRASE}, headers=CSRF)
        assert r.status_code == 200, r.text
        from app.core import settings
        from app.store import search
        assert settings.get("ai.key.openai") == "sk-saved-key"
        with db.read() as conn:
            assert conn.execute("SELECT narrative FROM clinical_records").fetchone()[0] == f"{MARKER} notes"
            assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA user_version").fetchone()[0] == db.newest_schema()
        assert search.ready() is not None


def test_the_original_stays_until_this_version_has_shown_it_runs_then_goes(tmp_path):
    path = config.db_path()
    _plaintext_database(path)
    with _start():
        assert any(f.name.startswith("syntropy-v") and not db.is_encrypted(f) for f in (tmp_path / "pre-upgrade").iterdir())
        assert encryption.finish_due(path, running_for=0) is False       # not straight away
        assert encryption.finish_due(path, running_for=100) is False     # nor while it's new
        copies = encryption.plain_copies(path)
        assert copies
        old = time.time() - encryption.KEEP_ORIGINAL_SECONDS - 5
        for copy in copies:
            os.utime(copy, (old, old))
        assert encryption.finish_due(path, running_for=100) is True
        done = encryption.finish_up(path)
        assert done["converted"] == len(copies) and done["left"] == 0
        assert encryption.plain_copies(path) == []
        assert _on_disk(tmp_path) == []
        assert all(db.is_encrypted(c) and db.opens(c, security.database_key()) for c in copies)


def test_older_unencrypted_copies_are_encrypted_too(tmp_path):
    path = config.db_path()
    _plaintext_database(path)
    older = tmp_path / "pre-upgrade" / "syntropy-v50-20260101-000000.db"
    older.parent.mkdir(exist_ok=True)
    shutil.copy(path, older)
    aside = tmp_path / "pre-restore" / "20260101-000000-abc"
    aside.mkdir(parents=True)
    shutil.copy(path, aside / "syntropy.db")
    with _start():
        for copy in encryption.plain_copies(path):
            os.utime(copy, (1, 1))
        encryption.finish_up(path)
    assert _on_disk(tmp_path) == []
    assert db.is_encrypted(older) and db.is_encrypted(aside / "syntropy.db")


def test_a_copy_that_can_not_be_encrypted_and_is_only_a_way_back_is_removed(tmp_path, monkeypatch):
    path = config.db_path()
    _plaintext_database(path)
    with _start():
        monkeypatch.setattr(encryption, "_convert_copy", lambda copy, key: (_ for _ in ()).throw(OSError("no room")))
        done = encryption.finish_up(path)
    assert done["removed"] and not encryption.plain_copies(path)


def test_finishing_leaves_a_key_file_alone_where_it_is_the_only_place_for_the_key(tmp_path):
    path = config.db_path()
    _plaintext_database(path)
    with _start():
        encryption.finish_up(path)
        assert (tmp_path / "secret.key").exists()


def test_after_the_change_the_old_database_is_not_needed_for_the_app_to_work(tmp_path):
    path = config.db_path()
    _plaintext_database(path)
    with _start():
        pass
    db.reset_migration_cache()
    security.forget_key()
    shutil.rmtree(tmp_path / "pre-upgrade")
    with _start() as c:
        assert c.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# Things that stop it, none of which leaves anyone without their data
# ---------------------------------------------------------------------------

def _works_unencrypted(client) -> None:
    assert client.get("/health").status_code == 200
    assert client.post("/api/auth/login", json={"password": PASSPHRASE}, headers=CSRF).status_code == 200
    assert not db.is_encrypted(config.db_path())


def test_without_room_it_carries_on_unencrypted_and_says_why(tmp_path, monkeypatch):
    path = config.db_path()
    _plaintext_database(path)

    class Nearly:
        free = 1024
    # Only here: the rest of the app would have its own things to say about a full disk.
    monkeypatch.setattr(encryption, "shutil", types.SimpleNamespace(disk_usage=lambda p: Nearly, copy2=shutil.copy2))
    with _start() as c:
        _works_unencrypted(c)
        said = encryption.status()
        assert not said["encrypted"] and "disk space" in said["why_not"]
        assert not Path(f"{path}{encryption.TEMP_SUFFIX}").exists()


def test_with_another_program_using_it_it_carries_on_and_tries_again_next_time(tmp_path):
    path = config.db_path()
    _plaintext_database(path)
    other = sqlite3.connect(str(path))
    other.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    with _start() as c:
        _works_unencrypted(c)
        assert "Another program" in encryption.status()["why_not"]
    other.close()
    db.reset_migration_cache()
    security.forget_key()
    with _start():
        assert db.is_encrypted(path)


def test_a_copy_that_does_not_match_is_thrown_away_and_not_tried_again_at_each_start(tmp_path, monkeypatch):
    path = config.db_path()
    _plaintext_database(path)
    before = path.read_bytes()

    def wrong(*a, **k):
        raise encryption._DidntMatch("clinical_records has a different number of rows")
    monkeypatch.setattr(encryption, "_verify", wrong)
    with _start() as c:
        _works_unencrypted(c)
        assert "didn't work" in encryption.status()["why_not"]
    assert not Path(f"{path}{encryption.TEMP_SUFFIX}").exists()
    assert path.read_bytes()[:len(before[:15])] == before[:15]
    calls = []
    monkeypatch.setattr(encryption, "_encrypt_in_place", lambda p: calls.append(p))
    db.reset_migration_cache()
    with _start():
        pass
    assert calls == []
    monkeypatch.setattr(encryption.time, "time", lambda real=time.time: real() + encryption.RETRY_AFTER_FAILURE + 1)
    db.reset_migration_cache()
    with _start():
        pass
    assert calls


def test_it_can_be_turned_off(tmp_path, monkeypatch):
    path = config.db_path()
    _plaintext_database(path)
    monkeypatch.setenv("SYNTROPY_DATABASE_ENCRYPTION", "0")
    with _start() as c:
        _works_unencrypted(c)
        assert "turned off" in encryption.status()["why_not"]


def test_a_rollback_to_the_previous_version_finds_a_database_it_can_read(tmp_path):
    """What the update scripts do when a new version won't start: the old version's restore_pre_upgrade, by the rules of that
    version: the newest copy since the update began, whatever it is, in the database's place."""
    path = config.db_path()
    _plaintext_database(path)
    since = time.time() - 1
    with _start():
        pass
    db.reset_migration_cache()
    assert db.is_encrypted(path)
    restored = db.restore_pre_upgrade(since, path)
    assert restored and not db.is_encrypted(path)
    conn = sqlite3.connect(str(path))
    assert conn.execute("SELECT narrative FROM clinical_records").fetchone()[0] == f"{MARKER} notes"
    conn.close()
    assert (tmp_path / "secret.key").exists()      # which that version will look for
    assert security.key_from_file((tmp_path / "secret.key").read_bytes())


def test_the_command_line_and_mcp_work_with_the_database_as_they_find_it(tmp_path):
    path = config.db_path()
    _plaintext_database(path)
    db.ensure_migrated(path)
    assert not db.is_encrypted(path)
    with db.read() as conn:
        assert conn.execute("SELECT narrative FROM clinical_records").fetchone()[0] == f"{MARKER} notes"


def test_a_database_that_is_already_encrypted_is_left_alone(tmp_path):
    with _start() as c:
        _set_up(c)
    path = config.db_path()
    before = path.stat().st_ino
    db.reset_migration_cache()
    with _start():
        pass
    assert path.stat().st_ino == before


# ---------------------------------------------------------------------------
# Stopped at every step, the way a power cut would
# ---------------------------------------------------------------------------

STEPS = ["key-ready", "locked", "before-export", "exported", "verified", "original-closed", "original-kept", "before-swap", "swapped"]
_STOPPING = textwrap.dedent("""
    import os, sys
    os.environ["SYNTROPY_NO_DOTENV"] = "1"
    from app.core import encryption
    target = sys.argv[1]
    real = encryption._step
    encryption._step = lambda name: os._exit(137) if name == target else real(name)
    encryption.at_start_up()
    os._exit(0)
""")


def _run_stopped_at(step: str) -> None:
    done = subprocess.run([sys.executable, "-c", _STOPPING, step], cwd=config.REPO_ROOT, capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(config.REPO_ROOT)}, timeout=120)
    assert done.returncode == 137, f"It wasn't stopped at {step}: {done.returncode}\n{done.stderr[-2000:]}"


@pytest.mark.parametrize("step", STEPS)
def test_stopped_at_any_step_the_data_is_there_and_the_next_start_finishes(tmp_path, step):
    path = config.db_path()
    _plaintext_database(path)
    expected = _everything_in(path)
    _run_stopped_at(step)
    db.reset_migration_cache()
    security.forget_key()

    # Right now, with nothing having cleaned up: it opens, with everything in it.
    assert _everything_in(path) == expected
    assert db.is_encrypted(path) == (step == "swapped")

    with _start() as c:      # and the next start carries on from there
        assert db.is_encrypted(path)
        assert c.post("/api/auth/login", json={"password": PASSPHRASE}, headers=CSRF).status_code == 200
        with db.read() as conn:
            assert conn.execute("SELECT narrative FROM clinical_records").fetchone()[0] == f"{MARKER} notes"
    assert not list(tmp_path.glob(f"*{encryption.TEMP_SUFFIX}*"))
    assert encryption.finish_up(path)["left"] == 0
    assert _on_disk(tmp_path) == []


def _everything_in(path: Path) -> dict[str, int]:
    """How many rows each table holds, read the way the app reads it."""
    conn = db.open_connection(path)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND rootpage > 0")]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names if n != "app_settings"}
    finally:
        conn.close()


def test_a_temporary_file_a_crash_left_is_cleared_before_starting_again(tmp_path):
    path = config.db_path()
    _plaintext_database(path)
    Path(f"{path}{encryption.TEMP_SUFFIX}").write_bytes(b"half a file")
    with _start():
        assert db.is_encrypted(path)
    assert not Path(f"{path}{encryption.TEMP_SUFFIX}").exists()


def test_the_verification_notices_a_copy_with_rows_missing(tmp_path):
    path = config.db_path()
    _plaintext_database(path)
    source = sqlite3.connect(str(path), isolation_level=None)
    copy = tmp_path / "copy.db"
    key = security.database_key(os.urandom(32))
    encryption._export(source, copy, key, version=source.execute("PRAGMA user_version").fetchone()[0])
    wrong = sqlite3.connect(str(copy), isolation_level=None)
    db.use_key(wrong, key)
    wrong.execute("DELETE FROM clinical_records")
    wrong.close()
    with pytest.raises(encryption._DidntMatch, match="clinical_records|record_search"):
        encryption._verify(source, copy, key, source.execute("PRAGMA user_version").fetchone()[0])
    source.close()


def test_the_verification_notices_changed_values_and_a_copy_with_no_encryption(tmp_path):
    path = config.db_path()
    _plaintext_database(path)
    source = sqlite3.connect(str(path), isolation_level=None)
    version = source.execute("PRAGMA user_version").fetchone()[0]
    copy = tmp_path / "copy.db"
    key = security.database_key(os.urandom(32))
    encryption._export(source, copy, key, version=version)
    wrong = sqlite3.connect(str(copy), isolation_level=None)
    db.use_key(wrong, key)
    wrong.execute("UPDATE clinical_records SET narrative = 'something else'")
    wrong.close()
    with pytest.raises(encryption._DidntMatch, match="different rows"):
        encryption._verify(source, copy, key, version)
    plain = tmp_path / "plain.db"
    encryption._export(source, plain, None, version=version)
    with pytest.raises(encryption._DidntMatch, match="without a key"):
        encryption._verify(source, plain, key, version)
    source.close()


def test_rows_keep_their_ids_in_the_encrypted_copy(tmp_path):
    """The search index points at records by these. SQLCipher's export renumbers a table with neither a primary key of its own
    nor an index, so none of ours may be one."""
    db_path = config.db_path()
    db.ensure_migrated(db_path)
    conn = db.open_connection(db_path)
    try:
        bare = []
        for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND rootpage > 0 AND name != 'sqlite_sequence'").fetchall():
            info = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
            integer_key = sum(1 for c in info if c[5]) == 1 and any(c[5] and c[2].upper() == "INTEGER" for c in info)
            if not integer_key and "WITHOUT ROWID" not in (sql or "").upper() and not conn.execute(f'PRAGMA index_list("{name}")').fetchall():
                bare.append(name)
    finally:
        conn.close()
    assert bare == []


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------

def test_a_lost_key_is_said_plainly_and_nothing_is_made_or_changed(tmp_path):
    with _start() as c:
        _set_up(c)
    path = config.db_path()
    before = path.read_bytes()
    (tmp_path / "secret.key").unlink()
    db.reset_migration_cache()
    security.forget_key()
    with _start() as c:
        r = c.get("/api/status")
        assert r.status_code == 503 and "encrypted" in r.json()["detail"] and "can't be" in r.json()["detail"]
        assert c.get("/health").status_code == 503
    assert not (tmp_path / "secret.key").exists()
    assert path.read_bytes()[:64] == before[:64]


def test_a_phone_is_told_to_wait_and_never_that_it_is_not_paired(tmp_path):
    """A 401 makes a phone throw its token away. A database that can't open yet isn't that."""
    with _start() as c:
        _set_up(c)
    (tmp_path / "secret.key").unlink()
    db.reset_migration_cache()
    security.forget_key()
    with _start() as c:
        r = c.post("/api/ingest/wearables", headers={"X-Syntropy-Device-Token": "whatever", **CSRF}, json={"samples": []})
        assert r.status_code == 503
        assert c.get("/api/wearables/summary", headers={"X-Syntropy-Device-Token": "whatever"}).status_code == 503


def test_putting_the_key_file_back_brings_everything_back(tmp_path):
    with _start() as c:
        _set_up(c)
    kept = (tmp_path / "secret.key").read_bytes()
    (tmp_path / "secret.key").unlink()
    db.reset_migration_cache()
    security.forget_key()
    with _start() as c:
        assert c.get("/health").status_code == 503
    (tmp_path / "secret.key").write_bytes(kept)
    db.reset_migration_cache()
    security.forget_key()
    with _start() as c:
        assert c.get("/health").status_code == 200
        assert c.post("/api/auth/login", json={"password": PASSPHRASE}, headers=CSRF).status_code == 200


def test_a_wrong_key_file_is_not_called_damage_and_changes_nothing(tmp_path):
    with _start() as c:
        _set_up(c)
    (tmp_path / "secret.key").write_bytes(base64.urlsafe_b64encode(os.urandom(32)))
    db.reset_migration_cache()
    security.forget_key()
    with _start() as c:
        assert c.app.state.problem_kind == "key"


def test_key_new_will_not_pretend_to_help_a_database_it_can_not_open(tmp_path, monkeypatch):
    from app import cli
    with _start() as c:
        _set_up(c)
    (tmp_path / "secret.key").unlink()
    db.reset_migration_cache()
    security.forget_key()
    monkeypatch.setattr("builtins.input", lambda prompt="": "NEW")
    with pytest.raises(SystemExit) as stopped:
        cli.main(["key", "new"])
    assert "can't be" in str(stopped.value.code) or "key" in str(stopped.value.code)
    assert not (tmp_path / "secret.key").exists()


def test_key_new_keeps_the_key_of_a_database_it_opens(tmp_path, monkeypatch, capsys):
    from app import cli
    with _start() as c:
        _set_up(c)
    before = (tmp_path / "secret.key").read_bytes()
    db.reset_migration_cache()
    security.forget_key()
    monkeypatch.setattr("builtins.input", lambda prompt="": "NEW")
    cli.main(["key", "new"])
    assert (tmp_path / "secret.key").read_bytes() == before
    assert "that key stays" in capsys.readouterr().out
    assert db.opens(config.db_path(), security.database_key())


def test_the_key_can_be_set_in_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTROPY_SECRET_KEY", "a key kept elsewhere")
    with _start() as c:
        _set_up(c)
    path = config.db_path()
    assert db.is_encrypted(path) and not (tmp_path / "secret.key").exists()
    assert encryption.status()["key_kind"] == "setting"
    monkeypatch.setenv("SYNTROPY_SECRET_KEY", "a different value")
    db.reset_migration_cache()
    with _start() as c:
        assert c.app.state.problem_kind == "key"


def test_the_key_can_be_in_a_file_somewhere_else(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SYNTROPY_DATA_DIR", str(data))
    monkeypatch.setenv("SYNTROPY_KEY_FILE", str(elsewhere / "key"))
    db.reset_migration_cache()
    security.forget_key()
    with _start() as c:
        _set_up(c)
    assert (elsewhere / "key").exists() and not (data / "secret.key").exists()
    assert db.is_encrypted(config.db_path())
    assert encryption.status()["key_kind"] == "key-file"
    # A copy of the data folder alone is no use without it.
    (elsewhere / "key").rename(elsewhere / "moved")
    db.reset_migration_cache()
    security.forget_key()
    with _start() as c:
        assert c.app.state.problem_kind == "key"


def test_a_key_file_someone_made_with_openssl_works(tmp_path, monkeypatch):
    chosen = tmp_path / "chosen.key"
    made = base64.b64encode(bytes(range(32)))
    chosen.write_bytes(made + b"\n")
    monkeypatch.setenv("SYNTROPY_KEY_FILE", str(chosen))
    security.forget_key()
    assert security.master_key() == bytes(range(32))
    chosen.write_text("0123456789abcdef not base64 at all")
    security.forget_key()
    assert len(security.master_key()) == 32


def test_a_missing_key_file_is_made_once_and_not_while_something_needs_the_old_one(tmp_path, monkeypatch):
    chosen = tmp_path / "chosen.key"
    monkeypatch.setenv("SYNTROPY_KEY_FILE", str(chosen))
    security.forget_key()
    db.ensure_migrated()
    assert chosen.exists() and stat.S_IMODE(chosen.stat().st_mode) in (0o600, 0o666)
    chosen.unlink()
    security.forget_key()
    db.reset_migration_cache()
    with pytest.raises(db.StartupProblem) as stopped:
        db.ensure_migrated()
    assert stopped.value.kind == "key" and not chosen.exists()


def test_exporting_the_key_writes_a_private_file_that_works(tmp_path, capsys):
    from app import cli
    db.ensure_migrated()
    target = tmp_path / "kept.key"
    cli.main(["key", "export", str(target)])
    assert security.key_from_file(target.read_bytes()) == security.master_key()
    assert os.name == "nt" or stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(SystemExit):
        cli.main(["key", "export", str(target)])       # never over something


def test_an_unencrypted_copy_on_request_opens_with_any_sqlite(tmp_path, capsys):
    from app import cli
    with _start() as c:
        _set_up(c)
        _with_record()
    target = tmp_path / "leaving.db"
    cli.main(["database", "decrypt", str(target)])
    assert target.read_bytes()[:15] == b"SQLite format 3"
    conn = sqlite3.connect(str(target))
    assert conn.execute("SELECT narrative FROM clinical_records").fetchone()[0] == f"{MARKER} notes"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.newest_schema()
    conn.close()
    assert os.name == "nt" or stat.S_IMODE(target.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

def _backup(tmp_path: Path, **kwargs) -> Path:
    folder = tmp_path / "out"
    made = backups.create(folder, **({"unencrypted": True} if kwargs.get("unencrypted") else {"passphrase": SECRET}))
    return Path(made["path"])


def _move(m, folder: Path) -> None:
    m.setenv("SYNTROPY_DATA_DIR", str(folder))
    db.reset_migration_cache()
    security.forget_key()


def test_an_encrypted_backup_restores_on_a_new_computer_with_only_its_passphrase(tmp_path, monkeypatch):
    with _start() as c:
        _set_up(c)
        _with_record()
        from app.core import settings
        settings.set("ai.key.openai", "sk-saved-key")
        out = _backup(tmp_path)
    assert MARKER.encode() not in out.read_bytes()
    with monkeypatch.context() as m:
        _move(m, tmp_path / "new")
        (tmp_path / "new").mkdir()
        item = backups.check(out, SECRET)
        assert item.report["key"]["status"] == "included" and item.report["key"]["ok"]
        backups.apply(item)
        assert db.is_encrypted(config.db_path())
        from app.core import settings
        assert settings.get("ai.key.openai") == "sk-saved-key"
        with db.read() as conn:
            assert conn.execute("SELECT narrative FROM clinical_records").fetchone()[0] == f"{MARKER} notes"
        db.reset_migration_cache()
    db.reset_migration_cache()
    security.forget_key()


def test_inside_an_encrypted_backup_the_database_is_encrypted_too(tmp_path, monkeypatch):
    from app.services import backup_file
    with _start() as c:
        _set_up(c)
        _with_record()
        out = _backup(tmp_path)
    plain = tmp_path / "opened.zip"
    with open(out, "rb") as fh, open(plain, "wb") as target:
        for chunk in backup_file.unseal(fh, SECRET):
            target.write(chunk)
    import zipfile
    with zipfile.ZipFile(plain) as z:
        inner = z.read("syntropy.db")
        assert inner[:15] != b"SQLite format 3" and MARKER.encode() not in inner
        assert json.loads(z.read("manifest.json"))["format"] == backups.FORMAT
        assert security.key_from_file(z.read("secret.key"))


def test_an_unencrypted_backup_needs_nothing_to_read_and_says_it_is_format_one(tmp_path):
    import zipfile
    with _start() as c:
        _set_up(c)
        _with_record()
        out = _backup(tmp_path, unencrypted=True)
    with zipfile.ZipFile(out) as z:
        inner = z.read("syntropy.db")
        assert inner[:15] == b"SQLite format 3" and MARKER.encode() in inner
        assert json.loads(z.read("manifest.json"))["format"] == backups.PLAIN_FORMAT


def test_an_unencrypted_backup_is_encrypted_as_it_comes_back(tmp_path, monkeypatch):
    with _start() as c:
        _set_up(c)
        _with_record()
        out = _backup(tmp_path, unencrypted=True)
    with monkeypatch.context() as m:
        _move(m, tmp_path / "new")
        (tmp_path / "new").mkdir()
        item = backups.check(out)
        backups.apply(item)
        assert db.is_encrypted(config.db_path())
        with db.read() as conn:
            assert conn.execute("SELECT narrative FROM clinical_records").fetchone()[0] == f"{MARKER} notes"
        assert _on_disk(tmp_path / "new") == []
        db.reset_migration_cache()
    db.reset_migration_cache()
    security.forget_key()


def test_a_backup_from_before_encryption_restores_and_is_encrypted(tmp_path, monkeypatch):
    """The old format: a plain database and its key."""
    path = config.db_path()
    _plaintext_database(path)
    with monkeypatch.context() as m:
        m.setenv("SYNTROPY_DATABASE_ENCRYPTION", "0")
        out = _backup(tmp_path)
    db.reset_migration_cache()
    security.forget_key()
    with monkeypatch.context() as m:
        _move(m, tmp_path / "new")
        (tmp_path / "new").mkdir()
        item = backups.check(out, SECRET)
        assert item.report["key"]["ok"]
        backups.apply(item)
        assert db.is_encrypted(config.db_path())
        from app.core import settings
        assert settings.get("ai.key.openai") == "sk-saved-key"
        db.reset_migration_cache()
    db.reset_migration_cache()
    security.forget_key()


def test_restoring_over_a_database_with_another_key_keeps_the_way_back(tmp_path, monkeypatch):
    with monkeypatch.context() as m:
        _move(m, tmp_path / "there")
        (tmp_path / "there").mkdir()
        with _start() as c:
            _set_up(c)
            _with_record()
            out = _backup(tmp_path)
        db.reset_migration_cache()
    security.forget_key()
    with _start() as c:      # here, with a key of its own
        _set_up(c)
        mine = security.master_key()
    db.reset_migration_cache()
    item = backups.check(out, SECRET)
    done = backups.apply(item)
    assert security.master_key() != mine
    with db.read() as conn:
        assert conn.execute("SELECT narrative FROM clinical_records").fetchone()[0] == f"{MARKER} notes"
    kept = Path(done["safety_copy"]) / "syntropy.db"
    assert db.is_encrypted(kept)
    assert db.opens(kept, security.database_key(mine))
    conn = db.open_copy(kept)
    assert conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 1
    conn.close()
    assert security.database_key(mine) in security.known_database_keys()


def test_a_restore_that_does_not_open_goes_back_to_what_was_here_with_its_key(tmp_path, monkeypatch):
    with monkeypatch.context() as m:
        _move(m, tmp_path / "there")
        (tmp_path / "there").mkdir()
        with _start() as c:
            _set_up(c)
            out = _backup(tmp_path)
        db.reset_migration_cache()
    security.forget_key()
    with _start() as c:
        _set_up(c)
        mine = security.master_key()
    db.reset_migration_cache()
    item = backups.check(out, SECRET)
    monkeypatch.setattr(backups, "_settle", lambda *a: (_ for _ in ()).throw(db.StartupProblem("damaged", "it didn't open")))
    with pytest.raises(backups.BackupProblem):
        backups.apply(item)
    security.forget_key()
    assert security.master_key() == mine
    assert db.opens(config.db_path(), security.database_key(mine))


def test_a_crash_midway_through_a_restore_still_finds_a_key_for_the_database_that_is_here(tmp_path, monkeypatch):
    """The new key is saved before the database moves. If it stops there, the old database still opens: it finds the old key."""
    with _start() as c:
        _set_up(c)
        mine = security.master_key()
    security.adopt_key(os.urandom(32))
    security.forget_key()
    db.reset_migration_cache()
    assert security.master_key() == mine
    assert db.opens(config.db_path(), security.database_key())


def test_key_only_brings_the_key_back_without_touching_the_records(tmp_path, monkeypatch, capsys):
    from app import cli
    with _start() as c:
        _set_up(c)
        _with_record()
        out = _backup(tmp_path)
    (tmp_path / "secret.key").unlink()
    db.reset_migration_cache()
    security.forget_key()
    before = config.db_path().read_bytes()[:64]
    secret = tmp_path / "pass.txt"
    secret.write_text(SECRET)
    cli.main(["restore", str(out), "--key-only", "--passphrase-file", str(secret)])
    assert "key is back" in capsys.readouterr().out
    assert config.db_path().read_bytes()[:64] == before
    security.forget_key()
    db.reset_migration_cache()
    with db.read() as conn:
        assert conn.execute("SELECT narrative FROM clinical_records").fetchone()[0] == f"{MARKER} notes"


def test_a_copy_from_pre_upgrade_can_be_restored_from_the_command_line(tmp_path, capsys):
    from app import cli
    with _start() as c:
        _set_up(c)
        _with_record()
    db.reset_migration_cache()
    copy = tmp_path / "kept.db"
    db.snapshot(copy)
    db.close_readers()
    cli.main(["restore", str(copy), "--yes"])
    assert "Restored" in capsys.readouterr().out
    with db.read() as conn:
        assert conn.execute("SELECT narrative FROM clinical_records").fetchone()[0] == f"{MARKER} notes"


def test_backups_still_report_damage(tmp_path):
    with _start() as c:
        _set_up(c)
        _with_record()
    path = config.db_path()
    db.close_readers()
    db.checkpoint(path)
    raw = bytearray(path.read_bytes())
    for page in range(4, len(raw) // 4096 - 1):
        raw[page * 4096 + 200] ^= 0xff
    path.write_bytes(bytes(raw))
    db.reset_migration_cache()
    with pytest.raises((db.StartupProblem, sqlite3.DatabaseError)):
        db.snapshot(tmp_path / "x.db")
    assert maintenance.check_integrity(path)["ok"] is False


def test_the_desktop_app_can_put_back_a_copy_it_kept_and_refuses_one_it_can_not_open(tmp_path):
    sys.path.insert(0, str(config.REPO_ROOT / "desktop"))
    import recovery
    with _start() as c:
        _set_up(c)
        _with_record()
    path = config.db_path()
    db.close_readers()
    copy = tmp_path / "kept.db"
    db.snapshot(copy)
    aside = recovery.restore(copy, path)
    assert (aside / "syntropy.db").exists()
    db.reset_migration_cache()
    with db.read() as conn:
        assert conn.execute("SELECT narrative FROM clinical_records").fetchone()[0] == f"{MARKER} notes"
    (tmp_path / "secret.key").rename(tmp_path / "moved")
    (tmp_path / "secret.key").write_bytes(base64.urlsafe_b64encode(os.urandom(32)))
    security.forget_key()
    with pytest.raises(recovery.RestoreRefused):
        recovery.restore(copy, path)
    assert db.is_encrypted(path)


# ---------------------------------------------------------------------------
# Who can read the folder
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="file permissions")
def test_everything_in_the_data_folder_is_private_even_what_was_not_before(tmp_path):
    path = config.db_path()
    _plaintext_database(path)
    os.chmod(tmp_path, 0o755)
    (tmp_path / "server.log").write_text("x")
    os.chmod(tmp_path / "server.log", 0o644)
    extra = tmp_path / "pre-restore" / "old"
    extra.mkdir(parents=True)
    (extra / "syntropy.db").write_bytes(b"x")
    os.chmod(extra, 0o755)
    os.chmod(extra / "syntropy.db", 0o644)
    with _start():
        pass
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    for f in tmp_path.rglob("*"):
        assert stat.S_IMODE(f.stat().st_mode) == (0o700 if f.is_dir() else 0o600), f


@pytest.mark.skipif(os.name == "nt", reason="file permissions")
def test_a_program_kept_in_the_folder_stays_a_program_and_an_update_on_its_way_is_left_alone(tmp_path):
    with _start():
        pass
    script = tmp_path / "pre-restore" / "tool"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    update = tmp_path / "updates" / "installer"
    update.parent.mkdir()
    update.write_text("x")
    update.chmod(0o755)
    encryption.make_private(config.db_path())
    assert stat.S_IMODE(script.stat().st_mode) == 0o700
    assert stat.S_IMODE(update.stat().st_mode) == 0o755
    assert stat.S_IMODE(update.parent.stat().st_mode) == 0o700 or stat.S_IMODE(update.parent.stat().st_mode) == 0o755


def test_windows_gets_an_access_list_for_a_folder_outside_the_profile(tmp_path, monkeypatch):
    calls = []

    class Done:
        stdout = '"me","S-1-5-21-111-222-333-1001"\n'

    def run(cmd, **k):
        calls.append(cmd)
        return Done()
    monkeypatch.setattr(encryption.subprocess, "run", run)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "somebody"))
    encryption._windows_acl(tmp_path)
    assert calls[0][0] == "whoami" and calls[1][0] == "icacls" and "/inheritance:r" in calls[1]
    assert "*S-1-5-21-111-222-333-1001:(OI)(CI)F" in calls[1] and "*S-1-5-18:(OI)(CI)F" in calls[1]
    calls.clear()
    monkeypatch.setenv("USERPROFILE", str(tmp_path.parent))      # the profile's own folders are already private
    encryption._windows_acl(tmp_path)
    assert calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows")
def test_windows_acl_really_limits_the_folder(tmp_path, monkeypatch):
    """Only CI can run this."""
    monkeypatch.setenv("USERPROFILE", "C:\\nowhere")
    encryption._windows_acl(tmp_path)
    listed = subprocess.run(["icacls", str(tmp_path)], capture_output=True, text=True, check=True).stdout
    assert "BUILTIN\\Users" not in listed and "Everyone" not in listed


# ---------------------------------------------------------------------------
# What Settings is told
# ---------------------------------------------------------------------------

def test_settings_says_how_it_is_and_only_the_owner_sees_where(tmp_path):
    from tests.test_accounts import MARIA_PASSWORD, _join, _person, _signed_in_owner
    with _start() as c:
        client = _signed_in_owner(c)
        said = client.get("/api/settings/encryption").json()
        assert said["encrypted"] and said["key_kind"] == "file" and said["key_file"].endswith("secret.key")
        maria, _ = _join(client, _person(client, "Maria", "partner"))
        theirs = maria.get("/api/settings/encryption").json()
        assert theirs["encrypted"] and theirs["key_where"] and theirs["key_file"] is None and theirs["plain_copies"] == []
        assert "accounts" not in theirs["password_recovery"] and MARIA_PASSWORD


def test_status_says_how_it_is(tmp_path):
    with _start() as c:
        _set_up(c)
        said = encryption.status()
    assert said["encrypted"] and "AES-256" in said["cipher"] and said["key_kind"] == "file" and said["key_beside_data"]
    assert said["why_not"] is None and said["plain_copies"] == []


def test_the_progress_is_there_while_it_works_and_gone_after(tmp_path, monkeypatch):
    path = config.db_path()
    _plaintext_database(path)
    seen = []
    real = encryption._verify

    def looking(*a, **k):
        seen.append(encryption.progress())
        return real(*a, **k)
    monkeypatch.setattr(encryption, "_verify", looking)
    encryption.at_start_up()
    assert seen and seen[0]["stage"].startswith("Checking") and encryption.progress() is None
