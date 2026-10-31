"""Run by tests/test_desktop_window.py in its own process: the real MacWindow and pywebview around a real WKWebView, with
two local sites standing in for the app and for a health system's sign-in page. Writes what happened as JSON."""

from __future__ import annotations

import gc
import http.server
import json
import os
import sys
import threading
import time
import weakref
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "desktop")]
HOME_PORT, AWAY_PORT = int(sys.argv[2]), int(sys.argv[3])
results: dict = {}
# What each of the sites answers with a redirect, the way a sign-in comes back: through the callback to the app's page.
SIGN_IN_REDIRECTS = {
    "/allow": lambda: f"http://localhost:{HOME_PORT}/callback/go",
    "/deny": lambda: f"http://localhost:{HOME_PORT}/callback/stop",
    "/callback/go": lambda: "/#/sources?connected=abc",
    "/callback/stop": lambda: "/#/sources?problem=xyz",
    "/ask-first": lambda: f"http://localhost:{HOME_PORT}/callback/choose",
    "/to-relay-page": lambda: f"http://localhost:{HOME_PORT}/callback#access_token=t&state=s",
    "/lookalike": lambda: f"http://localhost:{AWAY_PORT}/#/settings",
}


class Pages(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path in SIGN_IN_REDIRECTS:
            self.send_response(303)
            self.send_header("Location", SIGN_IN_REDIRECTS[path]())
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/slow":
            time.sleep(1.5)     # long enough to look at the bar while it's on its way
        if path == "/file.bin":
            body = b"not a page"
            self.send_response(200)
            self.send_header("Content-Type", "application/x-thing")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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
    # Its address changes before it commits, and a late commit would say the window has moved on from the page below.
    until(lambda: not on_main(lambda: mac.webview.isLoading()))

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

    # A page that can't load is reported. (With the window on screen, another site would open in the sign-in sheet.)
    on_main(lambda: mac.ns_window.orderOut_(None))
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

    sign_in_sheet()

    Path(sys.argv[1]).write_text(json.dumps(results, default=str))
    # AppKit won't quit with a sheet open, and says nothing: this run only ends if the app deals with that.
    open_sheet("/page")
    AppHelper.callAfter(mac.quit_app)


def sheet_js(code):
    """What a page in the sheet sees."""
    box, done = {}, threading.Event()

    def finished(value, error):
        box["value"], box["error"] = value, error
        done.set()

    AppHelper.callAfter(lambda: mac.sheet.webview.evaluateJavaScript_completionHandler_(code, finished))
    done.wait(5)
    return box.get("value")


def main_js(code):
    box, done = {}, threading.Event()

    def finished(value, _error):
        box["value"] = value
        done.set()

    AppHelper.callAfter(lambda: mac.webview.evaluateJavaScript_completionHandler_(code, finished))
    done.wait(5)
    return box.get("value")


def sheet_url():
    return on_main(lambda: str(mac.sheet.webview.URL().absoluteString()) if mac.sheet and mac.sheet.webview.URL() else "")


def bar():
    def read():
        sheet = mac.sheet
        return {"name": str(sheet.name.stringValue()), "lock": sheet.lock.image() is not None, "covered": sheet.covered,
                "words": str(sheet.words.stringValue()), "dialogs": len(sheet.dialogs)}
    return on_main(read)


def window_state():
    return on_main(lambda: {"frame": str(mac.ns_window.frame()), "style": int(mac.ns_window.styleMask()), "title": str(mac.ns_window.title()),
                            "back": any(str(i.itemIdentifier()) == "syntropy.back" for i in mac.ns_window.toolbar().items())})


def open_sheet(path):
    main_js(f"location.assign({json.dumps(AWAY + path)}); true")
    until(lambda: mac.sheet is not None and sheet_url().startswith(AWAY))
    until(lambda: mac.sheet and not mac.sheet.covered)


def press_in_dialog(title):
    def press():
        def find(view):
            for sub in view.subviews():
                if sub.isKindOfClass_(AppKit.NSButton) and str(sub.title()) == title:
                    return sub
                found = find(sub)
                if found is not None:
                    return found
            return None

        find(mac.sheet.dialogs[-1].contentView()).performClick_(None)

    on_main(press)
    time.sleep(0.4)


def sign_in_sheet():
    """A page in the window going to another site: the window stays, a sheet takes it."""
    sheet: dict = {}
    results["sheet"] = sheet
    mac.show_for_test = on_main(mac.show)
    window.load_url(f"{HOME}/page")
    until(lambda: (url() or "").endswith("/page") and not mac.away)
    time.sleep(0.5)
    main_js("window.stayed = true; window.closes = 0; addEventListener('syntropy-sign-in-closed', () => { window.closes++; }); true")
    before = window_state()
    history_before = on_main(lambda: len(mac.webview.backForwardList().backList() or []))

    open_sheet("/portal")
    sheet["open"] = bar()
    sheet["main_url_while_open"] = url()
    sheet["window_unchanged"] = window_state() == before
    sheet["not_away"] = not mac.away
    sheet["user_agent_same"] = sheet_js("navigator.userAgent") == main_js("navigator.userAgent")
    sheet["attached"] = on_main(lambda: mac.sheet.window.isSheet() and mac.ns_window.attachedSheet() == mac.sheet.window)

    # It can't reach the app, by any of the window's own routes.
    sheet["handlers"] = sheet_js("""JSON.stringify({ syntropy: typeof window.webkit?.messageHandlers?.syntropyDesktop,
        bridge: typeof window.webkit?.messageHandlers?.jsBridge, browser: typeof window.webkit?.messageHandlers?.browserDelegate,
        pywebview: typeof window.pywebview })""")
    asked = len(requests)
    sheet_js("try { window.webkit.messageHandlers.syntropyDesktop.postMessage(JSON.stringify({type:'title',title:'SPOOF'})); } catch (e) {} true")
    time.sleep(0.3)
    sheet["title_after_trying"] = on_main(lambda: str(mac.ns_window.title()))
    sheet["requests_after_trying"] = len(requests) - asked

    # Nothing but the web and mail leaves it, and that takes a click. A new window stays in the sheet.
    opened.clear()
    for target in ("ssh://evil.example/", "vscode://file/etc/hosts", "x-apple.systempreferences:com.apple.preference.security"):
        sheet_js(script_link(target))
        time.sleep(0.2)
    sheet_js("location.href='vscode://file/etc/hosts'; true")
    time.sleep(0.4)
    sheet["opened_by_script"] = list(opened)
    sheet["still_there"] = sheet_url().endswith("/portal")
    sheet["no_box_for_a_scripted_refusal"] = bar()["dialogs"]
    sheet_js(script_link("mailto:help@example.test"))
    time.sleep(0.3)
    sheet["mail_without_a_click"] = list(opened)
    mac.person_acted_at = time.monotonic()
    sheet_js(script_link("mailto:help@example.test"))
    time.sleep(0.3)
    sheet["mail_with_a_click"] = list(opened)
    opened.clear()
    sheet_js(script_link(f"{AWAY}/terms"))
    until(lambda: sheet_url().endswith("/terms"))
    sheet["new_window_stays_in_sheet"] = sheet_url().endswith("/terms")
    sheet["new_window_didnt_leave"] = list(opened)
    sheet["windows_after_new_window"] = on_main(lambda: mac.sheet.webview.window() == mac.sheet.window)
    on_main(lambda: mac.sheet.back_button.performClick_(None))
    until(lambda: sheet_url().endswith("/portal"))
    sheet["back_button"] = sheet_url().endswith("/portal")

    # A portal's alert, confirm and prompt work, and name the site. (Without an answer the page would wait for ever.)
    until(lambda: not on_main(lambda: mac.sheet.webview.isLoading()))
    on_main(lambda: mac.sheet.webview.evaluateJavaScript_completionHandler_("window.said = []; alert('hello'); window.said.push('alerted')", None))
    until(lambda: bar()["dialogs"] == 1, 5)
    sheet["alert_says"] = on_main(lambda: sorted(str(v.stringValue()) for v in mac.sheet.dialogs[-1].contentView().subviews()
                                                 if v.isKindOfClass_(AppKit.NSTextField)))
    press_in_dialog("OK")
    sheet["alert_answered"] = sheet_js("window.said.join()")
    on_main(lambda: mac.sheet.webview.evaluateJavaScript_completionHandler_("window.said.push(confirm('sure?'))", None))
    until(lambda: bar()["dialogs"] == 1, 5)
    press_in_dialog("Cancel")
    on_main(lambda: mac.sheet.webview.evaluateJavaScript_completionHandler_("window.said.push(confirm('sure?'))", None))
    until(lambda: bar()["dialogs"] == 1, 5)
    press_in_dialog("OK")
    on_main(lambda: mac.sheet.webview.evaluateJavaScript_completionHandler_("window.said.push(prompt('name?', 'Alex'))", None))
    until(lambda: bar()["dialogs"] == 1, 5)
    press_in_dialog("OK")
    sheet["confirm_and_prompt"] = sheet_js("window.said.join()")

    # A file isn't downloaded. A person who chose it hears why; a page doing it by script gets nowhere, silently.
    asked = Panel.asked
    sheet_js("location.href='/file.bin'; true")
    time.sleep(0.5)
    sheet["file_by_script"] = bar()["dialogs"]
    mac.person_acted_at = time.monotonic()
    sheet_js("location.href='/file.bin'; true")
    until(lambda: bar()["dialogs"] == 1, 5)
    sheet["file_by_click"] = bar()["dialogs"]
    press_in_dialog("OK")
    sheet["file_save_panels"] = Panel.asked - asked
    sheet["after_file"] = sheet_url().endswith("/portal")

    # The bar says where the page is, not where it's going: a page could otherwise draw a fake of the address it named.
    sheet_js("location.href='http://localhost:' + location.port + '/slow'; true")
    time.sleep(0.6)
    sheet["bar_while_going_elsewhere"] = bar()["name"]
    until(lambda: bar()["name"] == "localhost", 5)
    sheet["bar_once_there"] = bar()["name"]
    on_main(lambda: mac.sheet.webview.goBack_(None))
    until(lambda: bar()["name"] == "127.0.0.1", 5)

    # An address that only looks like the app's isn't the end of anything.
    sheet_js("location.href='/lookalike'; true")
    until(lambda: "#/settings" in sheet_url(), 5)
    sheet["lookalike_keeps_sheet"] = mac.sheet is not None
    sheet["lookalike_main_url"] = url()

    # A page of this server's that asks something is shown; the relay's page, which only passes tokens on, isn't.
    sheet_js(f"location.href={json.dumps(AWAY + '/ask-first')}; true")
    until(lambda: sheet_url().endswith("/callback/choose"), 5)
    until(lambda: not bar()["covered"], 5)
    sheet["ask_page"] = bar()
    sheet_js(f"location.href={json.dumps(AWAY + '/to-relay-page')}; true")
    until(lambda: "/callback" in sheet_url() and "choose" not in sheet_url(), 5)
    time.sleep(0.8)
    sheet["relay_page"] = bar()

    # Cancel: back where you were, told so, with no reload.
    on_main(lambda: mac.sheet.cancel_button.performClick_(None))
    until(lambda: mac.sheet is None)
    time.sleep(0.6)
    sheet["after_cancel"] = {"url": url(), "stayed": main_js("window.stayed === true"), "closes": main_js("window.closes"),
                             "window_unchanged": window_state() == before, "attached": on_main(lambda: mac.ns_window.attachedSheet() is not None),
                             "history": on_main(lambda: len(mac.webview.backForwardList().backList() or [])) - history_before}

    # Coming back through the callback: the sheet closes, and the page goes to the address without reloading.
    open_sheet("/portal")
    sheet_js("location.href='/allow'; true")
    until(lambda: mac.sheet is None)
    until(lambda: "connected" in (url() or ""), 5)
    sheet["after_finish"] = {"url": url(), "stayed": main_js("window.stayed === true"), "not_away": not mac.away, "window_unchanged": window_state() == before}
    open_sheet("/portal")
    sheet_js("location.href='/deny'; true")
    until(lambda: mac.sheet is None)
    until(lambda: "problem" in (url() or ""), 5)
    sheet["after_problem"] = {"url": url(), "stayed": main_js("window.stayed === true")}

    # ⌘W and the menus end it too, and the window's own commands reach the page in the sheet meanwhile.
    open_sheet("/portal")
    on_main(lambda: mac.sheet.window.performClose_(None))
    until(lambda: mac.sheet is None)
    sheet["close_command"] = mac.sheet is None and on_main(lambda: bool(mac.ns_window.isVisible()))
    open_sheet("/portal")
    on_main(lambda: mac.sheet.webview.evaluateJavaScript_completionHandler_("alert('still open')", None))
    until(lambda: bar()["dialogs"] == 1, 5)
    on_main(lambda: mac.go("#/overview"))     # over a dialog: WebKit has to have its answer before the page goes
    until(lambda: mac.sheet is None)
    until(lambda: (url() or "").endswith("#/overview"), 5)
    sheet["menu_over_a_dialog"] = url()
    open_sheet("/portal")
    on_main(mac.go_home)
    until(lambda: mac.sheet is None)
    sheet["home_command"] = mac.sheet is None

    # A finished sheet lets go of its web view, or each sign-in would leave a page's process running.
    open_sheet("/portal")
    gone = weakref.ref(mac.sheet)
    on_main(lambda: mac.sheet.cancel())
    until(lambda: mac.sheet is None)
    time.sleep(2.5)     # past the slide, and past the longest it waits for a page to paint
    on_main(gc.collect)
    sheet["let_go"] = gone() is None

    # Should a page go somewhere while the window can't show a sheet, it stays in the window, with the away chrome.
    on_main(lambda: mac.ns_window.orderOut_(None))
    main_js(f"location.assign({json.dumps(AWAY + '/hidden')}); true")
    until(lambda: mac.away, 5)
    sheet["no_window_on_screen"] = {"away": mac.away, "sheet": mac.sheet is None}
    window.load_url(f"{HOME}/page")
    until(lambda: (url() or "").endswith("/page") and not mac.away)
    on_main(mac.show)
    time.sleep(0.4)


def started():
    mac.install()
    threading.Thread(target=scenario, daemon=True).start()


os.environ.setdefault("PYWEBVIEW_LOG", "ERROR")
webview.start(started, private_mode=True)
