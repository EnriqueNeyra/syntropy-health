"""Ask in a real browser: what ends up on screen, which only the page itself can show. A fake model on this computer stands
in for the AI. Skipped where Playwright or Chrome isn't available."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
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
def engines():
    with playwright_api.sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(engines):
    try:
        chrome = engines.chromium.launch(channel="chrome", headless=True)
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


def test_a_model_first_found_to_have_no_tools_asks_before_trying_again(browser, app):
    base, api, _ = app
    context = browser.new_context(base_url=base, reduced_motion="reduce", viewport={"width": 1100, "height": 800})
    context.add_cookies([{"name": c.name, "value": c.value, "url": base} for c in api.cookies.jar])
    page = context.new_page()
    chats = []

    def cfg(route):
        res = route.fetch()
        route.fulfill(response=res, json={**res.json(), "sends_elsewhere": True, "cloud_ack": True, "tools": None, "summary_ack": False,
                                          "vendor_key": "openrouter", "vendor_name": "OpenRouter"})

    def chat(route):
        chats.append(route.request.post_data)
        route.fulfill(status=200, content_type="application/x-ndjson", body=json.dumps(
            {"type": "error", "code": "summary_ack", "vendor": "openrouter", "vendor_name": "OpenRouter",
             "message": "OpenRouter can't look things up. Confirm first, or choose another AI."}) + "\n")

    page.route("**/api/ai/config*", cfg)
    page.route("**/api/ai/chat", chat)
    page.goto("/#/ask?new=1")
    page.wait_for_selector("#chat-text")
    _ask(page, "A question")
    page.wait_for_selector("[data-retry]")
    assert len(chats) == 1
    page.click("[data-retry]")
    page.wait_for_selector("text=Send a summary of the record to OpenRouter?")
    assert len(chats) == 1
    context.close()


# ---------------------------------------------------------------------------
# The list of conversations
# ---------------------------------------------------------------------------

def _chat(api, title, answer="An answer.", question=None):
    chat_id = f"c_{uuid.uuid4().hex[:24]}"
    r = api.put(f"/api/ai/chats/{chat_id}", json={"title": title, "messages": [
        {"role": "user", "content": question or title}, {"role": "assistant", "content": answer, "by": "m · Local model"}]})
    assert r.status_code == 200, r.text
    return chat_id


def _saved_chat(api, chat_id):
    return api.get(f"/api/ai/chats/{chat_id}").json()


def _row(page, title):
    return page.locator(".ask-item", has_text=title)


def _menu_choice(page, row, choice):
    row.hover()
    row.locator(".ask-item-more").click()
    page.locator(".popover.dropdown:not(.closing) .menu-item", has_text=choice).click()


def _reopen(page, chat_id=None):
    """The page drawn afresh with what's been saved since it opened: that conversation, or a new one."""
    page.goto(f"/#/ask?chat={chat_id}" if chat_id else "/#/ask")
    page.reload()
    page.wait_for_selector("#chat-text")
    if chat_id:
        page.wait_for_selector(f".ask-item.current[data-id='{chat_id}']")
    else:
        page.click("#ask-new")
        page.wait_for_selector(".chat-empty")


def _ask_until_saved(page, question):
    with page.expect_response(lambda r: r.request.method == "PUT" and "/api/ai/chats/" in r.url):
        _ask(page, question)


def _mod(page):
    return "Meta" if page.evaluate("() => /Mac/.test(navigator.platform)") else "Control"


def _context(browser, app, **more):
    base, api, _ = app
    context = browser.new_context(base_url=base, **{"reduced_motion": "reduce", "viewport": {"width": 1100, "height": 800}, **more})
    context.add_cookies([{"name": c.name, "value": c.value, "url": base} for c in api.cookies.jar])
    return context


def _open(context, address="/#/ask?new=1"):
    page = context.new_page()
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto(address)
    page.wait_for_selector("#chat-text")
    return page


def test_conversations_are_listed_pinned_first_then_by_when_they_were_last_talked_in(page, app):
    now = time.time()
    day = 86400
    chats = [{"id": f"c_{name.lower().replace(' ', '')}00000000", "title": name, "created_at": now - age * day, "updated_at": now - age * day, "messages": 2, "pinned": pinned, "title_set": False}
             for name, age, pinned in [("Just now", 0, False), ("A day ago", 1, False), ("Three days ago", 3, False), ("Twenty days ago", 20, False),
                                       ("Sixty days ago", 60, False), ("Old but pinned", 90, True), ("Pinned today", 0, True)]]
    chats.sort(key=lambda c: -c["updated_at"])
    page.route("**/api/ai/chats?*", lambda route: route.fulfill(json={"chats": chats}))
    page.reload()
    page.wait_for_selector(".ask-item")
    titles = page.locator(".ask-item-title").all_text_contents()
    assert set(titles[:2]) == {"Pinned today", "Old but pinned"} and titles[2:] == ["Just now", "A day ago", "Three days ago", "Twenty days ago", "Sixty days ago"]
    month = page.evaluate("(at) => { const d = new Date(at * 1000); return d.toLocaleDateString(undefined, d.getFullYear() === new Date().getFullYear() ? { month: 'long' } : { month: 'long', year: 'numeric' }); }", now - 60 * day)
    heads = page.locator(".ask-group").all_text_contents()
    assert heads == ["Pinned", "Today", "Yesterday", "Previous 7 days", "Previous 30 days", month]
    assert page.errors == []


def test_rename_in_the_list_enter_saves_escape_cancels_and_a_later_answer_keeps_it(page, app):
    _, api, model = app
    chat_id = _chat(api, "Rename me please")
    _reopen(page, chat_id)
    row = _row(page, "Rename me please")
    height = row.bounding_box()["height"]
    _menu_choice(page, row, "Rename")
    field = page.locator(".ask-item .ask-rename")
    field.wait_for()
    assert row.bounding_box()["height"] == height       # the row doesn't jump as it becomes a field
    field.fill("Not this")
    field.press("Escape")
    assert _saved_chat(api, chat_id)["title"] == "Rename me please" and page.locator(".ask-rename").count() == 0
    _menu_choice(page, row, "Rename")
    field.wait_for()
    field.fill("My chosen name")
    with page.expect_response(lambda r: r.request.method == "PATCH"):
        field.press("Enter")
    assert _saved_chat(api, chat_id)["title"] == "My chosen name"
    assert page.locator("#ask-title-btn").inner_text() == "My chosen name"
    model.script.append({"text": "More."})
    _ask_until_saved(page, "Something else")
    _finished(page, 2)
    assert _saved_chat(api, chat_id)["title"] == "My chosen name" and page.locator(".ask-item.current .ask-item-title").inner_text() == "My chosen name"
    assert page.errors == []


def test_the_title_above_the_conversation_can_be_renamed_too(page, app):
    _, api, _ = app
    chat_id = _chat(api, "Title from the header")
    _reopen(page, chat_id)
    page.click("#ask-title-btn")
    field = page.locator(".ask-bar .ask-rename")
    field.fill("Named up here")
    with page.expect_response(lambda r: r.request.method == "PATCH"):
        field.press("Enter")
    assert _saved_chat(api, chat_id)["title"] == "Named up here" and _row(page, "Named up here").count() == 1
    assert page.evaluate("() => document.activeElement.id") == "ask-title-btn"     # the keyboard stays where it was
    page.click("#ask-title-btn")
    field.fill("Abandoned")
    field.press("Escape")
    assert page.locator("#ask-title-btn").inner_text() == "Named up here"


def test_a_name_given_before_the_first_answer_is_saved_stays(page, app):
    _, api, model = app
    model.script.append({"text": "In a moment", "hold": 1})
    with page.expect_response(lambda r: r.request.method == "PUT" and "/api/ai/chats/" in r.url, timeout=10000) as saved:
        _ask(page, "Named while waiting")
        page.wait_for_selector(".msg-ai .msg-body:has-text('In a moment')")
        assert page.locator(".ask-item.current .ask-item-title").inner_text() == "Named while waiting"     # listed straight away
        page.click("#ask-title-btn")
        field = page.locator(".ask-bar .ask-rename")
        field.fill("Early name")
        field.press("Enter")
    chat = saved.value.json()
    assert chat["title"] == "Early name" and chat["title_set"] is True


def test_pinning_moves_a_conversation_to_the_top_and_back_without_changing_when_it_was_last_used(page, app):
    _, api, _ = app
    chat_id = _chat(api, "Pin this one")
    _chat(api, "Newer than the pinned one")
    before = _saved_chat(api, chat_id)["updated_at"]
    _reopen(page)
    row = _row(page, "Pin this one")
    with page.expect_response(lambda r: r.request.method == "PATCH"):
        _menu_choice(page, row, "Pin")
    assert page.locator(".ask-group").first.text_content() == "Pinned" and page.locator(".ask-item-title").first.inner_text() == "Pin this one"
    assert _saved_chat(api, chat_id)["pinned"] is True and _saved_chat(api, chat_id)["updated_at"] == before
    page.reload()
    page.wait_for_selector(".ask-group")
    assert page.locator(".ask-item-title").first.inner_text() == "Pin this one"
    with page.expect_response(lambda r: r.request.method == "PATCH"):
        _menu_choice(page, _row(page, "Pin this one"), "Unpin")
    assert "Pinned" not in page.locator(".ask-group").all_text_contents() and _saved_chat(api, chat_id)["pinned"] is False


def test_a_change_the_server_refuses_goes_back_and_says_so(page, app):
    _, api, _ = app
    chat_id = _chat(api, "Stays as it was")
    _reopen(page, chat_id)
    page.route("**/api/ai/chats/*", lambda route: route.fulfill(status=500, json={"detail": "Couldn't do that"}) if route.request.method == "PATCH" else route.continue_())
    _menu_choice(page, _row(page, "Stays as it was"), "Pin")
    page.wait_for_selector(".toast.bad")
    assert "Couldn't do that" in page.locator(".toast.bad").inner_text() and page.locator(".ask-group", has_text="Pinned").count() == 0
    page.click("#ask-title-btn")
    page.locator(".ask-bar .ask-rename").fill("Refused")
    page.locator(".ask-bar .ask-rename").press("Enter")
    page.wait_for_function("() => document.querySelector('#ask-title-btn').textContent === 'Stays as it was'")


def test_right_clicking_a_conversation_opens_its_menu_at_the_pointer(page, app):
    _, api, _ = app
    _chat(api, "Right click me")
    _reopen(page)
    _row(page, "Right click me").click(button="right", position={"x": 40, "y": 10})
    menu = page.locator(".popover.dropdown:not(.closing)")
    menu.wait_for()
    assert menu.locator(".menu-item").all_inner_texts() == ["Rename", "Pin", "Export as Markdown", "Delete"]
    page.wait_for_timeout(50)       # its opening animation
    box, where = _row(page, "Right click me").bounding_box(), menu.bounding_box()
    assert abs(where["x"] - (box["x"] + 40)) < 2 and abs(where["y"] - (box["y"] + 10)) < 2
    page.keyboard.press("Escape")
    menu.wait_for(state="detached")


def test_deleting_asks_first_then_the_list_and_conversation_let_it_go(page, app):
    _, api, _ = app
    chat_id = _chat(api, "Delete me for good")
    _reopen(page, chat_id)
    _menu_choice(page, _row(page, "Delete me for good"), "Delete")
    page.locator(".modal button", has_text="Cancel").click()
    page.wait_for_selector(".modal", state="detached")
    assert api.get(f"/api/ai/chats/{chat_id}").status_code == 200 and page.locator(".msg-user").count() == 1
    _menu_choice(page, _row(page, "Delete me for good"), "Delete")
    page.locator(".modal .btn-danger").click()
    page.wait_for_selector(".chat-empty")
    assert api.get(f"/api/ai/chats/{chat_id}").status_code == 404 and _row(page, "Delete me for good").count() == 0
    assert page.locator("#ask-title-btn").inner_text() == "New chat"


def test_search_finds_what_was_said_and_clearing_it_brings_the_list_back(page, app):
    _, api, _ = app
    _chat(api, "Nothing in this title", answer="Your zebrafish count was **twelve**, as of March.")
    _chat(api, "Another conversation")
    _reopen(page)
    everything = page.locator(".ask-item").count()
    page.fill("#ask-search", "ZEBRAFISH")
    page.wait_for_selector(".ask-item-snippet mark")
    assert page.locator(".ask-item").count() == 1 and page.locator(".ask-item-title").inner_text() == "Nothing in this title"
    assert page.locator(".ask-item-snippet").inner_text().startswith("Your zebrafish count") and page.locator("mark").inner_text() == "zebrafish"
    page.keyboard.press("Enter")                 # opens the first one found
    page.wait_for_selector(".msg-ai .msg-body:has-text('twelve')")
    page.fill("#ask-search", "no such words anywhere")
    page.wait_for_selector(".ask-none")
    assert page.locator(".ask-none").inner_text() == "No conversations match."
    page.locator("#ask-search").press("Escape")
    page.wait_for_function("(n) => document.querySelectorAll('.ask-item').length === n", arg=everything)
    assert page.input_value("#ask-search") == ""


def test_a_conversation_downloads_as_markdown(page, app):
    _, api, _ = app
    _chat(api, "Worth keeping", answer="A **kept** answer.", question="What should I keep?")
    _reopen(page)
    with page.expect_download() as download:
        _menu_choice(page, _row(page, "Worth keeping"), "Export")
    assert download.value.suggested_filename == "Worth keeping.md"
    text = Path(download.value.path()).read_text()
    assert text.startswith("# Worth keeping\n") and "## You\n\nWhat should I keep?" in text and "A **kept** answer." in text
    assert page.errors == [] and page.locator("#chat-text").count() == 1


def test_a_title_is_only_ever_text(page, app):
    _, api, _ = app
    _chat(api, "<img src=x onerror=boom> & co", answer="The <b>raw</b> & the rest")
    _reopen(page)
    page.fill("#ask-search", "<img")
    page.wait_for_selector(".ask-item")
    page.fill("#ask-search", "raw")
    page.wait_for_selector(".ask-item-snippet")
    assert page.locator("#ask-side img, #ask-side .ask-item-snippet b").count() == 0
    assert page.errors == []


def test_saving_an_answer_leaves_the_scrolled_list_alone(browser, app):
    _, api, model = app
    for i in range(40):
        _chat(api, f"Filler conversation {i}")
    context = _context(browser, app)
    page = _open(context)
    place = page.evaluate("""() => { const l = document.querySelector('#ask-list'); l.scrollTop = 500;
        const top = l.getBoundingClientRect().top, row = [...l.querySelectorAll('.ask-item')].find((n) => n.getBoundingClientRect().top >= top);
        window.row = row; return [l.scrollTop, row.getBoundingClientRect().top]; }""")
    model.script.append({"text": "Saved without moving the list."})
    _ask_until_saved(page, "Does the list stay put?")
    _finished(page)
    page.wait_for_function("() => document.querySelector('.ask-item.current .ask-item-more:not([hidden])')")
    row_now = page.evaluate("() => { const l = document.querySelector('#ask-list'); return [window.row.isConnected, window.row.getBoundingClientRect().top]; }")
    assert row_now[0] is True and abs(row_now[1] - place[1]) < 1       # the very same element, at the same place on screen
    context.close()


def test_the_open_conversation_is_scrolled_into_view_in_the_list(browser, app):
    _, api, _ = app
    old = _chat(api, "Far down the list")
    for i in range(40):
        _chat(api, f"Newer filler {i}")
    context = _context(browser, app)
    page = _open(context, f"/#/ask?chat={old}")
    page.wait_for_selector(".ask-item.current")
    page.wait_for_function("""() => { const r = document.querySelector('.ask-item.current').getBoundingClientRect(), b = document.querySelector('#ask-list').getBoundingClientRect();
        return r.top >= b.top && r.bottom <= b.bottom; }""")
    assert page.locator(".ask-item.current .ask-item-title").inner_text() == "Far down the list"
    context.close()


# ---------------------------------------------------------------------------
# The panel opening and closing
# ---------------------------------------------------------------------------

def test_closing_the_panel_is_remembered_in_this_browser(page, app):
    assert page.get_attribute("#ask-split", "data-side") == "open" and page.locator("#ask-side").is_visible()
    page.click("#ask-toggle")
    assert page.get_attribute("#ask-split", "data-side") == "closed" and not page.locator("#ask-side").is_visible()
    assert page.get_attribute("#ask-toggle", "aria-expanded") == "false"
    page.reload()
    page.wait_for_selector("#chat-text")
    assert page.get_attribute("#ask-split", "data-side") == "closed"
    page.click("#ask-toggle")
    page.reload()
    page.wait_for_selector("#chat-text")
    assert page.get_attribute("#ask-split", "data-side") == "open"


def test_a_browser_that_refuses_storage_still_works(browser, app):
    context = _context(browser, app)
    context.add_init_script("Object.defineProperty(window, 'localStorage', { get() { throw new Error('blocked'); } })")
    page = _open(context)
    page.click("#ask-toggle")
    assert page.get_attribute("#ask-split", "data-side") == "closed" and page.errors == []
    context.close()


def test_with_reduced_motion_the_panel_simply_changes(page, app):
    page.click("#ask-toggle")
    assert page.evaluate("() => document.getAnimations().filter((a) => a.effect.target?.closest?.('#ask-split') && a.playState === 'running' && a.effect.getComputedTiming().duration > 1).length") == 0
    assert "side-leaving" not in page.get_attribute("#ask-split", "class")


@pytest.mark.parametrize("width", [1440, 1100])
def test_the_panel_glides_with_transforms_and_the_card_and_text_hold_still(browser, app, width):
    _, api, _ = app
    chat_id = _chat(api, "A long read", answer="\n\n".join(f"Paragraph {i} of a long answer, with enough words in it to wrap over at least a couple of lines at any width." for i in range(30)))
    context = _context(browser, app, reduced_motion="no-preference", viewport={"width": width, "height": 800})
    page = _open(context, f"/#/ask?chat={chat_id}")
    page.wait_for_selector(".msg-ai")
    # Frame by frame through closing and opening: nothing is resized along the way, and the ends of the message field carry on smoothly.
    samples = page.evaluate("""async () => {
        const q = (s) => document.querySelector(s), split = q('#ask-split'), thread = q('#chat-thread'), log = q('#chat-log');
        const frames = [];
        const watch = async () => { for (let t = performance.now(); performance.now() - t < 330;) {
            await new Promise(requestAnimationFrame);
            const card = split.getBoundingClientRect(), bg = q('#chat-input-bg').getBoundingClientRect();
            frames.push({ thread: thread.offsetWidth, card: [card.left, card.right], field: [bg.left, bg.right], gap: log.scrollHeight - log.scrollTop - log.clientHeight });
        } };
        q('#ask-toggle').click();
        const closing = split.dataset.side;
        const moving = document.getAnimations().filter((a) => a.effect.target === q('#ask-side'))[0];
        const sideKeys = moving.effect.getKeyframes().map((k) => Object.keys(k).filter((n) => !['offset', 'computedOffset', 'easing', 'composite'].includes(n))).flat();
        await watch();
        const closed = frames.splice(0);
        q('#ask-toggle').click();
        await watch();
        return { closing, sideKeys, closed, opened: frames, leftover: split.className };
    }""")
    assert samples["closing"] == "closed" and set(samples["sideKeys"]) == {"transform"}
    assert "side-" not in samples["leftover"]
    for frames in (samples["closed"], samples["opened"]):
        assert len({f["thread"] for f in frames}) == 1, "the text took a new width only once, not on every frame"
        assert len({tuple(f["card"]) for f in frames}) == 1, "the card's outer edges don't move"
        assert all(f["gap"] < 2 for f in frames), "reading at the end stays at the end"
        lefts = [f["field"][0] for f in frames]
        assert all(a - b >= -1 for a, b in zip(lefts, lefts[1:])) or all(b - a >= -1 for a, b in zip(lefts, lefts[1:])), "the message field moves one way, without jumping back"
    context.close()


def test_opening_and_closing_keeps_your_place_in_a_long_conversation(browser, app):
    _, api, _ = app
    chat_id = _chat(api, "Long enough to scroll", answer="\n\n".join(f"Paragraph {i} of a long answer, with enough words in it to wrap over at least a couple of lines at this width." for i in range(50)))
    context = _context(browser, app, viewport={"width": 1100, "height": 800})
    page = _open(context, f"/#/ask?chat={chat_id}")
    page.wait_for_selector(".msg-ai")
    page.evaluate("() => { document.querySelector('#chat-log').scrollTop = 900; }")
    for _ in range(2):
        y = page.evaluate("""() => { const top = document.querySelector('#chat-log').getBoundingClientRect().top;
            window.reading = [...document.querySelectorAll('.msg-body p')].find((n) => n.getBoundingClientRect().top >= top);
            return window.reading.getBoundingClientRect().top; }""")
        width = page.locator("#chat-thread").bounding_box()["width"]
        page.click("#ask-toggle")
        assert page.locator("#chat-thread").bounding_box()["width"] != width     # the text has new line breaks
        assert page.evaluate("() => window.reading.getBoundingClientRect().top") == pytest.approx(y, abs=1)
    context.close()


@pytest.mark.parametrize("width", [820, 390])
def test_on_a_narrow_window_the_panel_slides_over_and_gets_out_of_the_way(browser, app, width):
    _, api, _ = app
    _chat(api, f"Pick me at {width}")
    context = _context(browser, app, viewport={"width": width, "height": 800})
    page = _open(context)
    assert page.get_attribute("#ask-split", "data-side") == "closed" and page.get_attribute("#ask-side", "inert") is not None
    chat_box = page.locator("#ask-chat").bounding_box()
    page.click("#ask-toggle")
    assert page.get_attribute("#ask-split", "data-side") == "open" and page.locator("#ask-side").is_visible()
    assert page.get_attribute("#ask-chat", "inert") is not None
    assert page.locator("#ask-chat").bounding_box() == chat_box             # it slides over, nothing underneath moves
    page.keyboard.press("Escape")
    assert page.get_attribute("#ask-split", "data-side") == "closed"
    page.click("#ask-toggle")
    page.mouse.click(width - 40, 300)                                       # outside it
    assert page.get_attribute("#ask-split", "data-side") == "closed"
    page.click("#ask-toggle")
    page.locator(".ask-item", has_text=f"Pick me at {width}").locator(".ask-item-open").click()
    page.wait_for_selector(f".msg-user:has-text('Pick me at {width}')")
    assert page.get_attribute("#ask-split", "data-side") == "closed" and page.get_attribute("#ask-chat", "inert") is None
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1") and page.errors == []
    context.close()


def test_the_keyboard_stays_inside_the_panel_on_a_narrow_window(browser, app):
    context = _context(browser, app, viewport={"width": 820, "height": 800})
    page = _open(context)
    page.click("#ask-toggle")
    for _ in range(4):
        page.keyboard.press("Shift+Tab")
        assert page.evaluate("() => !!document.activeElement.closest('#ask-side')")
    context.close()


# ---------------------------------------------------------------------------
# The conversation's tools
# ---------------------------------------------------------------------------

def test_copying_an_answer_a_table_and_a_code_block(browser, app):
    _, _, model = app
    context = _context(browser, app)
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    page = _open(context)
    answer = f"Here:\n\n{TABLE}\n\nAnd:\n\n```\nDate,Steps\n2026-01-01,8000\n```"
    model.script.append({"text": answer})
    _ask(page)
    _finished(page)
    page.locator('.msg-ai [data-tool="copy"]').click()
    page.wait_for_selector('.msg-ai [data-tool="copy"].did')
    assert page.evaluate("() => navigator.clipboard.readText()").strip() == answer.strip()
    page.locator(".md-block:has(table)").hover()
    page.locator(".md-block:has(table) .md-copy").click()
    page.wait_for_selector(".md-block:has(table) .md-copy.did")
    assert page.evaluate("() => navigator.clipboard.readText()").splitlines()[1] == "A1c\t6.1 %"
    page.locator(".md-block:has(pre)").hover()
    page.locator(".md-block:has(pre) .md-copy").click()
    page.wait_for_selector(".md-block:has(pre) .md-copy.did")
    assert page.evaluate("() => navigator.clipboard.readText()").strip() == "Date,Steps\n2026-01-01,8000"
    assert page.errors == []
    context.close()


def test_copying_still_works_where_the_browser_has_no_clipboard_to_ask(browser, app):
    # Plain http on a home network has no navigator.clipboard.
    _, _, model = app
    context = _context(browser, app)
    context.add_init_script("Object.defineProperty(navigator, 'clipboard', { value: undefined })")
    page = _open(context)
    model.script.append({"text": "Copied the old way."})
    _ask(page)
    _finished(page)
    page.locator('.msg-ai [data-tool="copy"]').click()
    page.wait_for_selector('.msg-ai [data-tool="copy"].did')
    assert page.locator(".toast.bad").count() == 0 and page.locator(".msg-ai textarea").count() == 0
    context.close()


def test_regenerate_replaces_the_last_answer_and_keeps_the_question(page, app):
    _, _, model = app
    model.script.append({"text": "First answer."})
    _ask(page, "Ask me once")
    _finished(page)
    assert page.locator('.msg-ai [data-tool="regenerate"]:not([hidden])').count() == 1
    model.script.append({"text": "Second answer."})
    page.locator('.msg-ai:last-child [data-tool="regenerate"]').click()
    page.wait_for_selector(".msg-ai .msg-body:has-text('Second answer')")
    _finished(page)
    assert page.locator(".msg-user").count() == 1 and page.locator(".msg-ai").count() == 1
    assert [m["role"] for m in model.bodies[-1]["messages"] if m["role"] != "system"] == ["user"]
    assert model.bodies[-1]["messages"][-1]["content"] == "Ask me once"


def test_a_failed_regenerate_leaves_the_saved_answer_for_next_time(page, app):
    _, api, _ = app
    chat_id = _chat(api, "Keep the good answer", answer="The good answer.")
    _reopen(page, chat_id)
    page.wait_for_selector(".msg-ai")
    page.route("**/api/ai/chat", lambda route: route.fulfill(status=500, json={"detail": "It broke"}))
    page.locator('.msg-ai:last-child [data-tool="regenerate"]').click()
    page.wait_for_selector("[data-retry]")
    assert "It broke" in page.locator(".msg-ai .banner").inner_text()
    assert _saved_chat(api, chat_id)["messages"][-1]["content"] == "The good answer."
    page.unroute("**/api/ai/chat")
    page.reload()
    page.wait_for_selector(".msg-ai .msg-body:has-text('The good answer')")


def test_only_the_last_question_can_be_edited_and_resending_replaces_its_answer(page, app):
    _, api, model = app
    model.script.append({"text": "One."})
    _ask(page, "Question one")
    _finished(page)
    model.script.append({"text": "Two."})
    _ask(page, "Question too")
    _finished(page, 2)
    assert page.locator('.msg-q').first.locator('[data-tool="edit"]').is_hidden()
    assert page.locator('.msg-ai').first.locator('[data-tool="regenerate"]').is_hidden()
    last = page.locator(".msg-q").last
    last.hover()
    last.locator('[data-tool="edit"]').click()
    field = page.locator(".msg-edit textarea")
    assert field.input_value() == "Question too"
    field.fill("Thrown away")
    field.press("Escape")
    assert page.locator(".msg-edit").count() == 0 and page.locator(".msg-user").last.inner_text() == "Question too"
    last.hover()
    last.locator('[data-tool="edit"]').click()
    field.fill("Question two")
    model.script.append({"text": "Two, properly."})
    with page.expect_response(lambda r: r.request.method == "PUT" and "/api/ai/chats/" in r.url) as saved:
        field.press("Enter")
    assert page.locator(".msg-user").all_inner_texts() == ["Question one", "Question two"]
    assert page.locator(".msg-ai").count() == 2 and "Two, properly" in page.locator(".msg-ai .msg-body").last.inner_text()
    assert [m["content"] for m in model.bodies[-1]["messages"] if m["role"] == "user"] == ["Question one", "Question two"]
    stored = _saved_chat(api, saved.value.json()["id"])["messages"]
    assert [m["content"] for m in stored] == ["Question one", "One.", "Question two", "Two, properly."]


def test_the_button_back_to_the_latest_message_comes_and_goes(page, app):
    _, api, _ = app
    chat_id = _chat(api, "Long read for scrolling", answer="\n\n".join(f"Paragraph {i}." for i in range(80)))
    _reopen(page, chat_id)
    page.wait_for_selector(".msg-ai")
    latest = page.locator("#chat-latest")
    assert not latest.is_visible()
    page.evaluate("() => { document.querySelector('#chat-log').scrollTop = 0; }")
    page.wait_for_selector("#chat-latest.on")
    latest.click()
    page.wait_for_selector("#chat-latest:not(.on)", state="attached")
    assert page.evaluate("() => { const l = document.querySelector('#chat-log'); return l.scrollHeight - l.scrollTop - l.clientHeight; }") < 2


def test_reading_above_while_an_answer_streams_isnt_interrupted(page, app):
    _, api, model = app
    chat_id = _chat(api, "Read while it writes", answer="\n\n".join(f"Paragraph {i}." for i in range(80)))
    _reopen(page, chat_id)
    page.wait_for_selector(".msg-ai")
    model.script.append({"text": "Still writing this", "hold": 1})
    _ask(page, "One more")
    page.wait_for_selector(".msg-ai:last-child .msg-body:has-text('Still writing')")
    page.evaluate("() => { document.querySelector('#chat-log').scrollTop = 100; }")
    page.wait_for_selector("#chat-latest.on")
    _finished(page, 2)
    page.wait_for_selector(".msg-ai:last-child .msg-foot.ready")
    assert page.evaluate("() => document.querySelector('#chat-log').scrollTop") == 100


def test_finishing_an_answer_pushes_nothing(page, app):
    # Its tools have their row before they appear, and a note that comes with the end goes under the text.
    _, _, model = app
    model.script.append({"text": "Being written", "hold": 1})
    _ask(page)
    page.wait_for_selector(".msg-ai .msg-body:has-text('Being written')")
    text = page.locator(".msg-ai .msg-body").bounding_box()
    row = page.locator(".msg-ai .msg-foot").bounding_box()
    assert page.locator('.msg-ai [data-tool="copy"]').is_hidden()
    page.wait_for_selector(".msg-ai .msg-foot.ready")
    assert page.locator(".msg-ai .msg-body").bounding_box() == text
    note = page.locator(".msg-ai .msg-note")
    pushed = note.bounding_box()["height"] + 6 if note.is_visible() else 0      # only what's below it, and only by the note
    assert page.locator(".msg-ai .msg-foot").bounding_box()["y"] == pytest.approx(row["y"] + pushed, abs=1)


def test_another_conversation_can_be_read_while_one_is_answered(page, app):
    _, api, model = app
    other = _chat(api, "Something to read meanwhile", answer="Read this.")
    _reopen(page)
    model.script.append({"text": "A slow answer", "hold": 2})
    _ask(page, "Takes a while")
    page.wait_for_selector(".msg-ai .msg-body:has-text('A slow answer')")
    live_id = page.locator(".ask-item.current").get_attribute("data-id")
    _row(page, "Something to read meanwhile").locator(".ask-item-open").click()
    page.wait_for_selector(".msg-user:has-text('Something to read meanwhile')")
    assert page.locator("#chat-send.stop").count() == 0 and page.locator(".msg-ai .msg-body").inner_text() == "Read this."
    assert page.locator(".ask-item.live").get_attribute("data-id") == live_id
    # A second question has to wait, and isn't lost.
    _ask(page, "Me too")
    page.wait_for_selector(".toast")
    assert page.input_value("#chat-text") == "Me too" and page.locator(".msg-user").count() == 1
    page.fill("#chat-text", "")
    # Back to it: what was written so far is there, and it can be stopped.
    page.locator(".ask-item.live .ask-item-open").click()
    page.wait_for_selector(".msg-user:has-text('Takes a while')")
    assert "A slow answer" in page.locator(".msg-ai .msg-body").inner_text() and page.locator("#chat-send.stop").count() == 1
    _row(page, "Something to read meanwhile").locator(".ask-item-open").click()
    page.wait_for_selector(".ask-item.live", state="detached", timeout=10000)
    # It finished out of sight and went to the right conversation.
    page.wait_for_function("(id) => [...document.querySelectorAll('.ask-item')].some((n) => n.dataset.id === id && !n.querySelector('.ask-item-more').hidden)", arg=live_id)
    assert _saved_chat(api, live_id)["messages"][-1]["content"].startswith("A slow answer")
    assert _saved_chat(api, other)["messages"][-1]["content"] == "Read this."
    assert page.errors == []


def test_what_was_being_typed_stays_with_its_conversation(page, app):
    _, api, _ = app
    first = _chat(api, "First with a draft")
    _chat(api, "Second with a draft")
    _reopen(page, first)
    page.fill("#chat-text", "half a thought")
    _row(page, "Second with a draft").locator(".ask-item-open").click()
    page.wait_for_selector(".msg-user:has-text('Second with a draft')")
    assert page.input_value("#chat-text") == ""
    _row(page, "First with a draft").locator(".ask-item-open").click()
    page.wait_for_selector(".msg-user:has-text('First with a draft')")
    assert page.input_value("#chat-text") == "half a thought"


# ---------------------------------------------------------------------------
# The keyboard and the address
# ---------------------------------------------------------------------------

def test_enter_sends_and_shift_enter_is_a_new_line(page, app):
    _, _, model = app
    page.fill("#chat-text", "line one")
    page.press("#chat-text", "Shift+Enter")
    page.keyboard.type("line two")
    assert page.input_value("#chat-text") == "line one\nline two" and page.locator(".msg-user").count() == 0
    model.script.append({"text": "Sent."})
    page.press("#chat-text", "Enter")
    _finished(page)
    assert page.locator(".msg-user").inner_text() == "line one\nline two" and page.input_value("#chat-text") == ""


def test_shortcuts_find_search_toggle_the_panel_and_start_a_new_chat(page, app):
    _, api, _ = app
    chat_id = _chat(api, "Shortcut chat")
    _reopen(page, chat_id)
    mod = _mod(page)
    page.keyboard.press(f"{mod}+b")
    assert page.get_attribute("#ask-split", "data-side") == "closed"
    page.keyboard.press(f"{mod}+k")                # opens the panel if it's closed, and goes to search
    assert page.get_attribute("#ask-split", "data-side") == "open" and page.evaluate("() => document.activeElement.id") == "ask-search"
    page.keyboard.press(f"{mod}+Shift+o")
    page.wait_for_selector(".chat-empty")
    assert page.evaluate("() => location.hash") == "#/ask"


def test_the_arrow_keys_move_from_search_through_the_list(page, app):
    _, api, _ = app
    _chat(api, "Arrow target one")
    _chat(api, "Arrow target two")
    _reopen(page)
    page.locator("#ask-search").focus()
    page.keyboard.press("ArrowDown")
    assert page.evaluate("() => document.activeElement.className") == "ask-item-open"
    first = page.evaluate("() => document.activeElement.textContent")
    page.keyboard.press("ArrowDown")
    assert page.evaluate("() => document.activeElement.textContent") != first
    page.keyboard.press("ArrowUp")
    page.keyboard.press("ArrowUp")
    assert page.evaluate("() => document.activeElement.id") == "ask-search"


@pytest.mark.parametrize(("shell", "key"), [("Mac", None), ("Windows", "Control+n")])
def test_new_chat_in_the_desktop_apps_doesnt_redraw_the_page(browser, app, shell, key):
    _, api, model = app
    chat_id = _chat(api, "Before the new chat")
    context = _context(browser, app, user_agent=f"Mozilla/5.0 SyntropyHealthDesktop/1.6.0 ({shell})")
    if shell == "Windows":
        context.add_init_script("Object.defineProperty(navigator, 'platform', { get: () => 'Win32' })")
    page = _open(context, f"/#/ask?chat={chat_id}")
    assert page.evaluate("() => document.documentElement.dataset.shell") == shell.lower()
    page.evaluate("() => { document.querySelector('#view').dataset.same = 'yes'; }")
    model.script.append({"text": "Still coming", "hold": 2})
    _ask(page, "Keeps going past a new chat")
    page.wait_for_selector(".msg-ai .msg-body:has-text('Still coming')")
    if key:
        page.keyboard.press(key)
    else:
        page.evaluate("() => { location.hash = '#/ask?new=1'; }")      # what File → New Chat does (mac_window.py)
    page.wait_for_selector(".chat-empty")
    assert page.evaluate("() => document.querySelector('#view').dataset.same") == "yes" and page.evaluate("() => location.hash") == "#/ask"
    assert page.locator(".ask-item.live").count() == 1             # the answer carries on
    assert ("⌘N" if shell == "Mac" else "Ctrl+N") in page.get_attribute("#ask-new", "title")
    assert page.errors == []
    context.close()


def test_the_address_follows_the_conversation_so_a_reload_returns_to_it(page, app):
    _, api, _ = app
    old = _chat(api, "Come back to this")
    _chat(api, "Newer than that")
    _reopen(page)
    _row(page, "Come back to this").locator(".ask-item-open").click()
    page.wait_for_selector(".msg-user:has-text('Come back to this')")
    assert page.evaluate("() => location.hash") == f"#/ask?chat={old}"
    page.reload()
    page.wait_for_selector(".msg-user:has-text('Come back to this')")
    assert page.locator(".ask-item.current .ask-item-title").inner_text() == "Come back to this"


def test_an_address_naming_a_conversation_that_isnt_there_opens_the_latest(page, app):
    _, api, _ = app
    _chat(api, "The latest one")
    page.goto("/#/ask?chat=c_gone0000000000")
    page.reload()
    page.wait_for_selector(".msg-user:has-text('The latest one')")
    assert page.errors == []


# ---------------------------------------------------------------------------
# In the phone apps
# ---------------------------------------------------------------------------

PHONE_BRIDGE = """
window.sent = [];
window.webkit = { messageHandlers: { syntropy: { postMessage: (m) => { window.sent.push(JSON.parse(JSON.stringify(m))); } } } };
window.SyntropyApp = { capabilities: ["toolbar", "composer", "scroll", "sections", "haptics", "menu"] };
"""


def _phone_menu(page):
    return page.evaluate("() => window.sent.filter((m) => m.type === 'toolbar').at(-1).items.find((i) => i.id === 'history').menu")


def test_the_phone_apps_conversations_menu_can_rename_and_pin(browser, app):
    _, api, model = app
    chat_id = _chat(api, "On the phone")
    context = _context(browser, app, viewport={"width": 390, "height": 800}, user_agent="Mozilla/5.0 (iPhone) SyntropyHealthApp/2.1 SyntropyNav/1")
    context.add_init_script(PHONE_BRIDGE)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"/#/ask?chat={chat_id}")
    page.wait_for_selector(".msg-user:has-text('On the phone')")
    page.wait_for_function("() => window.sent.some((m) => m.type === 'toolbar')")
    # The app keeps its own field and title bar, and the page draws none of the panel.
    assert page.locator("#ask-side, #chat-text").count() == 0
    assert page.evaluate("() => window.sent.filter((m) => m.type === 'composer').at(-1).visible") is True
    assert page.locator(".chat-foot").inner_text().endswith("doesn't give medical advice.")
    assert [i["id"] for i in _phone_menu(page)[-1]["items"]] == ["chat:rename", "chat:pin", "chat:delete"]
    with page.expect_response(lambda r: r.request.method == "PATCH"):
        page.evaluate("() => window.SyntropyShell.action('chat:pin')")
    assert _saved_chat(api, chat_id)["pinned"] is True
    page.wait_for_function("() => window.sent.filter((m) => m.type === 'toolbar').at(-1).items.find((i) => i.id === 'history').menu.at(-1).items[1].title.startsWith('Unpin')")
    assert _phone_menu(page)[0]["items"][0]["title"] == "On the phone" and _phone_menu(page)[0]["items"][0]["symbol"] == "pin.fill"
    page.evaluate("() => window.SyntropyShell.action('chat:rename')")
    page.fill(".modal input", "Named on the phone")
    with page.expect_response(lambda r: r.request.method == "PATCH"):
        page.locator(".modal [data-yes]").click()
    assert _saved_chat(api, chat_id)["title"] == "Named on the phone"
    # The app's own message field still asks, and the name stays.
    model.script.append({"text": "An answer on the phone."})
    with page.expect_response(lambda r: r.request.method == "PUT" and "/api/ai/chats/" in r.url):
        page.evaluate("() => window.SyntropyShell.composer({ type: 'send', text: 'And then?' })")
    assert page.locator(".msg-user").count() == 2 and _saved_chat(api, chat_id)["title"] == "Named on the phone"
    page.wait_for_function("() => !window.sent.filter((m) => m.type === 'composer').at(-1).busy")
    assert errors == []
    context.close()


# ---------------------------------------------------------------------------
# WebKit, which the Mac and iPhone apps use
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def webkit(engines):
    try:
        engine = engines.webkit.launch()
    except Exception as exc:  # noqa: BLE001 - playwright install webkit hasn't been run here
        pytest.skip(f"WebKit isn't available: {exc}")
    yield engine
    engine.close()


@pytest.mark.parametrize(("width", "shell"), [(1280, "Mac"), (390, None)])
def test_the_panel_and_tools_work_in_webkit(webkit, app, width, shell):
    _, api, model = app
    _chat(api, "In WebKit", answer=f"Here:\n\n{TABLE}")
    context = _context(webkit, app, reduced_motion="no-preference", viewport={"width": width, "height": 800},
                       **({"user_agent": f"Mozilla/5.0 AppleWebKit/605.1.15 SyntropyHealthDesktop/1.6.0 ({shell})"} if shell else {}))
    page = _open(context)
    if width < 1024:
        page.click("#ask-toggle")
        page.wait_for_timeout(300)
    else:
        page.click("#ask-toggle")
        page.wait_for_selector("#ask-split:not(.side-gliding)", state="attached")
        assert page.get_attribute("#ask-split", "data-side") == "closed"
        page.click("#ask-toggle")
        page.wait_for_selector("#ask-split:not(.side-gliding)", state="attached")
    row = _row(page, "In WebKit").first
    row.hover()
    row.locator(".ask-item-more").click()
    page.locator(".popover.dropdown:not(.closing) .menu-item", has_text="Rename").click()
    page.locator(".ask-item .ask-rename").fill(f"Renamed in WebKit at {width}")
    with page.expect_response(lambda r: r.request.method == "PATCH"):
        page.locator(".ask-item .ask-rename").press("Enter")
    _row(page, f"Renamed in WebKit at {width}").locator(".ask-item-open").click()
    page.wait_for_selector(".msg-ai .md-block table")
    model.script.append({"text": "An answer in WebKit."})
    _ask(page, "Does it stream?")
    page.wait_for_selector(".msg-ai:last-child .msg-foot.ready")
    assert page.errors == [] and page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    context.close()
