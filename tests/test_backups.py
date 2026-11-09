"""A backup brings everything back: on this computer or a new one, from the command line, Settings or the first-run screen,
including sign-ins and passwords. Older backups still work or say why not, and nothing is ever left half done."""

from __future__ import annotations

import base64
import errno
import io
import json
import os
import shutil
import sqlite3
import stat
import struct
import sys
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cli
from app.core import config, db, security, settings
from app.services import backup_file, backups
from app.store import connections
from tests.conftest import BASE, CSRF, LOCAL_CLIENT, PASSPHRASE, connect_institution

SECRET = "a passphrase for this backup"


@pytest.fixture(autouse=True)
def quick_passphrases(monkeypatch):
    """The real cost is a third of a second each time, on purpose. The file says what it used, so restoring still works."""
    monkeypatch.setitem(backup_file._SCRYPT, "n", 2**10)


def test_a_passphrase_is_expensive_to_guess():
    source = (config.REPO_ROOT / "app" / "services" / "backup_file.py").read_text()
    assert '_SCRYPT = {"n": 2**17, "r": 8, "p": 1}' in source


def _owner_data(client) -> dict:
    """Records, a saved sign-in, an AI key: what a backup has to bring back."""
    pid = client.get("/api/profiles").json()["profiles"][0]["id"]
    connect_institution(client, "epic-stanford-health-care")
    settings.set("ai.key.openai", "sk-saved-key")
    entry = {"event_type": "note", "category": "notes", "name": "Note", "start": "2024-01-02T03:04:05Z", "note": "JOURNALZEBRA"}
    assert client.post("/api/journal", json=entry).status_code == 200
    with db.read() as conn:
        records = conn.execute("SELECT COUNT(*) FROM clinical_records").fetchone()[0]
    return {"profile": pid, "records": records}


def _make(tmp_path, **kwargs) -> Path:
    kwargs = kwargs or {"passphrase": SECRET}
    return Path(backups.create(tmp_path / "out", **kwargs)["path"])


def accounts_can_sign_in() -> bool:
    from app.store import accounts
    return any(accounts.verify_password(a["id"], PASSPHRASE) for a in accounts.list_accounts())


def _move_to(monkeypatch, folder: Path) -> None:
    monkeypatch.setenv("SYNTROPY_DATA_DIR", str(folder))
    db.reset_migration_cache()
    security.forget_key()


def _sign_in(password: str = PASSPHRASE):
    from app.main import create_app
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT) as c:
        c.headers.update(CSRF)
        yield c, c.post("/api/auth/login", json={"password": password, "name": "Alex"})


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

def test_the_backup_is_encrypted_and_holds_no_readable_text(client, tmp_path):
    _owner_data(client)
    out = _make(tmp_path)
    raw = out.read_bytes()
    assert out.suffix == ".syntropy" and raw.startswith(backup_file.MAGIC)
    assert b"JOURNALZEBRA" not in raw and b"Stanford" not in raw and b"syntropy.db" not in raw
    if os.name != "nt":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600


@pytest.mark.parametrize("size", [0, 1, backup_file.CHUNK - 1, backup_file.CHUNK, 3 * backup_file.CHUNK + 5])
def test_any_size_comes_back_exactly(size):
    plain = os.urandom(size)
    out = io.BytesIO()
    sealer = backup_file.Sealer(out, backup_file.new_seal_key(SECRET))
    for i in range(0, size, 70000):
        sealer.write(plain[i:i + 70000])
    sealer.close()
    out.seek(0)
    assert b"".join(backup_file.unseal(out, SECRET)) == plain


def _sealed(size: int = 3 * backup_file.CHUNK) -> bytes:
    out = io.BytesIO()
    sealer = backup_file.Sealer(out, backup_file.new_seal_key(SECRET))
    sealer.write(os.urandom(size))
    sealer.close()
    return out.getvalue()


def _read(raw: bytes, passphrase: str = SECRET) -> bytes:
    return b"".join(backup_file.unseal(io.BytesIO(raw), passphrase))


def test_the_wrong_passphrase_is_called_that(tmp_path):
    with pytest.raises(backup_file.WrongPassphrase):
        _read(_sealed(), "not the passphrase")


def _frames(raw: bytes) -> list[tuple[int, int]]:
    """(start, end) of each frame, including its length word."""
    pos = len(backup_file.MAGIC) + 4 + struct.unpack(">I", raw[len(backup_file.MAGIC):len(backup_file.MAGIC) + 4])[0]
    out = []
    while pos < len(raw):
        size = struct.unpack(">I", raw[pos:pos + 4])[0] & 0x7fffffff
        out.append((pos, pos + 4 + size))
        pos += 4 + size
    return out


def test_tampering_is_noticed_however_it_is_done():
    raw = _sealed()
    frames = _frames(raw)
    assert len(frames) == 4      # three full chunks, and the empty last one
    flipped = bytearray(raw)
    flipped[frames[1][0] + 50] ^= 1
    swapped = raw[:frames[0][0]] + raw[frames[1][0]:frames[1][1]] + raw[frames[0][0]:frames[0][1]] + raw[frames[2][0]:]
    fake_last = bytearray(raw[:frames[1][1]])
    fake_last[frames[1][0]] |= 0x80      # a shortened file that claims to be complete
    for bad in (bytes(flipped), swapped, raw[:frames[2][0]], raw[:-1], raw + b"more", bytes(fake_last)):
        with pytest.raises(backup_file.Damaged):
            _read(bad)


def test_a_file_can_not_ask_for_all_the_memory():
    raw = _sealed(10)
    start = len(backup_file.MAGIC) + 4
    size = struct.unpack(">I", raw[len(backup_file.MAGIC):start])[0]
    header = json.loads(raw[start:start + size])
    header["kdf"]["n"] = 2**30
    new = json.dumps(header).encode()
    with pytest.raises(backup_file.Damaged):
        _read(backup_file.MAGIC + struct.pack(">I", len(new)) + new + raw[start + size:])


def test_a_short_passphrase_is_refused(client, tmp_path):
    with pytest.raises(backups.BackupProblem):
        _make(tmp_path, passphrase="short")
    assert not list((tmp_path / "out").glob("*")) if (tmp_path / "out").exists() else True


def test_free_space_with_deleted_text_is_not_copied_into_a_backup(client, tmp_path):
    pid = client.get("/api/profiles").json()["profiles"][0]["id"]
    with db.db() as conn:
        conn.execute("PRAGMA secure_delete = OFF")
        for i in range(400):
            conn.execute("INSERT INTO health_events(id, profile_id, event_type, category, name, start_date, end_date, note, created_at) "
                         "VALUES (?, ?, 't', 'c', 'n', '2024-01-01', '2024-01-01', ?, 0)", (f"e{i}", pid, "OLDDELETEDZEBRA" * 20))
        conn.execute("DELETE FROM health_events")
    db.snapshot(tmp_path / "copy.db")
    assert b"OLDDELETEDZEBRA" not in (tmp_path / "copy.db").read_bytes()
    assert sqlite3.connect(tmp_path / "copy.db").execute("PRAGMA user_version").fetchone()[0] == db.newest_schema()


def test_a_backup_interrupted_partway_leaves_nothing_that_looks_like_one(client, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(backups, "_write_zip", fail)
    with pytest.raises(backups.BackupProblem, match="ran out of room"):
        _make(tmp_path)
    assert list((tmp_path / "out").iterdir()) == []
    assert not [f for f in config.data_dir().iterdir() if f.name.startswith(".backup")]


def test_a_backup_that_does_not_read_back_is_not_kept(client, tmp_path, monkeypatch):
    real = backups._read_back

    def spoiled(path, key, unencrypted):
        with open(path, "r+b") as fh:
            fh.seek(-5, 2)
            fh.write(b"xxxxx")
        real(path, key, unencrypted)
    monkeypatch.setattr(backups, "_read_back", spoiled)
    with pytest.raises(backups.BackupProblem, match="read back"):
        _make(tmp_path)
    assert list((tmp_path / "out").iterdir()) == []


def test_what_a_crash_left_in_the_data_folder_is_cleared(isolated_data):
    old = isolated_data / ".backup-work-abc"
    old.mkdir()
    (old / "syntropy.db").write_bytes(b"everything in the clear")
    os.utime(old, (1, 1))
    young = isolated_data / ".restore-young"
    young.mkdir()
    backups.clean_leftovers()
    assert not old.exists() and young.exists()


# ---------------------------------------------------------------------------
# A round trip
# ---------------------------------------------------------------------------

def test_a_round_trip_to_a_new_computer_brings_back_everything(client, tmp_path, monkeypatch, capsys):
    """F089: signing in, saved sign-ins and AI keys, with the secret key from the backup."""
    made = _owner_data(client)
    (config.data_dir() / "uploads").mkdir(exist_ok=True)
    (config.data_dir() / "uploads" / "report.pdf").write_bytes(b"%PDF")
    out = _make(tmp_path)
    original_key = (config.data_dir() / "secret.key").read_bytes()
    new_computer = tmp_path / "new-computer"
    pw = tmp_path / "pw"
    pw.write_text(SECRET + "\n")
    with monkeypatch.context() as m:
        _move_to(m, new_computer)
        cli.main(["restore", str(out), "--passphrase-file", str(pw), "--yes"])
        said = capsys.readouterr().out
        assert "password that was current when this backup was made" in said and "Alex" not in said.split("People:")[0]
        assert (new_computer / "secret.key").read_bytes() == original_key
        assert (new_computer / "uploads" / "report.pdf").read_bytes() == b"%PDF"
        assert settings.get("ai.key.openai") == "sk-saved-key"
        with db.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM clinical_records").fetchone()[0] == made["records"]
            assert conn.execute("SELECT note FROM health_events").fetchone()[0] == "JOURNALZEBRA"
            sources = [dict(r) for r in conn.execute("SELECT * FROM connections WHERE kind = 'ehr'")]
        assert connections.get(sources[0]["id"], include_credentials=True)["credentials"]["access_token"]
        security.check_key()
        assert security.key_problem() is None
        for c, login in _sign_in():
            assert login.status_code == 200, login.text
            assert c.get("/api/status").json()["authenticated"] is True
        db.reset_migration_cache()
    db.reset_migration_cache()
    security.forget_key()


def test_wrong_password_after_restoring_is_still_refused(client, tmp_path, monkeypatch):
    _owner_data(client)
    out = _make(tmp_path)
    with monkeypatch.context() as m:
        _move_to(m, tmp_path / "new")
        item = backups.check(out, SECRET)
        backups.apply(item)
        for _, login in _sign_in("not the password"):
            assert login.status_code == 401
        db.reset_migration_cache()
    db.reset_migration_cache()
    security.forget_key()


def test_the_backup_from_the_command_line_says_what_it_holds(client, tmp_path, capsys):
    _owner_data(client)
    pw = tmp_path / "pw"
    pw.write_text(SECRET)
    cli.main(["backup", str(tmp_path / "out"), "--passphrase-file", str(pw)])
    said = capsys.readouterr().out
    assert "1 person" in said and "record" in said and "encryption key" in said and "SYNTROPY_SECRET_KEY" not in said
    assert list((tmp_path / "out").glob("*.syntropy"))
    assert backups.last()["made_by"] == "command"


def test_an_unattended_backup_does_not_quietly_go_unencrypted(client, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit) as caught:
        cli.main(["backup", str(tmp_path / "out")])
    assert "--passphrase-file" in str(caught.value.code) and "--no-encrypt" in str(caught.value.code)


def test_an_unencrypted_backup_comes_with_a_warning(client, tmp_path, capsys):
    cli.main(["backup", str(tmp_path / "out"), "--no-encrypt"])
    assert "isn't encrypted" in capsys.readouterr().out


def test_passphrase_from_a_script_and_a_folder_it_can_not_write_to(client, tmp_path, monkeypatch):
    def nope(*args, **kwargs):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(backups, "create", nope)
    with pytest.raises(SystemExit) as caught:
        cli.main(["backup", str(tmp_path), "--no-encrypt"])
    assert "can't write to" in str(caught.value.code)


def test_a_folder_never_set_up_is_refused_not_backed_up_empty(tmp_path, monkeypatch):
    """F217: the wrong data folder used to make a backup that looked fine and held nothing."""
    db.ensure_migrated()
    with pytest.raises(SystemExit) as caught:
        cli.main(["backup", str(tmp_path / "out"), "--no-encrypt"])
    assert "nothing to back up" in str(caught.value.code)
    assert list((tmp_path / "out").iterdir()) == [] if (tmp_path / "out").exists() else True


@pytest.fixture
def key_in_the_setting(monkeypatch):
    monkeypatch.setenv("SYNTROPY_SECRET_KEY", "a key kept elsewhere")
    security.forget_key()


def test_a_key_in_the_setting_is_said_not_to_be_in_a_plain_backup(key_in_the_setting, client, tmp_path, capsys, monkeypatch):
    """F345. An encrypted backup has it, protected by the passphrase: see below."""
    out = tmp_path / "out"
    cli.main(["backup", str(out), "--no-encrypt"])
    said = capsys.readouterr().out
    assert "isn't in it" in said and "SYNTROPY_SECRET_KEY" in said and "key that unlocks" not in said
    with zipfile.ZipFile(next(out.glob("*.zip"))) as z:
        assert "secret.key" not in z.namelist()
        assert json.loads(z.read("manifest.json"))["key"] == "setting"


def test_a_key_in_the_setting_restores_with_the_same_setting_and_is_explained_without(tmp_path, monkeypatch):
    from app.main import create_app
    with monkeypatch.context() as m:
        m.setenv("SYNTROPY_SECRET_KEY", "a key kept elsewhere")
        with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT) as c:
            assert c.post("/api/setup", json={"password": PASSPHRASE, "profile_name": "Alex"}, headers=CSRF).status_code == 200
        out = _make(tmp_path)
        with monkeypatch.context() as m2:
            _move_to(m2, tmp_path / "same-setting")
            item = backups.check(out, SECRET)
            assert item.report["key"]["status"] == "setting" and item.report["key"]["ok"]
            backups.discard(item.id)
        m.delenv("SYNTROPY_SECRET_KEY")
        with monkeypatch.context() as m3:       # the passphrase is all a new computer needs
            _move_to(m3, tmp_path / "no-setting")
            item = backups.check(out, SECRET)
            assert item.report["key"]["status"] == "included" and item.report["key"]["ok"]
            backups.apply(item)
            assert accounts_can_sign_in()
    db.reset_migration_cache()
    security.forget_key()


def test_a_plain_backup_with_a_key_in_the_setting_needs_it_for_sign_ins_but_not_records(tmp_path, monkeypatch):
    from app.main import create_app
    with monkeypatch.context() as m:
        m.setenv("SYNTROPY_SECRET_KEY", "a key kept elsewhere")
        with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT) as c:
            assert c.post("/api/setup", json={"password": PASSPHRASE, "profile_name": "Alex"}, headers=CSRF).status_code == 200
        out = _make(tmp_path, unencrypted=True)
        m.delenv("SYNTROPY_SECRET_KEY")
        with monkeypatch.context() as m3:
            _move_to(m3, tmp_path / "no-setting")
            item = backups.check(out)
            assert not item.report["key"]["ok"] and "SYNTROPY_SECRET_KEY" in item.report["key"]["message"]
            with pytest.raises(backups.BackupProblem):
                backups.apply(item)
            backups.discard(item.id)
    db.reset_migration_cache()
    security.forget_key()


def test_a_setting_here_that_overrides_the_key_in_the_backup_is_caught_first(client, tmp_path, monkeypatch):
    settings.set("ai.key.openai", "sk-saved-key")
    out = _make(tmp_path)
    with monkeypatch.context() as m:
        _move_to(m, tmp_path / "new")
        m.setenv("SYNTROPY_SECRET_KEY", "a different key")
        item = backups.check(out, SECRET)
        assert item.report["key"]["status"] == "setting-differs" and not item.report["key"]["ok"]
        backups.discard(item.id)
    db.reset_migration_cache()
    security.forget_key()


# ---------------------------------------------------------------------------
# F090: over an existing folder, and with a leftover log
# ---------------------------------------------------------------------------

def _keep_the_log(conn) -> None:
    """Rows in the log, not checkpointed, as a killed server leaves it."""
    conn.execute("PRAGMA wal_autocheckpoint = 0")


def test_restoring_over_a_folder_with_a_leftover_log_gives_the_backup_exactly(client, tmp_path):
    _owner_data(client)
    out = _make(tmp_path)
    with db.read() as c:
        wanted = c.execute("SELECT COUNT(*) FROM health_events").fetchone()[0]
    db.close_readers()
    path = config.db_path()
    # More rows since the backup, left in the log of a process that was killed.
    writer = sqlite3.connect(path, isolation_level=None)
    _keep_the_log(writer)
    pid = writer.execute("SELECT id FROM profiles").fetchone()[0]
    writer.execute("BEGIN")
    for i in range(3000):
        writer.execute("INSERT INTO health_events(id, profile_id, event_type, category, name, start_date, end_date, created_at) "
                       "VALUES (?, ?, 't', 'c', 'n', '2024-01-01', '2024-01-01', 0)", (f"late{i}", pid))
    writer.execute("COMMIT")
    wal = Path(f"{path}-wal")
    assert wal.stat().st_size > 0
    saved_wal = wal.read_bytes()
    saved_shm = Path(f"{path}-shm").read_bytes()
    writer.close()
    wal.write_bytes(saved_wal)     # closing tidied it up; a killed process wouldn't have
    Path(f"{path}-shm").write_bytes(saved_shm)

    item = backups.check(out, SECRET)
    done = backups.apply(item)
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert check.execute("SELECT COUNT(*) FROM health_events").fetchone()[0] == wanted
    check.close()
    # What was replaced is whole, with the rows the backup didn't have.
    copy = sqlite3.connect(Path(done["safety_copy"]) / "syntropy.db")
    assert copy.execute("SELECT COUNT(*) FROM health_events").fetchone()[0] == wanted + 3000
    copy.close()


def test_restore_is_refused_while_the_server_runs(client, tmp_path, monkeypatch):
    out = _make(tmp_path)
    keep = db.open_connection(config.db_path())       # as any running server holds it
    keep.execute("SELECT 1 FROM profiles").fetchone()
    try:
        before = config.db_path().stat().st_mtime_ns
        with pytest.raises(SystemExit) as caught:
            cli.main(["restore", str(out), "--yes"])
        assert "Nothing was changed" in str(caught.value.code)
        assert config.db_path().stat().st_mtime_ns == before
    finally:
        keep.close()
    monkeypatch.setattr(cli, "_running_port", lambda: 8123)
    monkeypatch.setattr(cli, "_answers", lambda host, port: True)
    with pytest.raises(SystemExit) as caught:
        cli.main(["restore", str(out), "--yes"])
    assert "is running" in str(caught.value.code)


def test_a_check_changes_nothing_and_says_what_it_found(client, tmp_path, capsys, monkeypatch):
    _owner_data(client)
    out = _make(tmp_path)
    pw = tmp_path / "pw"
    pw.write_text(SECRET)
    before = (config.db_path().stat().st_mtime_ns, set(p.name for p in config.data_dir().iterdir()))
    with db.read() as conn:
        count = conn.execute("SELECT COUNT(*) FROM health_events").fetchone()[0]
    with db.db() as conn:
        conn.execute("DELETE FROM health_events")
    cli.main(["restore", str(out), "--check", "--passphrase-file", str(pw)])
    said = capsys.readouterr().out
    assert "undamaged" in said and "Alex" in said and "Nothing was changed" in said and "encryption key" in said
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM health_events").fetchone()[0] == 0
    assert count
    assert set(p.name for p in config.data_dir().iterdir()) - before[1] <= {"syntropy.db-wal", "syntropy.db-shm"}


def test_anything_in_a_bad_state_leaves_things_as_they_were(client, tmp_path, monkeypatch):
    """A restored file that won't open puts back what was there."""
    _owner_data(client)
    out = _make(tmp_path)
    with db.read() as conn:
        before = conn.execute("SELECT COUNT(*) FROM clinical_records").fetchone()[0]
    with db.db() as conn:
        conn.execute("DELETE FROM clinical_records WHERE rowid IN (SELECT rowid FROM clinical_records LIMIT 1)")
    db.close_readers()

    def broken(*args, **kwargs):
        raise sqlite3.DatabaseError("won't open")
    item = backups.check(out, SECRET)
    with monkeypatch.context() as m:
        m.setattr(backups, "_settle", broken)
        with pytest.raises(backups.BackupProblem, match="put back"):
            backups.apply(item)
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM clinical_records").fetchone()[0] == before - 1


def test_a_swap_that_fails_partway_puts_everything_back(client, tmp_path, monkeypatch):
    _owner_data(client)
    out = _make(tmp_path)
    key = (config.data_dir() / "secret.key").read_bytes()
    with db.db() as conn:
        conn.execute("DELETE FROM health_events")
    db.close_readers()
    item = backups.check(out, SECRET)
    real = os.replace
    calls = []

    def fail_on_key(src, dst):
        calls.append(str(dst))
        if str(dst).endswith("secret.key"):
            raise PermissionError(13, "in use")
        return real(src, dst)
    with monkeypatch.context() as m:
        m.setattr(backups.os, "replace", fail_on_key)
        with pytest.raises(PermissionError):
            backups._swap(item)
    assert (config.data_dir() / "secret.key").read_bytes() == key
    db.reset_migration_cache()
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM health_events").fetchone()[0] == 0
    backups.discard(item.id)


def test_only_the_last_safety_copies_are_kept(client, tmp_path):
    out = _make(tmp_path)
    for _ in range(4):
        db.close_readers()
        backups.apply(backups.check(out, SECRET))
    assert len(list((config.db_path().parent / db.PRE_RESTORE_DIR).iterdir())) == backups.SAFETY_COPIES_KEPT


def test_deleting_someone_removes_the_safety_copies_too(client, tmp_path):
    other = client.post("/api/profiles", json={"name": "Sam", "relationship": "other"}).json()["id"]
    out = _make(tmp_path)
    db.close_readers()
    backups.apply(backups.check(out, SECRET))
    assert (config.db_path().parent / db.PRE_RESTORE_DIR).exists()
    db.reset_migration_cache()
    r = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    r.headers.update(CSRF)
    r.post("/api/auth/login", json={"password": PASSPHRASE, "name": "Alex"})
    assert r.delete(f"/api/profiles/{other}").status_code == 200
    assert not (config.db_path().parent / db.PRE_RESTORE_DIR).exists()


# ---------------------------------------------------------------------------
# Older backups, and files that aren't backups
# ---------------------------------------------------------------------------

def _old_zip(tmp_path) -> Path:
    copy = tmp_path / "copy.db"
    db.snapshot(copy)
    target = tmp_path / "syntropy-backup-20240101-000000.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(copy, "syntropy.db")
        z.write(config.data_dir() / "secret.key", "secret.key")
    return target


def test_an_older_zip_restores_with_its_key(client, tmp_path, monkeypatch):
    _owner_data(client)
    old = _old_zip(tmp_path)
    with monkeypatch.context() as m:
        _move_to(m, tmp_path / "new")
        item = backups.check(old)
        assert item.report["kind"] == "older-zip" and item.report["key"]["status"] == "included"
        backups.apply(item)
        for _, login in _sign_in():
            assert login.status_code == 200
        db.reset_migration_cache()
    db.reset_migration_cache()
    security.forget_key()


def test_a_database_alone_from_settings_says_it_needs_the_key(plain_database, client, tmp_path, monkeypatch):
    """The .db older versions downloaded: on a new computer it needs secret.key, and says so before anything changes."""
    _owner_data(client)
    bare = tmp_path / "syntropy-backup-20240101-000000.db"
    db.snapshot(bare)
    key = tmp_path / "kept.key"
    shutil.copy(config.data_dir() / "secret.key", key)
    with monkeypatch.context() as m:
        _move_to(m, tmp_path / "new")
        item = backups.check(bare)
        assert item.report["kind"] == "database-only" and not item.report["key"]["ok"]
        assert "secret.key" in item.report["key"]["message"]
        with pytest.raises(backups.BackupProblem):
            backups.apply(item)
        assert not config.db_path().exists()
        backups.discard(item.id)
        # With the file, it all works.
        item = backups.check(bare, key_file=key)
        assert item.report["key"]["ok"]
        backups.apply(item)
        for _, login in _sign_in():
            assert login.status_code == 200
        db.reset_migration_cache()
    db.reset_migration_cache()
    security.forget_key()


def test_a_database_alone_on_this_computer_works_with_the_key_here(client, tmp_path):
    _owner_data(client)
    bare = tmp_path / "old.db"
    db.snapshot(bare)
    db.close_readers()
    item = backups.check(bare)
    assert item.report["key"]["status"] == "here"
    backups.apply(item)
    assert settings.get("ai.key.openai") == "sk-saved-key"


def test_a_database_alone_can_be_restored_without_its_key_if_someone_insists(plain_database, client, tmp_path, monkeypatch, capsys):
    _owner_data(client)
    bare = tmp_path / "old.db"
    db.snapshot(bare)
    with monkeypatch.context() as m:
        _move_to(m, tmp_path / "new")
        with pytest.raises(SystemExit) as caught:
            cli.main(["restore", str(bare), "--yes"])
        assert "--without-key" in str(caught.value.code) and not config.db_path().exists()
        cli.main(["restore", str(bare), "--yes", "--without-key"])
        assert "Restored" in capsys.readouterr().out
        # Records are there; the problem page explains the key, as for any lost key.
        with db.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM clinical_records").fetchone()[0]
        with pytest.raises(db.StartupProblem) as problem:
            security.master_key()
        assert problem.value.kind == "key"
        db.reset_migration_cache()
    db.reset_migration_cache()
    security.forget_key()


def test_a_backup_from_an_older_schema_is_upgraded(client, tmp_path, monkeypatch):
    from tests.test_migrations import _build
    old = tmp_path / "old-schema.db"
    conn = _build(old, 9)
    conn.execute("INSERT INTO profiles(id, name, relationship, is_default, created_at, updated_at) VALUES ('p', 'Old', 'self', 1, 0, 0)")
    conn.close()
    with monkeypatch.context() as m:
        _move_to(m, tmp_path / "new")
        item = backups.check(old)
        assert item.report["upgrade_needed"] and item.report["schema_version"] == 9
        backups.apply(item)
        with db.read() as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == db.newest_schema()
            assert c.execute("SELECT name FROM profiles").fetchone()[0] == "Old"
        db.reset_migration_cache()
    db.reset_migration_cache()
    security.forget_key()


def test_a_backup_from_a_newer_version_is_refused(client, tmp_path):
    newer = tmp_path / "newer.db"
    db.snapshot(newer)
    conn = sqlite3.connect(newer)
    conn.execute(f"PRAGMA user_version = {db.newest_schema() + 1}")
    conn.close()
    with pytest.raises(backups.BackupFileError, match="newer version"):
        backups.check(newer)


def _zip_with(tmp_path, entries: dict) -> Path:
    target = tmp_path / "made.zip"
    with zipfile.ZipFile(target, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return target


@pytest.mark.parametrize("name", ["../evil", "/abs/evil", "uploads/../../evil", "other.txt", "uploads/c:d",
                                  pytest.param("uploads/a\\b", marks=pytest.mark.skipif(sys.platform == "win32", reason="zipfile reads \\ as / on Windows: uploads/a/b"))])
def test_a_zip_can_not_put_files_anywhere_else(client, tmp_path, name):
    db.snapshot(tmp_path / "s.db")
    bad = _zip_with(tmp_path, {"syntropy.db": (tmp_path / "s.db").read_bytes(), name: b"x"})
    with pytest.raises(backups.BackupFileError, match="unexpected"):
        backups.check(bad)
    assert not (tmp_path / "evil").exists() and not (config.data_dir().parent / "evil").exists()


def test_things_that_are_not_backups_are_called_that(client, tmp_path):
    (tmp_path / "t.txt").write_text("hello")
    (tmp_path / "e.db").write_bytes(b"SQLite format 3\0" + b"\0" * 5000)
    conn = sqlite3.connect(tmp_path / "other.db")
    conn.execute("CREATE TABLE x(a)")
    conn.execute("PRAGMA user_version = 3")
    conn.close()
    for path, words in ((tmp_path / "t.txt", "isn't a Syntropy Health backup"),
                        (_zip_with(tmp_path, {"a.txt": b"hi"}), "no syntropy.db"),
                        (tmp_path / "other.db", "isn't a Syntropy Health database")):
        with pytest.raises(backups.BackupFileError, match=words):
            backups.check(path)
    with pytest.raises(backups.BackupFileError):
        backups.check(tmp_path / "e.db")
    assert not [f for f in config.data_dir().iterdir() if f.name.startswith(".restore")]


def test_a_damaged_database_in_a_backup_is_caught(plain_database, client, tmp_path):
    db.snapshot(tmp_path / "s.db")
    raw = bytearray((tmp_path / "s.db").read_bytes())
    for i in range(8192, len(raw) - 100, 1000):
        raw[i:i + 8] = b"\xff" * 8
    (tmp_path / "s.db").write_bytes(bytes(raw))
    with pytest.raises(backup_file.Damaged):
        backups.check(tmp_path / "s.db")


def test_a_corrupted_zip_is_caught(client, tmp_path):
    out = _make(tmp_path, unencrypted=True)
    raw = bytearray(out.read_bytes())
    raw[len(raw) // 2] ^= 0xff
    out.write_bytes(bytes(raw))
    with pytest.raises(backups.BackupFileError):
        backups.check(out)


def test_the_key_alone_can_be_taken_from_a_backup(client, tmp_path, capsys):
    settings.set("ai.key.openai", "sk-saved-key")
    pw = tmp_path / "pw"
    pw.write_text(SECRET)
    out = _make(tmp_path)
    key = config.data_dir() / "secret.key"
    original = key.read_bytes()
    with db.db() as conn:
        conn.execute("INSERT INTO app_settings(key, value, updated_at) VALUES ('newer', '1', 0)")
    db.close_readers()
    key.unlink()
    security.forget_key()
    cli.main(["restore", str(out), "--key-only", "--passphrase-file", str(pw)])
    assert key.read_bytes() == original and "weren't touched" in capsys.readouterr().out
    with db.read() as conn:
        assert conn.execute("SELECT 1 FROM app_settings WHERE key = 'newer'").fetchone()


# ---------------------------------------------------------------------------
# From Settings and the first-run screen
# ---------------------------------------------------------------------------

def _check(c, raw: bytes, passphrase=SECRET, **extra):
    return c.post("/api/restore/check", files={"file": ("backup.syntropy", raw, "application/octet-stream"), **extra},
                  data={"passphrase": passphrase} if passphrase else {})


def test_settings_downloads_an_encrypted_backup_and_does_not_keep_it(client, monkeypatch):
    _owner_data(client)
    monkeypatch.setattr(backups, "DOWNLOAD_GRACE", 0.05)
    made = client.post("/api/backups", json={"passphrase": SECRET})
    assert made.status_code == 200 and made.json()["encrypted"] and made.json()["filename"].endswith(".syntropy")
    assert SECRET not in made.json()["download"]
    got = client.get(made.json()["download"])
    assert got.status_code == 200 and got.content.startswith(backup_file.MAGIC)
    assert "attachment" in got.headers["content-disposition"]
    for _ in range(100):
        if not [f for f in config.data_dir().iterdir() if f.name.startswith(".backup")]:
            break
        time.sleep(0.02)
    assert not [f for f in config.data_dir().iterdir() if f.name.startswith(".backup")]
    assert client.get(made.json()["download"]).status_code == 404
    assert client.get("/api/backups").json()["last"]["encrypted"] is True


def test_deleting_someone_clears_copies_waiting_to_be_downloaded_or_restored(client):
    pid = _owner_data(client)["profile"]
    address = client.post("/api/backups", json={"unencrypted": True}).json()["download"]
    raw = client.get(address).content
    staged = _check(client, raw, passphrase=None).json()["id"]
    assert [f for f in config.data_dir().iterdir() if f.name.startswith((".backup-download-", ".restore-"))]
    assert client.delete(f"/api/profiles/{pid}/data").status_code == 200
    assert not [f for f in config.data_dir().iterdir() if f.name.startswith((".backup-download-", ".restore-"))]
    assert client.get(address).status_code == 404
    assert client.post("/api/restore/apply", json={"id": staged}).status_code == 404


def test_a_download_fetched_twice_in_a_row_works(client):
    """The Android app's web view asks first and gives up when it sees a download, then the app fetches it itself. A
    dropped connection looks the same. The first request can't be the end of the file."""
    _owner_data(client)
    address = client.post("/api/backups", json={"passphrase": SECRET}).json()["download"]
    first = client.get(address)
    second = client.get(address)
    assert second.status_code == 200 and second.content == first.content


def test_settings_needs_a_passphrase_or_an_explicit_choice(client):
    r = client.post("/api/backups", json={})
    assert r.status_code == 400 and "passphrase" in r.json()["detail"]
    assert client.post("/api/backups", json={"passphrase": "short"}).status_code == 400
    assert client.post("/api/backups", json={"unencrypted": True}).status_code == 200


def test_the_old_address_explains_itself(client):
    assert client.get("/api/export/backup").status_code == 410


def test_a_running_import_stops_before_a_restore_in_the_server(client, monkeypatch):
    from app.services import imports
    _owner_data(client)
    raw = client.get(client.post("/api/backups", json={"passphrase": SECRET}).json()["download"]).content
    order = []
    real_pause = db.pause
    monkeypatch.setattr(imports, "stop_running", lambda: order.append("imports stopped"))
    monkeypatch.setattr(db, "pause", lambda text: (order.append("paused"), real_pause(text)))
    assert client.post("/api/restore/apply", json={"id": _check(client, raw).json()["id"]}).status_code == 200
    assert order[:2] == ["imports stopped", "paused"]


def test_restoring_in_the_running_server(client, tmp_path):
    made = _owner_data(client)
    raw = client.get(client.post("/api/backups", json={"passphrase": SECRET}).json()["download"]).content
    with db.db() as conn:
        conn.execute("DELETE FROM clinical_records")
        conn.execute("UPDATE profiles SET name = 'Changed since'")

    assert _check(client, raw, "not the passphrase").status_code == 400
    assert "passphrase" in _check(client, raw, None).json()["detail"]
    checked = _check(client, raw)
    assert checked.status_code == 200, checked.text
    report = checked.json()
    assert report["people"] == ["Alex"] and report["counts"]["records"] == made["records"] and report["key"]["ok"]
    assert report["replaces"]["people"] == ["Changed since"]
    # Only a check so far.
    assert client.get("/api/profiles").json()["profiles"][0]["name"] == "Changed since"

    done = client.post("/api/restore/apply", json={"id": report["id"]})
    assert done.status_code == 200, done.text
    assert "password" in done.json()["sign_in"] and done.json()["safety_copy"]
    # Browsers sign in again, with the password from the backup.
    assert client.get("/api/profiles").status_code == 401
    assert client.post("/api/auth/login", json={"password": PASSPHRASE, "name": "Alex"}).status_code == 200
    assert client.get("/api/profiles").json()["profiles"][0]["name"] == "Alex"
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM clinical_records").fetchone()[0] == made["records"]
    assert settings.get("ai.key.openai") == "sk-saved-key"
    assert client.get("/health").status_code == 200
    log = client.get("/api/audit").json()["events"]
    assert any(e["action"] == "backup.restored" for e in log)
    assert client.post("/api/restore/apply", json={"id": report["id"]}).status_code == 404


def test_only_the_owner_restores(client):
    from app.store import accounts
    raw = b"x"
    r = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    r.headers.update(CSRF)
    assert _check(r, raw).status_code == 401
    assert r.post("/api/restore/apply", json={"id": "x"}).status_code == 401
    assert r.post("/api/backups", json={"unencrypted": True}).status_code == 401
    assert accounts.any_accounts()


def test_a_member_can_not_restore_or_back_up(client):
    from tests.test_accounts import _join
    pid = client.post("/api/profiles", json={"name": "Maria", "relationship": "other"}).json()["id"]
    member, _ = _join(client, pid)
    assert _check(member, b"x").status_code == 403
    assert member.post("/api/backups", json={"unencrypted": True}).status_code == 403
    assert member.get("/api/backups").status_code == 403


def test_a_busy_server_is_not_restored_underneath(client, tmp_path, monkeypatch):
    raw = client.get(client.post("/api/backups", json={"passphrase": SECRET}).json()["download"]).content
    report = _check(client, raw).json()
    monkeypatch.setattr(backups, "_BUSY_SECONDS", 0.05)
    release = []

    def hold():
        with db.read():
            release.append(1)
            import time
            while len(release) < 2:
                time.sleep(0.01)
    import threading
    t = threading.Thread(target=hold)
    t.start()
    while not release:
        pass
    r = client.post("/api/restore/apply", json={"id": report["id"]})
    release.append(1)
    t.join()
    assert r.status_code == 400 and "busy" in r.json()["detail"]
    assert client.get("/health").status_code == 200
    assert client.get("/api/profiles").status_code == 200


def test_another_program_using_the_database_stops_a_restore_in_the_server(client):
    raw = client.get(client.post("/api/backups", json={"passphrase": SECRET}).json()["download"]).content
    report = _check(client, raw).json()
    other = sqlite3.connect(config.db_path())
    other.execute("SELECT 1 FROM profiles").fetchone()
    try:
        r = client.post("/api/restore/apply", json={"id": report["id"]})
    finally:
        other.close()
    assert r.status_code == 400 and "Another program" in r.json()["detail"]
    assert client.get("/api/profiles").status_code == 200


_HOLDER = """
import sys
from app.core import db
from app.core.sqlite import sqlite3
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
db.use_key(conn, sys.argv[2])
conn.execute("SELECT COUNT(*) FROM t").fetchone()
print("open", flush=True)
sys.stdin.read()
"""


def test_a_program_holding_the_database_stops_a_restore_when_it_can_not_be_read_here(tmp_path, monkeypatch):
    """The command line without the key (a Keychain locked over SSH, say) can't read an encrypted database, and SQLite asks for
    its lock only after reading: the desktop app holding the file looked like nothing at all, and a restore went ahead under it."""
    from app.core.sqlite import HAS_ENCRYPTION, sqlite3 as cipher
    if not HAS_ENCRYPTION:
        pytest.skip("needs sqlcipher3")
    import subprocess
    path, key = config.db_path(), "x'" + "11" * 32 + "'"
    conn = cipher.connect(str(path), isolation_level=None)
    db.use_key(conn, key)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.close()
    assert db.is_encrypted(path)
    with pytest.raises(db.StartupProblem):      # no key here
        security.master_key()
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(path), key], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True, cwd=config.REPO_ROOT, env={**os.environ, "PYTHONPATH": str(config.REPO_ROOT)})
    try:
        assert holder.stdout.readline().strip() == "open"
        assert "Another program is using the database" in (backups.in_use_elsewhere() or "")
    finally:
        holder.communicate("", timeout=30)
    assert backups.in_use_elsewhere() is None       # let go: a damaged or unopenable file can still be restored over
    cli._pid_file().write_text(f"{os.getpid()} 8123")      # the desktop app says it's serving, as `serve` does
    # On Windows the port is believed only from a process started as the server or the app; this one is pytest.
    monkeypatch.setattr(cli, "_windows_command_line", lambda pid: "python -m app.cli serve" if pid == os.getpid() else "")
    try:
        assert cli._running_port() == 8123
    finally:
        cli.note_stopped()
    assert not cli._pid_file().exists()


def test_a_system_lock_that_can_not_be_asked_about_stops_a_restore(tmp_path, monkeypatch):
    config.db_path().write_bytes(os.urandom(8192))       # can't be read: no key, or damage
    monkeypatch.setattr(backups, "_locked_by_another_program", lambda path: (_ for _ in ()).throw(OSError(errno.ENOLCK, "No locks")))
    said = backups.in_use_elsewhere()
    assert said and "couldn't tell" in said and "No locks" in said


def test_while_restoring_everything_else_is_told_to_wait(client, monkeypatch):
    raw = client.get(client.post("/api/backups", json={"passphrase": SECRET}).json()["download"]).content
    report = _check(client, raw).json()
    seen = {}
    real = backups._swap

    def look(item):
        seen["problem"] = client.app.state.problem
        with pytest.raises(db.StartupProblem):
            with db.read():
                pass
        return real(item)
    monkeypatch.setattr(backups, "_swap", look)
    assert client.post("/api/restore/apply", json={"id": report["id"]}).status_code == 200
    assert seen["problem"] == backups.RESTORING_TEXT and client.app.state.problem is None


def test_restoring_from_the_first_run_screen(client, tmp_path, monkeypatch):
    _owner_data(client)
    raw = client.get(client.post("/api/backups", json={"passphrase": SECRET}).json()["download"]).content
    from app.main import create_app
    with monkeypatch.context() as m:
        _move_to(m, tmp_path / "new")
        with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT) as fresh:
            assert fresh.get("/api/status").json()["setup_complete"] is False
            assert fresh.post("/api/restore/check", files={"file": ("b", raw)}, data={"passphrase": SECRET}).status_code == 403
            fresh.headers.update(CSRF)
            report = _check(fresh, raw).json()
            assert report["replaces"] is None and report["key"]["ok"]
            done = fresh.post("/api/restore/apply", json={"id": report["id"]})
            assert done.status_code == 200, done.text
            status = fresh.get("/api/status").json()
            assert status["setup_complete"] is True and status["auth_required"] is True
            assert fresh.post("/api/auth/login", json={"password": PASSPHRASE, "name": "Alex"}).status_code == 200
            assert settings.get("ai.key.openai") == "sk-saved-key"
        # And from far away, before setup, nothing.
        db.reset_migration_cache()
    db.reset_migration_cache()
    security.forget_key()


def test_restoring_before_setup_is_only_from_nearby(tmp_path):
    from app.main import create_app
    with TestClient(create_app(), base_url=BASE, client=("8.8.8.8", 5000)) as far:
        far.headers.update(CSRF)
        assert far.post("/api/restore/check", files={"file": ("b", b"x")}).status_code == 403


def test_a_restored_key_of_the_wrong_kind_is_a_problem_not_a_lockout_at_the_door(plain_database, client, tmp_path):
    """A key file for a different database: caught at the check."""
    settings.set("ai.key.openai", "sk-saved-key")
    bare = tmp_path / "old.db"
    db.snapshot(bare)
    wrong = tmp_path / "wrong.key"
    wrong.write_bytes(base64.urlsafe_b64encode(os.urandom(32)))
    item = backups.check(bare, key_file=wrong)
    assert item.report["key"]["status"] == "wrong-key" and not item.report["key"]["ok"]
    backups.discard(item.id)
    junk = tmp_path / "junk.key"
    junk.write_bytes(b"not a key")
    with pytest.raises(backups.BackupFileError, match="isn't valid"):
        backups.check(bare, key_file=junk)


# ---------------------------------------------------------------------------
# F094: as root on Linux
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="Linux and Mac")
def test_as_root_the_service_account_makes_it_and_root_hands_it_over(client, tmp_path, monkeypatch, capsys):
    import pwd
    import subprocess
    me = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(cli, "_systemd_system", lambda: True)
    monkeypatch.setenv("SUDO_UID", str(os.getuid()))
    monkeypatch.setenv("SUDO_GID", str(os.getgid()))
    owners = []
    real_chown = os.chown
    monkeypatch.setattr(os, "chown", lambda p, u, g: owners.append((str(p), u, g)) or real_chown(p, os.getuid(), os.getgid()))
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        assert cmd[:3] == ["runuser", "-u", me.pw_name]
        folder = Path(cmd[cmd.index("backup") + 1])
        (folder / "syntropy-backup-x.syntropy").write_bytes(b"made by the service account")
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    target = tmp_path / "root-only"
    pw = tmp_path / "pw"
    pw.write_text(SECRET)
    cli.main(["--service-account", me.pw_name, "backup", str(target), "--passphrase-file", str(pw)])
    assert (target / "syntropy-backup-x.syntropy").exists()
    assert stat.S_IMODE((target / "syntropy-backup-x.syntropy").stat().st_mode) == 0o600
    assert calls[0][1]["input"] == SECRET
    assert "--passphrase-file" in calls[0][0] and "-" in calls[0][0]
    assert any(p.endswith("syntropy-backup-x.syntropy") and u == os.getuid() for p, u, g in owners)
    assert not [f for f in config.data_dir().iterdir() if f.name.startswith(".backup-work")]


def _as_root(monkeypatch, tmp_path) -> tuple[list[str], Path]:
    """The installer's command, with runuser making a backup: what's tested is root's part."""
    import pwd
    import subprocess

    monkeypatch.setattr(cli, "_systemd_system", lambda: True)

    def fake_run(cmd, **kwargs):
        if "backup" in cmd:
            (Path(cmd[cmd.index("backup") + 1]) / "syntropy-backup-x.syntropy").write_bytes(_MADE)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    pw = tmp_path / "pw"
    pw.write_text(SECRET)
    return ["--service-account", pwd.getpwuid(os.getuid()).pw_name], pw


_MADE = b"made by the service account" * 1000
_linux_command = pytest.mark.skipif(os.name == "nt", reason="the Linux installer's command")


def _no_work_folders() -> bool:
    return not [f for f in config.data_dir().iterdir() if f.name.startswith(".backup-work")]


@_linux_command
@pytest.mark.skipif(os.name != "nt" and os.getuid() == 0, reason="root can write anywhere")
def test_as_root_a_folder_it_can_not_write_to_is_said_before_any_work(client, tmp_path, monkeypatch):
    service, pw = _as_root(monkeypatch, tmp_path)
    target = tmp_path / "nas"
    target.mkdir()
    target.chmod(0o500)
    made = []
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: made.append(a))
    try:
        with pytest.raises(SystemExit) as stopped:
            cli.main([*service, "backup", str(target), "--passphrase-file", str(pw)])
    finally:
        target.chmod(0o700)
    assert "Nothing was backed up" in str(stopped.value.code) and str(target) in str(stopped.value.code)
    assert not made and _no_work_folders()


@_linux_command
def test_as_root_a_folder_that_can_not_be_made_is_said(client, tmp_path, monkeypatch):
    service, pw = _as_root(monkeypatch, tmp_path)
    (tmp_path / "a-file").write_text("x")
    with pytest.raises(SystemExit) as stopped:
        cli.main([*service, "backup", str(tmp_path / "a-file" / "below"), "--passphrase-file", str(pw)])
    assert "Nothing was backed up" in str(stopped.value.code)


@_linux_command
def test_as_root_a_finished_backup_that_can_not_be_handed_over_is_kept(client, tmp_path, monkeypatch):
    """The drive filled up or was unplugged. Making it again may not be possible, so it stays where it's safe."""
    service, pw = _as_root(monkeypatch, tmp_path)
    target = tmp_path / "drive"
    real = os.replace

    def refuse(src, dst):
        if str(dst).startswith(str(target)):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(src, dst)
    monkeypatch.setattr(cli.os, "replace", refuse)
    with pytest.raises(SystemExit) as stopped:
        cli.main([*service, "backup", str(target), "--passphrase-file", str(pw)])
    said = str(stopped.value.code)
    kept = config.data_dir() / "syntropy-backup-x.syntropy"
    assert kept.read_bytes() == _MADE and str(kept) in said and "No space left" in said and str(target) in said
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert not list(target.iterdir()) and _no_work_folders()


@_linux_command
def test_as_root_a_copy_to_another_drive_has_its_real_name_only_once_complete(client, tmp_path, monkeypatch):
    service, pw = _as_root(monkeypatch, tmp_path)
    target = tmp_path / "drive"
    real = os.replace
    seen = []

    def across_drives(src, dst):
        if ".backup-work" in str(src):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        seen.append(sorted(f.name for f in target.iterdir()))
        return real(src, dst)
    monkeypatch.setattr(cli.os, "replace", across_drives)
    cli.main([*service, "backup", str(target), "--passphrase-file", str(pw)])
    assert seen == [["syntropy-backup-x.syntropy.partial"]]
    final = target / "syntropy-backup-x.syntropy"
    assert final.read_bytes() == _MADE
    assert stat.S_IMODE(final.stat().st_mode) == 0o600 and _no_work_folders()


@_linux_command
def test_as_root_a_copy_cut_short_leaves_no_file_with_a_real_name(client, tmp_path, monkeypatch):
    service, pw = _as_root(monkeypatch, tmp_path)
    target = tmp_path / "drive"
    real = os.replace

    def across_drives(src, dst):
        if ".backup-work" in str(src) and str(dst).startswith(str(target)):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(src, dst)

    def cut_short(src, dst, *a):
        dst.write(b"half")
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(cli.os, "replace", across_drives)
    monkeypatch.setattr(cli.shutil, "copyfileobj", cut_short)
    with pytest.raises(SystemExit):
        cli.main([*service, "backup", str(target), "--passphrase-file", str(pw)])
    assert not list(target.iterdir())
    assert (config.data_dir() / "syntropy-backup-x.syntropy").read_bytes() == _MADE


@_linux_command
def test_as_root_a_drive_without_owners_still_gets_the_backup(client, tmp_path, monkeypatch, capsys):
    """exFAT, or a NAS that maps root to nobody: the file is there, and it says who can read it isn't settled."""
    service, pw = _as_root(monkeypatch, tmp_path)
    monkeypatch.setenv("SUDO_UID", str(os.getuid()))
    monkeypatch.setenv("SUDO_GID", str(os.getgid()))
    target = tmp_path / "drive"
    real = os.chown

    def refuse(path, uid, gid):
        if str(path).startswith(str(target)):
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real(path, uid, gid)
    monkeypatch.setattr(cli.os, "chown", refuse)
    cli.main([*service, "backup", str(target), "--passphrase-file", str(pw)])
    assert (target / "syntropy-backup-x.syntropy").exists()
    out = capsys.readouterr().out
    assert "only you can read" not in out and "who can read" in out


@_linux_command
def test_as_root_a_mistyped_file_to_restore_is_said_in_words(client, tmp_path, monkeypatch):
    service, _ = _as_root(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as stopped:
        cli.main([*service, "restore", str(tmp_path / "typo.syntropy")])
    assert "There's no file at" in str(stopped.value.code) and _no_work_folders()
    real = tmp_path / "real.syntropy"
    real.write_bytes(b"x")
    with pytest.raises(SystemExit) as stopped:
        cli.main([*service, "restore", str(real), "--key-file", str(tmp_path / "typo.key")])
    assert "There's no file at" in str(stopped.value.code) and _no_work_folders()


@_linux_command
def test_as_root_a_file_that_can_not_be_copied_for_restoring_is_said_in_words(client, tmp_path, monkeypatch):
    service, _ = _as_root(monkeypatch, tmp_path)
    real = tmp_path / "real.syntropy"
    real.write_bytes(b"x")

    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(cli.shutil, "copyfile", full)
    with pytest.raises(SystemExit) as stopped:
        cli.main([*service, "restore", str(real)])
    assert "No space left" in str(stopped.value.code) and _no_work_folders()


def test_the_ready_dialog_fits_a_phone():
    """A file name in the button pushed Close off a 390 pixel screen, with no way to reach it."""
    script = (config.REPO_ROOT / "app" / "static" / "js" / "backup.js").read_text()
    css = (config.REPO_ROOT / "app" / "static" / "css" / "app.css").read_text()
    assert "Download ${made.filename}" not in script and "The file is called ${made.filename}" in script
    assert "flex-wrap: wrap" in next(line for line in css.splitlines() if line.startswith(".modal-foot "))


def test_the_linux_wrapper_goes_through_root_for_backup_and_restore():
    script = (config.REPO_ROOT / "scripts" / "install.sh").read_text()
    assert '--service-account "$RUN_AS"' in script
    assert "serve|mcp|info|accounts|key|dev)" in script and "backup|restore)" in script


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset")
def test_the_date_in_the_sign_in_advice_is_the_date_here_not_greenwich(monkeypatch):
    """Made in the evening in California it's already tomorrow in UTC, and the report beside it shows today."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        info = {"accounts": [{"has_password": True}]}
        text = backups._sign_in_text(info, {"created_at": "2026-10-31T00:36:00Z"})
        assert "2026-10-30" in text and "2026-10-31" not in text
        assert "made," in backups._sign_in_text(info, {"created_at": "not a date"})
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
