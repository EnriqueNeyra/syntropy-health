"""Adding lab results from a report, as a person does: drop a file, check the rows beside the page, correct one, save, see them everywhere, open the original."""
from __future__ import annotations

import base64
import json

import pytest

from tests.e2e.conftest import CSRF, Session, seed
from tests.test_lab_report import two_columns

REPORT = two_columns()


def drop_on(page, selector, name="report.pdf", data=REPORT, kind="application/pdf"):
    transfer = page.evaluate_handle("""([data, name, type]) => {
      const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
      const dt = new DataTransfer();
      dt.items.add(new File([bytes], name, { type }));
      return dt;
    }""", [base64.b64encode(data).decode(), name, kind])
    page.dispatch_event(selector, "dragover", {"dataTransfer": transfer})
    page.dispatch_event(selector, "drop", {"dataTransfer": transfer})


def box_of(page, selector=".modal.lab-modal"):
    page.wait_for_timeout(300)      # past the opening animation
    return page.evaluate("(s) => { const r = document.querySelector(s).getBoundingClientRect(); return [r.x, r.y, r.width, r.height].map(Math.round); }", selector)


@pytest.mark.parametrize("width", ["desktop", "phone"])
def test_a_report_dropped_on_sources_ends_up_everywhere_and_stays_with_its_results(browser, fresh_server, width):
    c = seed(fresh_server, simulated=False)
    s = Session(browser, fresh_server, width=width)
    page = s.open("#/sources")
    drop_on(page, "[data-drop=lab]")
    page.wait_for_selector(".lab-panes.lab-both")
    page.wait_for_selector(".lab-page img")

    # The page and the rows are both there: side by side, or the page on top.
    a, b = page.evaluate("[...document.querySelectorAll('.lab-source, .lab-review')].map((e) => { const r = e.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }; })")
    assert (b["left"] >= a["right"] - 1) if width == "desktop" else (b["top"] >= a["bottom"] - 1)
    assert page.locator(".lab-row").count() == 4
    assert page.locator("#lab-date").input_value() == "2026-09-12"

    # Choosing a row points out its line on the page.
    row = page.locator(".lab-row", has=page.locator("[data-k=test][value='WBC']"))
    value = row.locator("[data-k=value]")
    value.click()
    assert page.locator(".lab-mark").count() == 1

    # Correct it, and save.
    value.fill("5.9")
    value.press("Tab")
    page.locator("[data-save]").click()

    # Saving lands on what was added, with the way back to the original.
    page.wait_for_selector("#rec-added")
    page.wait_for_selector("#rec-list .list-item")
    assert "4 results" in page.locator("#rec-added").inner_text() and "Sep 12, 2026" in page.locator("#rec-added").inner_text()
    assert page.locator("#rec-list .list-item").count() == 4

    # In the record, in Trends and on the Overview, with the correction, without a reload.
    saved = {r["title"]: r for r in c.get("/api/records", params={"category": "labs", "since": "2026-09-12", "until": "2026-09-12"}).json()["items"]}
    assert saved["White blood cells"]["value_num"] == 5.9 if "White blood cells" in saved else saved["WBC"]["value_num"] == 5.9
    page.evaluate("location.hash = '#/trends/labs'")
    page.wait_for_selector("#tr-list .list-item")
    listed = page.locator("#tr-list").inner_text()
    assert "Sodium" in listed and "5.9" in listed

    # The original opens from a result. In a browser it's the file itself.
    record = c.get("/api/records", params={"category": "labs", "q": "Sodium"}).json()["items"][0]
    page.evaluate("location.hash = '#/records/labs'")
    page.wait_for_selector("#rec-list .list-item")
    page.locator("#rec-list .list-item", has_text="Sodium").first.click()
    page.wait_for_selector(".drawer a[target=_blank]")
    href = page.locator(".drawer a[target=_blank]").get_attribute("href")
    opened = page.context.request.get(fresh_server.url + href)
    assert opened.status == 200 and opened.body() == REPORT
    assert record["id"] in href
    assert "Read from" in page.locator(".drawer").inner_text()
    assert s.errors == []
    s.close()
    c.close()


def test_the_dialog_doesnt_move_as_it_loads_or_as_the_tabs_change(browser, fresh_server):
    c = seed(fresh_server, simulated=False)
    s = Session(browser, fresh_server)
    page = s.open("#/records/labs")
    page.locator("[data-action=add-labs]").first.click()
    page.wait_for_selector(".modal.lab-modal")
    typed = box_of(page)
    page.locator("[data-mode=report]").click()
    assert box_of(page) == typed
    page.wait_for_selector(".lab-drop")
    page.locator("#lab-file").set_input_files({"name": "report.pdf", "mimeType": "application/pdf", "buffer": REPORT})
    page.wait_for_selector(".lab-panes.lab-both")
    assert box_of(page) == typed
    # The page's room is kept before its picture is decoded, so the panes don't shift either.
    assert page.evaluate("document.querySelector('.lab-page').style.aspectRatio") != ""
    page.locator("[data-mode=type]").click()
    assert box_of(page) == typed
    assert page.locator(".lab-source").is_hidden() and page.locator(".lab-row").count() == 4
    assert s.errors == []
    s.close()
    c.close()


def test_results_already_on_file_are_pointed_out_and_left_out(browser, fresh_server):
    c = seed(fresh_server, simulated=False)
    r = c.post("/api/labs", json={"collected": "2026-09-12", "lab": "Earlier", "results": [{"test": "Glucose", "loinc": "2345-7", "value": 92, "unit": "mg/dL"}, {"test": "Sodium", "value": 139, "unit": "mmol/L"}]})
    assert r.status_code == 200
    s = Session(browser, fresh_server)
    page = s.open("#/records/labs")
    page.locator("[data-action=add-report]").click()
    page.wait_for_selector(".lab-drop")
    page.locator("#lab-file").set_input_files({"name": "report.pdf", "mimeType": "application/pdf", "buffer": REPORT})
    page.wait_for_selector(".lab-panes.lab-both")
    glucose = page.locator(".lab-row", has=page.locator("[data-k=test][value='Glucose']"))
    sodium = page.locator(".lab-row", has=page.locator("[data-k=test][value='Sodium']"))
    assert "Already on file" in glucose.inner_text() and "lab-left-out" in glucose.get_attribute("class")
    assert "another result" in sodium.inner_text() and "lab-left-out" not in sodium.get_attribute("class")
    assert page.locator("[data-save]").inner_text() == "Save 3 results"
    glucose.locator("[data-keep]").click()
    assert page.locator("[data-save]").inner_text() == "Save 4 results"
    page.locator(".lab-row", has=page.locator("[data-k=test][value='Glucose']")).locator("[data-keep]").click()
    assert page.locator("[data-save]").inner_text() == "Save 3 results"
    # Changing the day asks again: on another day, nothing's on file.
    page.locator("#lab-date").fill("2026-09-11")
    page.locator("#lab-date").press("Tab")
    page.locator("[data-on-file]").first.wait_for(state="detached")
    assert page.locator("[data-save]").inner_text() == "Save 4 results"
    assert s.errors == []
    s.close()
    c.close()


def test_a_file_chosen_while_the_dialog_loads_is_read(browser, fresh_server):
    """A file picker hands its choice to the box it was opened from, so the rest of the dialog loading mustn't put a new
    box in its place: the choice would go nowhere. CI caught it as a test that chose a file the moment the box appeared."""
    c = seed(fresh_server, simulated=False)
    s = Session(browser, fresh_server)
    page = s.open("#/records/labs")
    held = []
    page.route("**/api/labs/reader", lambda route: held.append(route))
    page.locator("[data-action=add-report]").click()
    page.wait_for_selector(".lab-drop")
    page.evaluate("window.picker = document.querySelector('#lab-file')")     # the person opens the picker
    for _ in range(100):
        if held:
            break
        page.wait_for_timeout(50)
    held[0].continue_()
    page.wait_for_selector("#lab-tests option", state="attached")        # loaded
    page.evaluate("""(data) => {
      const dt = new DataTransfer();
      dt.items.add(new File([Uint8Array.from(atob(data), (c) => c.charCodeAt(0))], "report.pdf", { type: "application/pdf" }));
      window.picker.files = dt.files;
      window.picker.dispatchEvent(new Event("change", { bubbles: true }));
    }""", base64.b64encode(REPORT).decode())
    page.wait_for_selector(".lab-panes.lab-both")
    assert page.locator(".lab-row").count() == 4
    assert s.errors == []
    s.close()
    c.close()


def test_a_file_that_cant_be_read_says_why_in_place(browser, fresh_server):
    c = seed(fresh_server, simulated=False)
    s = Session(browser, fresh_server)
    page = s.open("#/records/labs")
    page.locator("[data-action=add-report]").click()
    page.wait_for_selector(".lab-drop")
    page.locator("#lab-file").set_input_files({"name": "notes.txt", "mimeType": "text/plain", "buffer": b"hello"})
    page.wait_for_selector(".lab-source .banner.bad")
    assert "PDF or a photo" in page.locator(".lab-source .banner.bad").inner_text()
    assert page.locator(".lab-drop").is_visible()       # ready for another try
    s.errors.clear()                                    # the browser logs the 400 it was told
    s.close()
    c.close()


def test_the_way_in_is_on_records_and_trends_and_the_android_app_isnt_offered_a_dead_box(browser, fresh_server):
    c = seed(fresh_server, simulated=False)
    s = Session(browser, fresh_server)
    page = s.open("#/records/labs")
    assert page.locator("[data-action=add-report]").is_visible()
    page = s.open("#/trends/labs")
    assert page.locator("#tr-add-report").is_visible()
    page = s.open("#/sources")
    assert page.locator("[data-drop=lab]").is_visible() and page.locator("#imp-lab").count() == 1
    assert s.errors == []
    s.close()
    android = Session(browser, fresh_server, width="phone", user_agent="Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/140.0 Mobile Safari/537.36 SyntropyHealthApp/1.0 SyntropyNav/1 SyntropyAndroid/1")
    page = android.open("#/sources")
    page.wait_for_selector("#import-card")
    assert page.locator("#import-card input[type=file]").count() == 0
    assert "can't open files yet" in page.locator("#import-card").inner_text()
    page = android.open("#/records/labs")
    assert page.locator("[data-action=add-report]").is_hidden()
    android.close()
    c.close()


def test_on_a_touch_screen_a_camera_is_offered_as_well_as_files(browser, fresh_server):
    c = seed(fresh_server, simulated=False)
    s = Session(browser, fresh_server, width="phone", has_touch=True, is_mobile=True)
    page = s.open("#/records/labs")
    page.locator("[data-action=add-report]").click()
    page.wait_for_selector(".lab-drop")
    assert page.locator("#lab-camera").get_attribute("capture") == "environment" and page.locator("#lab-camera").get_attribute("accept") == "image/*"
    assert "application/pdf" in page.locator("#lab-file").get_attribute("accept")
    assert s.errors == []
    s.close()
    c.close()


def test_the_original_is_drawn_inside_the_apps(browser, fresh_server):
    """The Mac, Windows and phone apps send a new window to the system browser, which isn't signed in: it opens in place instead."""
    c = seed(fresh_server, simulated=False)
    entry = {"collected": "2026-09-12", "results": [{"test": "Sodium", "value": 141, "unit": "mmol/L"}]}
    r = c.post("/api/labs/report", headers=CSRF, files={"file": ("report.pdf", REPORT, "application/pdf")}, data={"entry": json.dumps(entry)})
    assert r.status_code == 200, r.text
    s = Session(browser, fresh_server, width="phone", user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 SyntropyHealthApp/1.5.0 SyntropyNav/1")
    page = s.open("#/records/labs")
    page.locator("#rec-list .list-item", has_text="Sodium").first.click()
    page.wait_for_selector("[data-view-file]")
    page.locator("[data-view-file]").click()
    page.wait_for_selector(".lab-viewer img")
    assert page.locator(".lab-viewer img").count() == 1
    assert s.errors == []
    s.close()
    c.close()
