"""
Test fixtures. Every test gets an isolated data directory (database + encryption key),
and nothing touches the network: EHR connections run against the in-process simulator.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import traceback
from typing import Any
from urllib.parse import parse_qs, urlparse

# What a developer has set must not change what the tests see, so this runs before anything imports the app: their
# .env isn't loaded (app.core.config), and settings exported in their shell are dropped.
os.environ["SYNTROPY_NO_DOTENV"] = "1"
for _var in [v for v in os.environ if v != "SYNTROPY_NO_DOTENV" and (v.startswith("SYNTROPY_") or v in ("PORT", "FORWARDED_ALLOW_IPS"))]:
    del os.environ[_var]

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

BASE = "http://localhost:8000"
CSRF = {"X-Requested-With": "syntropy"}
PASSPHRASE = "correct horse battery"
LOCAL_CLIENT = ("127.0.0.1", 50000)      # setup is only accepted from nearby addresses
RELAY_KEY = "test-relay-key"


class BlockedNetwork(OSError):
    """A test tried to reach something other than this computer."""


_network_attempts: list[str] = []


def _is_this_computer(host: Any) -> bool:
    name = (host.decode() if isinstance(host, bytes) else str(host or "")).strip("[]").lower()
    return name in ("", "localhost", "::1", "::", "0.0.0.0") or name.startswith("127.") or name.endswith(".localhost")


def _network_guard(event: str, args: tuple) -> None:
    # An audit hook, so it also sees what C code and other libraries do. It raises, so the code under test behaves as
    # it would offline, and network_guard below fails the test even if that code catches the error.
    if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
        host = args[0]
    elif event in ("socket.connect", "socket.sendto") and isinstance(args[1], tuple):
        host = args[1][0]
    else:
        return
    if not _is_this_computer(host):
        _network_attempts.append(f"{event} {host}")
        raise BlockedNetwork(f"Tests don't use the network: {event} {host}")


sys.addaudithook(_network_guard)


@pytest.fixture(autouse=True)
def network_guard():
    """Nothing but this computer: a test that reaches for anything else fails, so it stubs that call instead."""
    _network_attempts.clear()
    yield
    attempts, _network_attempts[:] = list(_network_attempts), []
    assert not attempts, f"This test tried to use the network: {attempts}"


@pytest.fixture(autouse=True)
def writes_stay_off_the_event_loop(monkeypatch):
    """Waiting for the write lock on the event loop stops every request, so no test may reach db() from there."""
    from app.core import db
    on_the_loop: list[str] = []
    real = db._writer

    def writer(path):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return real(path)
        on_the_loop.append("".join(traceback.format_stack(limit=8)))
        return real(path)
    monkeypatch.setattr(db, "_writer", writer)
    yield
    assert not on_the_loop, f"A database write ran on the event loop:\n{on_the_loop[0]}"


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
    # Vendor credentials from a developer's .env would change which client each test sees.
    for var in [v for v in os.environ if v.endswith(("_CLIENT_ID", "_CLIENT_SECRET"))]:
        monkeypatch.delenv(var, raising=False)
    # The tests run as a release build, with Syntropy's registrations (test_relay_key covers a build from the source).
    # A key written by scripts/release_key.py in this checkout is ignored.
    monkeypatch.setenv("SYNTROPY_RELAY_KEY", RELAY_KEY)
    monkeypatch.setitem(sys.modules, "app.core.release_key", None)
    from app.core import auth, db
    from app.store import sample_counts
    db.reset_migration_cache()
    sample_counts.forget()   # counts are kept in memory; each test has its own database
    # Failed sign-ins are counted in memory too: another test's wrong passwords mustn't lock this one out.
    auth._failed_logins.clear()
    auth._all_failures.clear()
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
    # Health systems connect for real by default; the tests drive the simulator, in developer mode.
    assert raw_client.put("/api/settings/developer", json={"enabled": True}).status_code == 200
    assert raw_client.put("/api/settings/platforms/epic", json={"mode": "simulated"}).status_code == 200
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
