"""How passwords are kept and checked, and what a server without one yet will tell anybody."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core import auth, security
from tests.conftest import BASE, CSRF, LOCAL_CLIENT, PASSPHRASE
from tests.test_accounts import _signed_in_owner


def _old_hash(password: str) -> str:
    """As saved by every version until now."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${2**14}$8$1${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def _stored():
    from app.core import config
    c = sqlite3.connect(str(config.db_path()))
    value = c.execute("SELECT password_hash FROM accounts LIMIT 1").fetchone()[0]
    c.close()
    return value


def test_new_passwords_use_the_settings_current_guidance_asks_for():
    source = Path(security.__file__).read_text()
    assert re.search(r"_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2\*\*17, 8, 1", source)


def test_the_real_settings_work(monkeypatch):
    monkeypatch.setattr(security, "_SCRYPT_N", 2**17)
    stored = security.hash_password("a real password")
    assert stored.startswith(f"scrypt${2**17}$8$1$")
    assert security.verify_password("a real password", stored) and not security.verify_password("another", stored)
    assert not security.password_needs_upgrade(stored)
    assert security.password_needs_upgrade(_old_hash("x"))


def test_a_computer_short_of_memory_gets_as_much_as_it_can(monkeypatch):
    real = hashlib.scrypt

    def tight(*args, **kwargs):
        if kwargs["n"] > 2**15:
            raise ValueError("memory limit exceeded")
        return real(*args, **kwargs)

    monkeypatch.setattr(security, "_SCRYPT_N", 2**17)
    monkeypatch.setattr(security.hashlib, "scrypt", tight)
    stored = security.hash_password("a real password")
    assert stored.startswith(f"scrypt${2**15}$")
    assert security.verify_password("a real password", stored)


def test_an_old_password_still_works_and_is_upgraded_at_sign_in(raw_client, monkeypatch):
    from app.core.db import db
    client = _signed_in_owner(raw_client)
    monkeypatch.setattr(security, "_SCRYPT_N", 2**15)
    with db() as conn:
        conn.execute("UPDATE accounts SET password_hash = ?", (json.dumps(security.encrypt_json(_old_hash(PASSPHRASE))),))
    before = _stored()

    far = TestClient(client.app, base_url=BASE, client=("192.168.1.9", 5000))
    far.headers.update(CSRF)
    assert far.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert _stored() == before                                   # not on a wrong one
    assert client.get("/api/profiles").status_code == 200

    assert far.post("/api/auth/login", json={"password": PASSPHRASE}).status_code == 200
    assert _stored() != before
    assert security.decrypt_json(json.loads(_stored())).startswith(f"scrypt${2**15}$")
    assert client.get("/api/profiles").status_code == 200        # nobody was signed out
    far.cookies.clear()
    assert far.post("/api/auth/login", json={"password": PASSPHRASE}).status_code == 200
    upgraded = _stored()
    far.cookies.clear()
    assert far.post("/api/auth/login", json={"password": PASSPHRASE}).status_code == 200
    assert _stored() == upgraded                                 # once


def test_a_failed_upgrade_doesnt_stop_the_sign_in(raw_client, monkeypatch):
    from app.core.db import db
    from app.store import accounts
    client = _signed_in_owner(raw_client)
    with db() as conn:
        conn.execute("UPDATE accounts SET password_hash = ?", (json.dumps(security.encrypt_json(_old_hash(PASSPHRASE))),))
    monkeypatch.setattr(accounts, "db", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    far = TestClient(client.app, base_url=BASE, client=("192.168.1.9", 5000))
    far.headers.update(CSRF)
    monkeypatch.setattr(accounts, "touch_login", lambda account_id: None)
    assert far.post("/api/auth/login", json={"password": PASSPHRASE}).status_code == 200


@pytest.mark.parametrize("bad", ["password", "PASSWORD123", "12345678", "aaaaaaaaaaaa", "qwertyuiop"])
def test_passwords_anyone_would_guess_are_refused_when_chosen(raw_client, bad):
    r = raw_client.post("/api/setup", json={"password": bad, "profile_name": "Alex"}, headers=CSRF)
    assert r.status_code == 400 and "guess" in r.json()["detail"]


def test_an_existing_common_password_still_signs_in(raw_client):
    from app.core.db import db
    client = _signed_in_owner(raw_client)
    with db() as conn:
        conn.execute("UPDATE accounts SET password_hash = ?", (json.dumps(security.encrypt_json(security.hash_password("password"))),))
    far = TestClient(client.app, base_url=BASE, client=("192.168.1.9", 5000))
    assert far.post("/api/auth/login", json={"password": "password"}, headers=CSRF).status_code == 200


def test_an_unknown_name_takes_as_long_as_a_known_one(raw_client, monkeypatch):
    client = _signed_in_owner(raw_client)
    checks = []
    real = security.verify_password
    monkeypatch.setattr(security, "verify_password", lambda p, s: (checks.append(s), real(p, s))[1])
    assert client.post("/api/auth/login", json={"account": "Nobody", "password": "wrong"}).status_code == 401
    assert len(checks) == 1


def test_guessing_the_current_password_counts(raw_client):
    client = _signed_in_owner(raw_client)
    for _ in range(auth.LOGIN_MAX_FAILURES):
        r = client.post("/api/auth/password", json={"current": "wrong", "new": "a long new password"})
        assert r.status_code == 403
    r = client.post("/api/auth/password", json={"current": PASSPHRASE, "new": "a long new password"})
    assert r.status_code == 429
    assert client.post("/api/auth/password", json={"current": "x" * 257, "new": "a long new password"}).status_code == 422


def test_changing_a_password_clears_its_own_tries(raw_client):
    client = _signed_in_owner(raw_client)
    for _ in range(auth.LOGIN_MAX_FAILURES - 1):
        assert client.post("/api/auth/password", json={"current": "wrong", "new": "a long new password"}).status_code == 403
    assert client.post("/api/auth/password", json={"current": PASSPHRASE, "new": "a long new password"}).status_code == 200
    assert client.post("/api/auth/password", json={"current": "wrong", "new": "another long password"}).status_code == 403


# ---------------------------------------------------------------------------
# Before a password exists
# ---------------------------------------------------------------------------

def _no_password_yet(raw_client, monkeypatch, name="Grace Hopper", born="1906-12-09"):
    r = raw_client.post("/api/setup", json={"profile_name": name, "birth_date": born}, headers=CSRF)
    assert r.status_code == 200, r.text
    monkeypatch.delenv("SYNTROPY_ALLOW_NO_PASSWORD")
    assert auth.password_missing()


def test_a_server_without_a_password_yet_tells_nobody_who_is_on_it(raw_client, monkeypatch):
    _no_password_yet(raw_client, monkeypatch)
    for host in ("192.168.1.99", "127.0.0.1", "8.8.8.8"):
        c = TestClient(raw_client.app, base_url=BASE, client=(host, 5000))
        r = c.get("/api/status")
        assert r.status_code == 200
        assert "Grace" not in r.text and "1906" not in r.text
        status = r.json()
        assert status["password_missing"] and "profiles" not in status and "account" not in status
        assert "terms_accepted" in status       # the screen asking for the password needs this


def test_only_someone_nearby_or_holding_the_code_can_choose_the_first_password(raw_client, monkeypatch):
    _no_password_yet(raw_client, monkeypatch)
    far = TestClient(raw_client.app, base_url=BASE, client=("8.8.8.8", 5000))
    far.headers.update(CSRF)
    assert far.get("/api/status").json()["setup_code_needed"]
    r = far.post("/api/auth/password", json={"new": "a long new password"})
    assert r.status_code == 403 and "setup code" in r.json()["detail"]
    r = far.post("/api/auth/password", json={"new": "a long new password", "setup_code": "ZZZZ-ZZZZ"})
    assert r.status_code == 403
    assert auth.password_missing()

    code = auth.setup_code()
    r = far.post("/api/auth/password", json={"new": "a long new password", "setup_code": f"{code[:4]}-{code[4:]}", "accept_terms": True})
    assert r.status_code == 200, r.text
    assert not auth.password_missing()
    assert far.get("/api/profiles").status_code == 200
    assert not auth.setup_code_matches(code)      # once


def test_someone_nearby_needs_no_code(raw_client, monkeypatch):
    _no_password_yet(raw_client, monkeypatch)
    near = TestClient(raw_client.app, base_url=BASE, client=("192.168.1.99", 5000))
    near.headers.update(CSRF)
    assert "setup_code_needed" not in near.get("/api/status").json()
    assert near.post("/api/auth/password", json={"new": "a long new password", "accept_terms": True}).status_code == 200


def test_setting_up_from_far_away_with_the_code(raw_client):
    far = TestClient(raw_client.app, base_url=BASE, client=("8.8.8.8", 5000))
    assert far.get("/api/status").json()["setup_code_needed"]
    body = {"password": PASSPHRASE, "profile_name": "Alex", "accept_terms": True}
    assert far.post("/api/setup", json=body, headers=CSRF).status_code == 403
    assert far.post("/api/setup", json={**body, "setup_code": auth.setup_code()}, headers=CSRF).status_code == 200
    assert far.get("/api/status").json()["authenticated"]


def test_a_proxy_nobody_trusted_doesnt_make_everyone_nearby(raw_client):
    proxy = TestClient(raw_client.app, base_url=BASE, client=("172.18.0.5", 5000))
    headers = {**CSRF, "X-Forwarded-For": "8.8.8.8"}
    r = proxy.post("/api/setup", json={"password": PASSPHRASE, "profile_name": "Alex"}, headers=headers)
    assert r.status_code == 403
    # One this server was told to trust has already put the visitor's address in place: it's that address that counts.
    trusted = TestClient(raw_client.app, base_url=BASE, client=("192.168.1.9", 5000))
    r = trusted.post("/api/setup", json={"password": PASSPHRASE, "profile_name": "Alex"}, headers={**CSRF, "X-Forwarded-For": "192.168.1.9"})
    assert r.status_code == 200
    anon = TestClient(raw_client.app, base_url=BASE, client=("172.18.0.5", 5000))
    assert "accounts" not in anon.get("/api/status", headers={"X-Forwarded-For": "8.8.8.8"}).json()


def test_names_are_only_listed_to_a_name_of_this_server(raw_client, monkeypatch):
    _signed_in_owner(raw_client)
    near = TestClient(raw_client.app, base_url=BASE, client=LOCAL_CLIENT)
    assert [a["name"] for a in near.get("/api/status").json()["accounts"]] == ["Alex"]
    rebinding = near.get("/api/status", headers={"Host": "evil.example.com"})
    assert rebinding.status_code == 200 and "accounts" not in rebinding.json()
    monkeypatch.setenv("SYNTROPY_ALLOWED_HOSTS", "health.example.com")
    assert "accounts" in near.get("/api/status", headers={"Host": "health.example.com"}).json()


def test_a_refused_password_doesnt_use_up_the_setup_code(raw_client):
    far = TestClient(raw_client.app, base_url=BASE, client=("8.8.8.8", 5000))
    code = auth.setup_code()
    body = {"profile_name": "Alex", "setup_code": code, "accept_terms": True}
    assert far.post("/api/setup", json={**body, "password": "password123"}, headers=CSRF).status_code == 400
    assert far.post("/api/setup", json={**body, "password": "short"}, headers=CSRF).status_code == 400
    r = far.post("/api/setup", json={**body, "password": PASSPHRASE}, headers=CSRF)
    assert r.status_code == 200, r.text
    assert not auth.setup_code_matches(code)      # once


def test_a_refused_first_password_doesnt_use_up_the_code_either(raw_client, monkeypatch):
    _no_password_yet(raw_client, monkeypatch)
    far = TestClient(raw_client.app, base_url=BASE, client=("8.8.8.8", 5000))
    far.headers.update(CSRF)
    code = auth.setup_code()
    for weak in ("password123", "short", None):
        assert far.post("/api/auth/password", json={"new": weak, "setup_code": code}).status_code == 400
    assert auth.password_missing()
    r = far.post("/api/auth/password", json={"new": "a long new password", "setup_code": code, "accept_terms": True})
    assert r.status_code == 200, r.text
    assert not auth.setup_code_matches(code)      # once


def test_a_wrong_setup_code_still_counts_as_a_guess(raw_client):
    far = TestClient(raw_client.app, base_url=BASE, client=("8.8.8.8", 5000))
    auth.setup_code()
    body = {"password": PASSPHRASE, "profile_name": "Alex", "setup_code": "ZZZZ-ZZZZ"}
    codes = [far.post("/api/setup", json=body, headers=CSRF).status_code for _ in range(auth.LOGIN_MAX_FAILURES + 1)]
    assert codes[:-1] == [403] * auth.LOGIN_MAX_FAILURES and codes[-1] == 429


@pytest.mark.parametrize("forwarded", [
    "172.18.0.2, 8.8.8.8",              # nginx adds the visitor to what the visitor sent
    "8.8.8.8, 172.18.0.2, 8.8.8.8",
    [("X-Forwarded-For", "172.18.0.2"), ("X-Forwarded-For", "8.8.8.8")],     # HAProxy adds a line of its own
])
def test_saying_youre_the_proxy_doesnt_make_you_nearby(raw_client, forwarded):
    proxy = TestClient(raw_client.app, base_url=BASE, client=("172.18.0.2", 5000))
    headers = [*forwarded, *CSRF.items()] if isinstance(forwarded, list) else {**CSRF, "X-Forwarded-For": forwarded}
    assert proxy.get("/api/status", headers=headers).json()["setup_code_needed"]
    r = proxy.post("/api/setup", json={"password": PASSPHRASE, "profile_name": "Alex"}, headers=headers)
    assert r.status_code == 403, r.text
    assert not auth.is_setup_complete()


@pytest.mark.parametrize("allowed, forwarded, visitor, near", [
    ("127.0.0.1", "192.168.1.9", "192.168.1.9", True),
    ("127.0.0.1", "8.8.8.8", "8.8.8.8", False),
    ("127.0.0.1", "192.168.1.9, 8.8.8.8", "8.8.8.8", False),        # the proxy added 8.8.8.8; the rest is what they sent
    ("*", "192.168.1.9", "192.168.1.9", True),
    ("*", "192.168.1.9, 8.8.8.8", "192.168.1.9", False),            # trusting anyone, uvicorn believes the first address
])
def test_behind_a_trusted_proxy_it_is_the_visitors_address_that_counts(raw_client, allowed, forwarded, visitor, near):
    seen = {}

    async def spy(scope, receive, send):
        seen["client"] = (scope.get("client") or ("",))[0]
        await raw_client.app(scope, receive, send)
    proxied = TestClient(ProxyHeadersMiddleware(spy, trusted_hosts=allowed), base_url=BASE, client=("127.0.0.1", 5000))
    status = proxied.get("/api/status", headers={"X-Forwarded-For": forwarded}).json()
    assert seen["client"] == visitor
    assert ("setup_code_needed" not in status) == near
