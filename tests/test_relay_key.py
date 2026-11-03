"""The relay key: release builds carry it and offer Syntropy's registrations; a build from the source does neither."""

from __future__ import annotations

import importlib
import sys
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.connectors import smart
from app.connectors import wearable_common as wc
from app.connectors.smart import SmartError
from app.core import config, settings
from app.services import connect
from tests.conftest import RELAY_KEY


def _state(auth_url: str) -> dict:
    return smart.decode_state(parse_qs(urlparse(auth_url).query)["state"][0])


def test_release_build_offers_syntropys_registrations_and_signs_its_sign_ins(client):
    epic = settings.platform_config("epic", "production")
    assert epic["client_id"] == settings.EHR_PLATFORMS["epic"]["default_client_ids"]["production"]
    assert epic["client_id_source"] == "default" and not epic["needs_release_build"]
    r = client.post("/api/connections/wearable", json={"provider": "oura", "mode": "live"})
    assert r.status_code == 200, r.text
    url = r.json()["auth_url"]
    assert parse_qs(urlparse(url).query)["client_id"] == [settings.WEARABLES["oura"]["default_client_id"]]
    assert _state(url)["k"] == RELAY_KEY            # the relay and the callback page check it
    assert client.get("/api/settings").json()["relay"]["release_build"] is True


def test_build_from_source_uses_its_own_registrations(client, monkeypatch):
    monkeypatch.delenv("SYNTROPY_RELAY_KEY")
    assert not config.official_build() and "k" not in smart.decode_state(smart.make_state("smart", "http://localhost:8000"))

    epic = settings.platform_config("epic", "production")
    assert epic["client_id"] == "" and epic["needs_release_build"] and not epic["available"]
    with pytest.raises(SmartError, match="built from the source code"):
        connect.plan_ehr_connection(None, "http://localhost:8000", platform="epic", mode_override="production")
    # A platform Syntropy isn't registered with says so, not that this build lacks something.
    assert not settings.platform_config("nextgen", "production")["needs_release_build"]

    r = client.post("/api/connections/wearable", json={"provider": "whoop", "mode": "live"})
    assert r.status_code == 400 and "built from the source code" in r.text
    assert client.get("/api/settings").json()["relay"]["release_build"] is False

    # Its own registrations work as before.
    monkeypatch.setenv("EPIC_CLIENT_ID", "my-epic-app")
    monkeypatch.setenv("WHOOP_CLIENT_ID", "my-whoop-app")
    assert settings.platform_config("epic", "production")["client_id"] == "my-epic-app"
    assert settings.wearable_credentials("whoop")["client_id"] == "my-whoop-app"


def test_directory_says_when_a_build_from_source_lacks_a_registration(client, monkeypatch):
    assert client.put("/api/settings/developer", json={"enabled": False}).status_code == 200
    picks = client.get("/api/directory/featured").json()
    epic = next(i for i in picks["results"] if i["platform"] == "epic")
    assert epic["available"] and not epic["needs_release_build"]
    monkeypatch.delenv("SYNTROPY_RELAY_KEY")
    inst = client.get(f"/api/directory/{epic['id']}").json()
    assert not inst["available"] and inst["needs_release_build"]


@pytest.mark.parametrize("key", [RELAY_KEY, ""])
async def test_refresh_through_the_relay_sends_the_key(monkeypatch, key):
    monkeypatch.setenv("SYNTROPY_RELAY_KEY", key)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["key"] = str(request.url), request.headers.get(config.RELAY_KEY_HEADER)
        return httpx.Response(200, json={"access_token": "AT2", "refresh_token": "RT2", "expires_in": 3600})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    creds = await wc.refresh_tokens("https://api.ouraring.com/oauth/token", "oura", {"refresh_token": "RT"})
    assert creds["access_token"] == "AT2" and seen["url"].endswith("/refresh")
    assert seen["key"] == (key or None)


async def test_relay_turning_a_refresh_away_says_what_to_do(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"error": "release_build_required"})), **kw))
    with pytest.raises(SmartError, match="Update to the latest version"):
        await wc.refresh_tokens("https://api.ouraring.com/oauth/token", "oura", {"refresh_token": "RT"})


def test_release_workflow_builds_the_key_in(monkeypatch, tmp_path, capsys):
    sys.path.insert(0, str(config.REPO_ROOT / "scripts"))
    try:
        release_key = importlib.import_module("release_key")
    finally:
        sys.path.pop(0)
    target = tmp_path / "release_key.py"
    monkeypatch.setattr(release_key, "TARGET", target)
    monkeypatch.setenv("SYNTROPY_RELAY_KEY", "k-'\"-1")
    assert release_key.main() == 0
    namespace: dict = {}
    exec(target.read_text(), namespace)
    assert namespace["KEY"] == "k-'\"-1"

    # Without the key, a fork's build goes ahead without it; Syntropy's own fails rather than ship without it.
    monkeypatch.setenv("SYNTROPY_RELAY_KEY", "")
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/syntropy-health")
    assert release_key.main() == 0
    monkeypatch.setenv("GITHUB_REPOSITORY", release_key.UPSTREAM)
    assert release_key.main() == 1 and "::error::" in capsys.readouterr().out


def test_built_in_key_is_read_from_the_release_module(monkeypatch):
    monkeypatch.delenv("SYNTROPY_RELAY_KEY")
    module = type(sys)("app.core.release_key")
    module.KEY = " built-in "
    monkeypatch.setitem(sys.modules, "app.core.release_key", module)
    assert config.relay_key() == "built-in" and config.official_build()
    monkeypatch.setitem(sys.modules, "app.core.release_key", None)      # no module: a build from the source
    assert config.relay_key() == "" and not config.official_build()
