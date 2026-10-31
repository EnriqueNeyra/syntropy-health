"""What an audit of every route for access across people turned up. Each of these was possible before it was fixed."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.store import accounts, profiles
from tests.conftest import BASE, CSRF, LOCAL_CLIENT, PASSPHRASE
from tests.test_accounts import MARIA_PASSWORD, _join, _person, _phone, _signed_in_owner


def _second_account(client, name="Maria"):
    pid = _person(client, name, "partner")
    other, _ = _join(client, pid)
    return other, pid


def _account_id(client) -> str:
    return client.get("/api/status").json()["account"]["id"]


# ---------------------------------------------------------------------------
# An invitation
# ---------------------------------------------------------------------------

def _invite_as_carer(client):
    """Maria looks after Kid and makes the code that would let Kid sign in."""
    maria, _ = _second_account(client)
    kid = _person(client, "Kid", "child")
    assert client.put(f"/api/profiles/{kid}/access", json={"account_id": _account_id(maria), "level": "manage"}).status_code == 200
    code = maria.post(f"/api/profiles/{kid}/invite").json()["code"]
    return maria, kid, code


def _redeem(client, code: str):
    anon = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    return anon.post("/api/invites/accept", json={"code": code, "password": "someone taking over", "accept_terms": True}, headers=CSRF)


def test_a_code_made_by_someone_who_has_since_been_removed_can_not_take_the_person_over(raw_client):
    client = _signed_in_owner(raw_client)
    maria, kid, code = _invite_as_carer(client)
    assert client.put(f"/api/profiles/{kid}/access", json={"account_id": _account_id(maria), "level": None}).status_code == 200
    assert _redeem(client, code).status_code in (400, 404, 410)
    assert accounts.for_profile(kid) is None
    assert client.get("/api/profiles").json()    # and the owner still looks after them
    assert kid in {p["id"] for p in client.get("/api/profiles").json()["profiles"]}


def test_a_code_made_by_someone_now_able_only_to_look_is_no_good_either(raw_client):
    client = _signed_in_owner(raw_client)
    maria, kid, code = _invite_as_carer(client)
    assert client.put(f"/api/profiles/{kid}/access", json={"account_id": _account_id(maria), "level": "view"}).status_code == 200
    assert _redeem(client, code).status_code in (400, 404, 410)
    assert accounts.for_profile(kid) is None


def test_a_code_made_by_an_account_since_deleted_is_no_good(raw_client):
    client = _signed_in_owner(raw_client)
    maria, kid, code = _invite_as_carer(client)
    profiles.delete_profile(accounts.get(_account_id(maria))["profile_id"])
    assert _redeem(client, code).status_code in (400, 404, 410)


def test_a_code_from_someone_who_still_looks_after_them_works(raw_client):
    client = _signed_in_owner(raw_client)
    _, kid, code = _invite_as_carer(client)
    assert _redeem(client, code).status_code == 200
    assert accounts.for_profile(kid)


# ---------------------------------------------------------------------------
# The screens of a phone paired for someone else
# ---------------------------------------------------------------------------

def _paired_for_kid(client):
    kid = _person(client, "Kid", "child")
    _, _, _, screens = _phone(client, kid)
    return kid, screens


def test_a_phone_paired_for_someone_else_can_not_read_the_conversations_of_the_account_that_paired_it(raw_client):
    client = _signed_in_owner(raw_client)
    kid, screens = _paired_for_kid(client)
    saved = {"title": "Worried about Kid", "messages": [{"role": "user", "content": "private question"}]}
    assert client.put("/api/ai/chats/c_aaaaaaaa", params={"profile": kid}, json=saved).status_code == 200
    assert screens.get("/api/ai/chats", params={"profile": kid}).json()["chats"] == []
    assert screens.get("/api/ai/chats/c_aaaaaaaa", params={"profile": kid}).status_code == 404
    assert screens.delete("/api/ai/chats/c_aaaaaaaa", params={"profile": kid}).status_code == 404
    # Asking on that phone still works, without leaving a conversation under the other account's name.
    new = {"title": "On the phone", "messages": [{"role": "user", "content": "hello"}]}
    assert screens.put("/api/ai/chats/c_bbbbbbbb", params={"profile": kid}, json=new).status_code == 200
    assert [c["id"] for c in client.get("/api/ai/chats", params={"profile": kid}).json()["chats"]] == ["c_aaaaaaaa"]


def test_a_phone_paired_for_someone_else_can_not_agree_to_an_ai_for_the_account(raw_client):
    client = _signed_in_owner(raw_client)
    _, screens = _paired_for_kid(client)
    assert screens.post("/api/ai/acknowledge", json={"vendor": "openai"}).status_code == 403
    assert screens.delete("/api/ai/acknowledge", params={"vendor": "openai"}).status_code == 403


def test_a_phone_paired_for_someone_else_can_not_pass_their_data_on(raw_client):
    client = _signed_in_owner(raw_client)
    kid, screens = _paired_for_kid(client)
    for path in ("/api/export/fhir", "/api/export/ips", "/api/export/csv", "/api/export/archive"):
        assert screens.get(path, params={"profile": kid}).status_code == 403, path
    assert client.get("/api/export/ips", params={"profile": kid}).status_code == 200


def test_a_phone_paired_for_someone_else_does_not_show_what_the_account_asked_an_ai(raw_client):
    from app.core.db import audit, db
    client = _signed_in_owner(raw_client)
    kid, screens = _paired_for_kid(client)
    with db() as conn:
        audit(conn, f"ask:{_account_id(client)}", "mcp.search_records", {"query": "the words they searched for"}, kid)
    assert "the words they searched for" not in screens.get("/api/audit").text
    assert "the words they searched for" in client.get("/api/audit").text


# ---------------------------------------------------------------------------
# A whole-server backup, from a phone
# ---------------------------------------------------------------------------

def test_a_phone_needs_the_password_to_make_a_backup_of_everyone(raw_client):
    client = _signed_in_owner(raw_client)
    _, _, _, screens = _phone(client)
    assert screens.get("/api/backups").json()["needs_password"]
    assert not client.get("/api/backups").json()["needs_password"]
    body = {"passphrase": "chosen by whoever holds it"}
    assert screens.post("/api/backups", json=body).status_code == 403
    assert screens.post("/api/backups", json={**body, "password": "not it"}).status_code == 403
    assert screens.post("/api/backups", json={**body, "password": PASSPHRASE}).status_code == 200
    assert client.post("/api/backups", json=body).status_code == 200    # a browser that signed in has already given it


# ---------------------------------------------------------------------------
# A sign-in at a portal, started before access ended
# ---------------------------------------------------------------------------

def test_a_portal_sign_in_started_before_access_ended_can_not_finish(raw_client):
    from app.api import connections as api

    client = _signed_in_owner(raw_client)
    maria, _ = _second_account(client)
    kid = _person(client, "Kid", "child")
    assert client.put(f"/api/profiles/{kid}/access", json={"account_id": _account_id(maria), "level": "manage"}).status_code == 200
    pending = {"actor": f"user:{_account_id(maria)}", "profile_id": kid}
    from starlette.requests import Request as Real

    def asked_by(who) -> Real:
        cookie = "; ".join(f"{k}={v}" for k, v in who.cookies.items())
        return Real({"type": "http", "method": "GET", "path": "/callback", "headers": [(b"cookie", cookie.encode())], "client": ("127.0.0.1", 1), "query_string": b""})
    assert api._may_finish(asked_by(maria), pending) == "go"
    assert client.put(f"/api/profiles/{kid}/access", json={"account_id": _account_id(maria), "level": None}).status_code == 200
    assert api._may_finish(asked_by(maria), pending) == "refuse"
    assert api._may_finish(asked_by(client), pending) == "go"


# ---------------------------------------------------------------------------
# The activity log
# ---------------------------------------------------------------------------

def test_someone_who_can_only_look_does_not_learn_who_else_has_access(raw_client):
    client = _signed_in_owner(raw_client)
    maria, _ = _second_account(client)
    sam, _ = _second_account(client, "Sam")
    kid = _person(client, "Kid", "child")
    for who in (maria, sam):
        assert client.put(f"/api/profiles/{kid}/access", json={"account_id": _account_id(who), "level": "view"}).status_code == 200
    seen = {e["action"] for e in maria.get("/api/audit").json()["events"] if e["profile_id"] == kid}
    assert "access.granted" not in seen
    assert any(e["action"] == "access.granted" for e in client.get("/api/audit").json()["events"])      # the owner's log has it


def test_changing_a_password_does_not_pretend_to_end_phones(raw_client):
    """A phone has a token of its own: unpairing it is what ends it, and Settings says so."""
    client = _signed_in_owner(raw_client)
    _, anon, auth, screens = _phone(client)
    assert client.post("/api/auth/password", json={"current": PASSPHRASE, "new": MARIA_PASSWORD}).status_code == 200
    assert anon.post("/api/devices/web-session", headers=auth).status_code == 200
    device = client.get("/api/devices").json()["devices"][0]["id"]
    assert client.delete(f"/api/devices/{device}").status_code == 200
    assert anon.post("/api/devices/web-session", headers=auth).status_code == 401
