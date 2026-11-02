"""What keyboard and screen-reader users depend on, checked in a real browser."""
from __future__ import annotations

import re
import time

import pytest

from tests.e2e.conftest import Session, seed

PAGES = ["#/overview", "#/records", "#/timeline", "#/trends", "#/workouts", "#/journal", "#/sources", "#/settings/general", "#/ask", "#/prepare", "#/people"]


@pytest.mark.parametrize("hash_", PAGES)
def test_everything_that_acts_can_take_focus(session, hash_):
    page = session.open(hash_)
    stuck = page.evaluate("""() => [...document.querySelectorAll('[data-action]')]
      .filter((el) => el.tabIndex < 0 && !el.disabled && el.getAttribute('role') !== 'radio')
      .map((el) => el.outerHTML.slice(0, 80))""")
    assert stuck == []


def test_a_record_opens_from_the_keyboard(session):
    page = session.open("#/records")
    row = page.locator("[data-action=open]").first
    assert row.get_attribute("role") == "button"
    row.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector(".drawer")
    assert page.locator(".drawer h2").is_visible()


def test_a_switched_off_state_says_false(session):
    page = session.open("#/journal")
    states = page.eval_on_selector_all(".jr-moods [role=radio]", "els => els.map((e) => e.getAttribute('aria-checked'))")
    assert states and all(s in ("true", "false") for s in states)


def test_tab_stays_inside_a_dialog(session):
    page = session.open("#/records")
    page.locator("[data-action=open]").first.click()
    page.wait_for_selector(".drawer .modal-body :not(.skeleton)")
    assert page.evaluate("document.getElementById('root').inert")
    for _ in range(12):
        page.keyboard.press("Tab")
        assert page.evaluate("!document.getElementById('root').contains(document.activeElement)")
    page.keyboard.press("Escape")
    page.wait_for_selector(".drawer", state="detached")
    assert not page.evaluate("document.getElementById('root').inert")


def test_a_menu_works_from_the_keyboard(session):
    page = session.open("#/sources")
    opener = page.locator("[data-action=menu], [data-action=more], [aria-label^='More for']").first
    opener.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector(".dropdown")
    entries = page.locator(".dropdown .menu-item")
    assert page.evaluate("document.activeElement.classList.contains('menu-item')")
    first = entries.first.inner_text()
    page.keyboard.press("End")
    page.keyboard.press("Home")
    assert page.evaluate("document.activeElement.innerText") == first
    page.keyboard.press("Escape")
    page.wait_for_selector(".dropdown", state="detached")
    assert page.evaluate("document.activeElement.getAttribute('aria-expanded')") == "false"


def test_alerts_panel_has_a_name_and_takes_focus(session):
    page = session.open("#/overview")
    button = page.locator(".topbar .alerts-btn")
    button.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector(".popover[role=dialog]")
    assert page.locator(".popover[role=dialog]").get_attribute("aria-label") == "Alerts"
    assert page.evaluate("!!document.activeElement.closest('.popover')")
    page.keyboard.press("Escape")
    assert page.evaluate("document.activeElement.classList.contains('alerts-btn')")


def test_a_person_can_import_a_file_from_the_keyboard(session):
    page = session.open("#/sources")
    for selector in ("#imp-apple", "#imp-fhir"):
        box = page.locator(selector)
        if box.count():
            assert box.evaluate("el => el.tabIndex === 0 && !el.hidden")


def test_choosing_a_mood_keeps_focus_and_the_arrows_move(session):
    page = session.open("#/journal")
    mood = page.locator(".jr-moods [role=radio]").nth(1)
    mood.focus()
    page.keyboard.press("Enter")
    assert page.evaluate("document.activeElement.getAttribute('aria-checked')") == "true"
    value = page.evaluate("document.activeElement.dataset.value")
    page.keyboard.press("ArrowRight")
    assert page.evaluate("document.activeElement.getAttribute('aria-checked')") == "true"
    assert page.evaluate("document.activeElement.dataset.value") != value
    assert page.eval_on_selector_all(".jr-moods [role=radio]", "els => els.filter((e) => e.tabIndex === 0).length") == 1


def test_a_new_section_is_announced_by_moving_focus_to_its_heading(session):
    page = session.open("#/overview")
    assert page.locator("#nav a[aria-current=page]").get_attribute("href") == "#/overview"
    page.locator("#nav a[href='#/journal']").click()
    page.wait_for_selector("#view h1:focus")
    assert page.locator("#nav a[aria-current=page]").get_attribute("href") == "#/journal"


def test_a_skip_link_goes_to_the_content(session):
    page = session.open("#/overview")
    # Safari's Tab stops only on form fields unless Option is held or Full Keyboard Access is on.
    page.keyboard.press("Alt+Tab" if page.context.browser.browser_type.name == "webkit" else "Tab")
    assert page.evaluate("document.activeElement.classList.contains('skip-link')")
    page.keyboard.press("Enter")
    assert page.evaluate("!!document.activeElement.closest('#view')")


# ---------------------------------------------------------------- forms
def test_typed_results_survive_a_stray_click_and_escape_asks(session):
    page = session.open("#/journal")
    page.locator("[data-action=new]").first.click()
    page.wait_for_selector("#jr-note")
    page.locator("#jr-note").fill("Typed by hand")
    page.mouse.click(5, 5)
    page.wait_for_timeout(300)
    assert page.locator(".modal").count() == 1
    page.keyboard.press("Escape")
    page.get_by_role("button", name="Discard").wait_for()
    page.get_by_role("button", name="Cancel").last.click()
    page.wait_for_timeout(300)
    assert page.locator("#jr-form").count() == 1
    page.locator(".modal [data-close]").click()
    page.get_by_role("button", name="Discard").click()
    page.wait_for_selector("#jr-form", state="detached")


def test_an_untouched_dialog_still_closes_with_a_click_outside(session):
    page = session.open("#/journal")
    page.locator("[data-action=new]").first.click()
    page.wait_for_selector("#jr-form")
    page.mouse.click(5, 5)
    page.wait_for_selector("#jr-form", state="detached")


def test_double_clicking_save_adds_one_person(session):
    page = session.open("#/settings/people")
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.endswith("/api/profiles") else None)
    # A slow machine: the second click lands while the first is still on its way.
    page.route("**/api/profiles", lambda route: (time.sleep(0.2), route.continue_())[1])
    page.locator("[data-action=add-profile]").click()
    page.locator(".modal input").first.fill("Double Click")
    page.locator(".modal [data-save]").dblclick()
    session.settle()
    page.wait_for_timeout(600)
    page.unroute("**/api/profiles")
    assert len(posts) == 1


def test_a_date_with_no_time_shows_no_time(session):
    page = session.open("#/overview")
    shown = page.evaluate("import('/static/js/ui.js').then((m) => [m.fmtDateTime('2024-03-05'), m.fmtDateTime('2024-03'), m.fmtDateTime('2024'), m.fmtDateTime('2024-03-05T15:30:00')])")
    assert not re.search(r"\d:\d\d", shown[0])
    assert shown[1:3] == ["Mar 2024", "2024"]
    assert re.search(r"3:30|15:30", shown[3])


def test_a_failed_search_can_be_tried_again(session):
    page = session.open("#/records")
    page.route("**/api/records?*", lambda route: route.fulfill(status=500, json={"detail": "Something went wrong here."}))
    page.locator("#rec-q").fill("glucose")
    page.wait_for_selector(".page-error")
    assert page.locator(".skeleton").count() == 0
    page.unroute("**/api/records?*")
    page.locator(".page-error button").click()
    page.wait_for_selector(".page-error", state="detached")


def test_a_dialog_with_typing_in_it_waits_behind_the_sign_in_screen(browser, fresh_server):
    seed(fresh_server, simulated=False).close()
    s = Session(browser, fresh_server)
    page = s.open("#/journal")
    page.locator("[data-action=new]").first.click()
    page.locator("#jr-note").fill("A long note")
    s.ctx.clear_cookies()
    page.locator(".modal [data-save]").click()
    page.wait_for_selector("#login-form")
    assert not page.locator("#jr-note").is_visible()
    page.locator("#l-pass").fill("correct horse battery")
    page.locator("#login-form button[type=submit]").click()
    page.wait_for_selector("#jr-note")
    assert page.locator("#jr-note").input_value() == "A long note"
    assert page.evaluate("document.getElementById('root').inert")
    s.close()


def test_a_session_that_ran_out_keeps_the_note_and_says_why(browser, fresh_server):
    seed(fresh_server, simulated=False).close()
    s = Session(browser, fresh_server)
    page = s.open("#/journal")
    page.locator(".jr-moods [role=radio]").first.click()
    page.locator("#jr-quick-note").fill("Written before the session ended")
    s.ctx.clear_cookies()
    page.locator("[data-action=save-checkin]").click()
    page.wait_for_selector("#login-form")
    assert "signed out" in page.locator(".auth-card").inner_text().lower()
    assert "authentication required" not in page.locator("#toasts").inner_text().lower()
    page.locator("#l-pass").fill("correct horse battery")
    page.locator("#login-form button[type=submit]").click()
    page.wait_for_selector("#jr-quick-note")
    assert page.locator("#jr-quick-note").input_value() == "Written before the session ended"
    assert page.locator(".jr-moods [aria-checked=true]").count() == 1
    s.close()
