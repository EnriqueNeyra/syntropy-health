"""Household accounts: everyone signs in as themselves and sees only their own data and what they're given."""

from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from tests.conftest import BASE, CSRF, LOCAL_CLIENT, PASSPHRASE

MARIA_PASSWORD = "maria's own password"


def _signed_in_owner(raw_client):
    r = raw_client.post("/api/setup", json={"password": PASSPHRASE, "profile_name": "Alex", "accept_terms": True},
                        headers=CSRF)
    assert r.status_code == 200, r.text
    raw_client.headers.update(CSRF)
    return raw_client


def _person(client, name, relationship="partner"):
    r = client.post("/api/profiles", json={"name": name, "relationship": relationship})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _join(client, profile_id, password=MARIA_PASSWORD):
    """The invited person, on their own device: checks the code, chooses a password, and is signed in."""
    code = client.post(f"/api/profiles/{profile_id}/invite").json()["code"]
    theirs = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    theirs.headers.update(CSRF)
    assert theirs.post("/api/invites/check", json={"code": code}).json()["name"]
    r = theirs.post("/api/invites/accept", json={"code": code, "password": password, "accept_terms": True})
    assert r.status_code == 200, r.text
    return theirs, code


def _ids(client):
    return {p["name"]: p for p in client.get("/api/profiles").json()["profiles"]}


def _journal(client, profile_id):
    return client.post("/api/journal", params={"profile": profile_id},
                       json={"event_type": "headache", "category": "symptoms", "name": "Headache", "start": "2026-01-02T08:00:00Z"})


def test_setup_makes_the_owner(raw_client):
    client = _signed_in_owner(raw_client)
    status = client.get("/api/status").json()
    assert status["account"]["owner"] and status["account"]["name"] == "Alex"
    alex = _ids(client)["Alex"]
    assert alex["is_self"] and alex["is_default"] and alex["access"] == "manage"


def test_an_invited_person_gets_their_data_to_themselves(raw_client):
    client = _signed_in_owner(raw_client)
    maria = _person(client, "Maria")
    assert _journal(client, maria).status_code == 200            # looked after by Alex until she joins
    assert _ids(client)["Maria"]["invited_until"] is None
    maria_client, code = _join(client, maria)

    # Alex no longer sees her, or anything of hers.
    assert "Maria" not in _ids(client)
    assert client.get("/api/biometrics/events", params={"profile": maria}).status_code == 404
    assert _journal(client, maria).status_code == 404
    # She sees herself (with what Alex logged for her before), and nobody else.
    people = _ids(maria_client)
    assert list(people) == ["Maria"] and people["Maria"]["is_self"]
    assert maria_client.get("/api/biometrics/events", params={"profile": maria, "days": 0}).json()["events"]
    alex = _ids(client)["Alex"]["id"]
    assert maria_client.get("/api/summary", params={"profile": alex}).status_code == 404
    # The code worked once.
    again = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    assert again.post("/api/invites/accept", json={"code": code, "password": "another password", "accept_terms": True},
                      headers=CSRF).status_code == 404


def test_sharing_view_only_or_to_manage(raw_client):
    client = _signed_in_owner(raw_client)
    maria = _person(client, "Maria")
    maria_client, _ = _join(client, maria)
    alex_account = client.get("/api/status").json()["account"]["id"]

    r = maria_client.put(f"/api/profiles/{maria}/access", json={"account_id": alex_account, "level": "view"})
    assert r.status_code == 200 and r.json()["access"][0]["level"] == "view"
    assert _ids(client)["Maria"]["access"] == "view"
    assert client.get("/api/biometrics/events", params={"profile": maria}).status_code == 200
    denied = _journal(client, maria)
    assert denied.status_code == 403 and "Maria" in denied.json()["detail"]
    # Someone who only views can't hand out access or invite.
    assert client.get(f"/api/profiles/{maria}/access").status_code == 403

    maria_client.put(f"/api/profiles/{maria}/access", json={"account_id": alex_account, "level": "manage"})
    assert _journal(client, maria).status_code == 200
    maria_client.put(f"/api/profiles/{maria}/access", json={"account_id": alex_account, "level": None})
    assert "Maria" not in _ids(client)


def test_signing_in_as_someone(raw_client):
    client = _signed_in_owner(raw_client)
    _join(client, _person(client, "Maria"))
    anon = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    anon.headers.update(CSRF)
    status = anon.get("/api/status").json()
    assert {a["name"] for a in status["accounts"]} == {"Alex", "Maria"}      # to choose from, nearby
    far = TestClient(client.app, base_url=BASE, client=("8.8.8.8", 5000))
    assert "accounts" not in far.get("/api/status").json()                    # not from farther away

    assert anon.post("/api/auth/login", json={"password": MARIA_PASSWORD}).status_code == 400   # who?
    assert anon.post("/api/auth/login", json={"account": "maria", "password": PASSPHRASE}).status_code == 401
    assert anon.post("/api/auth/login", json={"account": "Maria", "password": MARIA_PASSWORD}).status_code == 200
    assert anon.get("/api/status").json()["account"]["name"] == "Maria"


def test_the_owner_changes_server_settings(raw_client):
    client = _signed_in_owner(raw_client)
    maria_client, _ = _join(client, _person(client, "Maria"))
    assert maria_client.put("/api/settings/general", json={"timezone": "Europe/Madrid"}).status_code == 403
    assert maria_client.put("/api/settings/wearables/oura", json={"client_id": "x"}).status_code == 403
    assert maria_client.get("/api/export/backup").status_code == 403
    assert maria_client.put("/api/system/network", json={"enabled": True}).status_code == 403
    # Her own preferences are hers.
    assert maria_client.put("/api/settings/general", json={"units": "us"}).status_code == 200
    assert maria_client.put("/api/preferences", json={"accent": "teal"}).json()["accent"] == "teal"
    assert maria_client.get("/api/status").json()["preferences"] == {"units": "us", "accent": "teal"}
    assert client.get("/api/status").json()["preferences"] == {"units": None, "accent": "heart"}
    # Each person agrees to the terms and finishes first-run for themselves.
    assert maria_client.get("/api/status").json()["terms_accepted"]
    assert client.put("/api/settings/general", json={"timezone": "Europe/Madrid"}).status_code == 200


def test_removing_people(raw_client):
    client = _signed_in_owner(raw_client)
    maria = _person(client, "Maria")
    maria_client, _ = _join(client, maria)
    alex = _ids(client)["Alex"]["id"]
    assert client.delete(f"/api/profiles/{alex}").status_code == 400          # the only owner
    leo = _person(maria_client, "Leo", "child")                               # Maria looks after Leo
    assert client.delete(f"/api/profiles/{leo}").status_code == 404           # Alex can't see him
    assert maria_client.delete(f"/api/profiles/{leo}").status_code == 200
    # An owner can remove someone from the server; they're signed out.
    assert client.delete(f"/api/profiles/{maria}").status_code == 200
    assert maria_client.get("/api/profiles").status_code == 401


def test_people_nobody_looks_after_go_to_the_owner(raw_client):
    client = _signed_in_owner(raw_client)
    maria = _person(client, "Maria")
    maria_client, _ = _join(client, maria)
    leo = _person(maria_client, "Leo", "child")
    assert "Leo" not in _ids(client)
    # Maria leaves (removing herself would take Leo's only guardian with her): Alex, the owner, looks after him.
    assert maria_client.delete(f"/api/profiles/{maria}").status_code == 200
    assert _ids(client)["Leo"]["access"] == "manage"
    assert client.get("/api/summary", params={"profile": leo}).status_code == 200


def _phone(client, profile_id=None):
    code = client.post("/api/devices/pairing-code", json={"profile_id": profile_id}).json()["code"]
    anon = TestClient(client.app, base_url=BASE)
    paired = anon.post("/api/devices/pair", json={"code": code, "device_name": "iPhone"}).json()
    auth = {"X-Syntropy-Device-Token": paired["device_token"]}
    cookie = anon.post("/api/devices/web-session", headers=auth).json()["cookie"]
    screens = TestClient(client.app, base_url=BASE, cookies={cookie["name"]: cookie["value"]})
    screens.headers.update(CSRF)
    return paired, anon, auth, screens


def test_a_phone_sends_for_one_person_and_opens_what_its_owner_sees(raw_client):
    client = _signed_in_owner(raw_client)
    leo = _person(client, "Leo", "child")
    alex = _ids(client)["Alex"]["id"]

    # Alex's own phone: sends Alex's data, and its screens open everyone Alex sees.
    paired, anon, auth, screens = _phone(client)
    assert paired["profile_id"] == alex and paired["profile_name"] == "Alex"
    batch = {"device_id": "ios-1", "device_name": "iPhone", "samples": [{"id": "s1", "metric_type": "heart_rate", "value": 60, "unit": "count/min",
                                                 "start_date": "2026-01-02T08:00:00Z", "end_date": "2026-01-02T08:00:00Z"}],
             "profile_id": leo}          # a phone can't choose someone else
    assert anon.post("/api/ingest/wearables", json=batch, headers=auth).json()["inserted"] == 1
    assert client.get("/api/biometrics/samples", params={"profile": alex}).json()["samples"]
    assert not client.get("/api/biometrics/samples", params={"profile": leo}).json()["samples"]
    assert set(_ids(screens)) == {"Alex", "Leo"}

    # A phone Alex paired for Leo: its screens open Leo alone.
    paired, _, _, screens = _phone(client, leo)
    assert paired["profile_id"] == leo
    assert list(_ids(screens)) == ["Leo"]
    assert screens.get("/api/summary", params={"profile": alex}).status_code == 404


def test_agent_tokens_read_only_what_their_account_sees(raw_client):
    client = _signed_in_owner(raw_client)
    maria_client, _ = _join(client, _person(client, "Maria"))
    token = maria_client.post("/api/agents/tokens", json={"name": "Claude"}).json()["token"]
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_profiles", "arguments": {}}}
    r = client.post("/mcp", json=call, headers={"Authorization": f"Bearer {token}"})
    people = json.loads(r.json()["result"]["content"][0]["text"])
    assert [p["name"] for p in people] == ["Maria"]
    assert client.get("/api/agents/tokens").json()["tokens"] == []           # Alex doesn't see Maria's tokens
    assert maria_client.get("/api/agents/local-command").status_code == 403  # the computer's own MCP reads everyone


def test_online_ai_consent_is_each_persons(raw_client):
    client = _signed_in_owner(raw_client)
    maria_client, _ = _join(client, _person(client, "Maria"))
    assert client.post("/api/ai/acknowledge").status_code == 200
    assert client.get("/api/ai/config").json()["cloud_ack"]
    assert not maria_client.get("/api/ai/config").json()["cloud_ack"]
    assert maria_client.put("/api/ai/config", json={"provider": "openai", "api_key": "sk-test"}).status_code == 403


def test_activity_log_shows_what_each_account_may_see(raw_client):
    client = _signed_in_owner(raw_client)
    maria = _person(client, "Maria")
    _journal(client, maria)
    maria_client, _ = _join(client, maria)
    alex = _ids(client)["Alex"]["id"]
    _journal(client, alex)
    theirs = maria_client.get("/api/audit").json()["events"]
    assert theirs and all(e["profile_id"] in (maria, None) for e in theirs)
    assert any(e["who"] == "Maria" for e in theirs)


def test_an_instance_from_before_accounts_keeps_its_password(tmp_path, monkeypatch):
    """The owner's password, agreement and layout carry over; everyone else stays in the owner's care."""
    from app.core import config, db, security

    path = config.db_path()
    conn = sqlite3.connect(str(path))
    for f in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
        if int(f.name.split("_", 1)[0]) > 8:
            break
        for statement in db._split_sql(f.read_text()):
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 8")
    rows = [("setup.completed", True), ("auth.required", True), ("terms.accepted", {"version": 1, "at": 1}),
            ("auth.password_hash", security.encrypt_json(security.hash_password(PASSPHRASE))),
            ("overview.layout.prf_a", [{"id": "sleep", "visible": True, "options": {}}]), ("ai.cloud_ack", 1.0),
            ("display.accent", "teal")]
    conn.executemany("INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, 0)", [(k, json.dumps(v)) for k, v in rows])
    conn.executemany("INSERT INTO profiles(id, name, relationship, is_default, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0)",
                     [("prf_a", "Alex", "self", 1, 1), ("prf_b", "Sam", "child", 0, 2)])
    conn.commit()
    conn.close()
    db.reset_migration_cache()

    from app.main import create_app
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT) as c:
        c.headers.update(CSRF)
        assert c.post("/api/auth/login", json={"password": PASSPHRASE}).status_code == 200
        status = c.get("/api/status").json()
        assert status["account"]["owner"] and status["terms_accepted"]
        assert status["preferences"]["accent"] == "teal"
        assert {p["name"]: p["access"] for p in status["profiles"]} == {"Alex": "manage", "Sam": "manage"}
        assert c.get("/api/profiles/prf_a/overview").json()["widgets"][0]["id"] == "sleep"
        assert c.get("/api/ai/config").json()["cloud_ack"]


def test_a_new_mac_can_look_for_a_server_to_join(raw_client, monkeypatch):
    """Before this server is set up, the Mac app's own window can list the household's servers (and nothing else can)."""
    from app.services import network
    monkeypatch.setattr(network, "discover", lambda: [{"name": "Alex's Mac", "url": "http://alexs-mac.local:8000",
                                                        "address": "http://192.168.1.20:8000", "version": "1.0.0"}])
    found = raw_client.get("/api/desktop/discover", headers=CSRF)
    assert found.status_code == 200 and found.json()["servers"][0]["name"] == "Alex's Mac"
    far = TestClient(raw_client.app, base_url=BASE, client=("192.168.1.30", 5000))
    assert far.get("/api/desktop/discover", headers=CSRF).status_code == 404
    assert raw_client.post("/api/desktop/probe", json={"url": "ftp://x"}, headers=CSRF).status_code == 400
    _signed_in_owner(raw_client)
    assert raw_client.get("/api/desktop/discover").status_code == 404      # this Mac is the server now


def test_profile_changes_are_checked_and_birth_date_can_be_cleared(client):
    pid = client.post("/api/profiles", json={"name": "Sam", "relationship": "child", "birth_date": "2015-06-01"}).json()["id"]
    assert client.patch(f"/api/profiles/{pid}", json={"birth_date": None}).json()["birth_date"] is None
    for bad in ({"name": "  "}, {"birth_date": "2015-02-31"}, {"birth_date": "June 1"}, {"relationship": "pet"},
                {"color": "red;"}):
        assert client.patch(f"/api/profiles/{pid}", json=bad).status_code == 422, bad
    assert client.post("/api/profiles", json={"name": "Kim", "birth_date": "yesterday"}).status_code == 422
    assert client.patch(f"/api/profiles/{pid}", json={"name": " Sammy "}).json()["name"] == "Sammy"
