"""scripts/check_sign_in.py against made-up health systems (no network)."""

import asyncio
import json
from typing import Callable

import httpx

from scripts.check_sign_in import RECORDED, check_server

BASE = "https://fhir.example.org/api/FHIR/R4"
AUTHORIZE = "https://fhir.example.org/oauth2/authorize"
RELAY = "https://health.syntropylabs.io/callback"
LOGIN = '<html><head><title>MyChart - Login Page</title></head><body><input type="password" name="pw"></body></html>'


def run(pages: Callable[[httpx.Request], httpx.Response]):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/smart-configuration"):
            return httpx.Response(200, json={"authorization_endpoint": AUTHORIZE, "token_endpoint": AUTHORIZE + "/token"})
        return pages(request)
    return asyncio.run(check_server(BASE, "client", RELAY, httpx.MockTransport(handle)))


def portal(start_status: int = 302, start_body: str = ""):
    def pages(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/oauth2/authorize":
            return httpx.Response(302, headers={"Location": "https://mychart.example.org/MyChart/Authentication/OAuth/Start?x=1"})
        if path == "/MyChart/Authentication/OAuth/Start":
            if start_status != 302:
                return httpx.Response(start_status, text=start_body)
            return httpx.Response(302, headers={"Location": "/MyChart/Authentication/Login"})
        if path.lower() == "/mychart/authentication/oauth/start":
            return httpx.Response(302, headers={"Location": "/MyChart/Authentication/Login"})
        if path == "/MyChart/Authentication/Login":
            return httpx.Response(200, text=LOGIN)
        return httpx.Response(404)
    return pages


def test_a_health_system_that_reaches_its_sign_in_passes():
    result = run(portal())
    assert result.status == "ok", result
    assert result.hops == ["302 fhir.example.org/oauth2/authorize", "302 mychart.example.org/MyChart/Authentication/OAuth/Start",
                           "200 mychart.example.org/MyChart/Authentication/Login"]


def test_a_failing_page_on_the_way_is_caught_like_kaisers():
    result = run(portal(500, "<title>Page Not Found</title>"))
    assert result.status == "portal_error"
    assert result.detail == ("HTTP 500 at mychart.example.org/MyChart/Authentication/OAuth/Start (Page Not Found); "
                             "the lowercase path answers 302")


def test_an_error_page_that_answers_200_is_not_a_pass():
    assert run(portal(200, "<title>Error</title><p>Something went wrong.</p>")).status == "error_page"


def test_a_health_system_that_doesnt_know_our_client_id_yet():
    def pages(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<title>OAuth2 Error</title><p>Invalid client.</p>")
    assert run(pages).status == "client_unknown"


def test_an_oauth_error_sent_to_our_redirect_address():
    def pages(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": f"{RELAY}?error=unauthorized_client&state=sign-in-check"})
    result = run(pages)
    assert result.status == "rejected" and "unauthorized_client" in result.detail


def test_script_redirects_are_followed_as_a_browser_would():
    def pages(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/authorize":
            return httpx.Response(302, headers={"Location": "https://mychart.example.org/bye.asp"})
        if request.url.path == "/bye.asp":
            return httpx.Response(200, text="<script>top.location='./inside.asp?mode=oauthlogin';</script>")
        return httpx.Response(200, text=LOGIN)
    result = run(pages)
    assert result.status == "ok"
    assert result.hops[-1] == "200 mychart.example.org/inside.asp"


def test_fallbacks_for_browsers_without_scripts_are_not_followed():
    def pages(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/authorize":
            return httpx.Response(302, headers={"Location": "https://mychart.example.org/OpenId"})
        if request.url.path == "/OpenId":
            return httpx.Response(200, text='<noscript><meta http-equiv="refresh" content="0;url=/nojs.asp" /></noscript>'
                                            '<script src="/openid.js"></script>')
        return httpx.Response(302, headers={"Location": "https://mychart.example.org/OpenId"})
    result = run(pages)
    assert result.status == "unclear" and result.hops[-1] == "200 mychart.example.org/OpenId"


def test_a_sign_in_site_that_turns_scripts_away_is_blocked_not_broken():
    def pages(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/authorize":
            return httpx.Response(302, headers={"Location": "https://mychart.example.org/MyChart/Authentication/OAuth/Start"})
        return httpx.Response(403, text="<title>Access Denied</title>")
    result = run(pages)
    assert (result.status, result.detail) == ("blocked", "HTTP 403 at mychart.example.org/MyChart/Authentication/OAuth/Start (Access Denied)")


def test_an_unreachable_server_is_reported_after_retrying(monkeypatch):
    from scripts import check_sign_in
    monkeypatch.setattr(check_sign_in, "RETRY_DELAYS", (0, 0))
    attempts = []

    def handle(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        raise httpx.ConnectError("offline", request=request)
    result = asyncio.run(check_server(BASE, "client", RELAY, httpx.MockTransport(handle)))
    assert result.status == "unreachable" and "ConnectError" in result.detail
    assert len(attempts) == 3


def test_an_error_page_titled_mychart_is_not_a_sign_in():
    def pages(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/authorize":
            return httpx.Response(302, headers={"Location": "https://mychart.example.org/app/error"})
        return httpx.Response(200, text="<title>MyChart - Application Error Page</title><p>Please log in again.</p>")
    assert run(pages).status == "error_page"


def test_pages_chrome_ends_on_are_classified():
    from scripts.check_sign_in import classify_page

    def page(**overrides):
        args = {"status": 200, "url": "https://mychart.example.org/MyChart/Authentication/Login", "title": "MyChart - Login",
                "has_password": True, "text": "", "redirect_host": "health.syntropylabs.io", **overrides}
        return classify_page(**args)[0]

    assert page() == "ok"
    assert page(title="Log in to Mayo Clinic", url="https://account.mayoclinic.org/login") == "ok"
    assert page(status=403, title="Access Denied", has_password=False) == "blocked"
    assert page(status=404, title="", has_password=False) == "portal_error"
    assert page(title="MyChart - Application Error Page", has_password=False) == "error_page"
    assert page(url="https://health.syntropylabs.io/callback?error=access_denied") == "rejected"
    assert page(title="", has_password=False, text="<div id=app></div>") == "unclear"
    assert page(title="Loading https://idp.example.org/saml2/sso/redirect", has_password=False,
                text="<script>if (error) showError()</script>") == "unclear"


def test_the_status_file_records_failures_since_first_seen_and_working_listings():
    from scripts.check_sign_in import Result, sign_in_status

    entries = [{"id": "upmc", "name": "UPMC", "fhir_base_url": "https://broken.example.org/FHIR/"},
               {"id": "upmc-portal", "name": "UPMC Patient Portal", "fhir_base_url": "https://works.example.org/FHIR"},
               {"id": "upmc-central", "name": "UPMC Central PA", "fhir_base_url": "https://pending.example.org/FHIR"},
               {"id": "mayo", "name": "Mayo Clinic", "fhir_base_url": "https://mayo.example.org/FHIR"}]
    results = [Result("portal_error", "404", "https://broken.example.org/FHIR"),
               Result("ok", "", "https://works.example.org/FHIR"),
               Result("client_unknown", "", "https://pending.example.org/FHIR"),
               Result("blocked", "403", "https://mayo.example.org/FHIR")]
    previous = {"servers": {"https://broken.example.org/fhir": {"status": "portal_error", "since": "2026-09-28"},
                            "https://pending.example.org/fhir": {"status": "unreachable", "since": "2026-09-28"}}}
    assert sign_in_status(results, entries, previous, "2026-10-05") == {"checked": "2026-10-05", "servers": {
        "https://broken.example.org/fhir": {"status": "portal_error", "since": "2026-09-28", "instead": ["upmc-portal"]},
        "https://pending.example.org/fhir": {"status": "client_unknown", "since": "2026-10-05"},
    }, "watching": {}}


def test_a_server_that_does_not_answer_is_recorded_the_second_week():
    """One bad night at a hospital, or for the runner, shouldn't warn everyone for a week."""
    from scripts.check_sign_in import Result, sign_in_status

    entries = [{"id": "a", "name": "A", "fhir_base_url": "https://a.example.org/r4"}]
    down = [Result("unreachable", "timeout", "https://a.example.org/r4")]
    first = sign_in_status(down, entries, {"servers": {}}, "2026-10-05")
    assert first["servers"] == {} and first["watching"] == {"https://a.example.org/r4": {"status": "unreachable", "since": "2026-10-05"}}
    second = sign_in_status(down, entries, first, "2026-10-12")
    assert second["servers"] == {"https://a.example.org/r4": {"status": "unreachable", "since": "2026-10-05"}} and second["watching"] == {}
    third = sign_in_status(down, entries, second, "2026-10-19")
    assert third["servers"] == second["servers"]
    back = [Result("ok", "", "https://a.example.org/r4")]
    assert sign_in_status(back, entries, first, "2026-10-12") == {"checked": "2026-10-12", "servers": {}, "watching": {}}


def test_a_refusal_at_smart_discovery_is_blocked_not_unreachable():
    """A hospital firewall turning away cloud addresses says nothing about what happens from someone's home."""
    for status in (401, 403, 429):
        def handle(request, status=status):
            return httpx.Response(status)
        result = asyncio.run(check_server(BASE, "client", RELAY, httpx.MockTransport(handle)))
        assert result.status == "blocked" and str(status) in result.detail and result.status not in RECORDED


def test_other_errors_at_smart_discovery_are_a_broken_server(monkeypatch):
    monkeypatch.setattr("scripts.check_sign_in.RETRY_DELAYS", ())
    for status in (404, 500):
        def handle(request, status=status):
            return httpx.Response(status)
        result = asyncio.run(check_server(BASE, "client", RELAY, httpx.MockTransport(handle)))
        assert result.status == "portal_error" and str(status) in result.detail


def test_write_status_only_fails_when_the_check_itself_breaks(monkeypatch, tmp_path):
    import scripts.check_sign_in as check
    status_file = tmp_path / "status.json"
    monkeypatch.setattr(check, "STATUS_FILE", status_file)
    monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check, "health_system_servers", lambda *a: {"https://a.example.org": ("epic", ["A"])})
    async def fails(servers, redirect):
        return [check.Result("portal_error", "500", "https://a.example.org", ["A"], platform="epic")]
    monkeypatch.setattr(check, "check_all", fails)
    monkeypatch.setattr(check, "_entries", lambda *a: [{"id": "a", "name": "A", "fhir_base_url": "https://a.example.org"}])
    assert check.main(["--write-status", "--no-browser"]) == 0
    saved = json.loads(status_file.read_text())
    assert saved["checked"] and saved["servers"].get("https://a.example.org") is not None
    assert check.main(["--no-browser"]) == 1       # asked by hand, a failure is still reported


def test_oracle_health_s_script_page_moves_on_to_its_session_service():
    props = ('{&#34;sessionServiceUri&#34;:&#34;https://fhir.example.org/session-api/realm/t-ch?to=x&amp;forceAuthn=true&#34;,'
             '&#34;clientId&#34;:&#34;client&#34;}')

    def pages(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/authorize":
            return httpx.Response(200, text=f'<title>Loading...</title><script id="bootstrap-app" '
                                            f'data-page-id="IdentityProviderRedirectPage" data-page-properties="{props}">'
                                            '</script><div id="reactRoot">Loading...</div>')
        if request.url.path == "/session-api/realm/t-ch":
            assert request.url.params["forceAuthn"] == "true"
            return httpx.Response(303, headers={"Location": "https://idp.example.org/saml2/sso/login"})
        return httpx.Response(200, text="<title>Example Hospital - Sign In</title><input type=password>")
    result = run(pages)
    assert result.status == "ok" and result.hops[-1] == "200 idp.example.org/saml2/sso/login"


def test_oracle_health_s_redirect_for_a_client_it_does_not_know():
    def pages(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/authorize":
            return httpx.Response(303, headers={"Location": "/errors/urn%3Acerner%3Aerror%3Aauthorization-server%3Aoauth2"
                                                            "%3Agrant%3Aunknown-client/instances/1"})
        return httpx.Response(200, text="<title>Authorization Server</title>")
    assert run(pages).status == "client_unknown"


def test_an_eclinicalworks_practice_without_patient_access_is_an_error_page():
    def pages(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/authorize":
            return httpx.Response(302, headers={"Location": "https://connect4.example.org/apps/jsp/fhir/error.jsp"})
        return httpx.Response(200, text="<title>healow - Invalid Request</title><p>The page you are looking for cannot be "
                                        "displayed because of an invalid request.</p>")
    assert run(pages).status == "error_page"
