"""Where the key lives: the Keychain (with a keychain of the test's own, never the real one), DPAPI (a stand-in here, the real
thing on Windows in CI), and an owner's password as the way back when those are lost."""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import config, db, encryption, keystore, security
from app.core.sqlite import HAS_ENCRYPTION
from tests.conftest import BASE, CSRF, LOCAL_CLIENT, PASSPHRASE, _background_finished

pytestmark = pytest.mark.skipif(not HAS_ENCRYPTION, reason="needs sqlcipher3, which CI and every release install")

on_a_mac = pytest.mark.skipif(sys.platform != "darwin", reason="the Keychain is on a Mac")


def _start(client=LOCAL_CLIENT):
    from app.main import create_app
    return TestClient(create_app(), base_url=BASE, client=client)


def _set_up(client) -> None:
    client.headers.update(CSRF)
    assert client.post("/api/setup", json={"password": PASSPHRASE, "profile_name": "Alex"}).status_code == 200


def _fresh_start() -> None:
    db.reset_migration_cache()
    security.forget_key()


@pytest.fixture(autouse=True)
def quick_password_locks(monkeypatch):
    monkeypatch.setattr(keystore, "WRAP_N", 2**10)


def _new_keychain(path: Path) -> Path:
    subprocess.run([keystore.SECURITY_TOOL, "create-keychain", "-p", "test", str(path)], check=True, capture_output=True)
    subprocess.run([keystore.SECURITY_TOOL, "unlock-keychain", "-p", "test", str(path)], check=True, capture_output=True)
    subprocess.run([keystore.SECURITY_TOOL, "set-keychain-settings", str(path)], check=True, capture_output=True)
    return path


@pytest.fixture
def keychain(tmp_path, monkeypatch):
    """A keychain file of its own, unlocked, in the test's folder."""
    path = _new_keychain(tmp_path.parent / f"{tmp_path.name}.keychain-db")
    monkeypatch.setenv("SYNTROPY_KEYCHAIN_FILE", str(path))
    monkeypatch.setenv("SYNTROPY_KEY_STORE", keystore.KEYCHAIN)
    yield path
    subprocess.run([keystore.SECURITY_TOOL, "delete-keychain", str(path)], capture_output=True)


@pytest.fixture
def dpapi(monkeypatch):
    """Windows' protected storage, as far as what's around it cares. test_real_dpapi is the real one."""
    def stand_in(data: bytes, protect: bool) -> bytes:
        if protect:
            return b"PROTECTED" + data[::-1]
        if not data.startswith(b"PROTECTED"):
            raise OSError("not ours")
        return data[len(b"PROTECTED"):][::-1]
    monkeypatch.setattr(keystore, "_dpapi", stand_in)
    monkeypatch.setenv("SYNTROPY_KEY_STORE", keystore.DPAPI)


# ---------------------------------------------------------------------------
# The Keychain
# ---------------------------------------------------------------------------

@on_a_mac
def test_a_new_key_goes_in_the_keychain_and_not_on_the_disk(keychain, tmp_path):
    with _start() as c:
        _set_up(c)
    assert not (tmp_path / "secret.key").exists()
    info = json.loads((tmp_path / "key.json").read_text())
    assert info["store"] == keystore.KEYCHAIN and info["account"] and "key" not in info
    key = security.master_key()
    assert base64.urlsafe_b64encode(key) not in (tmp_path / "key.json").read_bytes()
    assert keystore.from_store() == key
    _fresh_start()
    assert security.master_key() == key
    assert db.opens(config.db_path(), security.database_key())


@on_a_mac
def test_nothing_about_the_key_goes_on_a_command_line(keychain, monkeypatch):
    """Every program on the computer can read those."""
    seen = []
    real = subprocess.run

    def watching(cmd, **k):
        seen.append(" ".join(map(str, cmd)))
        return real(cmd, **k)
    monkeypatch.setattr(keystore.subprocess, "run", watching)
    key = os.urandom(32)
    assert keystore.save_to_store(key) == keystore.KEYCHAIN
    encoded = base64.urlsafe_b64encode(key).decode()
    assert seen and not any(encoded in s for s in seen)


@on_a_mac
def test_it_is_read_without_asking_by_any_later_program(keychain, tmp_path):
    """Whatever opens the database: another process, as the MCP server and the command line are, and an updated app."""
    with _start() as c:
        _set_up(c)
    key = security.master_key()
    code = ("import os; os.environ['SYNTROPY_NO_DOTENV'] = '1'; from app.core import security; print(security.master_key().hex())")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=config.REPO_ROOT, timeout=60,
                          env={**os.environ, "PYTHONPATH": str(config.REPO_ROOT)})
    assert done.stdout.strip() == key.hex(), done.stderr


@on_a_mac
def test_a_key_from_an_earlier_version_moves_to_the_keychain_and_its_file_goes_later(keychain, tmp_path, monkeypatch):
    with monkeypatch.context() as m:
        m.setenv("SYNTROPY_KEY_STORE", keystore.FILE)
        m.setenv("SYNTROPY_DATABASE_ENCRYPTION", "0")
        with _start() as c:
            _set_up(c)
        _fresh_start()
    key = security.key_from_file((tmp_path / "secret.key").read_bytes())
    path = config.db_path()
    with _start():
        assert db.is_encrypted(path)
        assert keystore.from_store() == key
        assert (tmp_path / "secret.key").exists()       # what the previous version would look for, if this one is undone
        assert encryption.finish_due(path, running_for=0) is False
        encryption.finish_up(path)
    assert not (tmp_path / "secret.key").exists()
    _fresh_start()
    with _start() as c:
        assert c.post("/api/auth/login", json={"password": PASSPHRASE}, headers=CSRF).status_code == 200


@on_a_mac
def test_a_copy_of_the_folder_without_the_keychain_opens_nothing(keychain, tmp_path, monkeypatch):
    with _start() as c:
        _set_up(c)
    _background_finished()      # or it carries on in the copy, once the environment names that
    db.close_readers()
    # The records in the database file itself, not only in its log: a copy whose database is still empty is a new install.
    # What the server started in the background may still be reading for a moment.
    deadline = time.monotonic() + 15
    while not db.checkpoint() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert (tmp_path / "syntropy.db").stat().st_size > 0
    elsewhere = tmp_path.parent / f"{tmp_path.name}-copy"
    shutil.copytree(tmp_path, elsewhere)
    # Another computer: the copy's own key.json, and a keychain that has never held its key.
    other = _new_keychain(tmp_path.parent / f"{tmp_path.name}-other.keychain-db")
    monkeypatch.setenv("SYNTROPY_KEYCHAIN_FILE", str(other))
    monkeypatch.setenv("SYNTROPY_DATA_DIR", str(elsewhere))
    _fresh_start()
    try:
        with _start() as c:
            assert c.app.state.problem_kind == "key"
            assert c.get("/health").status_code == 503
        assert not (elsewhere / "secret.key").exists()
    finally:
        subprocess.run([keystore.SECURITY_TOOL, "delete-keychain", str(other)], capture_output=True)
        shutil.rmtree(elsewhere)


@on_a_mac
def test_with_the_key_json_gone_the_keychain_is_still_found(keychain, tmp_path):
    with _start() as c:
        _set_up(c)
    key = security.master_key()
    (tmp_path / "key.json").unlink()
    _fresh_start()
    assert security.master_key() == key


@on_a_mac
def test_forgetting_removes_it_from_the_keychain(keychain, tmp_path):
    with _start() as c:
        _set_up(c)
    assert keystore.from_store()
    keystore.forget()
    assert not (tmp_path / "key.json").exists() and keystore._keychain_read(None) is None


def _empty_the_folder(tmp_path: Path) -> None:
    """As someone deleting the data folder to start again does: what's in the Keychain stays."""
    _background_finished()
    db.close_readers()
    _fresh_start()
    for entry in tmp_path.iterdir():
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()


@on_a_mac
def test_starting_again_in_an_emptied_folder_makes_a_key_of_its_own_that_backups_carry(keychain, tmp_path):
    """Not the one the Keychain kept from before, which nothing would name: backups left it out, and opened nowhere else."""
    from app.services import backups
    with _start() as c:
        _set_up(c)
    before = security.master_key()
    _empty_the_folder(tmp_path)
    with _start() as c:
        _set_up(c)
        made = backups.create(tmp_path / "out", passphrase="correct horse battery")
    key = security.master_key()
    assert key != before and keystore.store_of_record() == keystore.KEYCHAIN and keystore.from_store() == key
    assert backups.key_source() == "file" and made["key"] == "included"
    # Elsewhere, with none of this Keychain: the backup brings its key.
    account = json.loads((tmp_path / "key.json").read_text())["account"]
    assert keystore._keychain_delete(["-a", account])
    db.close_readers()
    _fresh_start()
    item = backups.check(Path(made["path"]), "correct horse battery")
    assert item.report["key"]["status"] == "included"
    backups.discard(item.id)


@on_a_mac
def test_without_key_json_the_key_that_opens_the_data_is_found_among_others_and_recorded(keychain, tmp_path):
    """As an install whose key.json went missing has it, including one that took a key left in the Keychain before this was
    fixed: the next start finds which opens the data, whichever comes first, and names it again, so backups carry it."""
    from app.services import backups
    keystore._keychain_write("left-from-before", os.urandom(32))
    with _start() as c:
        _set_up(c)
    key = security.master_key()
    assert key != keystore._keychain_read("left-from-before")
    account = json.loads((tmp_path / "key.json").read_text())["account"]
    (tmp_path / "key.json").unlink()
    _fresh_start()
    assert backups.key_source() == "file"       # an encrypted database relies on a key, whatever names it
    assert security.master_key() == key
    assert json.loads((tmp_path / "key.json").read_text()) == {"version": 1, "store": keystore.KEYCHAIN, "account": account}
    with _start():
        assert backups.create(tmp_path / "out", passphrase="correct horse battery")["key"] == "included"


@on_a_mac
def test_forgetting_removes_every_key_this_folder_had_and_no_other_folders(keychain, tmp_path, monkeypatch):
    with _start() as c:
        _set_up(c)
    first = json.loads((tmp_path / "key.json").read_text())["account"]
    security.adopt_key(os.urandom(32))      # a backup's key: a second item
    second = json.loads((tmp_path / "key.json").read_text())["account"]
    other = tmp_path.parent / f"{tmp_path.name}-other"
    with monkeypatch.context() as m:
        m.setenv("SYNTROPY_DATA_DIR", str(other))
        keystore._keychain_write("another-folders-key", os.urandom(32))
    keystore._keychain_write("left-by-this-folder-before", os.urandom(32))
    accounts = {i["acct"] for i in keystore._keychain_items()}
    assert {first, second, "another-folders-key", "left-by-this-folder-before"} <= accounts
    keystore.forget()
    assert {i["acct"] for i in keystore._keychain_items()} == {"another-folders-key"}
    assert not (tmp_path / "key.json").exists()
    shutil.rmtree(other, ignore_errors=True)


def test_a_keychain_that_will_not_answer_is_not_a_lost_key(monkeypatch, tmp_path):
    """Locked, or reached over SSH: nothing is made, nothing is changed, and it says what to do."""
    with _start() as c:
        _set_up(c)
    (tmp_path / "secret.key").rename(tmp_path / "moved")
    (tmp_path / "key.json").write_text(json.dumps({"store": keystore.KEYCHAIN, "account": "abc"}))
    _fresh_start()

    def locked(account):
        raise keystore.Unreachable("User interaction is not allowed.")
    monkeypatch.setattr(keystore, "_keychain_read", locked)
    with _start() as c:
        assert c.app.state.problem_kind == "key_locked"
        assert "Unlock" in c.get("/api/status").json()["detail"]
    assert not (tmp_path / "secret.key").exists()


def test_a_keychain_that_refuses_the_key_leaves_it_in_the_file_and_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNTROPY_KEY_STORE", keystore.KEYCHAIN)

    def refused(account, key):
        raise keystore.CantSave("no keychain here")
    monkeypatch.setattr(keystore, "_keychain_write", refused)
    monkeypatch.setattr(keystore, "_keychain_read", lambda account: None)       # never the real keychain, even to look
    with _start() as c:
        _set_up(c)
    assert (tmp_path / "secret.key").exists() and encryption.status()["key_kind"] == keystore.FILE
    assert encryption.status()["key_beside_data"]


def test_a_keychain_that_gives_back_something_else_is_not_relied_on(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNTROPY_KEY_STORE", keystore.KEYCHAIN)
    monkeypatch.setattr(keystore, "_keychain_write", lambda account, key: None)
    monkeypatch.setattr(keystore, "_keychain_read", lambda account: os.urandom(32))
    assert keystore.save_to_store(os.urandom(32)) == keystore.FILE


# ---------------------------------------------------------------------------
# DPAPI
# ---------------------------------------------------------------------------

def test_with_dpapi_the_key_is_in_key_json_protected(dpapi, tmp_path):
    with _start() as c:
        _set_up(c)
    assert not (tmp_path / "secret.key").exists()
    key = security.master_key()
    info = json.loads((tmp_path / "key.json").read_text())
    assert info["store"] == keystore.DPAPI and base64.b64decode(info["dpapi"]) != key
    _fresh_start()
    assert security.master_key() == key
    assert encryption.status()["key_kind"] == keystore.DPAPI and not encryption.status()["key_beside_data"]


def test_a_blob_from_another_windows_account_is_a_lost_key_not_a_new_one(dpapi, tmp_path, monkeypatch):
    with _start() as c:
        _set_up(c)
    before = (tmp_path / "key.json").read_bytes()
    _fresh_start()

    def other_account(data, protect):
        raise OSError(-2146893813, "Key not valid for use in specified state")
    monkeypatch.setattr(keystore, "_dpapi", other_account)
    with _start() as c:
        assert c.app.state.problem_kind == "key"
    assert (tmp_path / "key.json").read_bytes() == before


def test_dpapi_refusing_leaves_the_key_in_the_file(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNTROPY_KEY_STORE", keystore.DPAPI)

    def refused(data, protect):
        raise OSError(5, "Access is denied")
    monkeypatch.setattr(keystore, "_dpapi", refused)
    with _start() as c:
        _set_up(c)
    assert (tmp_path / "secret.key").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Only Windows has it. CI runs this")
def test_real_dpapi(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNTROPY_KEY_STORE", keystore.DPAPI)
    key = os.urandom(32)
    assert keystore._dpapi(keystore._dpapi(key, protect=True), protect=False) == key
    assert keystore._dpapi(key, protect=True) != key
    assert keystore.save_to_store(key) == keystore.DPAPI
    assert keystore.from_store() == key
    with _start() as c:
        _set_up(c)
    assert db.is_encrypted(config.db_path()) and not (tmp_path / "secret.key").exists()


# ---------------------------------------------------------------------------
# An owner's password, when the store is gone
# ---------------------------------------------------------------------------

def _lose_the_store(tmp_path: Path, monkeypatch) -> None:
    """Whatever held it has forgotten: a reset Mac password, a new Windows profile. key.json is still there."""
    monkeypatch.setattr(keystore, "_dpapi", lambda data, protect: (_ for _ in ()).throw(OSError("lost")))
    _fresh_start()


def test_signing_in_as_an_owner_makes_the_lock_and_it_opens_with_that_password_only(dpapi, tmp_path):
    with _start() as c:
        _set_up(c)
        assert security.recovery_applies()
        owner = keystore.recovery_entries()
        assert len(owner) == 1
        wrapped = next(iter(owner.values()))
        assert keystore.unwrap(wrapped, PASSPHRASE, security._wrap_kdf) == security.master_key()
        assert keystore.unwrap(wrapped, "something else", security._wrap_kdf) is None
    assert security.key_as_file(security.master_key()) not in (tmp_path / "key.json").read_bytes()


def test_an_earlier_owner_gets_a_lock_at_their_next_sign_in(dpapi, tmp_path):
    with _start() as c:
        _set_up(c)
        info = json.loads((tmp_path / "key.json").read_text())
        del info["recovery"]
        (tmp_path / "key.json").write_text(json.dumps(info))
        c.post("/api/auth/logout", headers=CSRF)
        assert not keystore.recovery_entries()
        assert c.post("/api/auth/login", json={"password": PASSPHRASE}, headers=CSRF).status_code == 200
        assert keystore.recovery_entries()


def test_changing_the_password_means_the_old_one_no_longer_opens_it(dpapi, tmp_path):
    from app.store import accounts
    with _start() as c:
        _set_up(c)
        owner = accounts.first_owner()
        before = keystore.recovery_entries()[owner["id"]]
        accounts.set_password(owner["id"], "a brand new password")
        after = keystore.recovery_entries()[owner["id"]]
    assert after != before
    assert keystore.unwrap(after, "a brand new password", security._wrap_kdf)
    assert keystore.unwrap(after, PASSPHRASE, security._wrap_kdf) is None


def test_an_owner_stepping_down_stops_their_password_opening_it(dpapi, tmp_path):
    from app.store import accounts, profiles
    with _start() as c:
        _set_up(c)
        first = accounts.first_owner()
        other = accounts.create(profiles.create_profile("Sam")["id"], "a second password", role="owner")
        accounts.set_role(other["id"], "owner", step_down=first["id"])
        assert first["id"] not in keystore.recovery_entries()


def test_an_owner_who_is_removed_or_leaves_no_longer_opens_it(dpapi, tmp_path):
    from app.store import accounts, profiles
    with _start() as c:
        _set_up(c)
        removed = accounts.create(profiles.create_profile("Sam")["id"], "sams own password", role="owner")
        leaving = accounts.create(profiles.create_profile("Kim")["id"], "kims own password", role="owner")
        assert {removed["id"], leaving["id"]} <= set(keystore.recovery_entries())
        assert c.delete(f"/api/profiles/{removed['profile_id']}", headers=CSRF).status_code == 200
        c.post("/api/auth/logout", headers=CSRF)
        assert c.post("/api/auth/login", json={"account": leaving["id"], "password": "kims own password"}, headers=CSRF).status_code == 200
        assert c.post("/api/accounts/me/delete", json={"password": "kims own password"}, headers=CSRF).status_code == 200
        assert not {removed["id"], leaving["id"]} & set(keystore.recovery_entries())
    assert not security.recover_key("sams own password") and not security.recover_key("kims own password")


def test_a_lock_left_by_someone_no_longer_an_owner_opens_nothing_and_goes(dpapi, tmp_path, monkeypatch):
    """As an earlier version left them when someone was removed, or a backup from before an owner joined brings back."""
    with _start() as c:
        _set_up(c)
    key = security.master_key()
    keystore.save_recovery("acc_gone", keystore.wrap(key, "a former owners password", security._wrap_kdf))
    _lose_the_store(tmp_path, monkeypatch)
    with _start() as c:
        assert c.app.state.problem_kind == "key"
        r = c.post("/unlock", data={"password": "a former owners password"}, follow_redirects=False)
        assert r.status_code == 401 and "acc_gone" not in keystore.recovery_entries()
        assert c.post("/unlock", data={"password": PASSPHRASE}, follow_redirects=False).status_code == 303
    keystore.save_recovery("acc_gone", keystore.wrap(key, "a former owners password", security._wrap_kdf))
    _fresh_start()
    with _start():
        assert "acc_gone" not in keystore.recovery_entries() and keystore.recovery_entries()


def test_a_member_password_never_opens_it(dpapi, tmp_path):
    from app.store import accounts, profiles
    with _start() as c:
        _set_up(c)
        member = accounts.create(profiles.create_profile("Sam")["id"], "a member password")
        c.post("/api/auth/logout", headers=CSRF)
        assert c.post("/api/auth/login", json={"account": member["id"], "password": "a member password"}, headers=CSRF).status_code == 200
        assert member["id"] not in keystore.recovery_entries()
        assert not security.recover_key("a member password")


def test_it_applies_only_where_the_key_is_apart_from_the_data(tmp_path):
    with _start() as c:
        _set_up(c)
        assert not security.recovery_applies() and not keystore.recovery_entries()


def test_it_can_be_turned_off_and_on(dpapi, tmp_path, capsys):
    from app import cli
    with _start() as c:
        _set_up(c)
        assert keystore.recovery_entries()
        cli.main(["key", "recovery", "off"])
        assert not keystore.recovery_entries() and not security.recovery_applies()
        c.post("/api/auth/logout", headers=CSRF)
        assert c.post("/api/auth/login", json={"password": PASSPHRASE}, headers=CSRF).status_code == 200
        assert not keystore.recovery_entries()
        cli.main(["key", "recovery", "on"])
        c.post("/api/auth/logout", headers=CSRF)
        assert c.post("/api/auth/login", json={"password": PASSPHRASE}, headers=CSRF).status_code == 200
        assert keystore.recovery_entries()


def test_the_page_brings_the_key_back_with_the_right_password_only(dpapi, tmp_path, monkeypatch):
    with _start() as c:
        _set_up(c)
    key = security.master_key()
    _lose_the_store(tmp_path, monkeypatch)
    with _start() as c:
        assert c.app.state.problem_kind == "key"
        page = c.get("/")
        assert page.status_code == 503 and 'action="/unlock"' in page.text
        wrong = c.post("/unlock", data={"password": "not it"}, follow_redirects=False)
        assert wrong.status_code == 401 and "open it" in wrong.text
        assert c.get("/api/status").status_code == 503
        right = c.post("/unlock", data={"password": PASSPHRASE}, follow_redirects=False)
        assert right.status_code == 303
        assert c.get("/health").status_code == 200
        assert security.master_key() == key
        assert (tmp_path / "secret.key").exists()      # the store still won't take it, so it's kept where it can be
        assert c.post("/api/auth/login", json={"password": PASSPHRASE}, headers=CSRF).status_code == 200
        assert c.post("/unlock", data={"password": PASSPHRASE}).status_code == 404      # nothing left to unlock


def test_unlocking_tells_an_update_waiting_to_hear_that_the_new_version_works(dpapi, tmp_path, monkeypatch):
    """Otherwise the update script, hearing nothing, put the earlier version back after 25 minutes, and with it the database
    from before the update: whatever was done since unlocking was gone."""
    monkeypatch.syspath_prepend(str(config.REPO_ROOT / "desktop"))
    import updater
    from app.services import updates
    with _start() as c:
        _set_up(c)
    _lose_the_store(tmp_path, monkeypatch)
    folder = tmp_path / "updates"
    folder.mkdir()
    (folder / updater.ATTEMPT).write_text(json.dumps({"version": config.APP_VERSION, "from": "1.0.0", "at": time.time()}))
    monkeypatch.setattr(updates, "_on_working", None)
    updates.set_on_working(lambda: updater.started_fine(folder))       # as the desktop app does
    with _start() as c:
        assert c.app.state.problem_kind == "key" and not (folder / updater.OK_MARK).exists()
        assert c.post("/unlock", data={"password": PASSPHRASE}, follow_redirects=False).status_code == 303
        assert (folder / updater.OK_MARK).exists() and not (folder / updater.ATTEMPT).exists()


def test_nobody_can_try_passwords_from_another_computer(dpapi, tmp_path, monkeypatch):
    with _start() as c:
        _set_up(c)
    _lose_the_store(tmp_path, monkeypatch)
    with _start(("8.8.8.8", 5000)) as far:
        page = far.get("/")
        assert page.status_code == 503 and "unlock" not in page.text.lower()
        r = far.post("/unlock", data={"password": PASSPHRASE})
        assert r.status_code == 503


def test_another_sites_page_can_not_post_guesses(dpapi, tmp_path, monkeypatch):
    with _start() as c:
        _set_up(c)
    _lose_the_store(tmp_path, monkeypatch)
    with _start() as c:
        r = c.post("/unlock", data={"password": "a guess"}, headers={"Origin": "https://example.com"})
        assert r.status_code == 403
        # Nor one that has pointed its own name at this computer (DNS rebinding), so that its address and origin agree.
        rebound = {"Host": "attacker.example:8000", "Origin": "http://attacker.example:8000"}
        assert c.post("/unlock", data={"password": PASSPHRASE}, headers=rebound).status_code == 403
        assert c.app.state.problem_kind == "key" and c.get("/health").status_code == 503
        assert c.post("/unlock", data={"password": PASSPHRASE}, headers={"Host": "attacker.example:8000"}).status_code == 403
        assert c.post("/unlock", data={"password": PASSPHRASE}, follow_redirects=False).status_code == 303


def test_guesses_are_limited_like_signing_in(dpapi, tmp_path, monkeypatch):
    with _start() as c:
        _set_up(c)
    _lose_the_store(tmp_path, monkeypatch)
    with _start() as c:
        codes = [c.post("/unlock", data={"password": "a guess"}).status_code for _ in range(20)]
    assert 429 in codes


def test_the_command_line_can_do_the_same(dpapi, tmp_path, monkeypatch, capsys):
    from app import cli
    with _start() as c:
        _set_up(c)
    key = security.master_key()
    _lose_the_store(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdin", type("Tty", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "not it")
    with pytest.raises(SystemExit):
        cli.main(["key", "recover"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSPHRASE)
    cli.main(["key", "recover"])
    assert "The key is back" in capsys.readouterr().out
    _fresh_start()
    assert security.master_key() == key


def test_a_backup_brings_the_key_back_when_the_store_is_gone(dpapi, tmp_path, monkeypatch, capsys):
    from app import cli
    from app.services import backups
    with _start() as c:
        _set_up(c)
        made = backups.create(tmp_path / "out", passphrase="correct horse battery")
    key = security.master_key()
    db.close_readers()
    _lose_the_store(tmp_path, monkeypatch)
    secret = tmp_path / "pass.txt"
    secret.write_text("correct horse battery")
    monkeypatch.setenv("SYNTROPY_KEY_STORE", keystore.FILE)
    cli.main(["restore", made["path"], "--key-only", "--passphrase-file", str(secret)])
    _fresh_start()
    assert security.master_key() == key
    assert db.opens(config.db_path(), security.database_key())


def test_a_restore_waits_for_a_store_that_can_not_answer_and_changes_nothing(tmp_path, monkeypatch):
    """A locked Keychain looked like no key at all: the restore went ahead, and the keys kept from before were replaced by
    an empty list."""
    from app.services import backups
    held: dict[str, bytes] = {}
    monkeypatch.setenv("SYNTROPY_KEY_STORE", keystore.KEYCHAIN)
    monkeypatch.setattr(keystore, "_keychain_write", lambda account, key: held.__setitem__(account, key))
    monkeypatch.setattr(keystore, "_keychain_read", lambda account: held.get(account))
    with _start() as c:
        _set_up(c)
        made = backups.create(tmp_path / "out", passphrase="correct horse battery")
    security.adopt_key(os.urandom(32))       # so there's a list of earlier keys to lose
    db.close_readers()
    item = backups.check(Path(made["path"]), "correct horse battery")
    before = {f: (tmp_path / f).read_bytes() for f in ("key.json", "syntropy.db")}

    def locked(account):
        raise keystore.Unreachable("User interaction is not allowed.")
    monkeypatch.setattr(keystore, "_keychain_read", locked)
    with pytest.raises(backups.BackupProblem) as caught:
        backups.apply(item)
    assert "Unlock" in str(caught.value) and "wasn't restored" in str(caught.value)
    assert {f: (tmp_path / f).read_bytes() for f in before} == before
    with pytest.raises(keystore.Unreachable):
        security.adopt_key(os.urandom(32))
    assert (tmp_path / "key.json").read_bytes() == before["key.json"]
    backups.discard(item.id)


def test_a_key_brought_back_with_a_password_keeps_the_keys_it_had_replaced(dpapi, tmp_path, monkeypatch):
    """The store has lost the key, so nothing says what it was: the list of keys it had replaced, which copies kept from
    before still need, was replaced by an empty one."""
    with _start() as c:
        _set_up(c)
    first = security.master_key()
    second = os.urandom(32)
    security.adopt_key(second)      # a backup's key, as restoring one brings
    assert security._retired_keys(second) == [first]
    _lose_the_store(tmp_path, monkeypatch)
    assert security.existing_key() is None
    security.adopt_key(second)      # as security.recover_key does with it
    assert security._retired_keys(second) == [first]


def test_restoring_on_a_computer_with_a_store_puts_the_key_there(dpapi, tmp_path, monkeypatch):
    from app.services import backups
    with monkeypatch.context() as m:
        m.setenv("SYNTROPY_KEY_STORE", keystore.FILE)
        with _start() as c:
            _set_up(c)
            made = backups.create(tmp_path / "out", passphrase="correct horse battery")
        _fresh_start()
    new = tmp_path.parent / f"{tmp_path.name}-new"
    new.mkdir()
    monkeypatch.setenv("SYNTROPY_DATA_DIR", str(new))
    _fresh_start()
    item = backups.check(Path(made["path"]), "correct horse battery")
    backups.apply(item)
    assert not (new / "secret.key").exists() and keystore.store_of_record() == keystore.DPAPI
    assert db.opens(config.db_path(), security.database_key())
    db.close_readers()      # Windows won't delete a file that's open
    shutil.rmtree(new)


# ---------------------------------------------------------------------------
# Making the first key
# ---------------------------------------------------------------------------

def test_processes_starting_together_agree_on_one_key(tmp_path):
    code = ("import os; os.environ['SYNTROPY_NO_DOTENV'] = '1'; from app.core import db, security; "
            "db.ensure_migrated(); print(security.master_key().hex())")
    env = {**os.environ, "PYTHONPATH": str(config.REPO_ROOT), "SYNTROPY_DATA_DIR": str(tmp_path)}
    running = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=config.REPO_ROOT, env=env) for _ in range(5)]
    said = [p.communicate(timeout=120) for p in running]
    keys = {out.strip() for out, _ in said}
    assert len(keys) == 1 and len(next(iter(keys))) == 64, said
    assert db.opens(config.db_path(), security.database_key(bytes.fromhex(next(iter(keys)))))


@pytest.mark.skipif(os.name == "nt", reason="file permissions")
def test_key_json_is_private(dpapi, tmp_path):
    with _start() as c:
        _set_up(c)
    assert stat.S_IMODE((tmp_path / "key.json").stat().st_mode) == 0o600
