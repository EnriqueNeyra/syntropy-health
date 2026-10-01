"""
Test fixtures. Every test gets an isolated data directory (database + encryption key),
and nothing touches the network: EHR connections run against the in-process simulator.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

BASE = "http://localhost:8000"
CSRF = {"X-Requested-With": "syntropy"}
PASSPHRASE = "correct horse battery"
LOCAL_CLIENT = ("127.0.0.1", 50000)      # setup is only accepted from nearby addresses


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTROPY_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SYNTROPY_DB_PATH", raising=False)
    monkeypatch.delenv("SYNTROPY_SECRET_KEY", raising=False)
    monkeypatch.setenv("SYNTROPY_SCHEDULER", "false")
    monkeypatch.setenv("SYNTROPY_MDNS", "0")
    # Many tests set up without a password (the development allowance); test_security covers requiring one.
    monkeypatch.setenv("SYNTROPY_ALLOW_NO_PASSWORD", "1")
    for var in ("SYNTROPY_LISTEN_HOST", "SYNTROPY_LISTEN_PORT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TZ", "UTC")
    for var in ("EPIC_CLIENT_ID", "CERNER_CLIENT_ID", "OURA_CLIENT_ID", "OURA_CLIENT_SECRET", "WHOOP_CLIENT_ID", "WHOOP_CLIENT_SECRET",
                "GOOGLE_HEALTH_CLIENT_ID", "GOOGLE_HEALTH_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    from app.core import db
    from app.store import sample_counts
    db.reset_migration_cache()
    sample_counts.forget()   # counts are kept in memory; each test has its own database
    yield tmp_path
    db.reset_migration_cache()


@pytest.fixture
def raw_client():
    from app.main import create_app
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT) as c:
        yield c


@pytest.fixture
def client(raw_client):
    """A client that has completed setup (with a passphrase) and is logged in."""
    r = raw_client.post("/api/setup", json={"passphrase": PASSPHRASE, "profile_name": "Alex"}, headers=CSRF)
    assert r.status_code == 200, r.text
    raw_client.headers.update(CSRF)
    return raw_client


def simulated_login(client: TestClient, auth_url: str, username: str = "alex.rivera", decision: str = "allow") -> str:
    """Drives the simulator's sign-in + consent pages like a browser; returns the callback URL."""
    parsed = urlparse(auth_url)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    page = client.get(f"{parsed.path}?{parsed.query}")
    assert page.status_code == 200, page.text
    login = client.post("/sim/oauth/authorize", data={**params, "step": "login", "username": username, "password": "syntropy"})
    assert login.status_code == 200, login.text
    ticket = re.search(r"name='ticket' value='([^']+)'", login.text).group(1)
    consent = client.post("/sim/oauth/authorize", data={**params, "step": "consent", "ticket": ticket, "decision": decision},
                          follow_redirects=False)
    assert consent.status_code == 302
    return consent.headers["location"]


def connect_institution(client: TestClient, institution_id: str, username: str = "alex.rivera",
                        profile_id: str | None = None) -> dict[str, Any]:
    r = client.post("/api/connections/ehr", json={"institution_id": institution_id, "profile_id": profile_id})
    assert r.status_code == 200, r.text
    callback = simulated_login(client, r.json()["auth_url"], username)
    done = client.get(callback[len(BASE):], follow_redirects=False)
    assert done.status_code == 303, done.text
    location = done.headers["location"]
    assert "connected=" in location, location
    return {"connection_id": location.split("connected=")[1], "location": location}
