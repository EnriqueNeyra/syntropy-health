"""What the pages put first and how they hold together, checked in a real browser."""
from __future__ import annotations

from tests.e2e.conftest import PASSWORD, Session


def test_a_new_overview_starts_with_the_medical_record_and_a_saved_one_stays_put(session):
    page = session.open("#/overview")
    fresh, saved = page.evaluate("""() => import('/static/js/views/overview.js').then((m) => [
      m.resolveLayout(null, ['steps']).map((i) => i.id),
      m.resolveLayout([{ id: 'workouts' }, { id: 'm:steps' }, { id: 'allergies' }], ['steps']).map((i) => i.id),
    ])""")
    assert fresh[:7] == ["insights", "new", "labs", "medications", "conditions", "allergies", "m:steps"]
    assert fresh.index("m:steps") < fresh.index("vitals") < fresh.index("sleep")
    assert [i for i in saved if i not in ("insights", "new")][:3] == ["workouts", "m:steps", "allergies"]


def test_charts_keep_their_lines_when_a_page_comes_back_from_the_browser(session):
    page = session.open("#/trends/heart")
    page.wait_for_selector(".chart .line")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))")
    page.wait_for_selector("html.no-intro", state="attached")
    page.wait_for_selector("#view:not([aria-busy]) .chart .line")
    session.settle()
    style = page.evaluate("""() => { const s = getComputedStyle(document.querySelector('.chart .line'));
      return s.strokeDasharray === 'none' || Number.parseFloat(s.strokeDashoffset) === 0; }""")
    assert style, "The chart's line is hidden"


def test_timeline_is_split_by_year_and_a_day_from_one_place_names_it_once(session):
    page = session.open("#/timeline")
    years = page.locator(".timeline-year").all_inner_texts()
    assert years and len(years) == len(set(years))
    assert page.locator(".timeline-year").first.evaluate("(el) => el.tagName") == "H2"
    day = page.locator(".timeline-day").first
    named = day.locator(".timeline-date small").inner_text()
    if named:
        assert named not in day.locator(".ev").first.inner_text()


def test_records_are_split_by_month_except_in_a_search(session):
    page = session.open("#/records")
    assert page.locator("#rec-list .list-head").count() >= 1
    page.locator("#rec-q").fill("Glucose")
    page.wait_for_selector("#rec-list .list-head", state="detached")
    assert page.locator("#rec-list .list-item").count() >= 1


def test_filter_chips_stay_on_one_line_on_a_phone(browser, server):
    s = Session(browser, server, width="phone")
    for hash_, strip in (("#/timeline", "#tl-filters"), ("#/records", "#rec-cats")):
        page = s.open(hash_)
        tops = page.evaluate("(sel) => [...new Set([...document.querySelectorAll(`${sel} .chip`)].map((c) => c.getBoundingClientRect().top))]", strip)
        assert len(tops) == 1, f"{hash_} wraps its filters"
        assert page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 1
    s.close()


def test_the_visit_summary_stacks_on_a_phone_and_stays_a_table_elsewhere(browser, server):
    s = Session(browser, server, width="phone")
    page = s.open("#/report")
    cell = page.locator(".report td").first
    assert cell.evaluate("(el) => getComputedStyle(el).display") == "block"
    assert page.locator(".report table").first.get_attribute("role") == "table"
    assert page.locator(".report td[data-label]").count() == page.locator(".report td").count()
    page.emulate_media(media="print")
    assert cell.evaluate("(el) => getComputedStyle(el).display") == "table-cell"
    s.close()
    s = Session(browser, server)
    assert s.open("#/report").locator(".report td").first.evaluate("(el) => getComputedStyle(el).display") == "table-cell"
    s.close()


def test_sources_lists_medical_records_first_and_offers_stay_quiet(session):
    page = session.open("#/sources")
    assert page.locator("#view h2, #view .src-group h3").first.inner_text() == "Medical records"
    assert page.locator(".src-offer .btn-primary").count() == 0


def test_first_visit_has_one_main_step_and_it_is_medical_records(browser, fresh_server):
    with fresh_server.client() as c:
        assert c.post("/api/setup", json={"password": PASSWORD, "profile_name": "Alex", "accept_terms": True}).status_code == 200
        assert c.post("/api/onboarding/done").status_code == 200
    s = Session(browser, fresh_server)
    page = s.open("#/overview")
    main = page.locator("#view .card .btn-primary")
    assert main.count() == 1
    assert main.get_attribute("href") == "#/sources?add=ehr"
    assert s.errors == []
    s.close()


def test_a_disabled_main_button_is_grey_not_faded_accent(session):
    page = session.open("#/journal")
    colour, opacity = page.evaluate("""() => { const b = document.createElement('button'); b.className = 'btn btn-primary'; b.disabled = true;
      document.body.append(b); const s = getComputedStyle(b); const out = [s.backgroundColor, s.opacity]; b.remove(); return out; }""")
    accent = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent-fill').trim()")
    assert opacity == "1"
    assert colour != accent


def test_a_name_of_only_spaces_stays_on_the_page(session):
    page = session.open("#/records")
    page.locator('[data-action="add-own"]').click()
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and "self-reported" in r.url else None)
    page.locator('#own-form [name="name"]').fill("   ")
    page.locator('#own-form [type="submit"], .modal-foot [type="submit"]').first.click()
    page.wait_for_timeout(200)
    assert posts == [] and page.locator("#own-form").count() == 1
