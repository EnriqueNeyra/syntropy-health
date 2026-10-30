"""A phone can only do what whoever paired it may still do, and it never stands in for the person it sends for."""

from __future__ import annotations

import sqlite3
import time

from fastapi.testclient import TestClient

from tests.conftest import BASE, CSRF, LOCAL_CLIENT
from tests.test_accounts import _ids, _join, _journal, _person, _phone, _signed_in_owner

SAMPLE = {"device_id": "ios-1", "device_name": "iPhone", "samples": [
    {"id": "s1", "metric_type": "heart_rate", "value": 60, "unit": "count/min",
     "start_date": "2026-01-02T08:00:00Z", "end_date": "2026-01-02T08:00:00Z"}]}


def _account(client):
    return client.get("/api/status").json()["account"]["id"]


def _db():
    from app.core import config
    c = sqlite3.connect(str(config.db_path()))
    c.row_factory = sqlite3.Row
    return c


def test_a_phone_paired_by_someone_else_stops_when_the_person_joins(raw_client):
    client = _signed_in_owner(raw_client)
    teen = _person(client, "Teen", "child")
    assert _journal(client, teen).status_code == 200
    paired, anon, auth, screens = _phone(client, teen)
    teen_client, _ = _join(client, teen)

    # The phone's screens end, it can't open a new one as Teen, and it no longer sends into her record.
    assert screens.get("/api/profiles").status_code == 401
    assert anon.post("/api/devices/web-session", headers=auth).status_code == 401
    assert anon.post("/api/ingest/wearables", json=SAMPLE, headers=auth).status_code == 401
    assert not teen_client.get("/api/biometrics/samples", params={"profile": teen}).json()["samples"]
    assert teen_client.get("/api/devices").json()["devices"] == []
    # Its source stays, shown as not paired, until she pairs a phone of her own.
    sources = teen_client.get("/api/connections", params={"profile": teen, "include_disconnected": True}).json()["connections"]
    assert [s["status"] for s in sources if s["kind"] == "device"] == ["disconnected"]

    # Her own phone works, and opens her own record.
    _, anon, auth, screens = _phone(teen_client)
    assert anon.post("/api/ingest/wearables", json=SAMPLE, headers=auth).status_code == 200
    assert screens.get("/api/status").json()["account"]["name"] == "Teen"


def test_joining_ends_a_pairing_code_still_waiting(raw_client):
    client = _signed_in_owner(raw_client)
    teen = _person(client, "Teen", "child")
    code = client.post("/api/devices/pairing-code", json={"profile_id": teen}).json()["code"]
    _join(client, teen)
    anon = TestClient(client.app, base_url=BASE)
    assert anon.post("/api/devices/pair", json={"code": code, "device_name": "Mom's phone"}).status_code == 400


def test_joining_ends_tokens_others_limited_to_them_and_their_conversations(raw_client):
    client = _signed_in_owner(raw_client)
    teen = _person(client, "Teen", "child")
    token = client.post("/api/agents/tokens", json={"name": "Claude", "profile_id": teen}).json()["token"]
    saved = {"title": "About Teen", "messages": [{"role": "user", "content": "her results"}]}
    assert client.put("/api/ai/chats/c_aaaaaaaa", params={"profile": teen}, json=saved).status_code == 200
    teen_client, _ = _join(client, teen)
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    assert client.post("/mcp", json=call, headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert _db().execute("SELECT COUNT(*) FROM chats").fetchone()[0] == 0
    assert teen_client.get("/api/ai/chats", params={"profile": teen}).json()["chats"] == []


def test_a_phone_paired_for_someone_else_is_not_the_whole_account(raw_client):
    client = _signed_in_owner(raw_client)
    kid = _person(client, "Kid", "child")
    alex = _ids(client)["Alex"]["id"]
    _journal(client, alex)
    _, _, _, screens = _phone(client, kid)

    status = screens.get("/api/status").json()
    assert not status["account"]["owner"] and [p["name"] for p in status["profiles"]] == ["Kid"]
    assert screens.get("/api/export/backup").status_code == 403
    assert screens.get("/api/health").status_code == 403
    assert screens.get("/api/agents/local-command").status_code == 403
    assert screens.get("/api/agents/tokens").status_code == 403
    assert screens.post("/api/agents/tokens", json={"name": "x"}).status_code == 403
    assert screens.post("/api/auth/password", json={"current": "x", "new": "a long new password"}).status_code == 403
    assert screens.get("/api/auth/sessions").status_code == 403
    assert screens.post("/api/profiles", json={"name": "New"}).status_code == 403
    assert screens.put(f"/api/profiles/{kid}/access", json={"account_id": _account(client), "level": "view"}).status_code == 403
    assert screens.post(f"/api/profiles/{kid}/invite").status_code == 403
    assert screens.delete(f"/api/profiles/{kid}/data").status_code == 403
    assert screens.delete(f"/api/profiles/{kid}").status_code == 403
    # It still does what it's for.
    assert screens.get("/api/summary", params={"profile": kid}).status_code == 200
    assert _journal(screens, kid).status_code == 200
    # And the log it shows is about that person, not what the account does elsewhere.
    assert {e["profile_id"] for e in screens.get("/api/audit").json()["events"]} == {kid}


def test_a_session_outliving_its_phone_opens_nothing(raw_client):
    from app.core.db import db
    client = _signed_in_owner(raw_client)
    _, _, _, screens = _phone(client)
    with db() as conn:
        conn.execute("DELETE FROM devices")
    assert screens.get("/api/profiles").status_code == 401


def test_cutting_a_managers_access_stops_their_phone(raw_client):
    client = _signed_in_owner(raw_client)
    maria = _person(client, "Maria")
    maria_client, _ = _join(client, maria)
    alex = _account(client)
    assert maria_client.put(f"/api/profiles/{maria}/access", json={"account_id": alex, "level": "manage"}).status_code == 200
    _, anon, auth, _ = _phone(client, maria)
    waiting = client.post("/api/devices/pairing-code", json={"profile_id": maria}).json()["code"]
    assert anon.post("/api/ingest/wearables", json=SAMPLE, headers=auth).status_code == 200

    # Down to view: the phone stops, and so does a code they'd made; reading still works.
    assert maria_client.put(f"/api/profiles/{maria}/access", json={"account_id": alex, "level": "view"}).status_code == 200
    assert anon.post("/api/ingest/wearables", json=SAMPLE, headers=auth).status_code == 401
    assert anon.post("/api/devices/pair", json={"code": waiting, "device_name": "x"}).status_code == 400
    assert client.get("/api/summary", params={"profile": maria}).status_code == 200

    # Her own phone isn't touched by what happens to Alex's access.
    _, own_anon, own_auth, _ = _phone(maria_client)
    assert maria_client.put(f"/api/profiles/{maria}/access", json={"account_id": alex, "level": None}).status_code == 200
    assert own_anon.post("/api/ingest/wearables", json=SAMPLE, headers=own_auth).status_code == 200


def test_a_code_made_before_access_ended_is_checked_when_used(raw_client):
    from app.core.db import db
    client = _signed_in_owner(raw_client)
    maria = _person(client, "Maria")
    maria_client, _ = _join(client, maria)
    alex = _account(client)
    maria_client.put(f"/api/profiles/{maria}/access", json={"account_id": alex, "level": "manage"})
    code = client.post("/api/devices/pairing-code", json={"profile_id": maria}).json()["code"]
    with db() as conn:      # as if it had been made by an earlier version, which left the code behind
        conn.execute("UPDATE profile_access SET level = 'view'")
    anon = TestClient(client.app, base_url=BASE)
    assert anon.post("/api/devices/pair", json={"code": code, "device_name": "x"}).status_code == 400


def test_upgrading_stops_phones_already_handed_over_or_left_with_a_former_manager(tmp_path):
    from app.core import config, db

    path = config.db_path()
    conn = sqlite3.connect(str(path))
    for f in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
        if int(f.name.split("_", 1)[0]) >= 40:
            break
        for statement in db._split_sql(f.read_text()):
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 39")
    conn.executemany("INSERT INTO profiles(id, name, relationship, is_default, created_at, updated_at) VALUES (?, ?, 'self', ?, 1, 1)",
                     [("prf_o", "Olivia", 1), ("prf_t", "Teen", 0), ("prf_b", "Bob", 0), ("prf_k", "Kid", 0), ("prf_u", "Uma", 0)])
    conn.executemany("INSERT INTO accounts(id, profile_id, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                     [("acc_owner", "prf_o", "owner", 500, 500), ("acc_t", "prf_t", "member", 100, 100),
                      ("acc_b", "prf_b", "member", 10, 10)])
    conn.executemany("INSERT INTO profile_access(account_id, profile_id, level, created_at) VALUES (?, ?, ?, 1)",
                     [("acc_b", "prf_t", "view"), ("acc_owner", "prf_k", "manage")])
    conn.executemany("INSERT INTO connections(id, profile_id, kind, provider, display_name, mode, status, created_at, updated_at) "
                     "VALUES (?, 'prf_t', 'device', 'apple_health', ?, 'live', 'active', 1, 1)",
                     [("con_handed", "Mom's phone"), ("con_mine", "Teen's phone"), ("con_shared", "Shared")])
    rows = [("d_owner", "prf_o", None, 50, "acc_owner"),         # older than a migrated owner's account: stays
            ("d_handed", "prf_t", "con_handed", 50, "acc_t"),    # paired before Teen joined: stops
            ("d_mine", "prf_t", "con_mine", 200, "acc_t"),       # hers: stays
            ("d_viewer", "prf_t", None, 200, "acc_b"),           # Bob is down to view: stops
            ("d_kid", "prf_k", None, 200, "acc_owner"),          # the owner still manages Kid: stays
            ("d_gone", "prf_t", "con_shared", 50, "acc_t"),      # stops, but its source has a good phone too
            ("d_good", "prf_t", "con_shared", 200, "acc_t")]
    conn.executemany("INSERT INTO devices(id, profile_id, connection_id, name, token_hash, created_at, account_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     [(d, p, c, f"Phone {d}", f"hash{d}", at, a) for d, p, c, at, a in rows])
    conn.execute("INSERT INTO user_sessions(token_hash, created_at, expires_at, last_seen_at, device_id, account_id) "
                 "VALUES ('s1', 1, ?, 1, 'd_handed', 'acc_t')", (time.time() + 1000,))
    conn.commit()
    conn.close()
    db.reset_migration_cache()
    db.ensure_migrated(path)

    c = sqlite3.connect(str(path))
    assert {r[0] for r in c.execute("SELECT id FROM devices")} == {"d_owner", "d_mine", "d_kid", "d_good"}
    assert c.execute("SELECT COUNT(*) FROM user_sessions").fetchone()[0] == 0
    status = dict(c.execute("SELECT id, status FROM connections").fetchall())
    assert status == {"con_handed": "disconnected", "con_mine": "active", "con_shared": "active"}
    logged = c.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'device.revoked' AND actor = 'system'").fetchone()[0]
    assert logged == 3
    c.close()


def test_people_joining_are_shown_what_they_inherit(raw_client):
    client = _signed_in_owner(raw_client)
    teen = _person(client, "Teen", "child")
    assert client.post("/api/connections/wearable", json={"provider": "oura", "mode": "simulated", "profile_id": teen}).status_code in (200, 400)
    _phone(client, teen)
    teen_client = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    teen_client.headers.update(CSRF)
    code = client.post(f"/api/profiles/{teen}/invite").json()["code"]
    assert teen_client.post("/api/invites/accept", json={"code": code, "password": "her own password", "accept_terms": True}).status_code == 200

    status = teen_client.get("/api/status").json()
    assert status["profiles"][0]["relationship"] == "self"
    review = status["join_review"]
    assert review["carers"] == ["Alex"] and review["phones"] == ["iPhone"]
    assert teen_client.post("/api/accounts/me/join-review/seen").status_code == 200
    assert "join_review" not in teen_client.get("/api/status").json()
