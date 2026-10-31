"""Records and charts that have to state exactly what the source said, checked in a real browser."""
from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator

import httpx
import pytest

from tests.e2e.conftest import CSRF, Server, Session, seed

LAB = {"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}
VITAL = {"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "vital-signs"}
INVITED_PASSWORD = "another good password"


def lab(id_: str, name: str, code: str, when: str, value: float, unit: str = "mg/dL", comparator: str | None = None) -> dict:
    quantity = {"value": value, "unit": unit, **({"comparator": comparator} if comparator else {})}
    return {"resourceType": "Observation", "id": id_, "status": "final", "category": [{"coding": [LAB]}],
            "code": {"coding": [{"system": "http://loinc.org", "code": code}], "text": name},
            "effectiveDateTime": when, "valueQuantity": quantity}


def pressure(id_: str, when: str, systolic: int, diastolic: int) -> dict:
    def part(code: str, value: int) -> dict:
        return {"code": {"coding": [{"system": "http://loinc.org", "code": code}]}, "valueQuantity": {"value": value, "unit": "mmHg"}}
    return {"resourceType": "Observation", "id": id_, "status": "final", "category": [{"coding": [VITAL]}],
            "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}], "text": "Blood pressure"}, "effectiveDateTime": when,
            "component": [part("8480-6", systolic), part("8462-4", diastolic)]}


RESOURCES = [
    {"resourceType": "Patient", "id": "p1", "name": [{"family": "T", "given": ["Alex"]}]},
    lab("o1", "Sodium", "2951-2", "2026-03-01T10:00:00Z", 140, "mmol/L"),
    lab("o2", "Potassium", "2823-3", "2026-03-01T10:00:00Z", 4.1, "mmol/L"),
    {"resourceType": "DiagnosticReport", "id": "d1", "status": "final", "code": {"text": "Basic panel"},
     "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0074", "code": "LAB"}]}],
     "effectiveDateTime": "2026-03-01T10:00:00Z", "result": [{"reference": "Observation/o1"}, {"reference": "Observation/o2"}]},
    {"resourceType": "Condition", "id": "c1", "clinicalStatus": {"coding": [{"code": "active"}]}, "code": {"text": "Asthma"}, "onsetDateTime": "2019"},
    {"resourceType": "Condition", "id": "c2", "clinicalStatus": {"coding": [{"code": "active"}]}, "code": {"text": "Hay fever"}, "onsetDateTime": "2021-05"},
    pressure("bp1", "2026-02-01", 120, 80),
    pressure("bp2", "2026-03-01", 130, 85),
    lab("crp0", "C reactive protein", "1988-5", "2026-01-05", 5, "mg/L", "<"),
    lab("crp1", "C reactive protein", "1988-5", "2026-02-05", 5, "mg/L", "<"),
    lab("crp2", "C reactive protein", "1988-5", "2026-03-05", 7, "mg/L"),
]


@pytest.fixture(scope="module")
def server(tmp_path_factory) -> Iterator[Server]:
    s = Server(tmp_path_factory.mktemp("e2e"))
    c = seed(s, simulated=False)
    bundle = json.dumps({"resourceType": "Bundle", "type": "collection", "entry": [{"resource": r} for r in RESOURCES]})
    pid = c.get("/api/profiles").json()["profiles"][0]["id"]
    r = c.post("/api/imports/fhir", files={"file": ("b.json", bundle, "application/json")}, data={"profile_id": pid})
    assert r.status_code == 200, r.text
    job = r.json().get("job_id")
    for _ in range(100):
        if not job or c.get(f"/api/imports/{job}").json()["status"] in ("completed", "error"):
            break
        time.sleep(0.1)
    c.close()
    yield s
    s.stop()


def open_panel(session: Session):
    page = session.open("#/records")
    page.locator("#rec-list .list-item", has_text="Basic panel").first.click()
    page.wait_for_selector("[data-open-result]")
    return page


@pytest.mark.parametrize("width", ["desktop", "phone"])
def test_a_result_opens_from_its_panel(browser, server, width):
    s = Session(browser, server, width=width)
    try:
        page = open_panel(s)
        page.locator("[data-open-result]", has_text="Sodium").click()
        page.wait_for_selector(".drawer h2:text-is('Sodium')")
        page.wait_for_timeout(500)
        assert page.locator(".drawer-overlay").count() == 1
        assert page.locator(".drawer h2").inner_text() == "Sodium"
        page.go_back()
        page.wait_for_selector(".drawer-overlay", state="detached")
        assert page.locator("#rec-list").is_visible()
        assert s.errors == []
    finally:
        s.close()


def test_closing_a_result_opened_from_a_panel_leaves_the_page_where_it_was(session):
    page = open_panel(session)
    page.locator("[data-open-result]", has_text="Sodium").click()
    page.wait_for_selector(".drawer h2:text-is('Sodium')")
    page.wait_for_timeout(300)
    page.locator(".drawer [data-close]").click()
    page.wait_for_selector(".drawer-overlay", state="detached")
    page.wait_for_timeout(300)
    assert page.evaluate("history.state?.drawer ?? null") is None
    assert page.url.endswith("#/records")


def test_a_record_with_only_a_year_is_not_given_a_month(session):
    page = session.open("#/records")
    heads = page.locator(".list-head b").all_inner_texts()
    assert heads[-2:] == ["May 2021", "2019"]


def pick(page, hash_: str, name: str):
    page.goto(f"{page.url.split('#')[0]}{hash_}")
    page.wait_for_load_state("networkidle")
    page.locator("[data-action=pick]", has_text=name).first.click()
    page.wait_for_selector("#tr-chart svg[role=img]")


def test_a_limit_result_keeps_its_sign_in_the_chart_table_and_summary(session):
    page = session.open("#/overview")
    pick(page, "#/trends/labs", "reactive")
    label = page.locator("#tr-chart svg[role=img]").get_attribute("aria-label")
    assert "Latest 7 mg/L, lowest <5 mg/L, highest 7 mg/L." in label
    page.locator("#tr-chart .chart-table summary").click()
    page.wait_for_selector("#tr-chart .chart-table table")
    cells = page.locator("#tr-chart .chart-table tbody td:nth-child(2)").all_inner_texts()
    assert cells == ["7 mg/L", "<5 mg/L", "<5 mg/L"]


def test_each_measure_of_a_blood_pressure_chart_gets_its_own_summary(session):
    page = session.open("#/overview")
    pick(page, "#/trends/vitals", "Blood pressure")
    label = page.locator("#tr-chart svg[role=img]").get_attribute("aria-label")
    assert re.fullmatch(r"Blood pressure\. .*: 2 values from Feb 1, 2026 to Mar 1, 2026\. Latest 130 mmHg, lowest 120 mmHg, highest 130 mmHg\."
                        r" .*: 2 values from Feb 1, 2026 to Mar 1, 2026\. Latest 85 mmHg, lowest 80 mmHg, highest 85 mmHg\.", label), label


def test_an_invited_person_is_not_told_their_data_is_on_their_own_computer(browser, fresh_server):
    owner = seed(fresh_server, simulated=False)
    invited = owner.post("/api/profiles", json={"name": "Jamie", "relationship": "partner"}).json()
    code = owner.post(f"/api/profiles/{invited['id']}/invite", json={}).json()["code"]
    jamie = httpx.Client(base_url=fresh_server.url, headers=CSRF, timeout=30)
    assert jamie.post("/api/invites/accept", json={"code": code, "password": INVITED_PASSWORD, "accept_terms": True}).status_code == 200
    jamie.post("/api/onboarding/done")
    account = jamie.get("/api/status").json()["account"]["id"]
    s = Session(browser, fresh_server, sign_in=False)
    try:
        r = s.ctx.request.post(f"{fresh_server.url}/api/auth/login", data={"password": INVITED_PASSWORD, "account": account}, headers=CSRF)
        assert r.ok, r.text()
        s.signed_in = True
        for hash_ in ("#/overview", "#/sources"):
            page = s.open(hash_)
            text = page.locator("body").inner_text().lower()
            assert "your own computer" not in text and "stored on your computer" not in text, hash_
            assert "computer that runs syntropy health" in page.locator(".page-head").inner_text().lower(), hash_
    finally:
        s.close()
        owner.close()
        jamie.close()


def test_a_lab_read_from_a_report_is_saved_under_the_name_the_dialog_promised(browser, fresh_server):
    c = seed(fresh_server, simulated=False)
    s = Session(browser, fresh_server)
    page = s.open("#/records/labs")
    read = {"results": [{"test": "GLUCOSE", "value": 95, "unit": "mg/dL", "loinc": "2345-7", "matched_test": "Glucose"},
                        {"test": "ODD THING", "value": 7, "unit": "U/L"}], "collected": "2026-02-03", "method": "local"}
    page.route("**/api/labs/extract", lambda route: route.fulfill(json=read))
    page.locator("[data-action=add-labs]").first.click()
    page.locator("[data-mode=report]").click()
    page.locator("#lab-file").set_input_files({"name": "report.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4"})
    page.wait_for_selector("[data-matched-note]")
    assert page.locator("[data-matched-note]").inner_text() == "Filed as Glucose"
    page.locator(".modal .btn-primary", has_text="Save").click()
    page.wait_for_selector(".modal", state="detached")
    labs = c.get("/api/records", params={"category": "labs", "q": ""}).json()["items"]
    saved = {r["title"]: r for r in labs if r["effective_at"][:10] == "2026-02-03"}
    assert set(saved) == {"Glucose", "ODD THING"}
    assert saved["Glucose"]["value_num"] == 95 and saved["Glucose"]["unit"] == "mg/dL"
    assert s.errors == []
    s.close()
    c.close()


def test_a_folded_day_of_labs_shows_what_you_said_about_one_of_them(browser, fresh_server):
    c = seed(fresh_server, simulated=False)
    r = c.post("/api/labs", json={"collected": "2026-03-04", "results": [{"test": f"Test {i}", "value": i, "unit": "U/L"} for i in range(5)]})
    assert r.status_code == 200, r.text
    rid = next(x["id"] for x in c.get("/api/records", params={"category": "labs"}).json()["items"] if x["title"] == "Test 2")
    assert c.put(f"/api/records/{rid}/override", json={"note": "Drawn after a meal"}).status_code == 200
    s = Session(browser, fresh_server)
    page = s.open("#/timeline")
    assert "Test 2 · You said: Drawn after a meal" in page.locator("[data-action=labs]").first.inner_text()
    assert s.errors == []
    s.close()
    c.close()
