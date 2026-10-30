"""The key that opens saved passwords, sign-ins and AI keys: made whole, never silently replaced, and missed out loud."""

from __future__ import annotations

import asyncio
import base64
import os
import stat

import pytest
from fastapi.testclient import TestClient

from app.core import config, db, security
from app.services import sync
from app.store import accounts, connections, profiles
from tests.conftest import BASE, CSRF, LOCAL_CLIENT, PASSPHRASE


def _key_path():
    return config.data_dir() / "secret.key"


def _forget_key():
    security._key_cache.clear()
    security._key_mismatch.clear()


def _new_key_file():
    _forget_key()
    _key_path().write_bytes(base64.urlsafe_b64encode(os.urandom(32)))


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

def test_the_key_file_is_written_whole_and_private():
    key = security.master_key()
    assert len(key) == 32
    assert base64.urlsafe_b64decode(_key_path().read_bytes()) == key
    assert not [p.name for p in _key_path().parent.iterdir() if ".tmp" in p.name]
    if os.name != "nt":
        assert stat.S_IMODE(_key_path().stat().st_mode) == 0o600


def test_a_crash_while_writing_it_leaves_no_empty_key(monkeypatch):
    def crash(fd):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(security.os, "fsync", crash)
    with pytest.raises(OSError):
        security.master_key()
    assert not _key_path().exists()


@pytest.mark.parametrize("contents", [b"", b"  \n", base64.urlsafe_b64encode(b"short"), b"!!!not base64!!!"])
def test_a_damaged_key_is_refused_once_something_depends_on_it(contents):
    security.encrypt_json("something")
    connections.create(profiles.default_profile()["id"], "wearable", "oura", "Oura", credentials={"access_token": "a"})
    _forget_key()
    _key_path().write_bytes(contents)
    with pytest.raises(db.StartupProblem) as caught:
        security.master_key()
    assert caught.value.kind == "key" and str(_key_path()) in str(caught.value)
    assert _key_path().read_bytes() == contents


def test_an_empty_key_from_a_crashed_first_run_is_simply_replaced():
    db.ensure_migrated()
    _key_path().write_bytes(b"")
    assert len(security.master_key()) == 32
    assert len(base64.urlsafe_b64decode(_key_path().read_bytes())) == 32


def test_the_server_explains_a_damaged_key():
    connections.create(profiles.default_profile()["id"], "wearable", "oura", "Oura", credentials={"access_token": "a"})
    _forget_key()
    _key_path().write_bytes(b"")
    from app.main import create_app
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT) as c:
        assert c.get("/health").status_code == 503
        assert "secret.key" in c.get("/api/status").json()["detail"]


def test_no_new_key_is_made_when_saved_secrets_need_the_old_one():
    connections.create(profiles.default_profile()["id"], "wearable", "oura", "Oura", credentials={"access_token": "a"})
    _forget_key()
    _key_path().unlink()
    with pytest.raises(db.StartupProblem) as caught:
        security.master_key()
    assert "missing" in str(caught.value) and "key new" in str(caught.value)
    assert not _key_path().exists()


def test_a_fresh_install_makes_one_without_asking():
    assert not _key_path().exists()
    security.check_key()
    assert _key_path().exists() and security.key_problem() is None


# ---------------------------------------------------------------------------
# The wrong key
# ---------------------------------------------------------------------------

def _start_fresh(client_app=None):
    db.reset_migration_cache()
    _forget_key()


def test_the_wrong_key_is_noticed_at_start_up_and_owners_are_told(client):
    token = None
    assert client.get("/api/status").json().get("problems") == []
    token = client.cookies
    _new_key_file()
    security.check_key()
    assert security.key_problem() and str(_key_path()) in security.key_problem()
    status = client.get("/api/status").json()        # a session survives: it isn't protected by the key
    assert status["problems"] and status["problems"][0]["id"] == "encryption-key"
    assert client.get("/api/health").json()["problems"][0]["id"] == "encryption-key"
    assert token is not None


def test_the_check_works_on_a_database_from_before_the_marker(client):
    with db.db() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (security._KEY_CHECK_SETTING,))
    security.check_key()     # the password hash opens with this key
    assert security.key_problem() is None
    with db.read() as conn:
        assert conn.execute("SELECT 1 FROM app_settings WHERE key = ?", (security._KEY_CHECK_SETTING,)).fetchone()
    with db.db() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = ?", (security._KEY_CHECK_SETTING,))
    _new_key_file()
    security.check_key()
    assert security.key_problem()
    with db.read() as conn:     # and it doesn't seal the wrong key as the right one
        assert not conn.execute("SELECT 1 FROM app_settings WHERE key = ?", (security._KEY_CHECK_SETTING,)).fetchone()


def test_signing_in_says_it_is_the_key_not_the_password(raw_client):
    raw_client.post("/api/setup", json={"passphrase": PASSPHRASE, "profile_name": "Alex"}, headers=CSRF)
    raw_client.post("/api/auth/logout", headers=CSRF)
    _new_key_file()
    security.check_key()
    r = raw_client.post("/api/auth/login", json={"password": PASSPHRASE}, headers=CSRF)
    assert r.status_code == 409 and "encryption key" in r.json()["detail"]
    r = raw_client.post("/api/auth/login", json={"password": "a wrong guess"}, headers=CSRF)
    assert r.status_code == 409


def test_a_portal_with_an_unreadable_sign_in_is_not_called_an_import(client):
    prof = profiles.default_profile()["id"]
    source = connections.create(prof, "ehr", "epic", "Stanford", mode="live",
                                credentials={"access_token": "a", "refresh_token": "r", "expires_at": 9999999999})["id"]
    assert connections.get(source)["status"] == "active" and not connections.get(source)["access_ended"]
    _new_key_file()
    conn = connections.get(source)
    assert conn["credentials_unreadable"] and conn["status"] == "needs_reauth" and not conn["access_ended"]
    assert source not in {c["id"] for c in sync.due_connections(0, 0)}
    result = asyncio.run(sync.sync_connection(source))
    assert result["status"] == "skipped" and "encryption key" in result["reason"]
    with db.read() as c:      # nothing was written over: the right key brings it back
        assert c.execute("SELECT status FROM connections WHERE id = ?", (source,)).fetchone()[0] == "active"


def test_the_right_key_back_makes_everything_work_again(client):
    prof = profiles.default_profile()["id"]
    source = connections.create(prof, "wearable", "oura", "Oura", credentials={"access_token": "a"})["id"]
    original = _key_path().read_bytes()
    _new_key_file()
    security.check_key()
    assert security.key_problem() and connections.get(source)["credentials_unreadable"]
    _forget_key()
    _key_path().write_bytes(original)
    security.check_key()
    assert security.key_problem() is None
    conn = connections.get(source, include_credentials=True)
    assert conn["credentials"]["access_token"] == "a" and conn["status"] == "active"


def test_starting_over_with_a_new_key_is_the_owners_choice(client, monkeypatch, capsys):
    from app import cli
    owner = accounts.first_owner()
    before = _key_path().read_bytes()
    _key_path().unlink()
    _forget_key()
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    with pytest.raises(SystemExit):
        cli.main(["key", "new"])
    assert not _key_path().exists()
    monkeypatch.setattr("builtins.input", lambda prompt="": "NEW")
    cli.main(["key", "new"])
    assert _key_path().exists() and _key_path().read_bytes() != before
    security.check_key()
    assert security.key_problem() is None
    accounts.set_password(owner["id"], "a new long password")      # what `accounts reset-password` does
    assert accounts.verify_password(owner["id"], "a new long password")


def test_a_new_key_keeps_the_old_file(client, monkeypatch):
    from app import cli
    before = _key_path().read_bytes()
    monkeypatch.setattr("builtins.input", lambda prompt="": "NEW")
    cli.main(["key", "new"])
    kept = list(_key_path().parent.glob("secret.key.replaced-*"))
    assert len(kept) == 1 and kept[0].read_bytes() == before
