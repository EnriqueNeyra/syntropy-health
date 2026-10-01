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


def test_login_throttled_across_addresses(client, monkeypatch):
    monkeypatch.setattr(auth, "_all_failures", auth.deque())
    monkeypatch.setattr(auth, "_failed_logins", auth.defaultdict(auth.deque))
    codes = []
    for i in range(auth.LOGIN_MAX_FAILURES_ALL + 1):
        with TestClient(client.app, base_url=BASE, client=(f"10.0.{i // 250}.{i % 250 + 1}", 5000)) as c:
            codes.append(c.post("/api/auth/login", json={"password": "wrong"}, headers=CSRF).status_code)
    assert codes[:auth.LOGIN_MAX_FAILURES_ALL] == [401] * auth.LOGIN_MAX_FAILURES_ALL and codes[-1] == 429


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_data_folder_and_database_are_private(client, isolated_data):
    from app.core import config
    assert stat.S_IMODE(os.stat(config.data_dir()).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(config.db_path()).st_mode) == 0o600
