"""Every route the server has, walked as someone who shouldn't get in, so a new route is covered the day it's added. And
what would break quietly: codes that stop expiring, routes nothing else ever calls."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount

from app.core import auth
from app.store import accounts, devices
from tests.conftest import BASE, CSRF, LOCAL_CLIENT, PASSPHRASE
from tests.test_accounts import _join, _person, _phone, _signed_in_owner

# What anyone may reach without signing in. A new one belongs here only after asking what it tells a stranger.
PUBLIC = {"/unlock", "/", "/health", "/privacy", "/callback", "/api/status", "/api/setup", "/api/auth/login", "/api/auth/logout", "/api/auth/recovery",
          "/api/invites/check", "/api/invites/accept", "/api/devices/pair", "/api/devices/prove", "/api/wearables/pair", "/api/connections/oauth/relay",
          "/api/openapi.json", "/api/desktop/discover", "/api/desktop/probe", "/api/desktop/join"}
# What a phone's own token opens, besides that.
DEVICE = {"/api/ingest/wearables", "/api/ingest/healthkit-export", "/api/wearables/history", "/api/wearables/summary", "/api/preferences",
          "/api/devices/web-session", "/api/devices/self/revoke", "/api/wearables/backfill"}
# For the server's owner alone: any account can reach their own side of the rest.
OWNER_ONLY_PREFIXES = ("/api/system/updates", "/api/settings/platforms", "/api/settings/wearables", "/api/settings/developer",
                       "/api/settings/source-order", "/api/export/backup", "/api/health", "/api/accounts")
OWN_ACCOUNT = {"/api/accounts/me/delete", "/api/accounts/me/join-review/seen"}


def _routes(app):
    """(method, path) for everything served: what's left out of the API's published list as well."""
    out = []
    for route in app.routes:
        if isinstance(route, Mount) or not getattr(route, "methods", None):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append((method, route.path))
    return out


def _call(client, method, path, *, headers=None, params=None, fill="x"):
    url = path
    for name in ("profile_id", "connection_id", "device_id", "token_id", "record_id", "entry_id", "chat_id", "job_id", "account_id",
                 "session_id", "platform", "provider", "metric", "workout_id", "metric_type", "institution_id", "event_id", "id", "kind"):
        url = url.replace(f"{{{name}}}", fill)
    url = url.split("{")[0] + "x" if "{" in url else url
    kwargs = {"headers": {**CSRF, **(headers or {})}, "params": params}
    if method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {}
    return client.request(method, url, follow_redirects=False, **kwargs)


def _app_with_people(raw_client):
    client = _signed_in_owner(raw_client)
    maria = _person(client, "Maria")
    maria_client, _ = _join(client, maria)
    return client, maria_client, maria


def test_a_stranger_gets_nothing_but_the_public_routes(raw_client):
    client, _, _ = _app_with_people(raw_client)
    anon = TestClient(client.app, base_url=BASE, client=("8.8.8.8", 5000))
    surprises = []
    for method, path in _routes(client.app):
        if path in PUBLIC or path.startswith("/sim/"):
            continue
        r = _call(anon, method, path)
        if r.status_code != 401:
            surprises.append(f"{method} {path}: {r.status_code}")
    assert not surprises, "These answer someone who isn't signed in; add to PUBLIC only if that's meant:\n" + "\n".join(surprises)


def test_a_phones_token_opens_only_what_a_phone_needs(raw_client):
    client, _, _ = _app_with_people(raw_client)
    _, anon, token, _ = _phone(client)
    surprises = []
    for method, path in _routes(client.app):
        if path in PUBLIC or path in DEVICE or path.startswith("/sim/"):
            continue
        r = _call(anon, method, path, headers=token)
        if r.status_code != 401:
            surprises.append(f"{method} {path}: {r.status_code}")
    assert not surprises, "\n".join(surprises)
    anon.cookies.clear()


def test_a_member_cant_reach_someone_who_hasnt_shared(raw_client):
    client, maria_client, _ = _app_with_people(raw_client)
    alex = client.get("/api/status").json()["account"]["profile_id"]
    surprises = []
    for method, path in _routes(client.app):
        if "{profile_id}" in path:
            r = _call(maria_client, method, path, fill=alex)
            if r.status_code not in (403, 404, 405):
                surprises.append(f"{method} {path}: {r.status_code}")
    # Everything that says which person it's about, in its query.
    for path, detail in client.app.openapi()["paths"].items():
        for method, op in detail.items():
            names = {p["name"] for p in op.get("parameters", []) if p["in"] == "query"}
            who = names & {"profile", "profile_id"}
            if method != "get" or not who or "{" in path:
                continue
            r = maria_client.get(path, params={who.pop(): alex}, follow_redirects=False)
            if r.status_code not in (403, 404, 422):
                surprises.append(f"GET {path}?profile: {r.status_code}")
    assert not surprises, "\n".join(surprises)


def test_a_member_cant_run_the_server(raw_client):
    client, maria_client, _ = _app_with_people(raw_client)
    surprises = []
    for method, path in _routes(client.app):
        if path.startswith(OWNER_ONLY_PREFIXES) and path not in OWN_ACCOUNT:
            r = _call(maria_client, method, path)
            if r.status_code != 403:
                surprises.append(f"{method} {path}: {r.status_code}")
    assert not surprises, "\n".join(surprises)


def test_state_changing_requests_need_the_header_browsers_cant_forge(raw_client):
    client, _, _ = _app_with_people(raw_client)
    bare = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT, cookies=dict(client.cookies))
    surprises = []
    for method, path in _routes(client.app):
        if method == "GET" or path in PUBLIC or path in DEVICE or path.startswith("/sim/") or path == "/mcp":
            continue
        url = path.split("{")[0] + "x" if "{" in path else path
        r = bare.request(method, url, json={} if method != "DELETE" else None, follow_redirects=False)
        if r.status_code != 403:
            surprises.append(f"{method} {path}: {r.status_code}")
    assert not surprises, "\n".join(surprises)


# ---------------------------------------------------------------------------
# Codes run out
# ---------------------------------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = time.time()

        def advance(self, seconds):
            self.now += seconds
    c = Clock()
    monkeypatch.setattr(time, "time", lambda: c.now)
    return c


def test_a_pairing_code_lasts_ten_minutes(raw_client, clock):
    client = _signed_in_owner(raw_client)
    assert devices.PAIRING_TTL == 600
    anon = TestClient(client.app, base_url=BASE)
    good = client.post("/api/devices/pairing-code", json={}).json()["code"]
    late = client.post("/api/devices/pairing-code", json={}).json()["code"]
    clock.advance(devices.PAIRING_TTL - 5)
    assert anon.post("/api/devices/pair", json={"code": good, "device_name": "x"}).status_code == 200
    clock.advance(10)
    assert anon.post("/api/devices/pair", json={"code": late, "device_name": "y"}).status_code == 400


def test_an_invitation_lasts_a_week(raw_client, clock):
    client = _signed_in_owner(raw_client)
    assert accounts.INVITE_TTL == 7 * 24 * 3600
    maria = _person(client, "Maria")
    made = client.post(f"/api/profiles/{maria}/invite").json()
    assert made["expires_at"] == pytest.approx(clock.now + accounts.INVITE_TTL)
    anon = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    anon.headers.update(CSRF)
    clock.advance(accounts.INVITE_TTL - 60)
    assert anon.post("/api/invites/check", json={"code": made["code"]}).status_code == 200
    clock.advance(120)
    assert anon.post("/api/invites/check", json={"code": made["code"]}).status_code == 404
    body = {"code": made["code"], "password": "maria's password", "accept_terms": True}
    assert anon.post("/api/invites/accept", json=body).status_code == 404


def test_a_session_runs_out_when_left_alone_and_slides_when_used(raw_client, clock):
    client = _signed_in_owner(raw_client)
    other = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    other.headers.update(CSRF)
    assert other.post("/api/auth/login", json={"password": PASSPHRASE}).status_code == 200
    used, left = client.cookies[auth.SESSION_COOKIE], other.cookies[auth.SESSION_COOKIE]
    bare = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)

    def opens(token):
        bare.cookies.clear()
        return bare.get("/api/profiles", headers={"Cookie": f"{auth.SESSION_COOKIE}={token}"}).status_code == 200

    clock.advance(auth.SESSION_TTL - 1000)
    assert opens(used)
    clock.advance(2000)
    assert opens(used)          # kept in use
    assert not opens(left)      # left alone


# ---------------------------------------------------------------------------
# Routes nothing else calls
# ---------------------------------------------------------------------------

def test_the_visit_summary_leaves_out_simulated_data(client):
    client.post("/api/connections/wearable", json={"provider": "oura", "mode": "simulated"})
    report = client.get("/api/report")
    assert report.status_code == 200
    assert client.get("/api/summary").status_code == 200
    assert not [h for h in report.json()["biometrics"] if "(simulated)" in ((h.get("latest") or {}).get("source") or "")]
    assert report.json()["profile"]["name"] == "Alex"


def test_agreeing_to_the_terms(raw_client):
    r = raw_client.post("/api/setup", json={"password": PASSPHRASE, "profile_name": "Alex"}, headers=CSRF)
    assert r.status_code == 200
    raw_client.headers.update(CSRF)
    assert not raw_client.get("/api/status").json()["terms_accepted"]
    assert raw_client.post("/api/terms/accept").status_code == 200
    assert raw_client.get("/api/status").json()["terms_accepted"]


def test_cancelling_an_invitation(raw_client):
    client = _signed_in_owner(raw_client)
    maria = _person(client, "Maria")
    code = client.post(f"/api/profiles/{maria}/invite").json()["code"]
    assert client.delete(f"/api/profiles/{maria}/invite").status_code == 200
    anon = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    assert anon.post("/api/invites/check", json={"code": code}, headers=CSRF).status_code == 404


def test_who_else_can_see_someone(raw_client):
    client = _signed_in_owner(raw_client)
    kid = _person(client, "Kid", "child")
    maria_client, _ = _join(client, _person(client, "Maria"))
    info = client.get(f"/api/profiles/{kid}/access").json()
    assert [a["name"] for a in info["access"]] == ["Alex"] and {a["name"] for a in info["accounts"]} == {"Alex", "Maria"}
    assert not info["signs_in"] and info["invited_until"] is None


def test_renaming_a_source_and_record_categories(client):
    cid = client.post("/api/connections/wearable", json={"provider": "oura", "mode": "simulated"}).json()["connection_id"]
    assert client.patch(f"/api/connections/{cid}", json={"display_name": "My ring"}).json()["display_name"] == "My ring"
    assert client.get("/api/categories").status_code == 200


def test_turning_the_ai_off_and_looking_for_local_ones(client, monkeypatch):
    from app.services import ai_local
    assert client.delete("/api/ai/config").status_code == 200

    async def nothing(skip_port=None):
        return []

    async def checked(url, key):
        return {"ok": True, "url": url, "models": []}
    monkeypatch.setattr(ai_local, "detect", nothing)
    monkeypatch.setattr(ai_local, "check", checked)
    assert client.get("/api/ai/local/detect").json()["servers"] == []
    assert client.post("/api/ai/local/check", json={"base_url": "http://localhost:11434"}).json()["ok"]


def test_discovering_a_fhir_server_and_checking_a_client_id(client, monkeypatch):
    from app.connectors import smart
    assert client.post("/api/connections/discover", json={"fhir_base_url": "ftp://plain.example/fhir"}).status_code == 400

    async def found(url, simulated=False):
        return {"authorization_endpoint": "https://fhir.example/auth"}
    monkeypatch.setattr(smart, "discover", found)
    assert client.post("/api/connections/discover", json={"fhir_base_url": "https://fhir.example/r4"}).status_code == 200
    r = client.post("/api/settings/platforms/nextgen/verify", params={"mode": "sandbox"})
    assert r.status_code == 200 and r.json()["ok"] in (False, None)
    assert client.post("/api/settings/platforms/nope/verify").status_code == 404


def test_mcp_over_get_says_to_post(client):
    r = client.get("/mcp")
    assert r.status_code == 405 and r.headers["allow"] == "POST"
