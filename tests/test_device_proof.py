"""A paired phone can tell its own server from another one at the same address: the server has an identity of its own,
and proves it knows the phone's token without the phone sending it."""

from __future__ import annotations

import hashlib
import hmac
import re

from fastapi.testclient import TestClient

from app.api import biometrics
from tests.conftest import BASE, CSRF, LOCAL_CLIENT, PASSPHRASE

NONCE = "a-fresh_Nonce-0123456789"


def _pair(client, name="Phone"):
    code = client.post("/api/devices/pairing-code", json={}).json()["code"]
    phone = TestClient(client.app, base_url=BASE)
    r = phone.post("/api/devices/pair", json={"code": code, "device_name": name})
    assert r.status_code == 200, r.text
    return phone, r.json()


def _as_a_phone_would(token: str, nonce: str) -> str:
    """What the iPhone and Android apps compute from the token they were given."""
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.new(key=token_hash.encode("utf-8"), msg=nonce.encode("utf-8"), digestmod=hashlib.sha256).hexdigest()


def test_the_server_says_who_it_is_before_anyone_signs_in(client):
    stranger = TestClient(client.app, base_url=BASE)
    info = stranger.get("/api/wearables/pair").json()
    assert re.fullmatch(r"[0-9a-f]{32}", info["server_id"])
    assert info["device_proof"] == 1
    _, paired = _pair(client)
    assert paired["server_id"] == info["server_id"]
    assert stranger.get("/api/wearables/pair").json()["server_id"] == info["server_id"]


def test_the_server_keeps_its_identity_through_a_restart():
    from app.main import create_app

    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT) as first:
        assert first.post("/api/setup", json={"password": PASSPHRASE, "profile_name": "Alex"}, headers=CSRF).status_code == 200
        before = first.get("/api/wearables/pair").json()["server_id"]
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT) as again:
        assert again.get("/api/wearables/pair").json()["server_id"] == before


def test_the_proof_is_what_the_phone_works_out_from_its_token(client):
    phone, paired = _pair(client)
    r = phone.post("/api/devices/prove", json={"device_id": paired["device_id"], "nonce": NONCE})
    assert r.status_code == 200, r.text
    assert r.json() == {"server_id": paired["server_id"], "proof": _as_a_phone_would(paired["device_token"], NONCE)}
    # Another nonce, another proof: a recorded answer can't be replayed for a new question.
    other = phone.post("/api/devices/prove", json={"device_id": paired["device_id"], "nonce": "z" * 128}).json()["proof"]
    assert other == _as_a_phone_would(paired["device_token"], "z" * 128) != r.json()["proof"]


def test_proving_needs_no_token_and_ignores_a_wrong_one(client):
    phone, paired = _pair(client)
    r = phone.post("/api/devices/prove", json={"device_id": paired["device_id"], "nonce": NONCE},
                   headers={"X-Syntropy-Device-Token": "not-its-token", "Authorization": "Bearer nope"})
    assert r.status_code == 200
    assert r.json()["proof"] == _as_a_phone_would(paired["device_token"], NONCE)


def test_a_phone_the_server_doesnt_know_gets_nothing(client):
    phone, paired = _pair(client)
    assert phone.post("/api/devices/prove", json={"device_id": "dev_nobody", "nonce": NONCE}).status_code == 404
    client.delete(f"/api/devices/{paired['device_id']}")
    assert phone.post("/api/devices/prove", json={"device_id": paired["device_id"], "nonce": NONCE}).status_code == 404


def test_a_nonce_has_to_be_a_real_one(client):
    phone, paired = _pair(client)
    for nonce in ("short", "x" * 129, "has spaces in it 1234", "slash/and+plus=0123456"):
        r = phone.post("/api/devices/prove", json={"device_id": paired["device_id"], "nonce": nonce})
        assert r.status_code == 422, nonce


def test_guessing_phones_is_slowed_per_address(client, monkeypatch):
    monkeypatch.setattr(biometrics, "_prove_failures", {})
    phone, paired = _pair(client)
    guesser = TestClient(client.app, base_url=BASE, client=("192.168.1.66", 4000))
    codes = [guesser.post("/api/devices/prove", json={"device_id": f"dev_{i}", "nonce": NONCE}).status_code
             for i in range(biometrics.PROVE_MAX_FAILURES + 1)]
    assert codes[:-1] == [404] * biometrics.PROVE_MAX_FAILURES and codes[-1] == 429
    # The real phone, from its own address, still gets its answer.
    assert phone.post("/api/devices/prove", json={"device_id": paired["device_id"], "nonce": NONCE}).status_code == 200
