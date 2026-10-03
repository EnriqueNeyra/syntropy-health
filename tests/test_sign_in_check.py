"""scripts/check_sign_in.py against made-up health systems (no network)."""

import asyncio
from typing import Callable

import httpx

from scripts.check_sign_in import check_server

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
