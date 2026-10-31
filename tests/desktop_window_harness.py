"""Run by tests/test_desktop_window.py in its own process: the real MacWindow and pywebview around a real WKWebView, with
two local sites standing in for the app and for a health system's sign-in page. Writes what happened as JSON."""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "desktop")]
HOME_PORT, AWAY_PORT = int(sys.argv[2]), int(sys.argv[3])
results: dict = {}


class Pages(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/backup.bin", "/plain.bin"):
            ok = "sid=abc" in self.headers.get("Cookie", "")
            body = b"NEW-BACKUP" if ok else b'{"detail":"Not authenticated"}'
            if not ok:
                results["download_without_cookie"] = True
            self.send_response(200 if ok else 401)
            self.send_header("Content-Type", "application/vnd.sqlite3")
            self.send_header("Content-Disposition", 'attachment; filename="backup.bin"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/broken.bin":
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.sqlite3")
            self.send_header("Content-Disposition", 'attachment; filename="broken.bin"')
            self.send_header("Content-Length", "100000")
            self.end_headers()
            self.wfile.write(b"partial")
            self.wfile.flush()
            self.connection.close()
            return
        body = b"<!doctype html><title>page</title><body><a id=link>link</a></body>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        if path == "/own":
            self.send_header("Set-Cookie", "sid=abc; Path=/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


for port in (HOME_PORT, AWAY_PORT):
    threading.Thread(target=http.server.ThreadingHTTPServer(("127.0.0.1", port), Pages).serve_forever, daemon=True).start()

import webbrowser  # noqa: E402

import AppKit  # noqa: E402
import webview  # noqa: E402
from PyObjCTools import AppHelper  # noqa: E402

opened: list[str] = []
webbrowser.open = lambda url, *a, **k: (opened.append(url), True)[1]
webview.settings["ALLOW_DOWNLOADS"] = True

import sites  # noqa: E402
from mac_window import MacWindow  # noqa: E402


class Prefs:
    def get(self, _key, default=None):
        return default

    def number(self, _key, default, _low, _high):
        return default

    def set(self, _key, _value):
        pass


class Panel:
    dest = ""
    answer = AppKit.NSFileHandlingPanelOKButton
    asked = 0

    def setDirectoryURL_(self, _):  # noqa: N802
        pass

    def setNameFieldStringValue_(self, _):  # noqa: N802
        pass

    def setTitle_(self, _):  # noqa: N802
        pass

    def runModal(self):  # noqa: N802
        Panel.asked += 1
        return Panel.answer

    def filename(self):
        return Panel.dest


class Panels:
    @staticmethod
    def savePanel():  # noqa: N802
        return Panel()


AppKit.NSSavePanel = Panels
HOME, AWAY = f"http://localhost:{HOME_PORT}", f"http://127.0.0.1:{AWAY_PORT}"
requests: list[dict] = []
failures: list[tuple] = []
sites.guard_external_links()
window = webview.create_window("Syntropy Health", f"{HOME}/own", width=900, height=600, hidden=True)
location = sites.Location(lambda: HOME)
mac = MacWindow(window, background=True, prefs=Prefs(),
                tray_actions={k: (lambda: None) for k in ("browser", "lan", "switch", "updates")},
                dock_menu_state=lambda: {}, on_request=requests.append, home_url=lambda: HOME, location=location,
                on_load_failed=lambda *a: failures.append(a),
                on_download=lambda *a: results.setdefault("reported", []).append(list(a)))


def on_main(fn):
    done = threading.Event()
    box = {}

    def run():
        try:
            box["value"] = fn()
        finally:
            done.set()

    AppHelper.callAfter(run)
    done.wait(10)
    return box.get("value")


def js(code, wait=1.0):
    on_main(lambda: mac.run_js(code))
    time.sleep(wait)


def url():
    return on_main(lambda: str(mac.webview.URL().absoluteString()) if mac.webview.URL() is not None else None)


def state():
    return {"url": url(), "away": mac.away, "title": on_main(lambda: str(mac.ns_window.title())),
            "back": on_main(lambda: any(str(i.itemIdentifier()) == "syntropy.back" for i in mac.ns_window.toolbar().items()))}


def until(check, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.1)


def go_to_away():
    window.load_url(f"{AWAY}/foreign")
    until(lambda: (url() or "").startswith(AWAY) and mac.away)
    time.sleep(0.3)


def post(payload):
    return f"window.webkit.messageHandlers.syntropyDesktop.postMessage(JSON.stringify({json.dumps(payload)}));"


def script_link(target):
    return f"var a=document.createElement('a');a.href={json.dumps(target)};a.target='_blank';document.body.appendChild(a);a.click();"


def click_for_real():
    """A person's click, as the app's event loop sees it."""
    def post_click():
        for kind in (AppKit.NSEventTypeLeftMouseDown, AppKit.NSEventTypeLeftMouseUp):
            event = AppKit.NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                kind, (5, 5), 0, 0, mac.ns_window.windowNumber(), None, 0, 1, 1.0)
            AppKit.NSApp.postEvent_atStart_(event, False)
    on_main(post_click)
    time.sleep(0.2)


def frame_that_says(attrs):
    say = "".join(f"window.webkit.messageHandlers.syntropyDesktop.postMessage(JSON.stringify({json.dumps(m)}));" for m in attrs["say"])
    page = f"<script>{say}</script>".replace('"', '\\"')
    return f"""var f=document.createElement('iframe');{attrs['set'].replace('PAGE', page)};document.body.appendChild(f);"""


def scenario():
    until(lambda: mac.webview is not None and (url() or "").startswith(HOME))
    time.sleep(1)
    results["home"] = state()

    # What a foreign page can open with script alone.
    go_to_away()
    results["away"] = state()
    for target in ("x-apple.systempreferences:com.apple.preference.security", "ssh://evil.example/", "smb://evil.example/share",
                   "vscode://file/etc/hosts", "javascript:alert(1)", "https://example.test/scripted"):
        js(script_link(target), 0.3)
    js("location.href='vscode://file/etc/hosts'", 0.6)
    results["opened_by_script"] = list(opened)
    opened.clear()
    results["away_after_scheme_navigation"] = state()
    click_for_real()
    js(script_link("https://example.test/clicked"), 0.3)
    click_for_real()
    js(script_link("ssh://evil.example/clicked"), 0.3)
    results["opened_after_click"] = list(opened)
    opened.clear()

    # Messages from another site, its frames and pywebview's own bridge.
    before = len(requests)
    js(post({"type": "title", "title": "SPOOF-main"}), 0.3)
    says = [{"type": "title", "title": "SPOOF-frame"}, {"type": "switch"}, {"type": "retry"}]
    js(frame_that_says({"say": says, "set": "f.src=\"data:text/html,PAGE\""}), 0.6)
    js(frame_that_says({"say": says, "set": "f.sandbox='allow-scripts';f.srcdoc=\"PAGE\""}), 0.6)
    results["title_after_messages"] = on_main(lambda: str(mac.ns_window.title()))
    results["requests_from_elsewhere"] = requests[before:]
    on_main(lambda: mac.ns_window.setFrameOrigin_((200, 200)))
    x = on_main(lambda: mac.ns_window.frame().origin.x)
    js("window.webkit.messageHandlers.jsBridge.postMessage(JSON.stringify({funcName:'pywebviewMoveWindow',params:[3,3],id:'x'}));", 0.6)
    results["moved_by_page"] = on_main(lambda: mac.ns_window.frame().origin.x) != x

    # Leaving the site for a page with no origin of its own keeps the "another site" chrome.
    js("location.href=URL.createObjectURL(new Blob(['<!doctype html><title>Syntropy Health</title><h1>Your session expired</h1>'],{type:'text/html'}))", 0.2)
    until(lambda: (url() or "").startswith("blob:"))
    time.sleep(0.3)
    results["blob"] = state()
    go_to_away()
    js("location.href='about:blank'", 0.2)
    until(lambda: url() == "about:blank")
    time.sleep(0.3)
    results["blank"] = state()

    # Menu commands on another site go to the app.
    go_to_away()
    on_main(lambda: mac.go("#/overview"))
    until(lambda: (url() or "").endswith("#/overview") and not mac.away)
    results["menu_from_away"] = state()

    # The app's own page: no site name, and it may talk to the app.
    location.showing_own_page()
    window.load_html("<!doctype html><title>own</title><script>window.webkit.messageHandlers.syntropyDesktop.postMessage("
                     f"JSON.stringify({{type:'retry',token:{json.dumps(sites.OWN_PAGE_TOKEN)}}}));</script>")
    until(lambda: [m for m in requests if m.get("_own_page")])
    results["own_page"] = state()
    results["own_page_requests"] = [m for m in requests if m.get("type") == "retry" and m.get("_own_page")]

    window.load_url(f"{HOME}/page")
    until(lambda: (url() or "").endswith("/page") and not mac.away)
    results["back_home"] = state()
    # The page may still be loading, and a message sent to the one on its way out goes nowhere: ask until the title shows it arrived.
    title = lambda: on_main(lambda: str(mac.ns_window.title()))  # noqa: E731
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and title() != "Named by the app":
        js(post({"type": "title", "title": "Named by the app"}), 0.2)
    results["home_title"] = title()
    before = len(requests)
    js(post({"type": "join", "url": "http://x"}), 0)
    until(lambda: len(requests) > before)
    time.sleep(0.3)     # nothing more should follow it
    results["home_requests"] = [m["type"] for m in requests[before:]]

    # A page that can't load is reported.
    window.load_url("http://127.0.0.1:9/")
    until(lambda: any("127.0.0.1:9" in str(f[0]) for f in failures))
    results["failures"] = [[str(a) for a in f] for f in failures]
    window.load_url(f"{HOME}/page")
    until(lambda: (url() or "").endswith("/page"))
    time.sleep(0.3)

    # Pressing Cancel in the save panel.
    folder = Path(sys.argv[1]).parent
    Panel.answer = AppKit.NSModalResponseCancel
    js("var a=document.createElement('a');a.href='/backup.bin';a.download='';document.body.appendChild(a);a.click();", 0.2)
    until(lambda: Panel.asked)
    time.sleep(1.5)     # WebKit calls it a failed download, a moment later
    Panel.answer = AppKit.NSFileHandlingPanelOKButton
    results["panel_asked_before_cancel"] = Panel.asked
    results["reported_after_cancel"] = list(results.get("reported", []))
    results["leftovers_after_cancel"] = sorted(p.name for p in folder.iterdir() if ".part" in p.name)

    # Saving over an existing file, and a download that breaks part way.
    Panel.dest = str(folder / "kept.bin")
    Path(Panel.dest).write_bytes(b"OLD")
    js("var a=document.createElement('a');a.href='/backup.bin';a.download='';document.body.appendChild(a);a.click();", 0.2)
    until(lambda: results.get("reported"))
    results["saved_over"] = Path(Panel.dest).read_bytes().decode()
    results["leftovers_after_saving"] = sorted(p.name for p in folder.iterdir() if ".part" in p.name)
    js("var a=document.createElement('a');a.href='/broken.bin';a.download='';document.body.appendChild(a);a.click();", 0.2)
    until(lambda: len(results["reported"]) >= 2)
    results["kept_after_failure"] = Path(Panel.dest).read_bytes().decode()
    results["leftovers_after_failure"] = sorted(p.name for p in folder.iterdir() if ".part" in p.name)
    # A link to a file type the window can't show (no download attribute) still sends the sign-in cookie.
    Panel.dest = str(folder / "plain.bin")
    js("location.href='/plain.bin'", 0.2)
    until(lambda: len(results["reported"]) >= 3)
    results["plain_link"] = Path(Panel.dest).read_bytes().decode() if Path(Panel.dest).exists() else None
    results["download_without_cookie"] = results.get("download_without_cookie", False)

    Path(sys.argv[1]).write_text(json.dumps(results, default=str))
    AppHelper.callAfter(AppKit.NSApp.terminate_, None)


def started():
    mac.install()
    threading.Thread(target=scenario, daemon=True).start()


os.environ.setdefault("PYWEBVIEW_LOG", "ERROR")
webview.start(started, private_mode=True)
