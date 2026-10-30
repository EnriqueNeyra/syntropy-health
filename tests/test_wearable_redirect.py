"""Each wearable's redirect stands alone: Google Health's setup leaves Oura and WHOOP on the relay, and refresh tokens only
ever go to the provider or to the relay."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.connectors import google_health, oura, smart
from app.connectors import wearable_common as wc
from app.connectors.smart import SmartError
from app.core import config, data_fixes, db, settings
from app.services import connect, sync
from app.store import connections, profiles
from tests.conftest import BASE

RELAY = config.DEFAULT_WEARABLE_RELAY_URL
RELAY_ORIGIN = "https://syntropy-auth-relay.syntropylabs.workers.dev"
SITE_CALLBACK = config.DEFAULT_RELAY_URL
ORIGIN = "http://192.168.1.20:8000"


def _start(client, provider: str) -> dict:
    r = client.post("/api/connections/wearable", json={"provider": provider, "mode": "live"})
    assert r.status_code == 200, r.text
    return {k: v[0] for k, v in parse_qs(urlparse(r.json()["auth_url"]).query).items()}


def _capture(monkeypatch, response=None) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response or httpx.Response(200, json={"access_token": "AT2", "refresh_token": "RT2", "expires_in": 3600})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return seen


def _own_app(provider: str, redirect: str | None = None) -> None:
    settings.set_wearable_credentials(provider, f"my-{provider}-app", f"my-{provider}-secret")
    if redirect:
        settings.set(f"wearable.{provider}.redirect_uri", redirect)


def test_setting_up_google_leaves_oura_and_whoop_on_the_relay(client):
    r = client.put("/api/settings/wearables/google", json={"client_id": "g-id", "client_secret": "g-secret", "redirect_uri": f"{BASE}/callback"})
    assert r.status_code == 200, r.text
    assert r.json()["redirect_uri"] == f"{BASE}/callback"
    google = _start(client, "google")
    assert (google["client_id"], google["redirect_uri"]) == ("g-id", f"{BASE}/callback")
    for provider in ("oura", "whoop"):
        q = _start(client, provider)
        assert q["client_id"] == settings.WEARABLES[provider]["default_client_id"]
        assert q["redirect_uri"] == RELAY


def test_a_saved_redirect_is_ignored_for_syntropys_own_apps(client):
    # The client ID Oura holds only the relay for: anything else would fail at Oura after the person signs in.
    settings.set("wearable.oura.redirect_uri", f"{BASE}/callback")
    assert _start(client, "oura")["redirect_uri"] == RELAY


def test_ones_own_app_defaults_to_this_servers_callback(client):
    _own_app("google")
    assert _start(client, "google")["redirect_uri"] == f"{BASE}/callback"
    _own_app("oura")
    assert _start(client, "oura")["redirect_uri"] == f"{BASE}/callback"
    assert _start(client, "whoop")["redirect_uri"] == RELAY


def test_the_relay_address_is_no_redirect_for_ones_own_app(client):
    # The relay would exchange the code with Syntropy's secret and fail.
    _own_app("oura", RELAY)
    assert _start(client, "oura")["redirect_uri"] == f"{BASE}/callback"


def test_the_code_is_exchanged_with_the_redirect_the_sign_in_used(client, monkeypatch):
    _own_app("google", SITE_CALLBACK)
    started = client.post("/api/connections/wearable", json={"provider": "google", "mode": "live"}).json()
    state = parse_qs(urlparse(started["auth_url"]).query)["state"][0]
    assert parse_qs(urlparse(started["auth_url"]).query)["redirect_uri"] == [SITE_CALLBACK]
    settings.set("wearable.google.redirect_uri", f"{BASE}/changed-since")    # after the sign-in began

    seen = _capture(monkeypatch)
    _, pending = connections.pop_pending(state)
    asyncio.run(connect.complete_wearable(pending, code="abc"))
    assert parse_qs(seen[0].content.decode())["redirect_uri"] == [SITE_CALLBACK]


def test_a_sign_in_begun_before_the_update_still_finishes(client, monkeypatch):
    _own_app("oura")
    seen = _capture(monkeypatch)
    me = profiles.list_profiles()[0]["id"]
    asyncio.run(connect.complete_wearable({"provider": "oura", "profile_id": me, "origin": ORIGIN, "local_exchange": True}, code="abc"))
    assert parse_qs(seen[0].content.decode())["redirect_uri"] == [f"{ORIGIN}/callback"]


@pytest.mark.parametrize("saved", [f"{BASE}/callback", "http://localhost:8110/callback", SITE_CALLBACK, "https://evil.example.com/callback"])
def test_a_refresh_through_the_relay_never_follows_a_redirect_setting(monkeypatch, saved):
    settings.set("wearable.google.redirect_uri", saved)
    settings.set("wearable.oura.redirect_uri", saved)
    settings.set("relay.wearable_redirect_uri", saved)
    seen = _capture(monkeypatch)
    creds = asyncio.run(wc.refresh_tokens(oura.TOKEN_URL, "oura", {"refresh_token": "RT"}))
    assert creds["access_token"] == "AT2"
    assert str(seen[0].url) == f"{RELAY_ORIGIN}/refresh"


def test_ones_own_app_refreshes_directly_with_the_provider(monkeypatch):
    _own_app("oura", f"{BASE}/callback")
    seen = _capture(monkeypatch)
    asyncio.run(wc.refresh_tokens(oura.TOKEN_URL, "oura", {"refresh_token": "RT"}))
    assert str(seen[0].url) == oura.TOKEN_URL
    assert "my-oura-secret" in seen[0].content.decode()


def test_google_without_a_secret_sends_its_refresh_token_nowhere(monkeypatch):
    settings.set_wearable_credentials("google", "g-id", None)
    settings.set("wearable.google.redirect_uri", SITE_CALLBACK)
    seen = _capture(monkeypatch)
    with pytest.raises(SmartError, match="client secret") as err:
        asyncio.run(google_health.refresh({"refresh_token": "RT"}))
    assert err.value.kind == "auth" and seen == []


def test_own_client_id_with_its_own_relay_keeps_working(monkeypatch):
    # A person who runs their own copy of the worker: their client ID here, their secret there.
    settings.set_wearable_credentials("oura", "my-oura-app", None)
    settings.set("wearable.oura.redirect_uri", "https://my-relay.example.workers.dev/callback")
    assert wc.redirect_uri("oura", ORIGIN) == "https://my-relay.example.workers.dev/callback"
    seen = _capture(monkeypatch)
    asyncio.run(wc.refresh_tokens(oura.TOKEN_URL, "oura", {"refresh_token": "RT"}))
    assert str(seen[0].url) == "https://my-relay.example.workers.dev/refresh"


@pytest.mark.parametrize("saved", [SITE_CALLBACK, RELAY, None])
def test_own_client_id_never_sends_its_token_to_syntropys_hosts(monkeypatch, saved):
    settings.set_wearable_credentials("oura", "my-oura-app", None)
    settings.set("wearable.oura.redirect_uri", saved)
    seen = _capture(monkeypatch)
    with pytest.raises(SmartError, match="client secret"):
        asyncio.run(wc.refresh_tokens(oura.TOKEN_URL, "oura", {"refresh_token": "RT"}))
    assert seen == []


@pytest.mark.parametrize("bad", ["ftp://x/callback", "callback", "javascript:alert(1)"])
def test_a_redirect_must_be_a_web_address(client, bad):
    assert client.put("/api/settings/wearables/oura", json={"redirect_uri": bad}).status_code == 400
    assert settings.get("wearable.oura.redirect_uri") is None


def test_clearing_a_redirect_goes_back_to_the_default(client):
    assert client.put("/api/settings/wearables/oura", json={"redirect_uri": "https://x.example/callback"}).json()["redirect_uri"]
    assert client.put("/api/settings/wearables/oura", json={"client_id": ""}).json()["redirect_uri"]    # untouched when absent
    assert client.put("/api/settings/wearables/oura", json={"redirect_uri": ""}).json()["redirect_uri"] == ""


def test_the_old_single_setting_is_gone_from_the_api(client):
    relay = client.get("/api/settings").json()["relay"]
    assert "wearable_redirect_uri" not in relay
    assert "redirect_uri" in relay and relay["release_build"] is True
    # A settings page left open from before the update doesn't get an error.
    r = client.put("/api/settings/general", json={"redirect_uri": "", "wearable_redirect_uri": f"{BASE}/callback"})
    assert r.status_code == 200
    assert settings._raw_get("relay.wearable_redirect_uri") is None


def _legacy(value) -> None:
    settings.set("relay.wearable_redirect_uri", value)


def _run_fix() -> None:
    with db.db() as conn:
        assert wc._move_wearable_redirect(conn, None, 100) is None


def test_the_old_setting_moves_only_to_providers_with_their_own_app():
    _legacy(f"{BASE}/callback")
    _own_app("google")
    settings.set_wearable_credentials("whoop", "my-whoop-app", None)
    _run_fix()
    assert settings.get("wearable.google.redirect_uri") == f"{BASE}/callback"
    assert settings.get("wearable.whoop.redirect_uri") == f"{BASE}/callback"
    assert settings.get("wearable.oura.redirect_uri") is None
    assert settings._raw_get("relay.wearable_redirect_uri") is None
    assert wc.redirect_uri("oura", ORIGIN) == RELAY
    assert wc.redirect_uri("google", ORIGIN) == f"{BASE}/callback"


def test_the_old_setting_counts_a_secret_from_the_environment(monkeypatch):
    _legacy(SITE_CALLBACK)
    monkeypatch.setenv("OURA_CLIENT_SECRET", "env-secret")
    _run_fix()
    assert settings.get("wearable.oura.redirect_uri") == SITE_CALLBACK


def test_the_old_default_moves_nowhere_and_nothing_is_overwritten():
    _legacy(RELAY)
    _own_app("oura", "https://mine.example/callback")
    _run_fix()
    assert settings.get("wearable.oura.redirect_uri") == "https://mine.example/callback"
    assert settings._raw_get("relay.wearable_redirect_uri") is None

    _legacy("https://other.example/callback")
    _run_fix()
    assert settings.get("wearable.oura.redirect_uri") == "https://mine.example/callback"
    # And with nothing saved at all, it's a quick no-op.
    _run_fix()


def test_moving_the_old_setting_is_a_registered_fix_that_runs_once():
    assert "wearable-redirect-per-provider" in data_fixes._fixes
    _legacy(f"{BASE}/callback")
    _own_app("google")
    data_fixes._stop.clear()    # a server stopping in an earlier test leaves this set
    data_fixes.run_pending()
    assert next(f for f in data_fixes.status() if f["name"] == "wearable-redirect-per-provider")["state"] == "done"
    assert settings.get("wearable.google.redirect_uri") == f"{BASE}/callback"


def test_tokens_with_an_empty_lifetime_still_connect(client, monkeypatch):
    async def no_sync(*_args, **_kw):
        return {}

    monkeypatch.setattr(sync, "sync_connection", no_sync)
    # What the relay sent when a provider left expires_in out, until it stopped sending empty fields.
    started = client.post("/api/connections/wearable", json={"provider": "oura", "mode": "live"}).json()
    state = parse_qs(urlparse(started["auth_url"]).query)["state"][0]
    r = client.post("/api/connections/oauth/relay", headers={"X-Requested-With": "syntropy"},
                    json={"state": state, "relay_provider": "oura", "access_token": "AT", "refresh_token": "", "expires_in": "", "scope": ""})
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True


def test_state_still_routes_for_the_relay():
    state = smart.decode_state(smart.make_state("oura", ORIGIN))
    assert state["d"] == ORIGIN and state["p"] == "oura"
