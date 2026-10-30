"""What a page may do in the app's window (desktop/sites.py), and the Windows window's handling of WebView2's events
(desktop/windows_window.py), checked against stand-ins since WebView2 only exists on Windows."""

from __future__ import annotations

import json
import sys
import threading
import webbrowser
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))
import own_pages  # noqa: E402
import sites  # noqa: E402
import windows_window  # noqa: E402


@pytest.fixture
def opened(monkeypatch):
    """webbrowser.open, guarded the way the app guards it, recording what got through."""
    got: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: (got.append(url), True)[1])
    sites.guard_external_links()
    return got


@pytest.mark.parametrize("url", ["ssh://evil.example/", "smb://evil.example/share", "vscode://file/etc/hosts", "javascript:alert(1)",
                                 "file:///etc/passwd", "x-apple.systempreferences:com.apple.preference.security",
                                 "ms-settings:", "search-ms:query=x", "calculator:", "", "data:text/html,x"])
def test_only_web_and_mail_links_leave_the_window(opened, url):
    assert webbrowser.open(url) is False
    assert opened == []


@pytest.mark.parametrize("url", ["https://health.syntropylabs.io/docs/", "http://localhost:8000", "HTTPS://example.test", "mailto:a@example.test"])
def test_web_and_mail_links_still_open(opened, url):
    assert webbrowser.open(url) is True
    assert opened == [url]


def test_a_pages_link_also_takes_a_click(opened):
    acted = {"now": False}
    pages = sites.only_when_clicked(lambda: acted["now"])
    assert pages.open("https://example.test/") is False
    acted["now"] = True
    assert pages.open("https://example.test/") is True
    assert pages.open("ssh://evil.example/") is False
    assert opened == ["https://example.test/"]


HOME = "http://localhost:8000"


@pytest.mark.parametrize("url,away", [
    ("http://localhost:8000/#/sources", False), ("http://127.0.0.1:8000/", False),
    ("http://localhost:8001/", True), ("https://localhost:8000/", True), ("https://mychart.example.org/login", True),
    ("blob:http://evil.example/abc", True), ("blob:http://localhost:8000/abc", True),
    ("about:blank", True), ("data:text/html,hello", True), ("file:///etc/passwd", True), ("http://[bad", True), (None, False)])
def test_anything_but_the_apps_own_origin_is_another_site(url, away):
    assert sites.Location(lambda: HOME).is_away(url) is away


def test_the_apps_own_page_counts_as_home_only_until_the_next_address():
    location = sites.Location(lambda: HOME)
    location.showing_own_page()
    assert location.is_away("about:blank") is False       # Windows reports this for a page the app wrote
    assert location.is_away("file:///Applications/Syntropy Health.app/") is False       # and a Mac this
    assert location.is_away("https://mychart.example.org/") is True
    assert location.is_away("about:blank") is True        # that site, sending itself to a blank page


def test_joined_server_is_home_and_this_computer_isnt():
    location = sites.Location(lambda: "http://mac-mini.tail1234.ts.net:8000")
    assert location.is_away("http://mac-mini.tail1234.ts.net:8000/#/overview") is False
    assert location.is_away("http://localhost:8000/") is True


@pytest.mark.parametrize("url,name", [("https://MyChart.example.org/login?a=b", "mychart.example.org"),
                                      ("blob:https://evil.example/abc", "evil.example"),
                                      ("about:blank", "A page with no address"), ("data:text/html,x", "A page with no address")])
def test_the_title_names_the_site(url, name):
    assert sites.site_name(url) == name


def test_own_page_token():
    assert sites.is_own_page_token(sites.OWN_PAGE_TOKEN)
    for wrong in (None, "", "x", 3, [sites.OWN_PAGE_TOKEN]):
        assert not sites.is_own_page_token(wrong)


def test_own_pages_reach_the_app_on_both_systems_and_escape_what_they_show():
    page = own_pages.problem_page("<script>bad</script>", "damaged", "C:\\Users\\a&b", "x.log")
    assert "<script>bad" not in page and "a&amp;b" in page
    assert sites.OWN_PAGE_TOKEN in page and "webkit.messageHandlers.syntropyDesktop" in page and "chrome.webview.postMessage" in page
    for action in ("problem-retry", "problem-restore", "problem-fresh", "problem-folder", "problem-quit"):
        assert f"send('{action}')" in page
    newer = own_pages.problem_page("x", "too_new", "f", "l")
    assert "problem-latest" in newer and "problem-restore" not in newer and "problem-fresh" not in newer


# ---------------------------------------------------------------------------
# Windows, against stand-ins for WebView2
# ---------------------------------------------------------------------------

class FakeEdge:
    def __init__(self):
        self.url = None
        self.alerts = []
        self.pywebview_calls = 0

    def on_webview_ready(self, sender, args):
        self.ready = True

    def on_source_changed(self, sender, args):
        self.url = sender.Source

    def on_script_notify(self, sender, args):
        self.alerts.append(args.get_WebMessageAsJson())

    def on_new_window_request(self, sender, args):
        raise AssertionError("pywebview's handler opens anything")


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


@pytest.fixture
def edge(monkeypatch):
    got = SimpleNamespace(sources=[], own=[], module=SimpleNamespace(js_bridge_call="pywebview's"), done=threading.Event())

    def own(message):
        got.own.append(message)
        got.done.set()

    cls = type("Edge", (FakeEdge,), {})
    windows_window.guard_edge(cls, got.module, got.sources.append, own)
    got.instance = cls()
    return got


def _message(value):
    return SimpleNamespace(get_WebMessageAsJson=lambda: json.dumps(value))


def _window_request(url, user):
    handled = []
    return handled, SimpleNamespace(set_Handled=handled.append, get_Uri=lambda: url, IsUserInitiated=user)


def test_windows_bridge_is_cut_off(edge):
    assert edge.module.js_bridge_call is sites.drop_bridge_call
    edge.instance.on_script_notify(None, _message(["pywebviewMoveWindow", "[3, 3]", "x"]))
    edge.instance.on_script_notify(None, _message(json.dumps({"type": "problem-quit"})))
    edge.instance.on_script_notify(None, _message(json.dumps({"type": "problem-quit", "token": "guess"})))
    assert edge.own == [] and edge.instance.alerts == []


def test_windows_listens_to_its_own_pages(edge):
    edge.instance.on_script_notify(None, _message(json.dumps({"type": "go-home", "token": sites.OWN_PAGE_TOKEN})))
    assert edge.done.wait(3)
    assert edge.own == [{"type": "go-home", "_own_page": True}]


def test_windows_keeps_alert_for_sign_in_pages(edge):
    edge.instance.on_script_notify(None, _message(["_pywebviewAlert", "\"Wrong password\"", "alert"]))
    assert len(edge.instance.alerts) == 1


def test_windows_new_window_needs_a_click_and_a_safe_scheme(edge, opened):
    handled, args = _window_request("https://example.test/script", False)
    edge.instance.on_new_window_request(None, args)
    assert handled == [True] and opened == []
    _, args = _window_request("https://example.test/clicked", True)
    edge.instance.on_new_window_request(None, args)
    _, args = _window_request("ssh://evil.example/", True)
    edge.instance.on_new_window_request(None, args)
    assert opened == ["https://example.test/clicked"]


def test_windows_refuses_external_schemes(edge):
    core = SimpleNamespace(LaunchingExternalUriScheme=Event())
    edge.instance.on_webview_ready(SimpleNamespace(CoreWebView2=core), SimpleNamespace(IsSuccess=True))
    args = SimpleNamespace(Cancel=False)
    core.LaunchingExternalUriScheme.handlers[0](None, args)
    assert args.Cancel is True


def test_windows_ready_survives_a_runtime_without_that_event(edge):
    edge.instance.on_webview_ready(SimpleNamespace(CoreWebView2=SimpleNamespace()), SimpleNamespace(IsSuccess=True))
    assert edge.instance.ready


def test_windows_reports_each_address(edge):
    edge.instance.on_source_changed(SimpleNamespace(Source="https://mychart.example.org/"), None)
    assert edge.sources == ["https://mychart.example.org/"] and edge.instance.url == "https://mychart.example.org/"


def test_windows_title_names_the_site_and_comes_back(monkeypatch):
    titles, loaded, bars, changes = [], [], [], []
    window = SimpleNamespace(set_title=titles.append, load_url=loaded.append, uid="x")
    win = windows_window.WindowsWindow(window, sites.Location(lambda: HOME, own_page=True), lambda: HOME, lambda m: None, lambda: changes.append(1))
    monkeypatch.setattr(win, "show_bar", bars.append)
    win.apply("about:blank")                                  # the starting page
    assert titles[-1] == "Syntropy Health" and not win.away and bars[-1] is None and not changes
    win.apply("https://mychart.example.org/login")
    assert titles[-1] == "mychart.example.org - Syntropy Health" and win.away and bars[-1] == "mychart.example.org" and changes == [1]
    win.go_home()
    assert loaded == [f"{HOME}/#/sources"]
    win.apply(f"{HOME}/#/sources")
    assert titles[-1] == "Syntropy Health" and not win.away and bars[-1] is None and changes == [1, 1]


def test_windows_bar_trouble_doesnt_stop_the_title(monkeypatch):
    titles = []
    win = windows_window.WindowsWindow(SimpleNamespace(set_title=titles.append), sites.Location(lambda: HOME), lambda: HOME,
                                       lambda m: None, lambda: None)

    def broken(_name):
        raise OSError("no WinForms here")

    monkeypatch.setattr(win, "show_bar", broken)
    win.apply("https://mychart.example.org/")
    assert titles == ["mychart.example.org - Syntropy Health"]
