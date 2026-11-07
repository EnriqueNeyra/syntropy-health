"""Fixes from a review of the web app: what a page shows stays true while it loads, and when something fails."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.e2e.conftest import Session, seed

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
