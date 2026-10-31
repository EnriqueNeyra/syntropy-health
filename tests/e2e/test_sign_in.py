"""Leaving for another site's sign-in and coming back, through the real dialog and the real callback, with a stand-in portal
on this computer. This is what a browser does, what the Windows app does (the same page, in Chromium) and what the phone
apps' web views do. The Mac app's sheet has its own tests (tests/test_desktop_window.py)."""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest

from tests.e2e.conftest import CSRF, PASSWORD, WIDTHS, Server
from tests.e2e.test_bridge import STAND_IN
from tests.e2e.test_bridge import UA as PHONE_UA

MAC_UA = "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15 SyntropyHealthDesktop/1.0 (Mac)"
WINDOWS_UA = "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36 SyntropyHealthDesktop/1.0 (Windows)"
# Once the page starts to go the browser can't be asked about it, so the page keeps a note of what it showed. (Where
# scroll bars take space their room is kept, and shows the same background: see scrollbar-gutter in app.css.)
NOTE_THE_MESSAGE = """
new MutationObserver(() => {
  const box = document.querySelector(".leaving");
  if (!box || sessionStorage.getItem("leaving")) return;
  const r = box.getBoundingClientRect();
  sessionStorage.setItem("leaving", JSON.stringify({ text: box.innerText, animation: getComputedStyle(box).animationName, covers: r.top <= 0 && r.left <= 0 &&
    r.bottom >= window.innerHeight && r.right >= window.innerWidth - 20, at: location.href }));
}).observe(document, { childList: true, subtree: true });
"""


def when_the_message_shows(act: str) -> str:
    """Runs in the page the moment the message appears: from outside, the moment can be missed, and the browser can't be
    asked anything once the page has started to go."""
    return f"""new MutationObserver(() => {{
      const box = document.querySelector(".leaving");
      if (box && !window.acted) {{ window.acted = true; {act} }}
    }}).observe(document, {{ childList: true, subtree: true }});"""


class Portal:
    """A health system's FHIR discovery and sign-in page, answering as slowly as a test asks."""

    def __init__(self) -> None:
        self.delay = 0.0
        self.asked = 0
        self.home = ""
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                parts = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(parts.query))
                if parts.path.endswith("/.well-known/smart-configuration"):
                    body = json.dumps({"authorization_endpoint": f"{outer.url}/authorize", "token_endpoint": f"{outer.url}/token"}).encode()
                    self.answer(body, "application/json")
                elif parts.path == "/authorize":
                    outer.asked += 1
                    time.sleep(outer.delay)
                    state = urllib.parse.quote(query.get("state", ""))
                    self.answer(f'<!doctype html><title>Portal</title><a id=deny href="/finish?state={state}">Deny</a>'.encode())
                elif parts.path == "/finish":
                    self.send_response(303)
                    self.send_header("Location", f"{outer.home}/callback?error=access_denied&state={urllib.parse.quote(query.get('state', ''))}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.send_error(404)

            def answer(self, body: bytes, kind: str = "text/html") -> None:
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:       # the page stopped waiting, which some of these tests are about
                    pass

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def portal(server: Server) -> Iterator[Portal]:
    p = Portal()
    p.home = server.url
    yield p
    p.server.shutdown()


@pytest.fixture
def opened(browser: Any, server: Server) -> Iterator[Any]:
    """A signed-in page, in a browser allowed to leave the server, on Sources with a choice of who it's pretending to be."""
    contexts = []

    def open_page(user_agent: str | None = None, width: str = "desktop", init: str | None = None) -> Any:
        w, h = WIDTHS[width]
        ctx = browser.new_context(viewport={"width": w, "height": h}, service_workers="block", **({"user_agent": user_agent} if user_agent else {}))
        contexts.append(ctx)
        if init:
            ctx.add_init_script(init)
        assert ctx.request.post(f"{server.url}/api/auth/login", data={"password": PASSWORD}, headers=CSRF).ok
        page = ctx.new_page()
        page.set_default_timeout(10000)
        page.goto(f"{server.url}/#/sources")
        page.wait_for_selector("#view:not([aria-busy])")
        return page

    yield open_page
    for ctx in contexts:
        ctx.close()


def start_connecting(page: Any, portal: Portal) -> None:
    """Add a source, by its address, as a person would."""
    page.evaluate("location.hash = '#/sources?add=ehr'")
    page.locator("[data-action=custom]").click()
    page.locator("#c-url").fill(f"{portal.url}/fhir")
    page.locator("#c-client").fill("test-client")
    page.locator("[data-action=custom-go]").click()


def the_message(page: Any) -> dict[str, Any]:
    return json.loads(page.evaluate("sessionStorage.getItem('leaving')"))


def test_leaving_says_where_to_before_going_and_coming_back_fades_in(opened, portal, server):
    page = opened(init=NOTE_THE_MESSAGE)
    start_connecting(page, portal)
    page.wait_for_url(f"{portal.url}/**")

    page.locator("#deny").click()
    page.wait_for_url(f"{server.url}/#/sources*")
    page.locator(".banner.bad").wait_for()
    shown = the_message(page)
    assert "Taking you to 127.0.0.1 to sign in." in shown["text"]
    assert "You'll come back here when you're done." in shown["text"]
    assert shown["at"].startswith(server.url) and shown["animation"] == "fade-in" and shown["covers"] is True
    assert "Access was not granted" in page.locator(".banner.bad").inner_text()
    assert page.evaluate("document.documentElement.hasAttribute('data-arriving')")
    assert page.evaluate("getComputedStyle(document.body).animationName") == "fade-in"
    assert page.locator(".leaving").count() == 0


def test_an_ordinary_load_of_the_app_doesnt_fade_in(opened):
    page = opened()
    assert not page.evaluate("document.documentElement.hasAttribute('data-arriving')")


BUTTON_BACK = "[data-action=custom-go]:not([disabled])"


def test_a_slow_site_can_be_abandoned_and_the_page_is_as_it_was(opened, portal, server):
    portal.delay = 8
    page = opened(init=when_the_message_shows("setTimeout(() => box.querySelector('button').click(), 900);"))
    start_connecting(page, portal)
    page.locator(BUTTON_BACK).wait_for()     # it has been on its way to the site for most of a second by then
    assert portal.asked == 1
    assert page.locator(".leaving").count() == 0
    page.wait_for_timeout(300)
    assert page.url.startswith(server.url)


def test_staying_before_the_page_has_gone_means_it_never_goes(opened, portal, server):
    page = opened(init=when_the_message_shows("box.querySelector('button').click();"))
    start_connecting(page, portal)
    page.locator(BUTTON_BACK).wait_for()
    page.wait_for_timeout(600)      # past when it would have gone
    assert portal.asked == 0
    assert page.url.startswith(server.url) and page.locator(".leaving").count() == 0


def test_the_browsers_back_button_brings_the_page_back_without_the_message(opened, portal, server):
    # What a browser does on Back when it kept the page: the same page, with everything still on it.
    page = opened(init=when_the_message_shows("window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));"))
    start_connecting(page, portal)
    page.locator(BUTTON_BACK).wait_for()
    assert page.locator(".leaving").count() == 0
    assert page.url.startswith(server.url)


def test_the_windows_app_gets_the_same_step(opened, portal, server):
    page = opened(WINDOWS_UA, init=NOTE_THE_MESSAGE)
    start_connecting(page, portal)
    page.wait_for_url(f"{portal.url}/**")
    page.locator("#deny").click()
    page.wait_for_url(f"{server.url}/#/sources*")
    assert "Taking you to 127.0.0.1 to sign in." in the_message(page)["text"]


def test_inside_the_phone_app_the_same_step_works_and_comes_back(opened, portal, server):
    page = opened(PHONE_UA, "phone", STAND_IN + NOTE_THE_MESSAGE)
    assert page.evaluate("document.documentElement.hasAttribute('data-native-nav')")
    start_connecting(page, portal)
    page.wait_for_url(f"{portal.url}/**")
    page.locator("#deny").click()
    page.wait_for_url(f"{server.url}/#/sources*")
    page.locator(".banner.bad").wait_for()
    shown = the_message(page)
    assert "Taking you to 127.0.0.1 to sign in." in shown["text"] and shown["covers"] is True
    assert page.evaluate("document.documentElement.hasAttribute('data-native-nav')")
    assert [m for m in page.evaluate("window.__sent") if m["type"] == "page"]


def test_the_mac_app_leaves_it_to_the_sheet(opened, portal, server):
    page = opened(MAC_UA, init=NOTE_THE_MESSAGE)
    assert page.evaluate("document.documentElement.getAttribute('data-shell')") == "mac"
    start_connecting(page, portal)
    page.wait_for_url(f"{portal.url}/**")
    page.locator("#deny").click()
    page.wait_for_url(f"{server.url}/#/sources*")
    page.locator(".banner.bad").wait_for()
    assert page.evaluate("sessionStorage.getItem('leaving')") is None


def test_the_mac_window_gives_the_page_its_button_back_once(opened):
    page = opened(MAC_UA)
    page.evaluate("""import('/static/js/sign-in.js').then((m) => {
      window.restored = 0;
      m.leaveForSignIn('#/sources', () => { window.restored++; });
    })""")
    for _ in range(2):
        page.evaluate("window.dispatchEvent(new CustomEvent('syntropy-sign-in-closed'))")
    assert page.evaluate("window.restored") == 1
