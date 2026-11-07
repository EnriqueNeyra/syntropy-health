"""Behind uvicorn's proxy headers, the address a request gives can be anyone's to write (FORWARDED_ALLOW_IPS="*"). The
guessing limits, "this computer" and "nearby" go by what can be believed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api import biometrics
from app.core import auth
from app.services import network
from tests.conftest import BASE, CSRF, LOCAL_CLIENT, PASSPHRASE

LAN = ("192.168.1.50", 40000)


def _behind_any_proxy(app, monkeypatch, peer=LAN):
    """uvicorn started with FORWARDED_ALLOW_IPS="*", as docker-compose.yml used to suggest, reached directly."""
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    return TestClient(ProxyHeadersMiddleware(app, trusted_hosts="*"), base_url=BASE, client=peer)


def _spoofed(i: int) -> dict:
    return {**CSRF, "X-Forwarded-For": f"10.9.{i // 250}.{i % 250}"}


def test_a_new_address_for_every_guess_gets_no_more_guesses(client, monkeypatch):
    lan = _behind_any_proxy(client.app, monkeypatch)
    codes = [lan.post("/api/auth/login", json={"account": "Alex", "password": f"guess-{i}"}, headers=_spoofed(i)).status_code
             for i in range(auth.LOGIN_MAX_FAILURES + 1)]
    assert codes == [401] * auth.LOGIN_MAX_FAILURES + [429]


def test_pairing_codes_and_phones_are_guessed_no_faster_that_way(client, monkeypatch):
    monkeypatch.setattr(biometrics, "_pair_failures", {})
    monkeypatch.setattr(biometrics, "_prove_failures", {})
    lan = _behind_any_proxy(client.app, monkeypatch)
    codes = [lan.post("/api/devices/pair", json={"code": "ABCD-EFGH"}, headers=_spoofed(i)).status_code
             for i in range(biometrics.PAIR_MAX_FAILURES + 1)]
    assert codes[-1] == 429
    codes = [lan.post("/api/devices/prove", json={"device_id": f"dev_{i}", "nonce": "n" * 16}, headers=_spoofed(i)).status_code
             for i in range(biometrics.PROVE_MAX_FAILURES + 1)]
    assert codes[-1] == 429


def test_one_password_guessed_from_everywhere_stops_however_proxies_are_set(client):
    """Addresses can be had by the thousand. Whatever FORWARDED_ALLOW_IPS says, one account's password tried from all of
    them together stops too, after more tries than one address gets."""
    for i in range(auth.LOGIN_MAX_FAILURES_ANYWHERE):
        c = TestClient(client.app, base_url=BASE, client=(f"10.1.{i // 250}.{i % 250 + 1}", 5000))
        assert c.post("/api/auth/login", json={"account": "Alex", "password": "wrong"}, headers=CSRF).status_code == 401
    elsewhere = TestClient(client.app, base_url=BASE, client=("10.2.0.1", 5000))
    assert elsewhere.post("/api/auth/login", json={"account": "Alex", "password": PASSPHRASE}, headers=CSRF).status_code == 429
    # The computer the server runs on, connecting directly, can still sign in: the household isn't locked out of it.
    here = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    assert here.post("/api/auth/login", json={"account": "Alex", "password": PASSPHRASE}, headers=CSRF).status_code == 200
    # Through a proxy on this computer, it's someone the proxy heard from.
    proxied = here.post("/api/auth/login", json={"account": "Alex", "password": PASSPHRASE},
                        headers={**CSRF, "X-Forwarded-For": "203.0.113.9"})
    assert proxied.status_code == 429


def test_the_cap_from_everywhere_holds_with_every_proxy_trusted_too(client, monkeypatch):
    """uvicorn told "*" on its command line rather than in FORWARDED_ALLOW_IPS: the app can't see that, and the cap still holds."""
    lan = TestClient(ProxyHeadersMiddleware(client.app, trusted_hosts="*"), base_url=BASE, client=LAN)
    codes = [lan.post("/api/auth/login", json={"account": "Alex", "password": "wrong"}, headers=_spoofed(i)).status_code
             for i in range(auth.LOGIN_MAX_FAILURES_ANYWHERE + 1)]
    assert codes[-1] == 429 and codes.count(401) == auth.LOGIN_MAX_FAILURES_ANYWHERE


def _request(client=("127.0.0.1", 5000), headers=()) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": list(headers), "client": client})


@pytest.mark.parametrize("header", [b"x-forwarded-for", b"forwarded", b"x-real-ip", b"x-forwarded-proto", b"x-forwarded-host"])
def test_a_request_a_proxy_passed_on_isnt_from_this_computer(header):
    assert network.from_this_computer(_request())
    assert not network.from_this_computer(_request(headers=[(header, b"127.0.0.1")]))


def test_saying_youre_this_computer_through_a_proxy_trusting_everyone_doesnt_work(client, monkeypatch):
    lan = _behind_any_proxy(client.app, monkeypatch)
    # Signed out, from the home network, the sign-in screen lists who signs in here; claiming to be this computer, or
    # anywhere else, through a proxy trusting everyone, it doesn't.
    plain = TestClient(client.app, base_url=BASE, client=LAN).get("/api/status")
    assert plain.json().get("accounts")
    spoofed = lan.get("/api/status", headers={"X-Forwarded-For": "127.0.0.1", "Host": "localhost"}).json()
    assert "accounts" not in spoofed


@pytest.mark.parametrize("trusted, near", [("*", False), ("127.0.0.1", True)])
def test_setting_up_from_far_away_through_a_proxy_trusting_everyone_takes_the_setup_code(raw_client, monkeypatch, trusted, near):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", trusted)
    proxied = TestClient(ProxyHeadersMiddleware(raw_client.app, trusted_hosts=trusted), base_url=BASE, client=("127.0.0.1", 5000))
    status = proxied.get("/api/status", headers={"X-Forwarded-For": "192.168.1.9"}).json()
    assert ("setup_code_needed" not in status) == near
    r = proxied.post("/api/setup", json={"password": PASSPHRASE, "profile_name": "Alex"},
                     headers={**CSRF, "X-Forwarded-For": "192.168.1.9"})
    assert (r.status_code == 200) == near and (r.status_code == 403) != near


def test_the_desktop_apps_first_run_routes_answer_this_computer_alone(raw_client):
    r = raw_client.get("/api/desktop/discover", headers={**CSRF, "X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 404
