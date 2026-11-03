"""Results typed in or read from a report: which test a name means, dates, and limits like "<0.5"."""

from datetime import date, timedelta

import pytest

from app.store import lab_catalog


def post_labs(client, collected=None, **result):
    body = {"collected": collected or date.today().isoformat(), "results": [{"test": "Ferritin", **result}]}
    return client.post("/api/labs", json=body)


# ---- F080: names mean the right test

@pytest.mark.parametrize("name, code", [
    ("Bilirubin, direct", "1968-7"), ("BILIRUBIN, DIRECT", "1968-7"), ("Direct bilirubin", "1968-7"), ("Bilirubin, total", "1975-2"),
    ("Bilirubin", "1975-2"), ("Microalbumin", "14957-5"), ("Urine microalbumin", "14957-5"), ("UACR", "9318-7"),
    ("Albumin/creatinine ratio", "9318-7"), ("Urea", "3091-6"), ("BUN", "3094-0"), ("Urea nitrogen", "3094-0"), ("GFR", "69405-9"),
    ("eGFR", "69405-9"), ("eGFR (CKD-EPI 2021)", "98979-8"), ("Insulin", "20448-7"), ("Insulin, fasting", "20448-7"),
    ("VITAMIN D,25-OH,TOTAL,IA", "1989-3"), ("LDL cholesterol, direct", "18262-6"), ("T4, free (direct)", "3024-7"), ("Testosterone, Total", "2986-8"),
    ("Glucose, serum", "2345-7"),
])
def test_report_names_mean_the_right_test(name, code):
    assert lab_catalog.loinc_codes(name)[0] == code


def test_a_name_it_doesnt_know_is_left_alone():
    assert lab_catalog.loinc_codes("Bilirubin, indirect") == []
    assert lab_catalog.matched_test("Bilirubin, indirect") is None
    assert lab_catalog.matched_test("Bilirubin, direct")["name"] == "Bilirubin, direct"


def test_a_name_made_of_brackets_is_answered_at_once():
    import time
    started = time.perf_counter()
    assert lab_catalog.loinc_codes("(" * 50_000) == []
    assert time.perf_counter() - started < 0.5


def test_every_code_in_the_catalog_is_only_listed_once():
    codes = [t["loinc"] for t in lab_catalog.common_labs()]
    assert len(codes) == len(set(codes))


def test_no_code_is_in_two_groups():
    codes = [c for group in lab_catalog.TEST_GROUPS for c in group]
    assert len(codes) == len(set(codes))


def test_a_direct_bilirubin_doesnt_join_the_total_trend(client):
    client.post("/api/labs", json={"collected": "2025-01-01", "results": [
        {"test": "Bilirubin, total", "loinc": "1975-2", "value": 1.0, "unit": "mg/dL"}, {"test": "Bilirubin, direct", "loinc": "1968-7", "value": 0.2, "unit": "mg/dL"}]})
    assert len(client.get("/api/observations", params={"category": "labs"}).json()["items"]) == 2


def test_the_reader_says_which_test_it_filed_a_row_under(client, monkeypatch):
    from app.services import lab_report
    with monkeypatch.context() as patch:
        patch.setattr(lab_report, "read", lambda *a: {"results": [{"test": "Bilirubin, Direct", "value": 0.2}, {"test": "Something else", "value": 1}], "read_as": "text"})
        out = client.post("/api/labs/extract", files={"file": ("r.pdf", b"x", "application/pdf")}, data={"method": "local"}).json()
    assert out["results"][0]["loinc"] == "1968-7" and out["results"][0]["matched_test"] == "Bilirubin, direct"
    assert "matched_test" not in out["results"][1]


# ---- F002, F186: limits and ranges

def test_a_limit_typed_as_text_stays_a_limit(client):
    assert post_labs(client, value_text="<0.5", unit="ng/mL", ref_high=3).status_code == 200
    row = client.get("/api/records", params={"category": "labs"}).json()["items"][0]
    assert row["value_text"] == "<0.5 ng/mL" and row["details"]["comparator"] == "<" and row["value_num"] == 0.5 and row["interpretation"] == "normal"


def test_words_stay_words(client):
    assert post_labs(client, value_text="Negative").status_code == 200
    row = client.get("/api/records", params={"category": "labs"}).json()["items"][0]
    assert row["value_text"] == "Negative" and row["value_num"] is None


def test_a_high_result_against_a_one_sided_range_is_flagged(client):
    assert post_labs(client, value=250, unit="mg/dL", ref_high=200).status_code == 200
    row = client.get("/api/records", params={"category": "labs"}).json()["items"][0]
    assert row["interpretation"] == "high"


# ---- F187: dates

@pytest.mark.parametrize("days, ok", [(0, True), (1, True), (2, False), (365, False)])
def test_a_collection_date_cant_be_in_the_future(client, days, ok):
    r = post_labs(client, collected=(date.today() + timedelta(days=days)).isoformat(), value=1)
    assert (r.status_code == 200) is ok
    if not ok:
        assert "future" in r.json()["detail"]


def test_a_results_own_date_cant_be_in_the_future(client):
    r = post_labs(client, value=1, date="2099-01-01")
    assert r.status_code == 400 and "Ferritin" in r.json()["detail"]


def test_a_date_that_doesnt_exist_is_refused(client):
    assert post_labs(client, collected="2025-02-31", value=1).status_code == 400


def test_a_journal_entry_cant_be_in_the_future(client):
    body = {"event_type": "headache", "category": "symptoms", "name": "Headache"}
    future = (date.today() + timedelta(days=30)).isoformat() + "T10:00:00Z"
    assert client.post("/api/journal", json={**body, "start": future}).status_code == 400
    past = (date.today() - timedelta(days=30)).isoformat() + "T10:00:00Z"
    assert client.post("/api/journal", json={**body, "start": past}).status_code == 200


# ---- what the assistant reads

def test_the_assistant_gets_the_same_rules(client):
    from app import mcp_server
    client.post("/api/labs", json={"collected": "2025-01-01", "results": [{"test": "X", "value_text": "<2", "unit": "mg/L"}, {"test": "X", "value": 3, "unit": "mg/L", "date": "2025-02-01"}]})
    trend = mcp_server.t_lab_trend({"test": "X"})
    assert [(r["value"], r.get("comparator")) for r in trend["results"]] == [(3, None), (2, "<")]     # newest first
    summary = mcp_server.t_health_summary({})
    for key in ("unconfirmed_conditions", "conditions_status_not_stated", "visit_diagnoses", "medications_may_be_out_of_date", "medications_planned_not_started"):
        assert key in summary


def test_the_csv_keeps_its_columns_and_adds_what_the_lab_said(client):
    post_labs(client, value_text="<0.5", unit="ng/mL")
    lines = client.get("/api/export/csv", params={"kind": "records"}).text.splitlines()
    header = lines[0].split(",")
    assert header[:15] == ["category", "title", "code_system", "code", "effective_at", "status", "value", "unit", "value_normalized", "unit_normalized",
                           "reference_low", "reference_high", "interpretation", "source", "narrative"]
    assert header[15:] == ["value_as_reported", "comparator", "reference_as_reported", "patient_reported_correction"]
    assert "<0.5 ng/mL,<," in lines[1]
