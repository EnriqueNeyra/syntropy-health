"""Safeguards for an app holding health records: a required password, setup only from nearby, login throttling, and
private files."""

import os
import stat

import pytest
from fastapi.testclient import TestClient

from app.core import auth
from tests.conftest import BASE, CSRF, PASSPHRASE


@pytest.fixture
def strict(monkeypatch):
    """The real rule: a password is required (no development allowance)."""
    monkeypatch.delenv("SYNTROPY_ALLOW_NO_PASSWORD", raising=False)


def test_setup_requires_a_password(raw_client, strict):
    r = raw_client.post("/api/setup", json={"profile_name": "Alex"}, headers=CSRF)
    assert r.status_code == 400 and "password" in r.json()["detail"]
    assert raw_client.post("/api/setup", json={"password": PASSPHRASE, "profile_name": "Alex", "accept_terms": True},
                           headers=CSRF).status_code == 200
    status = raw_client.get("/api/status").json()
    assert status["auth_required"] and not status["password_optional"] and status["terms_accepted"]


def test_an_older_setup_without_a_password_must_choose_one(raw_client, monkeypatch):
    raw_client.post("/api/setup", json={"profile_name": "Alex"}, headers=CSRF)      # allowed while developing...
    monkeypatch.delenv("SYNTROPY_ALLOW_NO_PASSWORD")                                 # ...then required
    raw_client.headers.update(CSRF)
    status = raw_client.get("/api/status").json()
    assert status["password_missing"] and not status["terms_accepted"]
    r = raw_client.get("/api/records")
    assert r.status_code == 403 and "password" in r.json()["detail"]
    assert raw_client.put("/api/system/network", json={"enabled": True, "without_password": True}).status_code == 403
    assert raw_client.post("/api/auth/password", json={"current": None, "new": None}).status_code == 400
    assert raw_client.post("/api/auth/password", json={"current": None, "new": PASSPHRASE, "accept_terms": True}).json()["ok"]
    status = raw_client.get("/api/status").json()
    assert not status["password_missing"] and status["terms_accepted"]
    assert raw_client.get("/api/records").status_code == 200


def test_password_cant_be_removed(client, strict):
    r = client.post("/api/auth/password", json={"current": PASSPHRASE, "new": None})
    assert r.status_code == 400


def test_development_allowance_from_the_command_line(raw_client, strict):
    from app.core import settings
    settings.set("dev.allow_no_password", True)
    assert raw_client.post("/api/setup", json={"profile_name": "Alex"}, headers=CSRF).status_code == 200
    assert raw_client.get("/api/status").json()["password_optional"] is True


def test_setup_only_from_nearby(strict):
    from app.main import create_app
    for host, ok in (("1.1.1.1", False), ("8.8.8.8", False), ("192.168.1.20", True), ("100.101.102.103", True)):
        with TestClient(create_app(), base_url=BASE, client=(host, 5000)) as c:
            r = c.post("/api/setup", json={"password": PASSPHRASE, "profile_name": "Alex"}, headers=CSRF)
            assert (r.status_code == 200) is ok, (host, r.text)
            if not ok:
                assert "home network" in r.json()["detail"]
        from app.core import db
        db.reset_migration_cache()
        if ok:
            break      # set up now; the rest is covered


def test_guessing_from_many_addresses_slows_down_but_never_locks_out_the_right_password(client, monkeypatch):
    slept = []
    # time.sleep is everyone's: only what a sign-in waits counts here, not a background rewrite's pause.
    monkeypatch.setattr(auth.time, "sleep", lambda s: slept.append(s) if s == auth.LOGIN_SLOW_S else None)
    # Spread over a password, invitation codes and names nobody signs in with: any one of them tried this often from
    # everywhere stops altogether (test_proxy_trust).
    for i in range(auth.LOGIN_SLOW_AFTER):
        c = TestClient(client.app, base_url=BASE, client=(f"10.0.{i // 250}.{i % 250 + 1}", 5000))
        if i % 3 == 0:
            assert c.post("/api/auth/login", json={"password": "wrong"}, headers=CSRF).status_code == 401
        elif i % 3 == 1:
            assert c.post("/api/invites/check", json={"code": "WRONG-CODE"}, headers=CSRF).status_code == 404
        else:
            assert c.post("/api/auth/login", json={"account": "Nobody", "password": "wrong"}, headers=CSRF).status_code == 401
    assert not slept
    c = TestClient(client.app, base_url=BASE, client=("10.9.9.9", 5000))
    assert c.post("/api/auth/login", json={"password": PASSPHRASE}, headers=CSRF).status_code == 200
    assert slept == [auth.LOGIN_SLOW_S]


def test_one_address_only_uses_up_its_own_tries_at_one_person(raw_client):
    client = raw_client
    assert client.post("/api/setup", json={"password": PASSPHRASE, "profile_name": "Alex"}, headers=CSRF).status_code == 200
    client.headers.update(CSRF)
    maria = client.post("/api/profiles", json={"name": "Maria"}).json()["id"]
    code = client.post(f"/api/profiles/{maria}/invite").json()["code"]
    c = TestClient(client.app, base_url=BASE, client=("10.0.0.5", 5000))
    c.headers.update(CSRF)
    for _ in range(auth.LOGIN_MAX_FAILURES):
        assert c.post("/api/auth/login", json={"account": "Alex", "password": "wrong"}).status_code == 401
    assert c.post("/api/auth/login", json={"account": "Alex", "password": PASSPHRASE}).status_code == 429
    # Another address isn't held back. Nor is another person, or an invitation, from this one.
    other = TestClient(client.app, base_url=BASE, client=("10.0.0.6", 5000))
    assert other.post("/api/auth/login", json={"account": "Alex", "password": PASSPHRASE}, headers=CSRF).status_code == 200
    assert c.post("/api/invites/check", json={"code": code}).status_code == 200
    assert c.post("/api/auth/login", json={"account": "Nobody", "password": "wrong"}).status_code == 401
    # Bad invitation codes don't use up sign-in tries, or the other way round.
    for _ in range(auth.LOGIN_MAX_FAILURES):
        assert c.post("/api/invites/check", json={"code": "ZZZZ-ZZZZ"}).status_code == 404
    assert c.post("/api/invites/check", json={"code": code}).status_code == 429


def test_a_right_password_clears_only_its_own_tries(raw_client):
    client = raw_client
    assert client.post("/api/setup", json={"password": PASSPHRASE, "profile_name": "Alex"}, headers=CSRF).status_code == 200
    c = TestClient(client.app, base_url=BASE, client=("10.0.0.5", 5000))
    c.headers.update(CSRF)
    for _ in range(auth.LOGIN_MAX_FAILURES - 1):
        assert c.post("/api/auth/login", json={"account": "Nobody", "password": "wrong"}).status_code == 401
    assert c.post("/api/auth/login", json={"account": "Alex", "password": PASSPHRASE}).status_code == 200
    c.cookies.clear()
    assert c.post("/api/auth/login", json={"account": "Nobody", "password": "wrong"}).status_code == 401
    assert c.post("/api/auth/login", json={"account": "Nobody", "password": "wrong"}).status_code == 429


def test_pairing_codes_can_only_be_guessed_so_many_times_in_all(client, monkeypatch):
    from app.api import biometrics
    monkeypatch.setattr(biometrics, "_pair_failures", {})
    monkeypatch.setattr(biometrics, "_all_pair_failures", [])
    monkeypatch.setattr(biometrics, "PAIR_MAX_FAILURES_ALL", 3)
    for i in range(3):
        c = TestClient(client.app, base_url=BASE, client=(f"10.0.0.{i + 1}", 5000))
        assert c.post("/api/devices/pair", json={"code": "ZZZZ-ZZZZ", "device_name": "x"}).status_code == 400
    c = TestClient(client.app, base_url=BASE, client=("10.0.1.1", 5000))
    assert c.post("/api/devices/pair", json={"code": "ZZZZ-ZZZZ", "device_name": "x"}).status_code == 429


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_data_folder_and_database_are_private(client, isolated_data):
    from app.core import config
    assert stat.S_IMODE(os.stat(config.data_dir()).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(config.db_path()).st_mode) == 0o600
