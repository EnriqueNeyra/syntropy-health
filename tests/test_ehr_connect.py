"""Signing in to a health system: whose chart it opened, whose browser came back, and which servers may be connected."""

import asyncio
import base64
import json
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.connectors import smart
from app.connectors import wearable_common as wc
from app.core import security
from app.core.db import db
from app.services import connect, sync as sync_svc
from app.simulator import server as sim
from app.store import connections, records
from tests.conftest import BASE, CSRF, LOCAL_CLIENT, connect_institution, simulated_login


def _problem_text(response) -> str:
    """What the page will say about a sign-in that didn't finish: the address carries it signed."""
    return security.unsign(unquote(response.headers["location"].split("problem=")[1]), "sign-in-problem")["message"]

BANNER = "banner-health"
RealClient = httpx.AsyncClient


@pytest.fixture(autouse=True)
def fresh_simulator():
    sim.reset_faults()
    yield
    sim.reset_faults()


def _reconnect(client, cid, username):
    url = client.post(f"/api/connections/{cid}/reconnect").json()["auth_url"]
    callback = simulated_login(client, url, username)
    return client.get(callback[len(BASE):], follow_redirects=False)


def _member(client, name="Maria"):
    profile = client.post("/api/profiles", json={"name": name, "relationship": "partner"}).json()["id"]
    code = client.post(f"/api/profiles/{profile}/invite").json()["code"]
    theirs = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    theirs.headers.update(CSRF)
    assert theirs.post("/api/invites/accept", json={"code": code, "password": "maria's own password", "accept_terms": True}).status_code == 200
    return theirs, profile


def _stranger(client):
    """A browser with no session on this server."""
    return TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)


# ---------------------------------------------------------------------------
# Reconnecting as someone else (F027)
# ---------------------------------------------------------------------------

def test_reconnecting_as_someone_else_changes_nothing_until_the_person_decides(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    before = connections.get(cid, include_credentials=True)
    count = before["record_count"]

    done = _reconnect(client, cid, "jordan.chen")
    assert done.status_code == 303 and done.headers["location"].startswith("/callback/choose?state=")
    page = client.get(done.headers["location"])
    assert "Jordan" in page.text and "Alex Rivera" in page.text and "Add Jordan" in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]

    after = connections.get(cid, include_credentials=True)
    assert after["patient_ref"] == before["patient_ref"] and after["credentials"] == before["credentials"]
    assert after["record_count"] == count
    assert len(client.get("/api/connections").json()["connections"]) == 1
    assert [i["full_name"] for i in client.get("/api/summary").json()["identities"]] == ["Alex Rivera"]


def test_leaving_it_as_it_was_drops_the_other_charts_tokens(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    before = connections.get(cid, include_credentials=True)
    place = _reconnect(client, cid, "jordan.chen").headers["location"]
    state = parse_qs(urlparse(place).query)["state"][0]
    no = client.post("/callback/choose", data={"state": state, "decision": "no"}, follow_redirects=False)
    assert "Nothing" in _problem_text(no)
    assert connections.get(cid, include_credentials=True)["credentials"] == before["credentials"]
    assert len(client.get("/api/connections").json()["connections"]) == 1
    assert client.post("/callback/choose", data={"state": state, "decision": "yes"}, follow_redirects=False).headers["location"].startswith("/#/sources?problem=")


def test_adding_the_other_chart_makes_a_separate_source_and_flags_the_mix(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    before = connections.get(cid)
    place = _reconnect(client, cid, "jordan.chen").headers["location"]
    state = parse_qs(urlparse(place).query)["state"][0]
    yes = client.post("/callback/choose", data={"state": state, "decision": "yes"}, follow_redirects=False)
    new = yes.headers["location"].split("connected=")[1]
    assert new != cid
    second = client.get(f"/api/connections/{new}").json()
    assert "Jordan" in second["display_name"] and second["record_count"] > 0 and second["patient_ref"] != before["patient_ref"]
    first = connections.get(cid)
    assert first["record_count"] == before["record_count"] and first["patient_ref"] == before["patient_ref"]
    assert client.get("/api/summary").json()["identity_conflict"]


def test_picking_the_wrong_chart_twice_doesnt_make_two_sources(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    for _ in range(2):
        place = _reconnect(client, cid, "jordan.chen").headers["location"]
        state = parse_qs(urlparse(place).query)["state"][0]
        client.post("/callback/choose", data={"state": state, "decision": "yes"}, follow_redirects=False)
    assert len(client.get("/api/connections").json()["connections"]) == 2


def test_the_same_person_under_a_new_id_renews_in_place(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    real = connections.get(cid)["patient_ref"]
    connections.update(cid, patient_ref="id-before-a-chart-merge")
    done = _reconnect(client, cid, "alex.rivera")
    assert done.headers["location"].endswith(cid)
    assert connections.get(cid)["patient_ref"] == real


def test_a_patient_read_failing_is_treated_as_someone_else_so_nothing_is_lost(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    connections.update(cid, patient_ref="id-before-a-chart-merge")
    sim.faults.answers.append({"type": "Patient", "read": True, "status": 500})
    done = _reconnect(client, cid, "alex.rivera")
    assert done.headers["location"].startswith("/callback/choose")
    assert connections.get(cid)["patient_ref"] == "id-before-a-chart-merge"
    assert "someone else" in client.get(done.headers["location"]).text


def test_reconnecting_to_the_same_chart_is_unchanged(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    connections.update(cid, status="needs_reauth")
    done = _reconnect(client, cid, "alex.rivera")
    assert done.headers["location"].endswith(cid) and connections.get(cid)["status"] == "active"


def test_a_source_with_no_patient_recorded_just_gets_the_sign_in(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    connections.update(cid, patient_ref=None)
    assert _reconnect(client, cid, "jordan.chen").headers["location"].endswith(cid)


# ---------------------------------------------------------------------------
# Whose browser came back (F157)
# ---------------------------------------------------------------------------

def _started(client, **kw):
    r = client.post("/api/connections/ehr", json={"institution_id": BANNER, **kw})
    assert r.status_code == 200, r.text
    return simulated_login(client, r.json()["auth_url"])


def test_one_account_with_no_session_goes_straight_through(client):
    """The Mac and Windows apps and the phone can come back through a browser with no session."""
    callback = _started(client)
    done = _stranger(client).get(callback[len(BASE):], follow_redirects=False)
    assert "connected=" in done.headers["location"]


def test_the_same_browser_goes_straight_through_with_several_accounts(client):
    _member(client)
    callback = _started(client)
    assert "connected=" in client.get(callback[len(BASE):], follow_redirects=False).headers["location"]


def test_another_accounts_browser_is_turned_away_and_the_link_still_works_for_its_owner(client):
    theirs, _ = _member(client)
    callback = _started(client)
    refused = theirs.get(callback[len(BASE):], follow_redirects=False)
    assert "someone else" in _problem_text(refused)
    assert client.get("/api/connections").json()["connections"] == []
    assert "connected=" in client.get(callback[len(BASE):], follow_redirects=False).headers["location"]


def test_someone_who_manages_that_person_may_finish_it(client):
    theirs, maria = _member(client)
    callback = _started(theirs)         # Maria started it for herself; Alex, the owner, can manage her records
    from app.store import accounts
    alex = accounts.first_owner()
    accounts.grant(alex["id"], maria, "manage")
    assert "connected=" in client.get(callback[len(BASE):], follow_redirects=False).headers["location"]


def test_no_session_with_several_accounts_asks_first_and_names_who_gets_the_records(client):
    _member(client)
    callback = _started(client)
    outsider = _stranger(client)
    asked = outsider.get(callback[len(BASE):], follow_redirects=False)
    place = asked.headers["location"]
    assert place.startswith("/callback/choose")
    assert "code=" not in place
    page = outsider.get(place)
    assert "Add these records to Alex?" in page.text and "Nothing has been saved yet" in page.text
    assert client.get("/api/connections").json()["connections"] == []

    state = parse_qs(urlparse(place).query)["state"][0]
    no = outsider.post("/callback/choose", data={"state": state, "decision": "no"}, follow_redirects=False)
    assert "problem=" in no.headers["location"]
    assert client.get("/api/connections").json()["connections"] == []


def test_confirming_finishes_it_in_the_account_that_started_it(client):
    _member(client)
    callback = _started(client)
    outsider = _stranger(client)
    place = outsider.get(callback[len(BASE):], follow_redirects=False).headers["location"]
    state = parse_qs(urlparse(place).query)["state"][0]
    yes = outsider.post("/callback/choose", data={"state": state, "decision": "yes"}, follow_redirects=False)
    assert "connected=" in yes.headers["location"]
    created = next(e for e in client.get("/api/audit").json()["events"] if e["action"] == "connection.created")
    assert created["actor"].startswith("user:acc") or created["who"] == "Alex"


def test_the_phones_way_back_through_the_relay_handoff(client, monkeypatch):
    from app.connectors import whoop

    async def fetched(credentials, since, until):
        return {key: [] for key in whoop.ENDPOINTS}
    monkeypatch.setattr(whoop, "fetch_live", fetched)
    theirs, _ = _member(client)

    def pending_state():
        r = client.post("/api/connections/wearable", json={"provider": "whoop", "mode": "live"})
        assert r.status_code == 200, r.text
        return parse_qs(urlparse(r.json()["auth_url"]).query)["state"][0]
    tokens = {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}

    state = pending_state()
    assert theirs.post("/api/connections/oauth/relay", json={"state": state, **tokens}).status_code == 403

    outsider = _stranger(client)
    asked = outsider.post("/api/connections/oauth/relay", json={"state": state, **tokens}, headers=CSRF)
    assert asked.status_code == 200 and asked.json()["choose"].startswith("/callback/choose")
    ticket = parse_qs(urlparse(asked.json()["choose"]).query)["state"][0]
    yes = outsider.post("/callback/choose", data={"state": ticket, "decision": "yes"}, follow_redirects=False)
    cid = yes.headers["location"].split("connected=")[1]
    assert client.get(f"/api/connections/{cid}").json()["provider"] == "whoop"


def test_a_choice_cant_be_reached_through_the_oauth_callback(client):
    callback = _started(client)
    assert "connected=" in client.get(callback[len(BASE):], follow_redirects=False).headers["location"]
    from app.store.connections import save_pending
    save_pending("ticket", "choice", {"action": "finish"})
    assert "problem=" in client.get("/callback", params={"code": "x", "state": "ticket"}, follow_redirects=False).headers["location"]


def test_the_relay_page_still_runs_under_its_policy(client):
    page = client.get("/callback")
    assert page.status_code == 200 and "choose" in page.text and "script-src 'sha256-" in page.headers["content-security-policy"]


# ---------------------------------------------------------------------------
# Who may point this server at what (F053)
# ---------------------------------------------------------------------------

def _addresses(*ips):
    return lambda host, port: [(2, 1, 6, "", (ip, 443)) for ip in ips]


def test_a_member_cant_point_the_server_at_a_private_address(client, monkeypatch):
    theirs, _ = _member(client)
    monkeypatch.setattr(smart, "_lookup", _addresses("127.0.0.1"))
    for url in ("https://127.0.0.1:8194/fhir", "https://169.254.169.254/latest", "https://[::1]/fhir", "https://[::ffff:10.0.0.1]/fhir",
                "https://internal.example.net/fhir", "http://example.org/fhir", "file:///etc/passwd", "ftp://example.org"):
        for route, body in (("/api/connections/ehr", {"fhir_base_url": url, "client_id": "x"}), ("/api/connections/discover", {"fhir_base_url": url})):
            r = theirs.post(route, json=body)
            assert r.status_code == 400, (url, route, r.text)


def test_a_name_with_one_private_address_among_public_ones_is_refused(client, monkeypatch):
    theirs, _ = _member(client)
    monkeypatch.setattr(smart, "_lookup", _addresses("93.184.216.34", "127.0.0.1"))
    assert theirs.post("/api/connections/discover", json={"fhir_base_url": "https://sneaky.example.net/fhir"}).status_code == 400


def test_an_institution_can_no_longer_carry_its_own_address(client, monkeypatch):
    theirs, _ = _member(client)
    r = theirs.post("/api/connections/ehr", json={"institution_id": "nope", "platform": "epic", "fhir_base_url": "https://127.0.0.1:8194/fhir", "client_id": "x"})
    assert r.status_code == 400 and "Unknown institution" in r.text


def test_a_redirect_to_a_private_address_is_stopped_for_a_member():
    """The guard runs on every hop, through the real client, so it can't be got past with a 302."""
    async def go():
        token = smart.GUARDED.set(True)
        original = smart._lookup
        smart._lookup = _addresses("93.184.216.34")

        def handler(request):
            if request.url.host == "public.example.org":
                return httpx.Response(302, headers={"Location": "https://127.0.0.1:8194/admin"})
            return httpx.Response(200, text="ROUTER-ADMIN-SECRET")
        try:
            client = smart.http_client()
            transport = httpx.MockTransport(handler)
            client._transport = transport
            with pytest.raises(smart.BlockedAddress):
                await client.get("https://public.example.org/fhir/Binary/1")
            with pytest.raises(smart.BlockedAddress):
                await client.get("http://public.example.org/fhir")
            await client.aclose()
        finally:
            smart.GUARDED.reset(token)
            smart._lookup = original
    asyncio.run(go())


def test_a_note_redirected_somewhere_forbidden_is_skipped_not_stored():
    async def go():
        token = smart.GUARDED.set(True)
        original = smart._lookup
        smart._lookup = _addresses("93.184.216.34")

        def handler(request):
            if request.url.host == "public.example.org":
                return httpx.Response(302, headers={"Location": "https://127.0.0.1:8194/admin"})
            return httpx.Response(200, text="ROUTER-ADMIN-SECRET")
        try:
            client = smart.http_client()
            client._transport = httpx.MockTransport(handler)
            fhir = smart.FhirClient("https://public.example.org/fhir", {"access_token": "t"}, "p")
            assert await fhir.read_binary_text(client, "https://public.example.org/fhir/Binary/1") is None
            await client.aclose()
        finally:
            smart.GUARDED.reset(token)
            smart._lookup = original
    asyncio.run(go())


def test_the_owner_can_use_their_own_network(client, monkeypatch):
    monkeypatch.setattr(smart, "_lookup", _addresses("192.168.1.20"))

    async def found(base, simulated=False):
        return {"authorization_endpoint": f"{base}/auth", "token_endpoint": f"{base}/token", "scopes_supported": []}
    monkeypatch.setattr(smart, "discover", found)
    client.put("/api/settings/developer", json={"enabled": False})
    r = client.post("/api/connections/ehr", json={"fhir_base_url": "http://hapi.home/fhir", "client_id": "x"})
    assert r.status_code == 200, r.text
    state = parse_qs(urlparse(r.json()["auth_url"]).query)["state"][0]
    assert connections.peek_pending(state)[1]["allow_private"] is True
    assert client.post("/api/connections/discover", json={"fhir_base_url": "https://hapi.home/fhir"}).status_code == 200


def test_a_public_server_the_owner_added_is_still_held_to_the_public_internet(client, monkeypatch):
    monkeypatch.setattr(smart, "_lookup", _addresses("93.184.216.34"))

    async def found(base, simulated=False):
        assert smart.GUARDED.get()
        return {"authorization_endpoint": f"{base}/auth", "token_endpoint": f"{base}/token", "scopes_supported": []}
    monkeypatch.setattr(smart, "discover", found)
    client.put("/api/settings/developer", json={"enabled": False})
    r = client.post("/api/connections/ehr", json={"fhir_base_url": "https://fhir.example.org/r4", "client_id": "x"})
    assert r.status_code == 200, r.text
    state = parse_qs(urlparse(r.json()["auth_url"]).query)["state"][0]
    assert connections.peek_pending(state)[1]["allow_private"] is False


def test_plain_http_to_the_internet_is_refused_even_for_the_owner_outside_developer_mode(client, monkeypatch):
    monkeypatch.setattr(smart, "_lookup", _addresses("93.184.216.34"))
    client.put("/api/settings/developer", json={"enabled": False})
    r = client.post("/api/connections/ehr", json={"fhir_base_url": "http://fhir.example.org/r4", "client_id": "x"})
    assert r.status_code == 400 and "https" in r.text


def test_a_member_can_still_add_a_public_https_server(client, monkeypatch):
    theirs, _ = _member(client)
    monkeypatch.setattr(smart, "_lookup", _addresses("93.184.216.34"))

    async def found(base, simulated=False):
        assert smart.GUARDED.get()
        return {"authorization_endpoint": f"{base}/auth", "token_endpoint": f"{base}/token", "scopes_supported": []}
    monkeypatch.setattr(smart, "discover", found)
    r = theirs.post("/api/connections/ehr", json={"fhir_base_url": "https://fhir.example.org/r4", "client_id": "x"})
    assert r.status_code == 200, r.text
    state = parse_qs(urlparse(r.json()["auth_url"]).query)["state"][0]
    assert connections.peek_pending(state)[1]["allow_private"] is False


def test_an_old_custom_source_on_a_private_address_stops_with_an_explanation(client, monkeypatch):
    profile = client.get("/api/connections").json()["profile_id"]
    cid = connections.create(profile, "ehr", "custom", "Old one", mode="production", fhir_base_url="https://10.0.0.5/fhir",
                             patient_ref="p", credentials={"access_token": "t"})["id"]
    res = asyncio.run(sync_svc.sync_connection(cid, "scheduled"))
    assert res["status"] == "error" and "private network" in res["error"]
    assert connections.get(cid)["status"] == "error"


def test_a_custom_source_the_owner_added_keeps_syncing_on_their_network(client):
    assert connect.guard_for({"provider": "custom", "metadata": {"allow_private": True}}) is False
    assert connect.guard_for({"provider": "custom", "metadata": {}}) is True
    assert connect.guard_for({"provider": "cerner", "metadata": {}}) is False     # listed systems are never second-guessed


def test_the_simulator_and_listed_systems_are_untouched(client):
    assert connect_institution(client, BANNER)["connection_id"]


def test_discovered_sign_in_addresses_must_be_secure():
    def server(authorize, token):
        def handler(request):
            return httpx.Response(200, json={"authorization_endpoint": authorize, "token_endpoint": token})
        return handler

    async def go(handler):
        real = smart.http_client
        smart.http_client = lambda simulated=False, timeout=30.0: httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await smart.discover("https://fhir.example.org/r4")
        finally:
            smart.http_client = real
    for bad in (("javascript:alert(1)", "https://x/token"), ("https://x/auth", "http://x/token"), ("data:text/html,x", "https://x/token")):
        with pytest.raises(smart.SmartError):
            asyncio.run(go(server(*bad)))
    assert asyncio.run(go(server("https://x/auth", "https://x/token")))["token_endpoint"] == "https://x/token"


def test_token_errors_repeat_oauths_words_and_nothing_else():
    html_page = httpx.Response(403, text="INTERNAL-POST-RESPONSE secret-from-internal-service")
    assert "INTERNAL" not in smart._error_text(html_page)
    oauth = httpx.Response(400, json={"error": "invalid_grant", "error_description": "Code expired"})
    assert smart._error_text(oauth) == "Code expired"
    other = httpx.Response(400, json={"note": "secret"})
    assert "secret" not in smart._error_text(other)


# ---------------------------------------------------------------------------
# Scopes (F138), discovery and token shapes (F247, F274)
# ---------------------------------------------------------------------------

REQUESTED = "openid launch/patient offline_access patient/Patient.read patient/Observation.read patient/Condition.read"


def test_a_registered_platforms_scopes_are_never_rewritten():
    v2_only = ["patient/Patient.rs", "patient/Observation.rs", "patient/Condition.rs"]
    assert smart.fit_scopes(REQUESTED, v2_only) == REQUESTED


def test_ones_own_server_offering_only_smart2_scopes_is_asked_that_way():
    v2_only = ["patient/Patient.rs", "patient/Observation.rs", "patient/Condition.cruds"]
    assert smart.fit_scopes(REQUESTED, v2_only, own_registration=True) == \
        "openid launch/patient offline_access patient/Patient.rs patient/Observation.rs patient/Condition.rs"
    both = ["patient/Patient.read", "patient/Patient.rs", "patient/Observation.read", "patient/Condition.read"]
    assert smart.fit_scopes(REQUESTED, both, own_registration=True) == REQUESTED


def test_every_platforms_scope_request_is_what_it_was(client):
    from app import directory
    for name, preset in directory.PLATFORMS.items():
        fitted = smart.fit_scopes(preset["scopes"], ["patient/Patient.rs", "patient/*.rs", "openid"])
        assert ".rs" not in fitted, name


def test_granted_smart2_scopes_are_understood():
    fhir = smart.FhirClient("https://x/r4", {"access_token": "t", "scope": "patient/Observation.rs patient/Condition.r"}, "p")
    assert fhir.granted == {"Observation", "Condition"}


def _handled(handler):
    return lambda simulated=False, timeout=30.0: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_discovery_falls_back_to_the_capability_statement(monkeypatch):
    def handler(request):
        if "smart-configuration" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, json={"resourceType": "CapabilityStatement", "rest": [{"security": {"extension": [{
            "url": "http://fhir-registry.smarthealthit.org/StructureDefinition/oauth-uris",
            "extension": [{"url": "authorize", "valueUri": "https://x/auth"}, {"url": "token", "valueUri": "https://x/token"}]}]}}]})
    monkeypatch.setattr(smart, "http_client", _handled(handler))
    found = asyncio.run(smart.discover("https://x/r4"))
    assert found["source"] == "metadata" and found["authorization_endpoint"] == "https://x/auth"


@pytest.mark.parametrize("body", [["a", "list"], {"rest": "nope"}, {"rest": [{"security": 3}]}, {"rest": ["x"]}, {}])
def test_discovery_on_nonsense_is_a_plain_error(monkeypatch, body):
    monkeypatch.setattr(smart, "http_client", _handled(lambda r: httpx.Response(200, json=body)))
    with pytest.raises(smart.SmartError):
        asyncio.run(smart.discover("https://x/r4"))


def test_discovery_on_html_and_when_unreachable(monkeypatch):
    monkeypatch.setattr(smart, "http_client", _handled(lambda r: httpx.Response(200, text="<html>")))
    with pytest.raises(smart.SmartError) as caught:
        asyncio.run(smart.discover("https://x/r4"))
    assert "invalid metadata" in str(caught.value)

    def down(request):
        raise httpx.ConnectError("All connection attempts failed")
    monkeypatch.setattr(smart, "http_client", _handled(down))
    with pytest.raises(smart.SmartError) as caught:
        asyncio.run(smart.discover("https://x/r4"))
    assert caught.value.kind == "network" and "attempts" not in str(caught.value)


def _exchange():
    return asyncio.run(smart.exchange_code(token_endpoint="https://x/token", code="c", redirect_uri="https://y/cb",
                                           client_id="id", code_verifier="v"))


@pytest.mark.parametrize("answer", [httpx.Response(200, text="<html>not json</html>"), httpx.Response(200, json=[1]),
                                    httpx.Response(200, json={"token_type": "Bearer"}), httpx.Response(500, text="boom")])
def test_a_bad_token_exchange_is_a_readable_error(monkeypatch, answer):
    monkeypatch.setattr(smart, "http_client", _handled(lambda r: answer))
    with pytest.raises(smart.SmartError):
        _exchange()


def test_token_exchange_unreachable(monkeypatch):
    def down(request):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(smart, "http_client", _handled(down))
    with pytest.raises(smart.SmartError) as caught:
        _exchange()
    assert caught.value.kind == "network"


def _id_token(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"head.{payload}.sig"


@pytest.mark.parametrize("tokens,patient", [
    ({"patient": "p1"}, "p1"),
    ({"patient": 42}, "42"),
    ({"id_token": _id_token({"patient": "p2"})}, "p2"),
    ({"id_token": _id_token({"fhirUser": "https://x/r4/Patient/p3"})}, "p3"),
    ({"id_token": _id_token({"fhirUser": "https://x/r4/Practitioner/d1"})}, None),
    ({"id_token": "not-a-jwt"}, None),
    ({"id_token": "a.%%%.c"}, None),
    ({}, None),
])
def test_the_patient_comes_from_the_token_answer(tokens, patient):
    assert smart._patient_from_tokens(tokens) == patient


def test_an_unknown_platform_is_a_plain_error(client):
    with pytest.raises(smart.SmartError):
        connect.plan_ehr_connection(None, BASE, platform="zzz", fhir_base_url="https://x/r4")


def test_an_import_with_junk_entries_still_reads_the_rest():
    from app.services import imports
    raw = json.dumps({"resourceType": "Bundle", "entry": ["oops", {"resource": {"resourceType": "Goal", "id": "g"}}, None]}).encode()
    assert [r["id"] for r in imports.parse_fhir_payload(raw)] == ["g"]


def test_failed_sign_in_shapes_at_the_callback_stay_on_the_sources_page(client, monkeypatch):
    r = client.post("/api/connections/ehr", json={"institution_id": BANNER})
    callback = simulated_login(client, r.json()["auth_url"])
    monkeypatch.setattr(smart, "http_client", _handled(lambda req: httpx.Response(200, text="<html>")))
    done = client.get(callback[len(BASE):], follow_redirects=False)
    assert done.status_code == 303 and "problem=" in done.headers["location"]


def test_scheduled_syncs_run_side_by_side_without_losing_track(client, monkeypatch):
    from app.services import scheduler
    ids = [connect_institution(client, i)["connection_id"] for i in (BANNER, "epic-duke-health")]
    with db() as c:
        c.execute("UPDATE connections SET last_sync_at = 0")
    synced = asyncio.run(scheduler.run_once())
    assert set(synced) == set(ids)


def test_a_person_can_see_how_each_kind_of_record_fared(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    sim.faults.answers.append({"type": "Goal", "status": 403})
    sim.faults.answers.append({"type": "Immunization", "status": 500})
    client.post(f"/api/connections/{cid}/sync")
    lines = {q["name"]: q for q in client.get(f"/api/connections/{cid}").json()["queries"]}
    assert lines["Name and birth date"]["words"] == "Received"
    assert lines["Goals"]["words"] == "This source didn't allow them to be read" and not lines["Goals"]["ok"]
    assert "try again" in lines["Immunizations"]["words"]
    assert lines["Lab results"]["words"].endswith("records") and lines["Lab results"]["ok"]
    assert "Imaging results" not in lines      # an extra kind this source never offered isn't worth a line


def test_any_shape_can_be_sent_through_the_simulator(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    patient = connections.get(cid)["patient_ref"]
    sim.faults.extra_resources.append({
        "resourceType": "Observation", "id": "comparator", "status": "final", "subject": {"reference": f"Patient/{patient}"},
        "category": [{"coding": [{"code": "laboratory"}]}], "code": {"text": "Creatinine"}, "effectiveDateTime": "2026-02-02",
        "valueQuantity": {"value": 0.2, "comparator": "<", "unit": "mg/dL"}})
    assert client.post(f"/api/connections/{cid}/sync").json()["inserted"] == 1


# ---------------------------------------------------------------------------
# The other real-world paths nothing else runs (F247)
# ---------------------------------------------------------------------------

def _whoop_client(monkeypatch, handler):
    from app.connectors import whoop
    monkeypatch.setattr(whoop.httpx, "AsyncClient", lambda **kw: RealClient(transport=httpx.MockTransport(handler), **kw))
    return whoop


def _whoop_fetch(whoop):
    from datetime import date

    async def go():
        async with httpx.AsyncClient(timeout=5) as client:
            return await whoop._fetch(client, "t", "v2/recovery", date(2026, 1, 1), date(2026, 1, 2))
    return asyncio.run(go())


def test_whoop_pages_through_everything(monkeypatch):
    def handler(request):
        if request.url.params.get("nextToken") == "two":
            return httpx.Response(200, json={"records": [{"id": 2}]})
        return httpx.Response(200, json={"records": [{"id": 1}], "next_token": "two"})
    assert [r["id"] for r in _whoop_fetch(_whoop_client(monkeypatch, handler))] == [1, 2]


def test_whoop_401_is_a_sign_in_problem_and_403_is_a_refusal(monkeypatch):
    with pytest.raises(smart.SmartError) as caught:
        _whoop_fetch(_whoop_client(monkeypatch, lambda r: httpx.Response(401)))
    assert caught.value.kind == "auth"
    with pytest.raises(wc.Refused):     # fetch_live decides what that means, from all the types together
        _whoop_fetch(_whoop_client(monkeypatch, lambda r: httpx.Response(403)))


@pytest.mark.parametrize("status", [429, 500, 503])
def test_whoop_trouble_comes_out_as_something_sync_can_classify(monkeypatch, status):
    with pytest.raises(httpx.HTTPStatusError):
        _whoop_fetch(_whoop_client(monkeypatch, lambda r: httpx.Response(status)))
    plain = sync_svc._plain_failure(httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://x"), response=httpx.Response(status)), "WHOOP")
    assert plain.kind == "network" and "WHOOP" in str(plain)


def test_a_client_error_from_a_vendor_isnt_called_temporary():
    plain = sync_svc._plain_failure(httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://x"), response=httpx.Response(400)), "WHOOP")
    assert plain.kind == "error"
    assert sync_svc._plain_failure(httpx.ConnectTimeout("t"), "WHOOP").kind == "network"


def _local_exchange(monkeypatch, handler):
    from app.core import settings
    monkeypatch.setattr(wc.httpx, "AsyncClient", lambda **kw: RealClient(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(settings, "wearable_credentials", lambda provider: {"client_id": "id", "client_secret": "secret"})
    return asyncio.run(wc.exchange_code_local("https://vendor.example/token", "whoop", "code", "https://me/callback"))


def test_a_wearable_code_exchange_with_ones_own_secret(monkeypatch):
    assert _local_exchange(monkeypatch, lambda r: httpx.Response(200, json={"access_token": "A", "refresh_token": "R", "expires_in": 60}))["access_token"] == "A"
    for answer in (httpx.Response(400, json={"error": "invalid_grant"}), httpx.Response(200, text="<html>"), httpx.Response(200, json=[1])):
        with pytest.raises(smart.SmartError):
            _local_exchange(monkeypatch, lambda r, a=answer: a)


def _verify(client, monkeypatch, handler, platform="cerner", mode="sandbox"):
    from app.api import settings as api
    monkeypatch.setattr(api.httpx, "AsyncClient", lambda **kw: RealClient(transport=httpx.MockTransport(handler), **kw))
    client.put(f"/api/settings/platforms/{platform}", json={"mode": "sandbox", "sandbox_client_id": "my-client"})
    return client.post(f"/api/settings/platforms/{platform}/verify", params={"mode": mode}).json()


def test_checking_a_client_id_reads_the_vendors_answer(client, monkeypatch):
    from app.api import settings as api
    if "cerner" not in api.PROBES:
        pytest.skip("no sandbox probe for this platform")
    ok = _verify(client, monkeypatch, lambda r: httpx.Response(302, headers={"Location": "https://login/x"}))
    assert ok["ok"] is True
    bad = _verify(client, monkeypatch, lambda r: httpx.Response(400, text="invalid_client"))
    assert bad["ok"] is False


def test_checking_a_client_id_when_the_vendor_is_unreachable(client, monkeypatch):
    from app.api import settings as api
    if "cerner" not in api.PROBES:
        pytest.skip("no sandbox probe for this platform")

    def down(request):
        raise httpx.ConnectError("no route")
    res = _verify(client, monkeypatch, down)
    assert res["ok"] is False


# ---------------------------------------------------------------------------
# Twins: a shared birth date and surname isn't the same person (F027)
# ---------------------------------------------------------------------------

BORN = "2015-03-02"


def _chart(system="urn:oid:1.2.3", value="M1", **extra):
    return {"identifier": [{"type": {"coding": [{"code": "MR"}]}, "system": system, "value": value}], **extra}


@pytest.mark.parametrize("stored, seen, same", [
    ("Emma Rivera", "Noah Rivera", False),              # twins
    ("Emma Rivera", "Emma Rivera", True),
    ("Emma Rivera", "Emma Rose Rivera", True),          # a middle name appearing is still her
    ("Emma Rose Rivera", "Emma Grace Rivera", False),
    ("Rivera", "Noah Rivera", False),                   # one word says too little
    ("Emma Rivera", "Emma Chen", False),                # a new surname is for the person to confirm
])
def test_renewing_in_place_takes_the_whole_name(stored, seen, same):
    assert connect._same_person({"birth_date": BORN, "full_name": stored}, {"birth_date": BORN, "name": seen}) is same


def test_the_same_chart_number_from_the_same_system_is_the_same_person_under_any_name():
    stored = {"birth_date": BORN, "full_name": "Emma Rivera", "raw": _chart()}
    assert connect._same_person(stored, {"birth_date": BORN, "name": "Emma Chen", "raw": _chart()})


def test_a_different_chart_number_from_the_same_system_is_someone_else_even_with_the_same_name():
    stored = {"birth_date": BORN, "full_name": "Emma Rivera", "raw": _chart()}
    assert not connect._same_person(stored, {"birth_date": BORN, "name": "Emma Rivera", "raw": _chart(value="M2")})


def test_numbers_that_arent_chart_numbers_dont_make_twins_one_person():
    member = {"identifier": [{"system": "urn:insurer", "value": "FAMILY-1"}]}
    stored = {"birth_date": BORN, "full_name": "Emma Rivera", "raw": member}
    assert not connect._same_person(stored, {"birth_date": BORN, "name": "Noah Rivera", "raw": member})


def test_a_new_birth_date_isnt_the_same_person_whatever_else_matches():
    stored = {"birth_date": BORN, "full_name": "Emma Rivera", "raw": _chart()}
    assert not connect._same_person(stored, {"birth_date": "2015-03-03", "name": "Emma Rivera", "raw": _chart()})


def test_picking_a_twins_chart_asks_and_changes_nothing(client, monkeypatch):
    cid = connect_institution(client, BANNER)["connection_id"]
    before = connections.get(cid, include_credentials=True)

    async def twin(pending, creds, patient):
        return {"name": "Jordan Rivera", "birth_date": next(i for i in records.identities(before["profile_id"]))["birth_date"]}
    monkeypatch.setattr(connect, "_patient_seen_by", twin)
    done = _reconnect(client, cid, "jordan.chen")
    assert done.headers["location"].startswith("/callback/choose?state=")
    after = connections.get(cid, include_credentials=True)
    assert after["patient_ref"] == before["patient_ref"] and after["credentials"] == before["credentials"]
    assert after["record_count"] == before["record_count"]


def test_a_patient_answer_of_an_odd_shape_asks_rather_than_failing(client):
    cid = connect_institution(client, BANNER)["connection_id"]
    connections.update(cid, patient_ref="id-before-a-chart-merge")
    sim.faults.answers.append({"type": "Patient", "read": True, "status": 200, "body": {"resourceType": "Patient", "name": 5}})
    done = _reconnect(client, cid, "alex.rivera")
    assert done.headers["location"].startswith("/callback/choose")


# ---------------------------------------------------------------------------
# A mistyped address gets words (F053)
# ---------------------------------------------------------------------------

def _no_such_name(host, port):
    raise OSError("nodename nor servname provided")


@pytest.mark.parametrize("owner, address", [(False, "https://no-such-host.example/fhir"), (True, "http://no-such-host.local/fhir")])
@pytest.mark.parametrize("path, extra", [("/api/connections/discover", {}), ("/api/connections/ehr", {"client_id": "x"})])
def test_a_name_that_doesnt_resolve_is_explained(client, monkeypatch, owner, address, path, extra):
    monkeypatch.setattr(smart, "_lookup", _no_such_name)
    monkeypatch.setattr(connect, "acting_as_owner", lambda: owner)
    monkeypatch.setattr(connect.settings, "developer_mode", lambda: False)
    res = client.post(path, json={"fhir_base_url": address, **extra})
    assert res.status_code == 400 and "no-such-host" in res.json()["detail"], res.text


@pytest.mark.parametrize("address", ["https://[broken/fhir", "https://example.org:99999/fhir", "https://with space.example/fhir", "not an address"])
@pytest.mark.parametrize("path, extra", [("/api/connections/discover", {}), ("/api/connections/ehr", {"client_id": "x"})])
def test_an_address_that_cant_exist_is_explained(client, path, extra, address):
    res = client.post(path, json={"fhir_base_url": address, **extra})
    assert res.status_code == 400 and "full address" in res.json()["detail"], res.text


def test_a_name_too_long_to_look_up_is_explained(client, monkeypatch):
    monkeypatch.setattr(connect, "acting_as_owner", lambda: False)
    res = client.post("/api/connections/discover", json={"fhir_base_url": f"https://{'a' * 70}.example/fhir"})
    assert res.status_code == 400, res.text
