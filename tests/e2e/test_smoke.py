"""The whole app in a browser: first run, every section at each width and theme, and an axe pass where axe-core is present."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tests.e2e.conftest import PASSWORD, Session, seed

ROUTES = ["#/overview", "#/ask", "#/timeline", "#/records", "#/records/labs", "#/trends", "#/trends?metric=sleep_duration", "#/trends/sleep", "#/workouts", "#/journal", "#/sources", "#/report", "#/prepare", "#/people",
          "#/settings/general", "#/settings/people", "#/settings/security", "#/settings/ai", "#/settings/data", "#/settings/developer"]
SETTINGS = [("desktop", False), ("phone", False), ("desktop", True), ("phone", True)]
AXE = os.environ.get("AXE_CORE_JS", "")


def _no_overflow(page) -> None:
    wide = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert wide <= 1, f"The page scrolls sideways by {wide}px at {page.url}"


@pytest.mark.parametrize(("width", "dark"), SETTINGS)
def test_every_section_draws_without_errors(browser, server, width, dark):
    s = Session(browser, server, width=width, dark=dark)
    for hash_ in ROUTES:
        page = s.open(hash_)
        assert page.locator("#view").count() == 1, hash_
        assert page.locator(".page-error").count() == 0, f"{hash_}: {page.locator('.page-error').inner_text()}"
        assert page.locator("#view").inner_text().strip(), hash_
        assert not re.search(r"\b(undefined|NaN|\[object Object\])\b", page.locator("#view").inner_text()), hash_
        _no_overflow(page)
    assert s.errors == []
    s.close()


def test_first_run_goes_from_setup_through_onboarding(browser, fresh_server):
    s = Session(browser, fresh_server, sign_in=False)
    page = s.open("#/")
    # A name of only spaces isn't a name.
    page.locator("#s-name").fill("   ")
    page.locator("#ob-name button[type=submit]").click()
    page.wait_for_selector("#s-name")
    assert page.locator("#s-pass").count() == 0
    page.locator("#s-name").fill("Alex")
    page.locator("#ob-name button[type=submit]").click()
    page.locator("#s-pass").fill(PASSWORD)
    page.locator("#s-pass2").fill(PASSWORD)
    page.locator("#setup-form input[type=checkbox]:not(#s-nopass)").check()
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.endswith("/api/setup") else None)
    page.locator("#setup-form button[type=submit]").dblclick()
    page.wait_for_selector(".onboard-dots[aria-label='Step 3 of 5']")
    assert len(posts) == 1
    # The address changing mid-way (Back, a pasted link) waits until the steps are done.
    page.evaluate("location.hash = '#/records'")
    page.wait_for_timeout(300)
    assert page.locator(".onboard-card").count() == 1
    for _ in range(6):
        if page.locator(".onboard-card").count() == 0:
            break
        page.locator(".onboard-foot [data-action=done], .onboard-foot [data-action=next]").last.click()
        page.wait_for_timeout(200)
    page.wait_for_selector("#view")
    assert s.errors == []
    s.close()


def test_a_person_with_no_data_gets_clean_pages(browser, fresh_server):
    seed(fresh_server, simulated=False).close()
    s = Session(browser, fresh_server, width="phone")
    for hash_ in ROUTES:
        assert s.open(hash_).locator(".page-error").count() == 0, hash_
    assert s.errors == []
    s.close()


def test_simulated_data_is_labelled_and_not_offered_outside_developer_mode(browser, server):
    s = Session(browser, server)
    for hash_ in ("#/overview", "#/trends", "#/ask"):
        page = s.open(hash_)
        page.wait_for_selector(".sim-note")
        assert "Includes simulated data" in page.locator(".sim-note").inner_text()
    page = s.open("#/journal")
    assert page.locator(".sim-note").count() == 0
    page = s.open("#/sources")
    assert page.get_by_text("Try simulated data").count() == 0
    s.close()


def test_developer_mode_asks_before_adding_simulated_data(browser, fresh_server):
    c = seed(fresh_server, simulated=False)
    assert c.put("/api/settings/developer", json={"enabled": True}).status_code == 200
    s = Session(browser, fresh_server)
    page = s.open("#/sources")
    page.get_by_text("Try simulated data").first.click()
    page.get_by_role("button", name="Add simulated data").wait_for()
    page.get_by_role("button", name="Cancel").last.click()
    page.wait_for_timeout(300)
    assert not [x for x in c.get("/api/connections").json()["connections"] if x["mode"] == "simulated"]
    c.close()
    s.close()


def test_view_only_people_arent_shown_controls_that_fail(browser, fresh_server):
    c = seed(fresh_server, simulated=False)
    me = c.get("/api/status").json()["profiles"][0]
    invited = c.post("/api/profiles", json={"name": "Jamie", "relationship": "partner"}).json()
    code = c.post(f"/api/profiles/{invited['id']}/invite", json={}).json()["code"]
    jamie = fresh_server.client()
    assert jamie.post("/api/invites/accept", json={"code": code, "password": "another good password", "accept_terms": True}).status_code == 200
    jamie.post("/api/onboarding/done")
    jamie_account = jamie.get("/api/status").json()["account"]["id"]
    assert c.put(f"/api/profiles/{me['id']}/access", json={"account_id": jamie_account, "level": "view"}).status_code == 200

    s = Session(browser, fresh_server, sign_in=False)
    r = s.ctx.request.post(f"{fresh_server.url}/api/auth/login", data={"password": "another good password", "account": jamie_account},
                           headers={"X-Requested-With": "syntropy"})
    assert r.ok
    page = s.open("#/journal")
    page.evaluate(f"localStorage.setItem('syntropy-profile', '{me['id']}')")
    page.reload()
    page.wait_for_selector("#view-only span")
    assert not page.locator("#jr-checkin").is_visible()
    page = s.open("#/overview")
    assert page.locator("[data-action=edit]:visible").count() == 0

    # Sharing ends while Jamie is here: back to Jamie's own record, saying why.
    assert c.put(f"/api/profiles/{me['id']}/access", json={"account_id": jamie_account, "level": None}).status_code == 200
    page.evaluate("location.hash = '#/records'")
    page.wait_for_selector("#toasts .toast")
    assert "no longer have access" in page.locator("#toasts").inner_text()
    page.wait_for_selector(".page-error", state="detached")
    c.close()
    jamie.close()
    s.close()


def test_back_comes_back_to_the_list_and_closes_a_drawer(browser, server):
    s = Session(browser, server, width="phone")
    s.open("#/overview")
    page = s.open("#/trends")
    page.locator("[data-action=pick]").first.click()
    s.settle()
    page.go_back()
    page.wait_for_selector("#view:not([aria-busy])")
    s.settle()
    assert "#/trends" in page.url
    assert page.locator("[data-action=pick]").first.is_visible()
    s.close()

    s = Session(browser, server)
    s.open("#/journal")
    page = s.open("#/records")
    page.locator("[data-action=open]").first.click()
    page.wait_for_selector(".drawer")
    page.go_back()
    page.wait_for_selector(".drawer", state="detached")
    assert page.url.endswith("#/records")
    # Closing it on the page leaves Back where it was too.
    page.locator("[data-action=open]").first.click()
    page.wait_for_selector(".drawer")
    page.locator(".drawer [data-close]").click()
    page.wait_for_selector(".drawer", state="detached")
    page.wait_for_timeout(100)
    assert page.url.endswith("#/records")
    page.go_back()
    page.wait_for_selector("#jr-checkin")
    s.close()


def test_overview_layout_is_saved_when_leaving_straight_away(browser, server):
    s = Session(browser, server)
    page = s.open("#/overview")
    puts: list[str] = []
    page.on("request", lambda r: puts.append(r.url) if r.method == "PUT" and r.url.endswith("/overview") else None)
    page.locator("[data-action=edit]").first.click()
    page.locator("[data-action=remove]").first.click()
    s.open("#/journal")
    page.wait_for_timeout(100)
    assert len(puts) == 1
    s.close()


def test_back_returns_to_where_the_page_was_scrolled(browser, server):
    s = Session(browser, server, width="phone")
    page = s.open("#/sources")
    page.evaluate("window.scrollTo(0, 300)")
    assert page.evaluate("window.scrollY") == 300
    s.open("#/journal")
    assert page.evaluate("window.scrollY") == 0
    page.go_back()
    page.wait_for_selector("#imp-status", state="attached")
    s.settle()
    assert page.evaluate("window.scrollY") == 300
    # Going there afresh starts at the top.
    s.open("#/journal")
    s.open("#/sources")
    assert page.evaluate("window.scrollY") == 0
    s.close()


def test_long_names_dont_widen_the_page(browser, fresh_server):
    seed(fresh_server, simulated=False).close()
    c = fresh_server.client()
    c.cookies.clear()
    s = Session(browser, fresh_server, width="phone")
    name = "A" * 80
    r = s.ctx.request.post(f"{fresh_server.url}/api/profiles", data={"name": name, "relationship": "child"}, headers={"X-Requested-With": "syntropy"})
    assert r.ok
    page = s.open("#/settings/people")
    page.evaluate(f"localStorage.setItem('syntropy-profile', '{r.json()['id']}')")
    page.reload()
    for hash_ in ("#/overview", "#/sources", "#/report", "#/settings/people", "#/settings/security"):
        page = s.open(hash_)
        widest = page.evaluate("""() => [...document.querySelectorAll('body *')]
          .filter((e) => e.getBoundingClientRect().right > innerWidth + 1 && !e.closest('.table-wrap, .truncate'))
          .slice(-4).map((e) => e.localName + '.' + e.className).join(' ')""")
        wide = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert wide <= 1, f"{hash_}: {widest}"
    page.set_viewport_size({"width": 320, "height": 700})
    _no_overflow(s.open("#/settings/general"))
    _no_overflow(s.open("#/report"))
    c.close()
    s.close()


def test_a_chart_reads_out_its_values_and_opens_as_a_table(session):
    page = session.open("#/trends")
    page.wait_for_selector(".chart svg")
    label = page.locator(".chart svg[role=img]").first.get_attribute("aria-label")
    assert re.search(r"\d+ values? (from|on) .*Latest .*lowest .*highest", label), label
    box = page.locator(".chart-table").first
    box.locator("summary").focus()
    page.keyboard.press("Enter")
    box.locator("tbody tr").first.wait_for()
    assert session.errors == []


def _axe(page, script: str) -> dict[str, list[str]]:
    page.add_script_tag(content=script)
    res = page.evaluate("axe.run(document, { resultTypes: ['violations'] })")
    return {f"{page.url.split('#')[-1]} {v['id']}": [n["html"][:110] for n in v["nodes"][:3]] for v in res["violations"] if v["impact"] in ("serious", "critical")}


@pytest.mark.skipif(not AXE or not Path(AXE).is_file(), reason="AXE_CORE_JS names a copy of axe-core; CI fetches one")
def test_axe_on_sign_in_setup_and_open_panels(browser, server, fresh_server):
    script = Path(AXE).read_text()
    found: dict[str, list[str]] = {}
    s = Session(browser, fresh_server, sign_in=False, bypass_csp=True)
    page = s.open("#/")
    page.wait_for_selector("#s-name")
    found |= _axe(page, script)
    s.close()
    s = Session(browser, server, sign_in=False, bypass_csp=True)
    page = s.open("#/")
    page.wait_for_selector("#login-form")
    found |= _axe(page, script)
    s.close()
    s = Session(browser, server, bypass_csp=True)
    page = s.open("#/overview")
    page.locator(".topbar .alerts-btn").click()
    found |= _axe(page, script)
    page.keyboard.press("Escape")
    page = s.open("#/sources")
    page.locator("[data-action=add]").first.click()
    page.wait_for_selector(".dropdown")
    found |= _axe(page, script)
    page.keyboard.press("Escape")
    page.locator("[data-action=add-ehr]").first.click()
    page.wait_for_selector(".modal")
    s.settle()
    page.wait_for_timeout(350)
    found |= _axe(page, script)
    s.close()
    assert not found, found


@pytest.mark.skipif(not AXE or not Path(AXE).is_file(), reason="AXE_CORE_JS names a copy of axe-core; CI fetches one")
@pytest.mark.parametrize(("width", "dark"), SETTINGS)
def test_axe_finds_nothing_serious(browser, server, width, dark):
    s = Session(browser, server, width=width, dark=dark, bypass_csp=True)
    script = Path(AXE).read_text()
    found: dict[str, list[str]] = {}
    for hash_ in ROUTES:
        page = s.open(hash_)
        page.add_script_tag(content=script)
        res = page.evaluate("axe.run(document, { resultTypes: ['violations'] })")
        for v in res["violations"]:
            if v["impact"] in ("serious", "critical"):
                found.setdefault(f"{hash_} {v['id']}", []).extend(n["html"][:110] + " | " + (n.get("any") or [{}])[0].get("message", "")[:90] for n in v["nodes"][:3])
    s.close()
    assert not found, "\n".join(f"{k}: {v}" for k, v in sorted(found.items()))
