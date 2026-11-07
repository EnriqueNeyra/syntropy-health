"""Fixes from a review of the web app: what a page shows stays true while it loads, and when something fails."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest

from tests.e2e.conftest import Session, seed
from tests.test_ask_browser import Model
from tests.test_data import EXPORT_XML

# Holds back the answers to chosen addresses for a while: window.__slow is a list of [part of the address, milliseconds].
SLOW = """(() => { const orig = window.fetch; window.__slow = window.__slow || [];
  window.fetch = async (u, o) => { const s = String(u); const hit = window.__slow.find(([p]) => s.includes(p));
    if (hit) await new Promise((r) => setTimeout(r, hit[1])); return orig(u, o); }; })()"""


@pytest.fixture
def two_people(fresh_server):
    """Alex, with a recent out-of-range result (an alert in the bell), and Sam, with nothing."""
    c = seed(fresh_server, simulated=False)
    alex = c.get("/api/profiles").json()["profiles"][0]["id"]
    recent = (date.today() - timedelta(days=3)).isoformat()
    r = c.post("/api/labs", json={"collected": recent, "results": [{"test": "Potassium", "value": 6.4, "unit": "mmol/L", "ref_low": 3.5, "ref_high": 5.1}]})
    assert r.status_code == 200, r.text
    sam = c.post("/api/profiles", json={"name": "Sam", "relationship": "child"}).json()["id"]
    c.close()
    return fresh_server, alex, sam


def _switch_to(page, profile_id):
    page.click("#profile-btn")
    page.click(f".popover [data-profile='{profile_id}']")


def _bell(page):
    """What the bell's panel lists."""
    page.wait_for_selector(".popover", state="detached")
    page.click(".topbar .alerts-btn")
    panel = page.locator(".popover[aria-label=Alerts] .popover-body")
    text = panel.inner_text()
    page.keyboard.press("Escape")
    return text


def test_switching_person_empties_the_bell_until_theirs_have_loaded(browser, two_people):
    server, alex, sam = two_people
    s = Session(browser, server)
    page = s.page
    page.add_init_script(SLOW)
    s.open("#/overview")
    page.wait_for_selector(".topbar .alerts-btn .count-badge:not([hidden])")
    assert "Potassium" in _bell(page)
    s.settle()
    page.evaluate(f"() => window.__slow.push(['/api/summary?profile={sam}', 3000])")
    seen: list[str] = []
    page.on("request", lambda r: seen.append(r.url) if r.method == "POST" and "/alerts" in r.url else None)
    _switch_to(page, sam)
    assert page.locator(".topbar .alerts-btn .count-badge").is_hidden()
    assert "Potassium" not in _bell(page)
    page.wait_for_selector("#view:not([aria-busy])")
    s.settle()
    assert "Potassium" not in _bell(page)
    # Opening the bell marks what's in it seen: never Alex's alerts, for Sam.
    assert not [u for u in seen if alex in u]
    _switch_to(page, alex)
    page.wait_for_selector("#view:not([aria-busy])")
    s.settle()
    assert "Potassium" in _bell(page)
    assert s.errors == []
    s.close()


def test_a_page_still_loading_for_the_last_person_leaves_the_bell_alone(browser, two_people):
    server, alex, sam = two_people
    s = Session(browser, server)
    page = s.page
    # Alex's Overview takes a while: Sam is chosen before it's done, and it finishes afterwards.
    page.add_init_script(f"window.__slow = [['/api/new-records?profile={alex}', 2500]];")
    page.add_init_script(SLOW)
    page.goto(f"{server.url}/#/overview")
    page.wait_for_selector("#profile-btn .avatar")
    _switch_to(page, sam)
    page.wait_for_timeout(3500)
    s.settle()
    assert "Sam" in page.inner_text("#profile-btn")
    assert page.locator(".topbar .alerts-btn .count-badge").is_hidden()
    assert "Potassium" not in _bell(page)
    assert s.errors == []
    s.close()


def test_alerts_that_cant_be_loaded_arent_left_from_before(browser, two_people):
    server, alex, sam = two_people
    s = Session(browser, server)
    page = s.page
    s.open("#/timeline")
    page.wait_for_selector(".topbar .alerts-btn .count-badge:not([hidden])")
    # Every page but the Overview asks for the alerts itself; this time the server can't answer.
    s.ctx.route(f"**/api/summary?profile={sam}*", lambda route: route.fulfill(status=500, body="{}"))
    _switch_to(page, sam)
    page.wait_for_selector("#view:not([aria-busy])")
    s.settle()
    assert "Potassium" not in _bell(page)
    s.close()


def test_a_record_files_warnings_stay_on_the_page(browser, fresh_server, tmp_path):
    c = seed(fresh_server, simulated=False)
    alex = c.get("/api/profiles").json()["profiles"][0]["id"]
    assert c.patch(f"/api/profiles/{alex}", json={"birth_date": "1980-01-01"}).status_code == 200
    c.close()
    # Someone else's record: the import says so, after the list has been drawn again with the new file in it.
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Zed"], "family": "Other"}], "birthDate": "1990-05-05"}},
        {"resource": {"resourceType": "Condition", "id": "c1", "subject": {"reference": "Patient/p1"}, "code": {"text": "Asthma"},
                      "clinicalStatus": {"coding": [{"code": "active"}]}, "recordedDate": "2024-01-01"}}]}
    f = tmp_path / "other-person.json"
    f.write_text(json.dumps(bundle))
    s = Session(browser, fresh_server)
    page = s.open("#/sources")
    with page.expect_response(lambda r: "/api/imports/fhir" in r.url) as answer:
        page.set_input_files("#imp-fhir", str(f))
    assert answer.value.json()["warnings"]
    page.locator("#imp-status .banner.warn").wait_for()
    assert "something to check" in page.inner_text("#imp-status")
    s.close()


def test_an_apple_health_imports_progress_shows_while_the_list_is_drawn_again(browser, server, tmp_path):
    s = Session(browser, server)
    page = s.page
    # A source syncing: the list is drawn again every second and a half while the import runs.
    def syncing(route):
        res = route.fetch()
        body = res.json()
        for c in body.get("connections", []):
            c["syncing"] = True
        route.fulfill(response=res, json=body)
    polls = {"n": 0}
    def running(route):
        if route.request.method != "GET" or polls["n"] >= 6:
            return route.fallback()
        polls["n"] += 1
        route.fulfill(json={"id": route.request.url.rsplit("/", 1)[-1], "status": "running", "processed": 100 * polls["n"]})
    page.route("**/api/connections?*", syncing)
    page.route(re.compile(r".*/api/imports/[^/?]+$"), running)
    page = s.open("#/sources")
    f = tmp_path / "export.xml"
    f.write_text(EXPORT_XML)
    page.set_input_files("#imp-apple", str(f))
    page.wait_for_function("() => /processed/.test(document.querySelector('#imp-status')?.textContent)")
    page.wait_for_function("() => /[3-6]00 samples processed/.test(document.querySelector('#imp-status')?.textContent)")
    page.wait_for_function("() => /Imported/.test(document.querySelector('#imp-status')?.textContent)", timeout=20000)
    s.close()


def test_ask_reads_an_answer_out_once_when_its_written(browser, fresh_server):
    model = Model()
    c = seed(fresh_server, simulated=False)
    assert c.put("/api/ai/config", json={"provider": "local", "base_url": model.url, "model": "m"}).status_code == 200
    c.close()
    answer = " ".join(f"Sentence number {i} about your sleep and your heart rate." for i in range(40))
    model.script.append({"text": answer})
    # With motion: the answer is written out over many frames.
    s = Session(browser, fresh_server, reduced_motion="no-preference")
    page = s.open("#/ask?new=1")
    page.wait_for_selector("#chat-text")
    # Nothing the conversation draws is read out as it's drawn: neither each frame of an answer nor a whole thread opened.
    assert not page.evaluate("!!document.querySelector('#chat-log').closest('[aria-live], [role=status], [role=log], [role=alert]')")
    page.evaluate("""() => { window.__said = []; window.__busy = [];
      const status = document.querySelector('#ask-said');
      new MutationObserver(() => { if (status.textContent.trim()) window.__said.push(status.textContent); })
        .observe(status, { childList: true, characterData: true, subtree: true });
      new MutationObserver(() => { for (const n of document.querySelectorAll('.msg-ai.streaming')) window.__busy.push(n.getAttribute('aria-busy')); })
        .observe(document.querySelector('#chat-log'), { childList: true, characterData: true, subtree: true }); }""")
    page.fill("#chat-text", "How did I sleep?")
    page.press("#chat-text", "Enter")
    page.wait_for_function("() => window.__said.length > 0", timeout=20000)
    busy = page.evaluate("window.__busy")
    assert len(busy) > 5 and set(busy) == {"true"}, busy[:10]
    assert page.locator(".msg-ai[aria-busy]").count() == 0
    said = page.evaluate("window.__said")
    assert len(said) == 1 and "Sentence number 39" in said[0], said
    assert page.get_attribute("#ask-said", "role") == "status"
    # Opening it again draws the whole thread: nothing more is read out.
    chat = page.url.split("#", 1)[1]
    page.click("#ask-new")
    page.evaluate("(h) => { location.hash = h; }", f"#{chat}")
    page.wait_for_selector(".msg-ai")
    page.wait_for_timeout(300)
    assert page.evaluate("window.__said.length") == 1
    assert s.errors == []
    s.close()


def _token_dialog(page):
    page.click("[data-action=connect-app]")
    page.locator(".ai-app", has_text="Cursor").click()
    return page.locator("#tok-profile")


def test_an_ai_apps_access_starts_at_the_one_person_and_everyone_says_it_includes_people_added_later(browser, fresh_server):
    c = seed(fresh_server, simulated=False)
    alex = c.get("/api/profiles").json()["profiles"][0]["id"]
    s = Session(browser, fresh_server)
    page = s.open("#/settings/ai")
    who = _token_dialog(page)
    assert who.input_value() == alex
    assert who.locator("option:checked").inner_text() == "Only Alex's records"
    everyone = who.locator("option[value='']").inner_text()
    assert "including people added later" in everyone and "Your records" not in everyone
    with page.expect_request(lambda r: r.url.endswith("/api/agents/tokens") and r.method == "POST") as sent:
        page.click("[data-step=create]")
    assert sent.value.post_data_json["profile_id"] == alex
    page.keyboard.press("Escape")
    # With two people, the one open is chosen; "everyone" still says what it means.
    sam = c.post("/api/profiles", json={"name": "Sam", "relationship": "child"}).json()["id"]
    page.evaluate(f"localStorage.setItem('syntropy-profile', '{sam}')")
    page.reload()
    page = s.open("#/settings/ai")
    who = _token_dialog(page)
    assert who.input_value() == sam
    assert "including people added later" in who.locator("option[value='']").inner_text()
    c.close()
    s.close()


def test_a_visit_question_that_couldnt_be_saved_stays_in_the_box_to_try_again(browser, fresh_server):
    c = seed(fresh_server, simulated=False)
    alex = c.get("/api/profiles").json()["profiles"][0]["id"]
    s = Session(browser, fresh_server)
    page = s.open("#/prepare")
    failing = lambda route: route.fulfill(status=500, json={"detail": "The disk is full."}) if route.request.method == "PUT" else route.fallback()
    page.route("**/api/report/preferences", failing)
    page.fill("#prep-question", "Should I keep taking the iron?")
    page.click("#prep-form button[type=submit]")
    page.locator(".toast", has_text="Couldn't save").wait_for()
    assert page.input_value("#prep-question") == "Should I keep taking the iron?"
    assert page.locator("#prep-questions li").count() == 0
    page.unroute("**/api/report/preferences")
    page.click("#prep-form button[type=submit]")
    page.locator("#prep-questions li", has_text="iron").wait_for()
    assert page.input_value("#prep-question") == ""
    assert c.get("/api/report/preferences", params={"profile": alex}).json()["questions"] == ["Should I keep taking the iron?"]
    # Taking one off that can't be saved leaves it on the list too.
    page.route("**/api/report/preferences", failing)
    page.click("[data-remove-question='0']")
    page.locator(".toast", has_text="Couldn't save").last.wait_for()
    page.wait_for_timeout(200)
    assert page.locator("#prep-questions li", has_text="iron").count() == 1
    c.close()
    s.close()
