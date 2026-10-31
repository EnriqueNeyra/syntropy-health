"""Sessions, and what this server shows people who haven't been given it."""

from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from tests.conftest import BASE, CSRF, LOCAL_CLIENT, PASSPHRASE, connect_institution
from tests.test_accounts import _join, _journal, _person, _phone, _signed_in_owner


def _second_browser(client, password=PASSPHRASE, account="Alex"):
    other = TestClient(client.app, base_url=BASE, client=("192.168.1.7", 5000), headers={"User-Agent": "Other browser"})
    other.headers.update(CSRF)
    assert other.post("/api/auth/login", json={"account": account, "password": password}).status_code == 200
    return other


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def test_ending_one_sign_in(raw_client):
    client = _signed_in_owner(raw_client)
    other = _second_browser(client)
    sessions = client.get("/api/auth/sessions").json()["sessions"]
    assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1
    assert all("token_hash" not in s for s in sessions)
    theirs = next(s for s in sessions if not s["current"])

    maria_client, _ = _join(client, _person(client, "Maria"))
    assert maria_client.delete(f"/api/auth/sessions/{theirs['id']}").status_code == 404      # someone else's
    assert other.get("/api/profiles").status_code == 200
    assert client.delete("/api/auth/sessions/nothing").status_code == 404

    assert client.delete(f"/api/auth/sessions/{theirs['id']}").status_code == 200
    assert other.get("/api/profiles").status_code == 401
    assert client.get("/api/profiles").status_code == 200


def test_signing_out_everywhere_else_leaves_this_browser_and_the_phone(raw_client):
    client = _signed_in_owner(raw_client)
    other = _second_browser(client)
    _, anon, auth, screens = _phone(client)
    assert client.post("/api/auth/sessions/end-others").json()["ended"] == 1
    assert other.get("/api/profiles").status_code == 401
    assert client.get("/api/profiles").status_code == 200
    assert screens.get("/api/profiles").status_code == 200
    # A phone's screens are ended by unpairing it, since it can always ask again.
    phone_session = next(s for s in client.get("/api/auth/sessions").json()["sessions"] if s["device_name"])
    assert client.delete(f"/api/auth/sessions/{phone_session['id']}").status_code == 404


def test_a_browser_session_ends_after_six_months_however_much_its_used(raw_client):
    from app.core import auth
    from app.core.db import db
    client = _signed_in_owner(raw_client)
    _, _, _, screens = _phone(client)
    with db() as conn:
        conn.execute("UPDATE user_sessions SET created_at = ?, last_seen_at = ?", (time.time() - auth.SESSION_MAX_AGE - 1, time.time()))
    assert client.get("/api/profiles").status_code == 401
    assert screens.get("/api/profiles").status_code == 200        # a phone's app signs in by itself anyway


# ---------------------------------------------------------------------------
# Members and internals
# ---------------------------------------------------------------------------

def test_members_arent_told_where_things_are_on_the_owners_computer(raw_client, monkeypatch):
    from app.services import ai_agents
    client = _signed_in_owner(raw_client)
    maria_client, _ = _join(client, _person(client, "Maria"))
    found = {"available": True, "agents": [{"id": "x", "installed": True, "path": "/Users/alex/bin/x"}]}
    monkeypatch.setattr(ai_agents, "detect", lambda refresh=False: found)

    assert "data_dir" in client.get("/api/settings").json()
    assert client.get("/api/ai/agents").json()["agents"][0]["path"]
    assert "data_dir" not in maria_client.get("/api/settings").json()
    assert "/Users" not in maria_client.get("/api/ai/agents").text
    assert found["agents"][0]["path"]        # what's kept for the owner isn't touched


def test_the_network_is_told_only_whats_needed_to_find_the_server():
    from app.services import network
    assert network.ANNOUNCED == {"path": "/", "service": "syntropy-health"}


def test_the_sign_in_screen_can_say_how_to_reset_a_password(raw_client, monkeypatch):
    from app.services import updates
    r = raw_client.get("/api/auth/recovery")
    assert r.status_code == 200 and 'accounts reset-password "NAME"' in r.json()["command"]
    monkeypatch.setattr(updates, "method", lambda: {"kind": "docker"})
    assert raw_client.get("/api/auth/recovery").json()["command"].startswith("docker exec -it syntropy-health")
    monkeypatch.setattr(updates, "method", lambda: {"kind": "linux"})
    assert raw_client.get("/api/auth/recovery").json()["command"].startswith("sudo syntropy-health")
    monkeypatch.setattr(updates, "method", lambda: {"kind": "app"})
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", "/Users/alex/Syntropy Health")
    assert "/Users/alex" in raw_client.get("/api/auth/recovery").json()["command"]
    far = TestClient(raw_client.app, base_url=BASE, client=("8.8.8.8", 5000))
    assert "/Users/alex" not in far.get("/api/auth/recovery").json()["command"]


@pytest.mark.parametrize("client, headers", [
    (("192.168.1.77", 5000), {}),                                   # someone else on the home network
    (("100.64.0.9", 5000), {}),                                     # Tailscale
    (("127.0.0.1", 5000), {"X-Forwarded-For": "192.168.1.77"}),     # a proxy on this computer
    (("127.0.0.1", 5000), {"Host": "evil.example.com"}),            # a web page that points its own name here
])
def test_the_path_to_the_program_stays_on_this_computer(raw_client, monkeypatch, client, headers):
    from app.services import updates
    _signed_in_owner(raw_client)
    monkeypatch.setattr(updates, "method", lambda: {"kind": "app"})
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys.executable", "C:/Users/alex/AppData/Local/Programs/Syntropy Health/Syntropy Health.exe")
    assert "alex" in raw_client.get("/api/auth/recovery").json()["command"]
    reply = TestClient(raw_client.app, base_url=BASE, client=client).get("/api/auth/recovery", headers=headers).json()
    assert reply["command"] == 'syntropy-health accounts reset-password "NAME"' and reply["run_there"]


def test_recovery_says_to_go_to_the_computer_only_for_the_apps(raw_client, monkeypatch):
    from app.services import updates
    near = TestClient(raw_client.app, base_url=BASE, client=("192.168.1.77", 5000))
    for kind in ("docker", "linux", "source"):
        monkeypatch.setattr(updates, "method", lambda kind=kind: {"kind": kind})
        assert not near.get("/api/auth/recovery").json().get("run_there")
    monkeypatch.setattr(updates, "method", lambda: {"kind": "app"})
    monkeypatch.setattr("sys.frozen", True, raising=False)
    assert not raw_client.get("/api/auth/recovery").json().get("run_there")


def test_the_age_for_suggesting_a_sign_in_is_the_owners_to_set(raw_client):
    client = _signed_in_owner(raw_client)
    maria_client, _ = _join(client, _person(client, "Maria"))
    assert client.get("/api/status").json()["adult_age"] == 18
    assert client.put("/api/settings/general", json={"adult_age": 16}).status_code == 200
    assert client.get("/api/settings").json()["adult_age"] == 16
    assert maria_client.get("/api/status").json()["adult_age"] == 16
    assert maria_client.put("/api/settings/general", json={"adult_age": 21}).status_code == 403
    assert client.put("/api/settings/general", json={"adult_age": 3}).status_code == 422


def test_the_api_route_map_is_only_for_developers(raw_client):
    assert raw_client.get("/api/docs").status_code == 404
    assert raw_client.get("/api/openapi.json").status_code == 404
    _signed_in_owner(raw_client)
    assert raw_client.put("/api/settings/developer", json={"enabled": True}).status_code == 200
    assert "/api/status" in raw_client.get("/api/openapi.json").json()["paths"]


# ---------------------------------------------------------------------------
# The simulator
# ---------------------------------------------------------------------------

SIM_ASK = {"response_type": "code", "client_id": "c", "state": "s", "code_challenge": "x", "code_challenge_method": "S256",
           "aud": "http://localhost:8000/sim/fhir/epic/epic-stanford-health-care/r4", "scope": "patient/*.read"}


def test_the_simulator_isnt_there_outside_developer_mode(raw_client):
    client = _signed_in_owner(raw_client)
    ask = {**SIM_ASK, "redirect_uri": "http://localhost:8000/callback"}
    assert client.get("/sim/oauth/authorize", params=ask).status_code == 404
    assert client.post("/sim/oauth/token", data={"grant_type": "x"}).status_code == 404
    assert client.get("/sim/fhir/epic/epic-stanford-health-care/r4/metadata").status_code == 404


def test_the_simulator_only_sends_people_back_to_this_server(client):
    r = client.get("/sim/oauth/authorize", params={**SIM_ASK, "redirect_uri": "https://evil.example/cb"})
    assert r.status_code == 400 and "evil" not in r.text
    r = client.post("/sim/oauth/authorize", data={**SIM_ASK, "redirect_uri": "https://evil.example/cb", "step": "consent", "decision": "deny"},
                    follow_redirects=False)
    assert r.status_code == 400
    page = client.get("/sim/oauth/authorize", params={**SIM_ASK, "redirect_uri": "http://localhost:8000/callback"})
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    assert "script-src 'sha256-" in csp and "unsafe-inline" not in csp.split("style-src")[0] and "onclick" not in page.text


def test_simulated_sources_keep_syncing_with_developer_mode_off(client):
    from app.services import sync
    import asyncio
    connected = connect_institution(client, "epic-stanford-health-care")
    assert client.put("/api/settings/developer", json={"enabled": False}).status_code == 200
    result = asyncio.run(sync.sync_connection(connected["connection_id"], "scheduled"))
    assert result["status"] != "error", result


# ---------------------------------------------------------------------------
# The sign-in banner
# ---------------------------------------------------------------------------

def _problem_after(client, url):
    r = client.get(url, follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    assert "error=" not in location and "problem=" in location
    token = location.split("problem=")[1]
    from urllib.parse import unquote
    return client.get("/api/sign-in-problem", params={"token": unquote(token)}).json()["message"]


def test_a_link_cant_put_words_in_the_sign_in_banner(client):
    message = _problem_after(client, "/callback?error=x&error_description=Call%20555-0100")
    assert "555" not in message and "Start the connection again" in message
    message = _problem_after(client, "/callback?error=x&state=made-up&error_description=Call%20555-0100")
    assert "555" not in message
    assert client.get("/api/sign-in-problem", params={"token": "made.up"}).json()["message"] is None


def test_a_health_systems_own_words_are_shown_for_a_sign_in_this_server_started(client):
    r = client.post("/api/connections/ehr", json={"institution_id": "epic-stanford-health-care"})
    state = r.json()["auth_url"].split("state=")[1].split("&")[0]
    message = _problem_after(client, f"/callback?error=server_error&state={state}&error_description=Portal%20is%20down")
    assert message == "Portal is down"


def test_refusing_at_the_portal_needs_no_pending_sign_in(client):
    assert "No records were shared" in _problem_after(client, "/callback?error=access_denied")


# ---------------------------------------------------------------------------
# The log
# ---------------------------------------------------------------------------

def test_everyone_sees_backups_and_password_resets_in_their_log(raw_client, monkeypatch):
    from app import cli
    client = _signed_in_owner(raw_client)
    maria_client, _ = _join(client, _person(client, "Maria"))
    assert client.post("/api/backups", json={"passphrase": "a long backup passphrase"}, headers=CSRF).status_code == 200
    monkeypatch.setattr("getpass.getpass", lambda prompt: "a brand new password")
    cli.accounts_command("reset-password", "Alex")
    client = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    assert client.post("/api/auth/login", json={"account": "Alex", "password": "a brand new password"}, headers=CSRF).status_code == 200
    events = maria_client.get("/api/audit").json()["events"]
    assert any(e["action"] == "export.backup" and e["who"] == "Alex" for e in events)
    assert any(e["action"] == "auth.password_reset" and e["who"] == "cli" for e in events)
    # But not what else the owner did.
    assert not any(e["action"] == "auth.login" and e["who"] == "Alex" for e in events)


def test_removing_someone_is_logged_and_their_entries_lose_their_details(raw_client):
    from app.core import config
    client = _signed_in_owner(raw_client)
    leo = _person(client, "Leo", "child")
    assert client.post("/api/connections/wearable", json={"provider": "oura", "mode": "simulated", "profile_id": leo}).status_code == 200
    _journal(client, leo)
    assert client.delete(f"/api/profiles/{leo}").status_code == 200
    events = client.get("/api/audit", params={"limit": 500}).json()["events"]
    assert any(e["action"] == "profile.removed" and e["who"] == "Alex" for e in events)
    c = sqlite3.connect(str(config.db_path()))
    kept = c.execute("SELECT action, detail FROM audit_log WHERE profile_id = ?", (leo,)).fetchall()
    c.close()
    assert kept and all(detail is None for _, detail in kept)


def test_what_ai_apps_asked_is_kept_short_and_for_their_own_account(client):
    from app.api.system import _ai_lookup_detail
    from app.core import config
    token = client.post("/api/agents/tokens", json={"name": "Claude"}).json()["token"]
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_records", "arguments": {"query": "hiv test " * 100, "limit": 2}}}
    assert client.post("/mcp", json=call, headers={"Authorization": f"Bearer {token}"}).status_code == 200
    c = sqlite3.connect(str(config.db_path()))
    logged = [r[0] for r in c.execute("SELECT detail FROM audit_log WHERE action LIKE 'mcp.%'")]
    c.close()
    assert logged and all(len(d) < 600 for d in logged)
    assert "hiv" not in (_ai_lookup_detail(logged[0], False) or "")
