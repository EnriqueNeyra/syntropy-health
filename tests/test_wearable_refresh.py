"""Renewing a wearable's access, locally and through the relay: which failures pass and which mean signing in again."""

import asyncio

import httpx
import pytest

from app.connectors import oura, wearable_common as wc
from app.connectors.smart import SmartError
from app.core import settings
from app.services import sync
from app.store import connections
from tests.conftest import RELAY_KEY


class Provider:
    def __init__(self, status=200, body=None, text=None, headers=None, raises=None):
        self.status, self.body, self.text, self.headers, self.raises = status, body, text, headers, raises
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if self.raises:
            raise self.raises
        if self.text is not None:
            return httpx.Response(self.status, text=self.text, headers=self.headers)
        return httpx.Response(self.status, json=self.body if self.body is not None else {}, headers=self.headers)


@pytest.fixture
def respond(monkeypatch):
    real = httpx.AsyncClient

    def install(provider):
        monkeypatch.setattr(wc.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(provider), **kw))
        return provider
    return install


def _refresh(provider="oura"):
    return asyncio.run(wc.refresh_tokens("https://api.ouraring.com/oauth/token", provider, {"access_token": "a", "refresh_token": "r"}))


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_a_relay_or_provider_outage_is_temporary(respond, status):
    respond(Provider(status, {"error": "busy"}, headers={"Retry-After": "90"}))
    with pytest.raises(SmartError) as caught:
        _refresh()
    assert caught.value.kind == "network" and caught.value.retry_after == 90
    assert "Reconnect" not in str(caught.value)


def test_an_html_error_page_from_the_relay_is_temporary(respond):
    respond(Provider(500, text="<html>Something went wrong</html>"))
    with pytest.raises(SmartError) as caught:
        _refresh()
    assert caught.value.kind == "network" and "<html" not in str(caught.value)


def test_network_failure_is_temporary(respond):
    respond(Provider(raises=httpx.ConnectError("offline")))
    with pytest.raises(SmartError) as caught:
        _refresh()
    assert caught.value.kind == "network"


@pytest.mark.parametrize("description", ["HTTP 503", "HTTP 429", "HTTP 500", "temporarily_unavailable"])
def test_relays_already_deployed_are_read_correctly(respond, description):
    # They answer 400 refresh_failed for everything, but say why.
    respond(Provider(400, {"error": "refresh_failed", "error_description": description}))
    with pytest.raises(SmartError) as caught:
        _refresh()
    assert caught.value.kind == "network"


@pytest.mark.parametrize("body", [{"error": "refresh_failed", "error_description": "invalid_grant"},
                                  {"error": "refresh_failed", "error_description": "The refresh token is invalid"},
                                  {"error": "refresh_failed", "error_description": "HTTP 400"},
                                  {"error": "invalid_grant"}])
def test_a_real_refusal_still_asks_to_reconnect(respond, body):
    respond(Provider(400, body))
    with pytest.raises(SmartError) as caught:
        _refresh()
    assert caught.value.kind == "auth"


def test_the_relays_release_check_keeps_its_own_message(respond):
    respond(Provider(403, {"error": "release_build_required"}))
    with pytest.raises(SmartError) as caught:
        _refresh()
    assert caught.value.kind == "auth" and "Update to the latest" in str(caught.value)


@pytest.mark.parametrize("provider", [Provider(200, {"token_type": "bearer"}), Provider(200, text="<html>")])
def test_an_answer_without_a_token_is_temporary_and_readable(respond, provider):
    respond(provider)
    with pytest.raises(SmartError) as caught:
        _refresh()
    assert caught.value.kind == "network" and "access token" in str(caught.value)


def test_a_new_token_is_returned_and_the_relay_gets_the_key(respond):
    provider = respond(Provider(200, {"access_token": "A2", "refresh_token": "R2", "expires_in": 60}))
    creds = _refresh()
    assert creds["access_token"] == "A2" and creds["refresh_token"] == "R2"
    assert provider.requests[0].headers["x-syntropy-key"] == RELAY_KEY


def test_with_the_persons_own_secret_it_goes_straight_to_the_provider(client, respond):
    settings.set("wearable.oura.client_id", "mine")
    settings.set("wearable.oura.client_secret", "secret")
    if not settings.wearable_credentials("oura")["client_secret"]:
        pytest.skip("this build keeps wearable secrets elsewhere")
    provider = respond(Provider(503, {}))
    with pytest.raises(SmartError) as caught:
        _refresh()
    assert caught.value.kind == "network" and "ouraring" in str(provider.requests[0].url)
    respond(Provider(400, {"error": "invalid_grant"}))
    with pytest.raises(SmartError) as caught:
        _refresh()
    assert caught.value.kind == "auth"


def _live_oura(client):
    cid = client.post("/api/connections/wearable", json={"provider": "oura", "mode": "simulated"}).json()["connection_id"]
    connections.update(cid, mode="live", credentials={"access_token": "t", "refresh_token": "r", "expires_at": 1})
    from app.core.db import db
    with db() as c:
        c.execute("UPDATE connections SET mode = 'live' WHERE id = ?", (cid,))
    return cid


def test_a_blip_leaves_the_wearable_on_the_schedule_with_a_short_retry(client, respond):
    cid = _live_oura(client)
    respond(Provider(503, {}))
    res = client.post(f"/api/connections/{cid}/sync").json()
    conn = connections.get(cid)
    assert res["kind"] == "network" and conn["status"] == "active" and conn["next_retry_at"]
    from app.core.db import db
    with db() as c:
        c.execute("UPDATE connections SET next_retry_at = 1 WHERE id = ?", (cid,))
    assert cid in {c["id"] for c in sync.due_connections(24, 24)}


def test_a_revoked_wearable_still_asks_to_reconnect(client, respond):
    cid = _live_oura(client)
    respond(Provider(400, {"error": "refresh_failed", "error_description": "invalid_grant"}))
    client.post(f"/api/connections/{cid}/sync")
    assert connections.get(cid)["status"] == "needs_reauth"
    assert cid not in {c["id"] for c in sync.due_connections(0, 0)}


def test_oura_paging_and_failures_reach_the_person_as_words(client, monkeypatch):
    cid = _live_oura(client)
    connections.update(cid, credentials={"access_token": "t", "refresh_token": "r"})
    calls = []

    def handler(request):
        calls.append(request.url)
        if "next_token" in str(request.url):
            return httpx.Response(429, headers={"Retry-After": "600"}, json={})
        return httpx.Response(200, json={"data": [{"day": "2026-01-01"}], "next_token": "x"})
    real = httpx.AsyncClient
    monkeypatch.setattr(oura.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    res = client.post(f"/api/connections/{cid}/sync").json()
    conn = connections.get(cid)
    assert res["kind"] == "network" and "try again" in conn["last_error"] and conn["next_retry_at"]
    assert any("next_token" in str(u) for u in calls)
