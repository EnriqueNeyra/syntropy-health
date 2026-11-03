"""Who runs the server, who's on it, what 'change' access allows, and signing in when names repeat."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from tests.conftest import BASE, CSRF, LOCAL_CLIENT, PASSPHRASE
from tests.test_accounts import MARIA_PASSWORD, _ids, _join, _journal, _person, _signed_in_owner


def _account(client):
    return client.get("/api/status").json()["account"]["id"]


def _db():
    from app.core import config
    c = sqlite3.connect(str(config.db_path()))
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------

def test_handing_the_server_over(raw_client):
    client = _signed_in_owner(raw_client)
    maria = _person(client, "Maria")
    maria_client, _ = _join(client, maria)
    maria_id = _account(maria_client)
    alex_id = _account(client)

    # Only an owner can, and only with their own password.
    assert maria_client.put(f"/api/accounts/{maria_id}/role", json={"role": "owner", "password": MARIA_PASSWORD}).status_code == 403
    assert client.put(f"/api/accounts/{maria_id}/role", json={"role": "owner", "password": "wrong"}).status_code == 403
    assert client.put(f"/api/accounts/{maria_id}/role", json={"role": "owner"}).status_code == 403
    assert client.put("/api/accounts/acc_nobody/role", json={"role": "owner", "password": PASSPHRASE}).status_code == 404
    assert client.put(f"/api/accounts/{maria_id}/role", json={"role": "king", "password": PASSPHRASE}).status_code == 422
    assert maria_client.get("/api/export/backup").status_code == 403

    # The last owner can't simply stop being one.
    r = client.put(f"/api/accounts/{alex_id}/role", json={"role": "member", "password": PASSPHRASE})
    assert r.status_code == 400 and "owner" in r.json()["detail"]
    assert client.get("/api/status").json()["account"]["owner"]

    # Handing over: one step, always one owner.
    r = client.put(f"/api/accounts/{maria_id}/role", json={"role": "owner", "step_down": True, "password": PASSPHRASE})
    assert r.status_code == 200, r.text
    assert maria_client.get("/api/status").json()["account"]["owner"]
    assert not client.get("/api/status").json()["account"]["owner"]
    assert maria_client.put("/api/settings/general", json={"timezone": "Europe/Madrid"}).status_code == 200
    assert client.put("/api/settings/general", json={"timezone": "Europe/Paris"}).status_code == 403
    actions = [e for e in maria_client.get("/api/audit").json()["events"] if e["action"] == "account.role"]
    assert actions and all(e["who"] == "Alex" for e in actions)

    # Now the former owner can leave, as anyone can.
    assert client.post("/api/accounts/me/delete", json={"password": PASSPHRASE}).status_code == 200
    assert maria_client.get("/api/accounts").json()["accounts"][0]["name"] == "Maria"


def test_the_command_line_can_make_someone_owner(raw_client, monkeypatch, capsys):
    from app import cli
    client = _signed_in_owner(raw_client)
    maria_client, _ = _join(client, _person(client, "Maria"))
    cli.accounts_command("make-owner", "Maria")
    assert maria_client.get("/api/status").json()["account"]["owner"]
    assert client.get("/api/status").json()["account"]["owner"]


# ---------------------------------------------------------------------------
# Everyone who signs in
# ---------------------------------------------------------------------------

def test_owners_see_who_signs_in_but_not_their_health_data(raw_client):
    client = _signed_in_owner(raw_client)
    maria = _person(client, "Maria")
    maria_client, _ = _join(client, maria)
    assert "Maria" not in _ids(client)
    listed = {a["name"]: a for a in client.get("/api/accounts").json()["accounts"]}
    assert set(listed) == {"Alex", "Maria"} and listed["Alex"]["is_self"] and listed["Maria"]["role"] == "member"
    assert set(listed["Maria"]) == {"id", "name", "color", "profile_id", "role", "created_at", "last_login_at", "has_password", "is_self"}
    assert maria_client.get("/api/accounts").status_code == 403
    # And can remove them, though they've never shared anything.
    assert client.delete(f"/api/profiles/{maria}").status_code == 200
    assert maria_client.get("/api/profiles").status_code == 401
    assert [a["name"] for a in client.get("/api/accounts").json()["accounts"]] == ["Alex"]


def test_a_member_can_delete_their_own_account_with_their_password(raw_client):
    client = _signed_in_owner(raw_client)
    maria = _person(client, "Maria")
    maria_client, _ = _join(client, maria)
    assert _journal(maria_client, maria).status_code == 200
    assert maria_client.delete(f"/api/profiles/{maria}").status_code == 403
    assert maria_client.post("/api/accounts/me/delete", json={"password": "wrong"}).status_code == 403
    assert maria_client.get("/api/profiles").status_code == 200
    assert maria_client.post("/api/accounts/me/delete", json={"password": MARIA_PASSWORD}).status_code == 200
    assert maria_client.get("/api/profiles").status_code == 401
    assert _db().execute("SELECT COUNT(*) FROM profiles WHERE id = ?", (maria,)).fetchone()[0] == 0
    assert _db().execute("SELECT COUNT(*) FROM health_events WHERE profile_id = ?", (maria,)).fetchone()[0] == 0


def test_removing_someone_takes_their_conversations_about_anyone(raw_client):
    client = _signed_in_owner(raw_client)
    maria = _person(client, "Maria")
    maria_client, _ = _join(client, maria)
    leo = _person(maria_client, "Leo", "child")
    saved = {"title": "About Leo", "messages": [{"role": "user", "content": "his results"}]}
    assert maria_client.put("/api/ai/chats/c_aaaaaaaa", params={"profile": leo}, json=saved).status_code == 200
    assert client.delete(f"/api/profiles/{maria}").status_code == 200
    assert _db().execute("SELECT COUNT(*) FROM chats").fetchone()[0] == 0


def test_ending_someones_access_takes_their_conversations_about_that_person(raw_client):
    client = _signed_in_owner(raw_client)
    maria = _person(client, "Maria")
    maria_client, _ = _join(client, maria)
    alex = _account(client)
    maria_client.put(f"/api/profiles/{maria}/access", json={"account_id": alex, "level": "view"})
    saved = {"title": "About Maria", "messages": [{"role": "user", "content": "her results"}]}
    assert client.put("/api/ai/chats/c_aaaaaaaa", params={"profile": maria}, json=saved).status_code == 200
    assert maria_client.put(f"/api/profiles/{maria}/access", json={"account_id": alex, "level": None}).status_code == 200
    assert _db().execute("SELECT COUNT(*) FROM chats").fetchone()[0] == 0


def test_conversations_nobody_can_reach_go_at_the_upgrade():
    from app.core import config, db

    path = config.db_path()
    conn = sqlite3.connect(str(path))
    for f in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
        if int(f.name.split("_", 1)[0]) >= 41:
            break
        for statement in db._split_sql(f.read_text()):
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 40")
    conn.executemany("INSERT INTO profiles(id, name, relationship, is_default, created_at, updated_at) VALUES (?, ?, 'self', 0, 1, 1)",
                     [("prf_o", "Olivia"), ("prf_t", "Teen"), ("prf_k", "Kid")])
    conn.executemany("INSERT INTO accounts(id, profile_id, role, created_at, updated_at) VALUES (?, ?, ?, 1, 1)",
                     [("acc_o", "prf_o", "owner"), ("acc_t", "prf_t", "member")])
    conn.execute("INSERT INTO profile_access(account_id, profile_id, level, created_at) VALUES ('acc_o', 'prf_k', 'manage', 1)")
    chats = [("own", "prf_t", "acc_t"),           # her own: stays
             ("gone", "prf_k", "acc_removed"),    # by an account that's gone
             ("carer", "prf_t", "acc_o"),         # by a former carer, who can't open it
             ("kid", "prf_k", "acc_o"),           # by someone still looking after Kid: stays
             ("dev", "prf_o", "")]                # kept with no account, while developing without a password: stays
    conn.executemany("INSERT INTO chats(id, profile_id, account_id, title, messages_json, created_at, updated_at) "
                     "VALUES (?, ?, ?, 't', '[]', 1, 1)", chats)
    conn.execute("INSERT INTO audit_log(at, actor, action, profile_id, detail) VALUES (1, 'x', 'connection.created', 'prf_gone', '{\"name\":\"Stanford\"}')")
    conn.execute("INSERT INTO audit_log(at, actor, action, profile_id, detail) VALUES (1, 'x', 'connection.created', 'prf_t', '{\"name\":\"Kept\"}')")
    conn.commit()
    conn.close()
    db.reset_migration_cache()
    db.ensure_migrated(path)
    c = sqlite3.connect(str(path))
    assert {r[0] for r in c.execute("SELECT id FROM chats")} == {"own", "kid", "dev"}
    assert dict(c.execute("SELECT profile_id, detail FROM audit_log WHERE action = 'connection.created'").fetchall()) == \
        {"prf_gone": None, "prf_t": '{"name":"Kept"}'}
    c.close()


# ---------------------------------------------------------------------------
# What 'change' allows
# ---------------------------------------------------------------------------

def _helper(client):
    """Teen signs in and lets Sam change her data."""
    teen = _person(client, "Teen")
    teen_client, _ = _join(client, teen)
    sam = _person(client, "Sam")
    sam_client, _ = _join(client, sam, "sam's own password")
    assert teen_client.put(f"/api/profiles/{teen}/access", json={"account_id": _account(sam_client), "level": "manage"}).status_code == 200
    return teen, teen_client, sam_client


def test_change_access_doesnt_let_someone_reshare_or_wipe(raw_client):
    client = _signed_in_owner(raw_client)
    teen, teen_client, sam_client = _helper(client)
    alex = _account(client)
    assert _journal(sam_client, teen).status_code == 200                     # helping is what it's for

    r = sam_client.put(f"/api/profiles/{teen}/access", json={"account_id": alex, "level": "view"})
    assert r.status_code == 403 and "Teen" in r.json()["detail"]
    assert sam_client.get(f"/api/profiles/{teen}/access").status_code == 403
    assert "Teen" not in _ids(client)
    r = sam_client.delete(f"/api/profiles/{teen}/data")
    assert r.status_code == 403 and "Teen" in r.json()["detail"]
    assert teen_client.get("/api/biometrics/events", params={"profile": teen, "days": 0}).json()["events"]

    # The person themselves still can.
    assert teen_client.delete(f"/api/profiles/{teen}/data").status_code == 200


def test_change_access_still_runs_everything_for_someone_who_doesnt_sign_in(raw_client):
    client = _signed_in_owner(raw_client)
    kid = _person(client, "Kid", "child")
    maria_client, _ = _join(client, _person(client, "Maria"))
    maria_id = _account(maria_client)
    assert client.put(f"/api/profiles/{kid}/access", json={"account_id": maria_id, "level": "manage"}).status_code == 200
    assert maria_client.get(f"/api/profiles/{kid}/access").status_code == 200
    assert maria_client.put(f"/api/profiles/{kid}/access", json={"account_id": _account(client), "level": "view"}).status_code == 200
    assert maria_client.delete(f"/api/profiles/{kid}/data").status_code == 200


# ---------------------------------------------------------------------------
# Two people with one name
# ---------------------------------------------------------------------------

def _two_sams(client):
    first = _person(client, "Sam")
    first_client, _ = _join(client, first, "first sam password")
    second = _person(client, "Sam")
    code = client.post(f"/api/profiles/{second}/invite").json()["code"]
    theirs = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    theirs.headers.update(CSRF)
    check = theirs.post("/api/invites/check", json={"code": code}).json()
    assert check["name_taken"]
    return first, second, code, theirs, first_client


def test_a_new_name_is_asked_for_when_someone_already_signs_in_with_theirs(raw_client):
    client = _signed_in_owner(raw_client)
    _, second, code, theirs, _ = _two_sams(client)
    r = theirs.post("/api/invites/accept", json={"code": code, "password": "second sam password", "accept_terms": True, "name": "sam"})
    assert r.status_code == 409 and "different name" in r.json()["detail"]
    r = theirs.post("/api/invites/accept", json={"code": code, "password": "second sam password", "accept_terms": True, "name": "Samuel"})
    assert r.status_code == 200, r.text
    assert theirs.get("/api/status").json()["account"]["name"] == "Samuel"
    assert theirs.patch(f"/api/profiles/{second}", json={"name": "SAM"}).status_code == 409
    assert theirs.patch(f"/api/profiles/{second}", json={"name": "Sammy"}).status_code == 200


def test_people_who_share_a_name_each_sign_in_with_their_own_password(raw_client):
    # The phone apps don't ask for a new name, and earlier versions didn't either.
    client = _signed_in_owner(raw_client)
    _, second, code, theirs, _ = _two_sams(client)
    assert theirs.post("/api/invites/accept", json={"code": code, "password": "second sam password", "accept_terms": True}).status_code == 200
    far = TestClient(client.app, base_url=BASE, client=("8.8.8.8", 5000))
    far.headers.update(CSRF)
    assert far.post("/api/auth/login", json={"account": "Sam", "password": "wrong"}).status_code == 401
    assert far.post("/api/auth/login", json={"account": "Sam", "password": "second sam password"}).status_code == 200
    assert far.get("/api/status").json()["account"]["profile_id"] == second
    far.cookies.clear()
    assert far.post("/api/auth/login", json={"account": "sam", "password": "first sam password"}).status_code == 200


def test_the_command_line_tells_people_with_one_name_apart(raw_client, monkeypatch, capsys):
    import pytest

    from app import cli
    client = _signed_in_owner(raw_client)
    _, _, code, theirs, _ = _two_sams(client)
    theirs.post("/api/invites/accept", json={"code": code, "password": "second sam password", "accept_terms": True})
    cli.accounts_command("list", None)
    listed = capsys.readouterr().out
    ids = [a["id"] for a in client.get("/api/accounts").json()["accounts"] if a["name"] == "Sam"]
    assert all(i in listed for i in ids)
    with pytest.raises(SystemExit) as stopped:
        cli.accounts_command("reset-password", "Sam")
    assert "the id" in str(stopped.value)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "a brand new password")
    cli.accounts_command("reset-password", ids[1])
    far = TestClient(client.app, base_url=BASE, client=("8.8.8.8", 5000))
    assert far.post("/api/auth/login", json={"account": "Sam", "password": "a brand new password"}, headers=CSRF).status_code == 200
    assert far.get("/api/status").json()["account"]["id"] == ids[1]
