"""End-to-end SMART on FHIR flow against the built-in simulator."""

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx

from tests.conftest import BASE, connect_institution, simulated_login


def test_full_connection_flow_imports_records(client):
    result = connect_institution(client, "epic-stanford-health-care")
    conn = client.get(f"/api/connections/{result['connection_id']}").json()
    assert conn["mode"] == "simulated"
    assert conn["provider"] == "epic"
    assert conn["status"] == "active"
    assert conn["last_sync_status"] in ("success", "partial"), conn
    assert conn["record_count"] > 50

    summary = client.get("/api/summary").json()
    names = {c["title"] for c in summary["active_conditions"]}
    assert "Essential hypertension" in names
    assert any(m["title"].startswith("Lisinopril") for m in summary["active_medications"])
    assert any(a["title"] == "Penicillin G" for a in summary["allergies"])
    assert summary["identities"][0]["full_name"] == "Alex Rivera"


def test_denied_consent_redirects_with_error(client):
    r = client.post("/api/connections/ehr", json={"institution_id": "epic-duke-health"})
    callback = simulated_login(client, r.json()["auth_url"], decision="deny")
    done = client.get(callback[len(BASE):], follow_redirects=False)
    assert "error=" in done.headers["location"]
    assert client.get("/api/connections").json()["connections"] == []


def test_state_is_single_use(client):
    r = client.post("/api/connections/ehr", json={"institution_id": "epic-duke-health"})
    callback = simulated_login(client, r.json()["auth_url"])
    first = client.get(callback[len(BASE):], follow_redirects=False)
    assert "connected=" in first.headers["location"]
    replay = client.get(callback[len(BASE):], follow_redirects=False)
    assert "error=" in replay.headers["location"]


def test_multiple_institutions_deduplicate_shared_facts(client):
    connect_institution(client, "epic-stanford-health-care")
    connect_institution(client, "epic-sutter-health")
    conns = client.get("/api/connections").json()["connections"]
    assert len(conns) == 2
    meds = client.get("/api/records", params={"category": "medications"}).json()["items"]
    lisinopril = [m for m in meds if m["title"].startswith("Lisinopril")]
    assert len(lisinopril) == 1
    assert len(lisinopril[0]["sources"]) == 2
    raw = client.get("/api/records", params={"category": "medications", "dedupe": "false"}).json()["items"]
    assert len([m for m in raw if m["title"].startswith("Lisinopril")]) == 2


def test_resync_is_idempotent(client):
    cid = connect_institution(client, "epic-stanford-health-care")["connection_id"]
    before = client.get(f"/api/connections/{cid}").json()["record_count"]
    res = client.post(f"/api/connections/{cid}/sync").json()
    assert res["status"] in ("success", "partial")
    assert res["inserted"] == 0
    assert client.get(f"/api/connections/{cid}").json()["record_count"] == before


def test_reconnect_reuses_connection(client):
    cid = connect_institution(client, "epic-stanford-health-care")["connection_id"]
    r = client.post(f"/api/connections/{cid}/reconnect")
    callback = simulated_login(client, r.json()["auth_url"])
    done = client.get(callback[len(BASE):], follow_redirects=False)
    assert done.headers["location"].endswith(cid)
    assert len(client.get("/api/connections").json()["connections"]) == 1


def test_notes_bodies_fetched_from_binary(client):
    for inst in ("epic-stanford-health-care", "epic-sutter-health", "epic-duke-health", "epic-cleveland-clinic"):
        connect_institution(client, inst)
    notes = client.get("/api/records", params={"category": "notes", "limit": 500}).json()["items"]
    assert notes
    assert all(n["narrative"] and "CHIEF COMPLAINT" in n["narrative"] for n in notes)
    via_binary = [n for n in notes if any(a.get("url") for a in n["details"].get("attachments", []))]
    assert via_binary, "expected at least one institution to serve notes via Binary"


def test_lab_trends_and_unit_normalization(client):
    connect_institution(client, "epic-stanford-health-care")
    catalog = client.get("/api/observations", params={"category": "labs"}).json()["items"]
    a1c = next(i for i in catalog if i["code"] == "4548-4")
    assert a1c["count"] >= 3
    series = client.get("/api/observations/series", params={"code": "4548-4"}).json()
    assert series["ref_high"] == 5.6
    assert series["points"] == sorted(series["points"], key=lambda p: p["t"])
    weights = client.get("/api/observations/series", params={"code": "29463-7"}).json()
    assert weights["unit"] == "kg"
    assert all(80 < p["v"] < 110 for p in weights["points"])
    bp = client.get("/api/observations/series", params={"code": "85354-6"}).json()
    assert {c["name"] for c in bp["components"]} == {"Systolic blood pressure", "Diastolic blood pressure"}


def test_record_detail_includes_raw_fhir(client):
    connect_institution(client, "epic-stanford-health-care")
    item = client.get("/api/records", params={"category": "conditions"}).json()["items"][0]
    detail = client.get(f"/api/records/{item['id']}").json()
    assert detail["raw"]["resourceType"] == "Condition"


def test_timeline_is_reverse_chronological(client):
    connect_institution(client, "epic-stanford-health-care")
    items = client.get("/api/timeline").json()["items"]
    dates = [i["effective_at"] for i in items]
    assert dates == sorted(dates, reverse=True)


def test_smoking_status_filed_under_survey_is_imported(client):
    cid = connect_institution(client, "epic-stanford-health-care")["connection_id"]
    runs = client.get(f"/api/connections/{cid}").json()
    items = client.get("/api/records", params={"category": "observations"}).json()["items"]
    smoking = [i for i in items if i["title"] == "Tobacco smoking status"]
    assert smoking, runs
    assert smoking[0]["value_text"] == "Never smoked tobacco"


def test_records_for_different_people_are_flagged(client):
    connect_institution(client, "epic-stanford-health-care")
    assert client.get("/api/summary").json()["identity_conflict"] == []
    from app.store import records
    people = records.distinct_people([
        {"full_name": "Camila Maria Lopez", "birth_date": "1987-09-12", "source_name": "Epic"},
        {"full_name": "Camila Lopez", "birth_date": "1987-09-12", "source_name": "Cerner"},
        {"full_name": "Abdul Koepp", "birth_date": "1956-08-03", "source_name": "SMART"}])
    assert [p["sources"] for p in people] == [["Epic", "Cerner"], ["SMART"]]


def test_resync_reports_no_updates_when_nothing_changed(client):
    cid = connect_institution(client, "epic-stanford-health-care")["connection_id"]
    res = client.post(f"/api/connections/{cid}/sync").json()
    assert res["inserted"] == 0 and res["updated"] == 0, res


def test_report_detail_lists_its_results(client):
    connect_institution(client, "epic-stanford-health-care")
    reports = client.get("/api/records", params={"category": "reports", "limit": 200}).json()["items"]
    with_refs = [r for r in reports if client.get(f"/api/records/{r['id']}").json()["details"].get("result_refs")]
    assert with_refs
    detail = client.get(f"/api/records/{with_refs[0]['id']}").json()
    assert detail["results"] and all(x["title"] for x in detail["results"])


def _credentials(cid):
    from app.store import connections
    return connections.get(cid, include_credentials=True)["credentials"]


def _expire(cid):
    from app.store import connections
    connections.update(cid, credentials={**_credentials(cid), "expires_at": 1})  # long past


def test_epic_registers_a_dynamic_client_instead_of_a_refresh_token(client):
    cid = connect_institution(client, "epic-stanford-health-care")["connection_id"]
    creds = _credentials(cid)
    assert "refresh_token" not in creds          # Epic gives public clients none
    dynamic = creds["dynamic_client"]
    assert dynamic["client_id"] and dynamic["kid"] and "PRIVATE KEY" in dynamic["private_key"]
    assert creds["client_id"] == "syntropy-health"  # the app's own id is kept as the software_id

    old_token = creds["access_token"]
    _expire(cid)
    res = client.post(f"/api/connections/{cid}/sync").json()
    assert res["status"] in ("success", "partial"), res
    renewed = _credentials(cid)
    assert renewed["access_token"] != old_token and renewed["expires_at"] > 1
    assert renewed["dynamic_client"] == dynamic
    assert client.get(f"/api/connections/{cid}").json()["status"] == "active"


def test_other_platforms_keep_using_refresh_tokens(client):
    cid = connect_institution(client, "banner-health")["connection_id"]
    creds = _credentials(cid)
    assert creds.get("refresh_token") and "dynamic_client" not in creds
    _expire(cid)
    assert client.post(f"/api/connections/{cid}/sync").json()["status"] in ("success", "partial")


def test_failed_registration_still_connects_then_asks_to_reconnect(client, monkeypatch):
    from app.connectors import smart

    async def refuse(**_):
        raise smart.SmartError("Dynamic client registration failed (403): not enabled for this app")
    monkeypatch.setattr(smart, "register_dynamic_client", refuse)
    cid = connect_institution(client, "epic-duke-health")["connection_id"]
    conn = client.get(f"/api/connections/{cid}").json()
    assert conn["status"] == "active" and conn["record_count"] > 0
    _expire(cid)
    client.post(f"/api/connections/{cid}/sync")
    assert client.get(f"/api/connections/{cid}").json()["status"] == "needs_reauth"


def test_simulator_rejects_forged_and_replayed_assertions(client):
    from app.connectors import smart
    from app.core import security
    cid = connect_institution(client, "epic-stanford-health-care")["connection_id"]
    dynamic = _credentials(cid)["dynamic_client"]
    endpoint = f"{BASE}/sim/oauth/token"
    grant = {"grant_type": smart.JWT_BEARER, "client_id": dynamic["client_id"]}

    assertion = smart.client_assertion(dynamic, endpoint)
    assert client.post("/sim/oauth/token", data={**grant, "assertion": assertion}).status_code == 200
    replay = client.post("/sim/oauth/token", data={**grant, "assertion": assertion})
    assert replay.status_code == 400 and "already used" in replay.json()["error_description"]

    other_key, _ = security.generate_rsa_signing_key()
    forged = smart.client_assertion({**dynamic, "private_key": other_key}, endpoint)
    assert client.post("/sim/oauth/token", data={**grant, "assertion": forged}).status_code == 400
    wrong_aud = smart.client_assertion(dynamic, f"{BASE}/elsewhere/token")
    assert client.post("/sim/oauth/token", data={**grant, "assertion": wrong_aud}).status_code == 400


def test_registration_endpoint_is_derived_next_to_the_token_endpoint():
    from app.connectors.smart import registration_endpoint
    kp = "https://fhir.kp.org/KPPolarisPortal/esb-envlbl/226/oauth2/token"
    assert registration_endpoint({}, kp) == "https://fhir.kp.org/KPPolarisPortal/esb-envlbl/226/oauth2/register"
    assert registration_endpoint({"registration_endpoint": "https://x/reg"}, kp) == "https://x/reg"


def test_scopes_are_fitted_to_what_the_server_offers():
    from app.connectors.smart import fit_scopes
    requested = "openid launch/patient offline_access patient/Patient.read patient/CarePlan.read patient/Observation.read"
    va = ["offline_access", "patient/Patient.read", "patient/Observation.read"]   # no CarePlan, no openid listed
    assert fit_scopes(requested, va) == "openid launch/patient offline_access patient/Patient.read patient/Observation.read"
    assert fit_scopes(requested, ["openid", "fhirUser", "launch"]) == requested      # Epic lists no resource scopes
    assert fit_scopes(requested, None) == requested


def test_record_types_the_patient_did_not_grant_are_skipped_quietly(client, monkeypatch):
    from app import directory
    monkeypatch.setattr(directory, "USCDI_SCOPES", directory.USCDI_SCOPES.replace(" patient/CareTeam.read", ""))
    cid = connect_institution(client, "epic-stanford-health-care")["connection_id"]
    run = client.post(f"/api/connections/{cid}/sync").json()
    assert run["status"] == "success", run
    assert run["queries"]["CareTeam"] == "not-granted"


def test_reconnect_works_after_an_institution_leaves_the_directory(client, monkeypatch):
    from app import directory
    from app.store import connections
    cid = connect_institution(client, "epic-duke-health")["connection_id"]
    connections.update(cid, institution_id="epic-no-longer-listed")
    r = client.post(f"/api/connections/{cid}/reconnect")
    assert r.status_code == 200, r.text
    assert directory.get_institution("epic-no-longer-listed") is None


def test_ecw_sandbox_mode_explains_there_is_no_shared_sandbox(client):
    client.put("/api/settings/platforms/healow", json={"mode": "sandbox", "sandbox_client_id": "abc"})
    r = client.post("/api/connections/ehr", json={"institution_id": "ecw-bdeaed"})
    assert r.status_code == 400 and "Custom FHIR server" in r.text, r.text


def test_epic_uses_syntropys_registered_client_id_for_each_mode(client, monkeypatch):
    from app.core import settings

    assert settings.platform_config("epic")["client_id"] == ""   # simulated needs none
    client.put("/api/settings/developer", json={"enabled": False})  # a new install: production
    cfg = settings.platform_config("epic")
    assert cfg["mode"] == "production"
    assert (cfg["client_id"], cfg["client_id_source"]) == ("eb7944d2-58e9-4805-bae2-8c68fd70cfdf", "default")
    # A reconnect keeps the connection's own mode, and with it that mode's client ID.
    assert settings.platform_config("epic", "sandbox")["client_id"] == "280d55bb-e8a2-45b0-8a7b-2e3826ef5e9c"

    monkeypatch.setenv("EPIC_CLIENT_ID", "from-env")
    monkeypatch.setenv("EPIC_SANDBOX_CLIENT_ID", "sandbox-from-env")
    assert settings.platform_config("epic")["client_id"] == "from-env"
    assert settings.platform_config("epic", "sandbox")["client_id"] == "sandbox-from-env"
    client.put("/api/settings/platforms/epic", json={"sandbox_client_id": "from-settings"})
    assert settings.platform_config("epic", "sandbox")["client_id"] == "from-settings"
    assert settings.platform_config("epic")["client_id"] == "from-env"     # a sandbox ID never reaches production


def test_outside_developer_mode_epic_goes_to_the_health_systems_own_sign_in(client, monkeypatch):
    """A sandbox client ID or mode saved while testing must not send Kaiser to Epic's sandbox (or a test client)."""
    from app.core import settings
    from app.connectors import smart

    settings.set("platform.epic.mode", "sandbox")
    settings.set("platform.epic.client_id", "280d55bb-e8a2-45b0-8a7b-2e3826ef5e9c")   # saved before 1.2: one ID for all modes
    settings.set("platform.cerner.client_id", "my-cerner-sandbox-id")
    settings.set("platform.cerner.mode", "sandbox")
    settings.migrate_client_ids()
    assert settings.get("platform.epic.client_id") is None
    assert settings.get("platform.epic.sandbox_client_id") is None          # the built-in sandbox ID covers it
    assert settings.get("platform.cerner.sandbox_client_id") == "my-cerner-sandbox-id"

    client.put("/api/settings/developer", json={"enabled": False})
    seen = {}

    async def fake_discover(base, simulated=False):
        seen["base"] = base
        return {"authorization_endpoint": "https://fhir.kp.org/oauth2/authorize", "token_endpoint": "https://fhir.kp.org/oauth2/token"}
    monkeypatch.setattr(smart, "discover", fake_discover)

    async def no_kaiser_fix(url):
        return url
    monkeypatch.setattr(smart, "kaiser_sign_in_url", no_kaiser_fix)
    r = client.post("/api/connections/ehr", json={"institution_id": "epic-kaiser-permanente-california-northern"})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "production"
    assert seen["base"].lower().startswith("https://fhir.kp.org/")
    params = parse_qs(urlparse(r.json()["auth_url"]).query)
    assert params["client_id"] == ["eb7944d2-58e9-4805-bae2-8c68fd70cfdf"]

    # Developer mode brings back each platform's own mode and its sandbox client ID.
    client.put("/api/settings/developer", json={"enabled": True})
    cerner = settings.platform_config("cerner")
    assert (cerner["mode"], cerner["client_id"]) == ("sandbox", "my-cerner-sandbox-id")


KAISER_AUTHORIZE = "https://fhir.kp.org/KPPolarisPortal/esb-envlbl/226/oauth2/authorize?client_id=c&state=s"


def _kaiser(location: str, start_status: int) -> tuple[list[str], httpx.MockTransport]:
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.url.host}{request.url.path}")
        if request.url.host == "fhir.kp.org":
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(start_status)
    return seen, httpx.MockTransport(handle)


def test_kaiser_sign_in_skips_its_broken_capitalized_page():
    from app.connectors import smart

    start = "https://healthy.kaiserpermanente.org:443/fhircs/Authentication/OAuth/Start?client_id=c&state=s&aud=https%3a%2f%2fFHIR.KP.ORG%2fx"
    seen, transport = _kaiser(start, 500)
    url = asyncio.run(smart.kaiser_sign_in_url(KAISER_AUTHORIZE, transport))
    assert url == "https://healthy.kaiserpermanente.org/fhircs/authentication/oauth/start?client_id=c&state=s&aud=https%3a%2f%2fFHIR.KP.ORG%2fx"
    assert seen == ["fhir.kp.org/KPPolarisPortal/esb-envlbl/226/oauth2/authorize",
                    "healthy.kaiserpermanente.org/fhircs/Authentication/OAuth/Start"]

    # Once Kaiser fixes the page, or for Washington's separate server, sign-in goes the normal way.
    assert asyncio.run(smart.kaiser_sign_in_url(KAISER_AUTHORIZE, _kaiser(start, 200)[1])) == KAISER_AUTHORIZE
    washington = "https://wa-membervanilla.kaiserpermanente.org:443/mychart-classic/Authentication/OAuth/Start?state=s"
    assert asyncio.run(smart.kaiser_sign_in_url(KAISER_AUTHORIZE, _kaiser(washington, 500)[1])) == KAISER_AUTHORIZE


def test_kaiser_sign_in_check_never_blocks_a_connection():
    from app.connectors import smart

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)
    assert asyncio.run(smart.kaiser_sign_in_url(KAISER_AUTHORIZE, httpx.MockTransport(unreachable))) == KAISER_AUTHORIZE
    other = "https://mychart.example.org/oauth2/authorize?state=s"
    assert asyncio.run(smart.kaiser_sign_in_url(other, httpx.MockTransport(unreachable))) == other


def test_paging_follows_links_whose_host_differs_only_in_case():
    from app.connectors import smart

    base = "https://fhir.kp.org/api/FHIR/R4"
    pages = {"1": "https://FHIR.KP.ORG/api/FHIR/R4/DocumentReference?page=2", "2": None}

    def handle(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        links = [{"relation": "next", "url": pages[page]}] if pages[page] else []
        entry = {"resource": {"resourceType": "DocumentReference", "id": page}}
        return httpx.Response(200, json={"resourceType": "Bundle", "link": links, "entry": [entry]})

    fhir = smart.FhirClient(base, {"access_token": "t"}, "p")

    async def fetch(transport):
        async with httpx.AsyncClient(transport=transport) as client:
            return await fhir.search_all(client, "DocumentReference", {})
    found, status = asyncio.run(fetch(httpx.MockTransport(handle)))
    assert (status, [r["id"] for r in found]) == ("ok", ["1", "2"])

    # A link to another server still stops, so the access token never leaves the patient's health system.
    pages["1"] = "https://elsewhere.example.org/api/FHIR/R4/DocumentReference?page=2"
    found, status = asyncio.run(fetch(httpx.MockTransport(handle)))
    assert (status, [r["id"] for r in found]) == ("truncated", ["1"])


def test_directory_marks_platforms_that_cannot_connect_yet(client):
    client.put("/api/settings/developer", json={"enabled": False})
    res = client.get("/api/directory", params={"q": "medical center", "limit": 200}).json()["results"]
    flags = [r["available"] for r in res]
    assert True in flags and False in flags
    assert {r["platform"] for r in res if r["available"]} == {"epic"}
    featured = client.get("/api/directory/featured").json()["results"]
    assert featured and all(r["available"] for r in featured)
    assert not any(r["platform"] == "smart-health-it" for r in client.get("/api/directory", params={"q": "smart demo"}).json()["results"])
    oracle = client.get("/api/directory", params={"platform": "cerner"}).json()["results"][0]
    r = client.post("/api/connections/ehr", json={"institution_id": oracle["id"]})
    assert r.status_code == 400 and "isn't registered with Oracle Health" in r.json()["detail"]
    r = client.post("/api/connections/ehr", json={"institution_id": "smart-health-it"})
    assert r.status_code == 400 and "developer mode" in r.json()["detail"]


def test_a_health_system_whose_sign_in_failed_the_check_says_so(client, monkeypatch):
    from app import directory

    epic = [i for i in directory._institutions() if i["platform"] == "epic" and i.get("fhir_base_url")]
    broken, working = epic[0], epic[1]
    key = broken["fhir_base_url"].strip().rstrip("/").lower()
    monkeypatch.setattr(directory, "_sign_in_status", lambda: {
        key: {"status": "portal_error", "since": "2026-10-03", "instead": [working["id"], "gone-from-directory"]}})
    client.put("/api/settings/developer", json={"enabled": False})

    found = client.get(f"/api/directory/{broken['id']}").json()
    assert found["sign_in_problem"] == {"status": "portal_error", "since": "2026-10-03",
                                        "instead": [{"id": working["id"], "name": working["name"]}]}
    assert client.get(f"/api/directory/{working['id']}").json()["sign_in_problem"] is None
    assert client.get("/api/directory/not-a-real-id").status_code == 404
