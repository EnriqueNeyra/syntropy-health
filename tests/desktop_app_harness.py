"""Run by tests/test_desktop_start.py in its own process: the whole Mac app (window, menu bar item, its own server) with a
data folder of the test's making. Writes what the window showed as JSON."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "desktop")]
scenario, out = sys.argv[1], Path(sys.argv[2])
results: dict = {"pages": []}

import AppKit  # noqa: E402
import syntropy_desktop  # noqa: E402
from PyObjCTools import AppHelper  # noqa: E402

app = syntropy_desktop.DesktopApp(background=scenario == "damaged-at-login")

if scenario == "slow":
    from app import main as server_main
    start_up = server_main._start_up

    def slowly():
        time.sleep(8)
        start_up()

    server_main._start_up = slowly


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


def page_text():
    box, done = {}, threading.Event()

    def finished(value, _error):
        box["text"] = value
        done.set()

    on_main(lambda: app.mac.webview.evaluateJavaScript_completionHandler_("document.body ? document.body.innerText : ''", finished))
    done.wait(5)
    return str(box.get("text") or "")


def picture(name):
    """Only when looking at it by hand (HARNESS_PICTURES names a folder)."""
    folder = os.environ.get("HARNESS_PICTURES")
    if not folder:
        return
    done = threading.Event()

    def taken(image, _error):
        rep = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
        rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(f"{folder}/{name}.png", True)
        done.set()

    on_main(lambda: app.mac.webview.takeSnapshotWithConfiguration_completionHandler_(None, taken))
    done.wait(5)


def click(label):
    """The page's own button, as a person would press it."""
    on_main(lambda: app.mac.webview.evaluateJavaScript_completionHandler_(
        f"[...document.querySelectorAll('button')].find(b => b.textContent.includes({json.dumps(label)})).click()", None))


def wait_for(check, seconds=40):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.2)
    return False


def snapshot():
    return {"url": on_main(lambda: str(app.mac.webview.URL().absoluteString()) if app.mac.webview.URL() else None),
            "away": app.mac.away, "visible": on_main(lambda: bool(app.mac.ns_window.isVisible())), "text": page_text()}


def answers():
    try:
        with urllib.request.urlopen(f"{app.server.check_url}/health", timeout=3) as r:
            return json.loads(r.read()).get("status")
    except OSError as exc:
        return getattr(exc, "code", "no answer")


def script():
    wait_for(lambda: app.mac and app.mac.webview is not None)
    if scenario == "slow":
        results["while_starting"] = None
        wait_for(lambda: "Starting" in page_text(), 10)
        time.sleep(5)       # a normal start never shows it: the window waits four seconds for the app's page
        results["while_starting"] = snapshot()
        picture("starting")
    wait_for(lambda: not app.starting, 60)
    time.sleep(1.5)
    results["first"] = snapshot()
    results["health"] = answers()
    picture(f"first-{scenario}")

    if scenario in ("damaged", "damaged-at-login"):
        results["buttons"] = page_buttons()
        # A person chooses a backup that's no good, then a good one.
        app.window.create_confirmation_dialog = lambda *_a, **_k: True
        app.message = lambda title, text: results.setdefault("messages", []).append([title, text])
        app.window.create_file_dialog = lambda *_a, **_k: (str(out.parent / "not-a-backup.db"),)
        click("Restore from a Backup")
        wait_for(lambda: results.get("messages"), 30)
        wait_for(lambda: not app.starting and "can't start" in page_text(), 40)
        results["after_bad_backup"] = snapshot()
        app.window.create_file_dialog = lambda *_a, **_k: (str(out.parent / "good-backup.db"),)
        click("Restore from a Backup")
        wait_for(lambda: answers() == "ok", 40)
        time.sleep(1.5)
        results["after_good_backup"] = snapshot()
        results["health_after"] = answers()
    elif scenario == "fresh":
        app.window.create_confirmation_dialog = lambda *_a, **_k: True
        click("Set It Aside")
        wait_for(lambda: answers() == "ok", 40)
        time.sleep(1.5)
        results["after_fresh"] = snapshot()
        results["health_after"] = answers()
    elif scenario == "normal":
        results["own_home"] = app.home_url
        started = time.monotonic()
        AppHelper.callAfter(AppKit.NSApp.terminate_, None)
        results["terminate_asked"] = started

    out.write_text(json.dumps(results, default=str))
    time.sleep(0.5)
    os._exit(0)


def page_buttons():
    box, done = {}, threading.Event()

    def finished(value, _error):
        box["value"] = value
        done.set()

    on_main(lambda: app.mac.webview.evaluateJavaScript_completionHandler_(
        "[...document.querySelectorAll('button')].map(b => b.textContent).join('|')", finished))
    done.wait(5)
    return str(box.get("value") or "").split("|")


threading.Thread(target=script, daemon=True).start()
app.run()
