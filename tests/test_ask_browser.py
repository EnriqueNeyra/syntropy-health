"""Ask in a real browser: what ends up on screen, which only the page itself can show. A fake model on this computer stands
in for the AI. Skipped where Playwright or Chrome isn't available."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

CSRF = {"X-Requested-With": "syntropy"}
TABLE = "| Test | Value |\n|---|---|\n| A1c | 6.1 % |\n| Glucose | 114 mg/dL |\n| LDL | 162 mg/dL |"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Model:
    """An OpenAI-compatible server: each POST streams the next scripted list of pieces of text."""

    def __init__(self):
        self.script = []
        self.bodies = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._json(200, {"data": [{"id": "m"}, {"id": "n"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                if self.path.startswith("/api/"):
                    return self._json(404, {})
                outer.bodies.append(body)
                reply = outer.script.pop(0) if outer.script else {"text": "Fine."}
                if reply.get("hold"):
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': reply['text']}}]})}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(reply["hold"])
                    return
                chunks = [{"choices": [{"delta": {"content": reply["text"]}, **({"finish_reason": reply["finish"]} if reply.get("finish") else {})}]}]
                data = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(data.encode())))
                self.end_headers()
                self.wfile.write(data.encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            chrome = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # noqa: BLE001 - no Chrome on this machine
            pytest.skip(f"Chrome isn't available: {exc}")
        yield chrome
        chrome.close()


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    model = Model()
    port = _free_port()
    env = {**os.environ, "SYNTROPY_DATA_DIR": str(tmp_path_factory.mktemp("ask")), "SYNTROPY_SCHEDULER": "false", "SYNTROPY_MDNS": "0",
           "SYNTROPY_ALLOW_NO_PASSWORD": "1"}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
                              env=env, cwd=Path(__file__).parent.parent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    api = httpx.Client(base_url=base, headers=CSRF, trust_env=False, timeout=20)
    try:
        for _ in range(200):
            try:
                api.get("/api/status")
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        assert api.post("/api/setup", json={"passphrase": "correct horse battery", "profile_name": "Alex", "accept_terms": True}).status_code == 200
        api.post("/api/onboarding/done")
        assert api.put("/api/ai/config", json={"provider": "local", "base_url": model.url, "model": "m"}).status_code == 200
        yield base, api, model
    finally:
        server.terminate()
        server.wait(timeout=10)


@pytest.fixture
def page(browser, app):
    base, api, _ = app
    context = browser.new_context(base_url=base, reduced_motion="reduce", viewport={"width": 1100, "height": 800})
    context.add_cookies([{"name": c.name, "value": c.value, "url": base} for c in api.cookies.jar])
    page = context.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto("/#/ask?new=1")
    page.wait_for_selector("#chat-text")
    yield page
    context.close()


def _ask(page, question="What are my labs?"):
    page.fill("#chat-text", question)
    page.press("#chat-text", "Enter")


def _finished(page, n=1):
    page.wait_for_function("n => document.querySelectorAll('.msg-ai').length >= n && !document.querySelector('.msg-ai.streaming') && !document.querySelector('#chat-send.stop')", arg=n)


def test_an_answer_ending_in_a_table_keeps_its_last_row(page, app):
    app[2].script.append({"text": TABLE})
    _ask(page)
    _finished(page)
    assert page.locator(".msg-ai tbody tr").count() == 3
    assert "LDL" in page.locator(".msg-ai tbody tr").last.inner_text()
    assert page.errors == []


def test_a_dose_with_asterisks_keeps_its_number(page, app):
    app[2].script.append({"text": "take 5*3*2 mg and *really* this"})
    _ask(page)
    _finished(page)
    body = page.locator(".msg-ai .msg-body").last
    assert "5*3*2" in body.inner_text() and body.locator("i").inner_text() == "really"


def test_a_cut_short_answer_says_so_and_can_continue_as_a_new_question(page, app):
    model = app[2]
    model.script.append({"text": "Your A1c is 6.1 and the", "finish": "length"})
    _ask(page)
    page.wait_for_selector("[data-continue]")
    assert "cut short" in page.locator(".msg-ai .msg-note").last.inner_text()
    model.script.append({"text": "rest of it."})
    page.click("[data-continue]")
    _finished(page, 2)
    last = model.bodies[-1]["messages"]
    assert last[-1]["role"] == "user" and "continue" in last[-1]["content"].lower() and last[-2]["role"] == "assistant"


def test_when_the_ai_changed_nothing_is_sent_and_try_again_goes_to_the_new_one(page, app):
    _, api, model = app
    api.put("/api/ai/config", json={"provider": "local", "model": "m"})
    sent = len(model.bodies)
    page.reload()
    page.wait_for_selector("#chat-text")
    api.put("/api/ai/config", json={"provider": "local", "model": "n"})
    _ask(page, "Is this private?")
    page.wait_for_selector("[data-retry]")
    assert "wasn't sent" in page.locator(".msg-ai .banner").last.inner_text()
    assert len(model.bodies) == sent
    assert page.input_value("#chat-text") == "Is this private?"
    page.click("[data-retry]")
    page.wait_for_selector(".msg-ai:last-child .msg-by:has-text('n ·')")
    assert model.bodies[-1]["model"] == "n"
    assert "n" in page.locator(".msg-ai .msg-by").last.inner_text()
    api.put("/api/ai/config", json={"provider": "local", "model": "m"})


def test_a_stopped_answer_is_saved_as_stopped(page, app):
    _, api, model = app
    model.script.append({"text": "Partial answer", "hold": 3})
    _ask(page)
    page.wait_for_selector(".msg-ai .msg-body:has-text('Partial')")
    page.click("#chat-send")
    page.wait_for_selector(".msg-ai .msg-by:has-text('Stopped')")
    page.wait_for_timeout(300)
    page.reload()
    page.wait_for_selector(".msg-ai .msg-by")
    assert "Stopped" in page.locator(".msg-ai .msg-by").last.inner_text()


def test_a_question_too_long_to_send_stays_in_the_box_with_a_plain_message(page, app):
    _ask(page, "x" * 40_001)
    page.wait_for_selector(".toast")
    assert "too long" in page.locator(".toast").last.inner_text() and "[object" not in page.content()
    assert len(page.input_value("#chat-text")) == 40_001


def test_an_error_keeps_the_question_and_offers_try_again(page, app):
    page.route("**/api/ai/chat", lambda route: route.fulfill(status=422, json={"detail": [{"msg": "Too long"}]}))
    _ask(page, "A question")
    page.wait_for_selector("[data-retry]")
    assert page.locator(".msg-ai .banner").inner_text().startswith("Too long")
    assert page.input_value("#chat-text") == "A question"
    page.unroute("**/api/ai/chat")
    page.click("[data-retry]")
    _finished(page)
    page.wait_for_selector(".msg-ai .msg-by")
    assert page.locator(".msg-user").count() == 1 or page.locator(".msg-user").last.inner_text() == "A question"


def test_settings_calls_only_a_private_server_private_and_agreements_can_be_taken_back(page, app):
    _, api, _ = app
    api.post("/api/ai/acknowledge", json={"vendor": "openai", "name": "OpenAI"})
    page.goto("/#/settings/ai")
    page.wait_for_selector(".ai-default")
    assert page.locator(".ai-conn .badge.good").count() == 1      # the fake model is on this computer
    page.wait_for_selector("[data-action=withdraw-ai]")
    page.click("[data-action=withdraw-ai]")
    page.click("button:has-text('Take back') >> nth=-1")
    page.wait_for_selector("[data-action=withdraw-ai]", state="detached")
    assert api.get("/api/ai/config").json()["cloud_acks"] == []
    assert page.errors == []


@pytest.mark.parametrize("width", [390])
def test_ask_on_a_phone_width_has_no_page_errors(browser, app, width):
    base, api, model = app
    context = browser.new_context(base_url=base, viewport={"width": width, "height": 800})
    context.add_cookies([{"name": c.name, "value": c.value, "url": base} for c in api.cookies.jar])
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto("/#/ask?new=1")
    page.wait_for_selector("#chat-text")
    model.script.append({"text": TABLE})
    _ask(page)
    _finished(page)
    assert errors == [] and page.locator(".msg-ai tbody tr").count() >= 3
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    context.close()
