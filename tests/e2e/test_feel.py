"""How the app behaves under a pointer: things that a still screenshot can't show. Real drags, hovers and timing."""
from __future__ import annotations

import pytest

from tests.e2e.conftest import CSRF, Session


def to_center(box):
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


@pytest.fixture
def moving(browser, server):
    s = Session(browser, server, reduced_motion="no-preference")
    yield s
    s.close()


def editing_overview(session):
    page = session.open("#/overview")
    page.get_by_role("button", name="Edit").click()
    page.wait_for_selector(".dash.editing")
    return page


def first_tile(page):
    tile = page.locator(".dash-item.is-tile").first
    tile.scroll_into_view_if_needed()
    return tile


def drag_corner(page, tile, dx, dy, corner="br"):
    x, y = to_center(tile.locator(f'[data-corner="{corner}"]').bounding_box())
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx, y + dy, steps=8)


def test_small_charts_keep_round_dots_and_their_colour(session):
    page = session.open("#/overview")
    dots = page.evaluate("[...document.querySelectorAll('.tile .spark i')].map((d) => { const r = d.getBoundingClientRect(); return [r.width, r.height]; })")
    assert dots and all(w == h for w, h in dots)
    colours = page.evaluate("""(() => {
      const line = getComputedStyle(document.querySelector('.tile .spark path')).stroke;
      const probe = document.createElement('i'); probe.style.color = 'var(--series-1)'; document.body.append(probe);
      const accent = getComputedStyle(probe).color; probe.remove();
      return [line, accent];
    })()""")
    assert colours[0] == colours[1]
    assert session.errors == []


def test_a_card_snaps_through_sizes_from_its_corner_and_keeps_them(session):
    page = editing_overview(session)
    tile = first_tile(page)
    id_ = tile.get_attribute("data-id")
    box = tile.locator(".dash-inner").bounding_box()
    drag_corner(page, tile, box["width"] + 20, 170)
    assert page.locator(".dash-sizer").inner_text() == "2 wide · 2 tall"
    page.mouse.up()
    assert page.locator(".dash-sizer").count() == 0
    item = page.locator(f'.dash-item[data-id="{id_}"]')
    assert "size-m" in item.get_attribute("class") and item.get_attribute("data-rows") == "2"
    assert item.locator(".dash-inner").bounding_box()["width"] > box["width"] * 1.8
    with page.expect_response(lambda r: "/overview" in r.url and r.request.method == "PUT"):
        page.get_by_role("button", name="Done").click()
    page.reload()
    page.wait_for_selector("#dash")
    item = page.locator(f'.dash-item[data-id="{id_}"]')
    assert "size-m" in item.get_attribute("class") and item.get_attribute("data-rows") == "2"
    # Back as it was, for the tests that follow.
    client = session.ctx.request
    pid = client.get(f"{session.server.url}/api/profiles").json()["profiles"][0]["id"]
    assert client.delete(f"{session.server.url}/api/profiles/{pid}/overview", headers=CSRF).ok
    assert session.errors == []


def test_sideways_alone_leaves_a_cards_height_to_its_content(session):
    page = editing_overview(session)
    tile = first_tile(page)
    box = tile.locator(".dash-inner").bounding_box()
    drag_corner(page, tile, box["width"] + 20, 0)
    page.mouse.up()
    assert "size-m" in tile.get_attribute("class") and tile.get_attribute("data-rows") is None
    client = session.ctx.request
    pid = client.get(f"{session.server.url}/api/profiles").json()["profiles"][0]["id"]
    session.settle()
    client.delete(f"{session.server.url}/api/profiles/{pid}/overview", headers=CSRF)


def test_escape_puts_a_card_back_as_it_was(session):
    page = editing_overview(session)
    tile = first_tile(page)
    id_ = tile.get_attribute("data-id")
    box = tile.locator(".dash-inner").bounding_box()
    drag_corner(page, tile, box["width"] + 20, 170)
    page.keyboard.press("Escape")
    assert page.locator(".dash-sizer").count() == 0
    page.mouse.up()
    item = page.locator(f'.dash-item[data-id="{id_}"]')
    assert "size-s" in item.get_attribute("class") and item.get_attribute("data-rows") is None


def test_the_arrow_keys_resize_from_the_corner_and_move_from_the_grip(session):
    page = editing_overview(session)
    tile = first_tile(page)
    id_ = tile.get_attribute("data-id")
    tile.locator('[data-corner="br"]').focus()
    page.keyboard.press("ArrowRight")
    assert "size-m" in page.locator(f'.dash-item[data-id="{id_}"]').get_attribute("class")
    page.keyboard.press("ArrowLeft")
    assert "size-s" in page.locator(f'.dash-item[data-id="{id_}"]').get_attribute("class")
    order = lambda: page.evaluate("[...document.querySelectorAll('.dash-item')].map((i) => i.dataset.id)")
    before = order()
    page.locator(f'.dash-item[data-id="{id_}"] [data-handle]').focus()
    page.keyboard.press("ArrowRight")
    after = order()
    assert after != before and sorted(after) == sorted(before)
    page.keyboard.press("ArrowLeft")
    assert order() == before
    session.settle()
    client = session.ctx.request
    pid = client.get(f"{session.server.url}/api/profiles").json()["profiles"][0]["id"]
    client.delete(f"{session.server.url}/api/profiles/{pid}/overview", headers=CSRF)


def test_picking_up_a_card_moves_it_and_the_others_make_way(moving):
    page = editing_overview(moving)
    order = lambda: page.evaluate("[...document.querySelectorAll('.dash-item')].map((i) => i.dataset.id)")
    before = order()
    tiles = page.locator(".dash-item.is-tile")
    tiles.nth(0).scroll_into_view_if_needed()
    a = tiles.nth(0).locator(".dash-inner").bounding_box()
    b = tiles.nth(2).locator(".dash-inner").bounding_box()
    page.mouse.move(a["x"] + 30, a["y"] + 60)
    page.mouse.down()
    page.mouse.move(a["x"] + 40, a["y"] + 70, steps=3)
    assert page.locator(".dash-ghost").count() == 1 and page.locator(".dash-item.dragging").count() == 1
    page.mouse.move(b["x"] + b["width"] - 10, b["y"] + 50, steps=10)
    page.wait_for_timeout(150)
    # While it travels, the cards it passed are on their way, not already there.
    assert page.evaluate("[...document.querySelectorAll('.dash-inner')].some((i) => i.getAnimations().length)")
    page.mouse.up()
    page.wait_for_selector(".dash-ghost", state="detached")
    assert page.locator(".dash-item.dragging").count() == 0
    assert order() != before
    moving.settle()
    client = moving.ctx.request
    pid = client.get(f"{moving.server.url}/api/profiles").json()["profiles"][0]["id"]
    client.delete(f"{moving.server.url}/api/profiles/{pid}/overview", headers=CSRF)
    assert moving.errors == []


def test_journals_new_entry_dialog_keeps_its_size_between_kinds(session):
    page = session.open("#/journal")
    page.get_by_role("button", name="New entry").click()
    page.wait_for_selector("#jr-form .jr-fields input")
    sizes = set()
    for kind in ("checkin", "symptom", "note", "medication", "period", "other"):
        page.click(f'#jr-form [data-kind="{kind}"]')
        session.settle()
        page.wait_for_timeout(120)     # past the dialog's own entrance
        box = page.locator(".modal").bounding_box()
        sizes.add((round(box["width"]), round(box["height"])))
    assert len(sizes) == 1, sizes


def test_choosing_a_mood_changes_only_the_choice(session):
    page = session.open("#/journal")
    page.evaluate("document.querySelector('.jr-mood').dataset.same = '1'")
    page.locator(".jr-mood").nth(3).click()
    assert page.locator(".jr-mood").first.get_attribute("data-same") == "1"
    assert page.locator(".jr-mood.active").count() == 1
    assert page.locator('[data-action="save-checkin"]').is_enabled()


def test_a_trend_chart_finds_the_nearest_value_with_its_tooltip_beside_it(session):
    page = session.open("#/trends?metric=resting_heart_rate")
    page.wait_for_selector("#tr-chart svg[role=img]")
    assert page.locator("text=Before and after").count() == 0
    svg = page.locator("#tr-chart svg[role=img]")
    box = svg.bounding_box()
    # Anywhere near will do, and the pointer can be nowhere near the line up or down.
    page.mouse.move(box["x"] + box["width"] * 0.6, box["y"] + box["height"] - 40)
    tip = page.locator("#tr-chart .chart-tip")
    assert tip.is_visible() and "bpm" in tip.inner_text()
    dot = page.evaluate("(() => { const r = document.querySelector('#tr-chart .hover-dot').getBoundingClientRect(); return [r.left, r.right]; })()")
    tipped = tip.bounding_box()
    assert dot[1] <= tipped["x"] or dot[0] >= tipped["x"] + tipped["width"]
    # At the very edge it stays inside the chart.
    page.mouse.move(box["x"] + box["width"] - 2, box["y"] + 60)
    tipped = page.locator("#tr-chart .chart-tip").bounding_box()
    assert tipped["x"] >= box["x"] - 1 and tipped["x"] + tipped["width"] <= box["x"] + box["width"] + 1
    page.mouse.move(box["x"] - 200, box["y"])
    assert not tip.is_visible()
    # The keyboard reaches the same values.
    svg.focus()
    page.keyboard.press("ArrowRight")
    first = tip.inner_text()
    page.keyboard.press("ArrowRight")
    assert tip.is_visible() and tip.inner_text() != first
    page.keyboard.press("Escape")
    assert not tip.is_visible()
    assert session.errors == []


def test_medications_and_diagnoses_start_off_the_charts(session):
    page = session.open("#/trends?metric=resting_heart_rate")
    page.wait_for_selector("#tr-chart svg[role=img]")
    assert page.locator("#tr-chart .event").count() == 0
    assert page.evaluate("(() => { try { return localStorage.getItem('syntropy-trends-show-events') } catch { return 'x' } })()") in (None, "0")


def test_a_new_range_keeps_the_chart_on_screen_until_the_next_is_ready(session):
    page = session.open("#/trends?metric=resting_heart_rate")
    page.wait_for_selector("#tr-chart svg[role=img]")
    release = []
    page.route("**/api/biometrics/daily*", lambda route: release.append(route))
    page.locator('button[data-days="90"]').click()
    page.wait_for_selector("#tr-detail.refreshing")
    assert page.locator("#tr-detail .skeleton").count() == 0 and page.locator("#tr-chart svg[role=img]").count() == 1
    assert page.locator('button[data-days="90"]').get_attribute("class") == "active"
    for route in release:
        route.continue_()
    page.unroute("**/api/biometrics/daily*")
    page.wait_for_selector("#tr-detail:not(.refreshing)")


def test_opening_a_page_with_cards_doesnt_shift_them(moving):
    page = moving.open("#/prepare")
    page.evaluate("""() => {
      window.shifted = 0;
      new PerformanceObserver((list) => { for (const e of list.getEntries()) window.shifted += e.value; }).observe({ type: 'layout-shift' });
    }""")
    moving.open("#/overview")
    page.wait_for_selector("#dash")
    page.wait_for_timeout(500)
    assert page.evaluate("window.shifted") < 0.03


def test_removing_a_source_says_so_while_its_data_is_deleted(session):
    client = session.ctx.request
    url = session.server.url
    pid = client.get(f"{url}/api/profiles").json()["profiles"][0]["id"]
    code = client.post(f"{url}/api/devices/pairing-code", data={"profile_id": pid}, headers=CSRF, params={"profile": pid}).json()["code"]
    assert client.post(f"{url}/api/devices/pair", data={"code": code, "device_name": "Slow iPhone", "platform": "ios"}, headers=CSRF).ok
    page = session.open("#/sources")
    row = page.locator(".src-row", has_text="Slow iPhone").first
    row.locator('[data-action="menu"]').click()
    page.locator(".dropdown .menu-item", has_text="Remove").click()
    held = []
    page.route("**/api/connections/*?delete_data=true", lambda route: held.append(route))
    page.click(".modal [data-wipe]")
    page.wait_for_selector(".src-row.is-going")
    assert "Removing" in page.locator(".src-row.is-going .src-line").inner_text()
    assert held
    held[0].continue_()
    page.unroute("**/api/connections/*?delete_data=true")
    page.locator(".src-name", has_text="Slow iPhone").wait_for(state="detached")
