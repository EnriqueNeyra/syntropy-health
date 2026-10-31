"""What tools do with arguments, how much they return, and how the rest is reached."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app import mcp_server
from app.connectors import fhir_normalize
from app.core import data_fixes, settings
from app.core.db import db
from app.services import ai
from app.store import activity, profiles, records
from tests.conftest import connect_institution


def call(name, **arguments):
    resp = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    return resp["result"]


def ok(name, **arguments):
    result = call(name, **arguments)
    assert result["isError"] is False, result["content"][0]["text"]
    return json.loads(result["content"][0]["text"])


def fails(name, **arguments):
    result = call(name, **arguments)
    assert result["isError"] is True, result["content"][0]["text"][:300]
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def chart(client):
    connect_institution(client, "epic-stanford-health-care")
    client.post("/api/connections/wearable", json={"provider": "oura", "mode": "simulated"})
    return client


def _person_id():
    return profiles.default_profile()["id"]


def _note(record_id, text, *, category="notes", when="2026-08-05T08:05:00Z", title="Discharge summary", details=None):
    profile = _person_id()
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO connections(id, profile_id, kind, provider, display_name, mode, status, created_at, updated_at, metadata_json) "
                     "VALUES ('con_t', ?, 'ehr', 'epic', 'Test Hospital', 'live', 'active', 1, 1, '{}')", (profile,))
        conn.execute("INSERT INTO clinical_records(id, profile_id, connection_id, resource_type, resource_id, category, title, effective_at, narrative, "
                     "details_json, first_seen_at, updated_at) VALUES (?, ?, 'con_t', 'DocumentReference', ?, ?, ?, ?, ?, ?, 1, 1)",
                     (record_id, profile, record_id, category, title, when, text, json.dumps(details or {})))


def _workouts(count):
    profile = _person_id()
    start = date(2026, 10, 1)
    with db() as conn:
        for i in range(count):
            day = (start - timedelta(days=i)).isoformat()
            conn.execute("INSERT INTO workouts(id, profile_id, name, start_date, end_date, duration_s, distance_m, active_energy_kcal, avg_hr, created_at) "
                         "VALUES (?, ?, 'Running', ?, ?, 1800, 5000, 300, 150, 1)",
                         (f"w{i}", profile, f"{day}T10:00:00Z", f"{day}T10:30:00Z"))


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

def test_an_unknown_metric_is_an_error_naming_the_closest(chart):
    for wrong, right in (("sleep", "sleep_duration"), ("steps", "step_count"), ("hrv", "hrv_rmssd")):
        body = fails("get_daily_metric", metric=wrong)
        assert body["code"] == "unknown_metric" and right in body["closest"], wrong
    assert "sleep_duration" in fails("get_daily_metric", metric="slep_duraton")["closest"]


def test_a_known_metric_with_nothing_in_the_period_says_where_the_data_is(chart):
    result = ok("get_daily_metric", metric="sleep_duration", start="2001-01-01", end="2001-01-31")
    assert result["days"] == [] and result["no_data_in_period"]["first_day"] and result["no_data_in_period"]["last_day"]
    # A metric the app knows, that this person simply doesn't have, isn't a mistake.
    assert "no_data_in_period" not in ok("get_daily_metric", metric="walking_heart_rate_average")


@pytest.mark.parametrize("tool, args, code", [
    ("search_records", {"since": "last week"}, "bad_date"),
    ("search_records", {"until": "yesterday"}, "bad_date"),
    ("search_records", {"since": "2026-02-30"}, "bad_date"),
    ("search_records", {"since": "2026-09-01", "until": "2026-08-01"}, "bad_range"),
    ("search_records", {"category": "bogus"}, "bad_value"),
    ("search_records", {"limit": -1}, "bad_number"),
    ("search_records", {"limit": 0}, "bad_number"),
    ("search_records", {"limit": "many"}, "bad_number"),
    ("search_records", {"query": {"a": 1}}, "bad_value"),
    ("search_records", {"since": {"a": 1}}, "bad_value"),
    ("search_records", {"start_date": "2026-01-01"}, "unknown_argument"),
    ("search_records", {"cursor": "nonsense"}, "bad_cursor"),
    ("get_daily_metric", {"metric": "step_count", "days": 0}, "bad_number"),
    ("get_daily_metric", {"metric": "step_count", "days": -5}, "bad_number"),
    ("get_daily_metric", {"metric": "step_count", "days": 1e400}, "bad_number"),
    ("get_daily_metric", {"metric": ""}, "missing_argument"),
    ("get_daily_metric", {"metric": None}, "missing_argument"),
    ("get_sleep_nights", {"days": -1}, "bad_number"),
    ("get_sleep_nights", {"start": "2001-01-01"}, "period_too_long"),
    ("list_lab_tests", {"category": "bogus"}, "bad_value"),
    ("get_training_summary", {"workout_type": "Bogus"}, "unknown_workout_type"),
    ("compare_measures", {"a": "hrv", "b": "step_count"}, "unknown_measure"),
    ("get_lab_trend", {"test": ""}, "missing_argument"),
    ("get_lab_trend", {"test": "unobtainium"}, "unknown_test"),
    ("get_record", {"record_id": "nope"}, "not_found"),
])
def test_bad_arguments_get_an_error_the_model_can_act_on(chart, tool, args, code):
    assert fails(tool, **args)["code"] == code


def test_a_cursor_only_works_on_the_tool_that_gave_it(chart):
    cursor = ok("search_records", limit=1)["next_cursor"]
    assert ok("search_records", limit=1, cursor=cursor)["records"]
    assert fails("list_lab_tests", cursor=cursor)["code"] == "bad_cursor"


def test_errors_say_what_is_allowed(chart):
    assert "labs" in fails("search_records", category="bogus")["valid"]
    assert "since" in fails("search_records", start_date="2026-01-01")["valid"]
    assert "hrv_rmssd" in fails("compare_measures", a="hrv", b="step_count")["closest"]
    assert "2026-08-05" in fails("search_records", since="last week")["message"]


def test_arguments_that_arent_an_object_are_an_error():
    result = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_goals", "arguments": [1]}})["result"]
    assert result["isError"] and json.loads(result["content"][0]["text"])["code"] == "bad_arguments"


def test_local_models_loose_arguments_still_work(chart):
    assert len(ok("get_daily_metric", metric="sleep_duration", days="14")["days"]) >= 13
    assert len(ok("get_daily_metric", metric="sleep_duration", days=14.0, profile=None)["days"]) >= 13
    assert ok("search_records", category="LABS", query=None, since="", limit="5")["records"]
    assert ok("get_goals", profile=_person_id())


def test_going_over_a_maximum_uses_the_maximum_and_says_so(chart):
    result = ok("search_records", limit=5000)
    assert result["adjusted"] == {"limit": 100} and len(result["records"]) <= 100


def test_internal_errors_dont_reach_the_model(chart, monkeypatch):
    monkeypatch.setattr(records, "summary", lambda *_: (_ for _ in ()).throw(AttributeError("'NoneType' has no attribute secret_path")))
    body = fails("get_health_summary")
    assert body["code"] == "internal_error" and "secret_path" not in json.dumps(body)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def test_until_includes_the_whole_last_day(chart):
    _note("r_until", "text", when="2026-08-05T23:30:00Z", title="Late note")
    ids = {r["id"] for r in ok("search_records", since="2026-08-05", until="2026-08-05", category="notes")["records"]}
    assert "r_until" in ids
    assert "r_until" not in {r["id"] for r in ok("search_records", until="2026-08-04", category="notes")["records"]}
    assert "r_until" not in {r["id"] for r in ok("search_records", since="2026-08-06", category="notes")["records"]}


def test_date_only_records_and_the_rest_api_follow_the_same_day(chart):
    _note("r_day", "text", when="2026-08-05", title="Plain date")
    assert "r_day" in {r["id"] for r in ok("search_records", since="2026-08-05", until="2026-08-05", category="notes")["records"]}
    items = chart.get("/api/records", params={"category": "notes", "since": "2026-08-05", "until": "2026-08-05"}).json()["items"]
    assert "r_day" in {r["id"] for r in items}


def test_a_day_means_a_day_where_the_person_is(chart):
    from app.core import settings
    settings.set("display.timezone", "America/Los_Angeles")
    _note("r_evening", "text", when="2026-08-06T03:00:00Z", title="8pm on the 5th in Los Angeles")
    _note("r_offset", "text", when="2026-08-05T20:30:00-07:00", title="Stored with its own offset")
    found = {r["id"] for r in ok("search_records", since="2026-08-05", until="2026-08-05", category="notes")["records"]}
    assert {"r_evening", "r_offset"} <= found
    assert not {"r_evening", "r_offset"} & {r["id"] for r in ok("search_records", since="2026-08-06", until="2026-08-06", category="notes")["records"]}


def test_a_full_time_bound_still_works(chart):
    _note("r_time", "text", when="2026-08-05T12:00:00Z", title="Noon")
    assert "r_time" in {r["id"] for r in ok("search_records", since="2026-08-05T11:00:00Z", until="2026-08-05T13:00:00-00:00", category="notes")["records"]}
    assert "r_time" not in {r["id"] for r in ok("search_records", until="2026-08-05T11:00:00Z", category="notes")["records"]}


def test_results_say_today_and_whose_they_are(chart):
    result = ok("get_goals")
    assert result["person"] and str(date.today().year) in result["as_of"]


def test_older_data_can_be_reached_by_period(chart):
    today = date.today()
    past = (today - timedelta(days=500)).isoformat()
    profile = _person_id()
    with db() as conn:
        conn.execute("INSERT INTO biometric_daily(profile_id, day, metric_type, source_name, value, sample_count, unit) "
                     "VALUES (?, ?, 'step_count', 'Old phone', 12345, 1, 'count')", (profile, past))
    days = ok("get_daily_metric", metric="step_count", start=past, end=past)["days"]
    assert [d["value"] for d in days] == [12345]
    assert ok("get_daily_metric", metric="step_count", end=past, days=1)["days"][0]["value"] == 12345


def test_workouts_and_journal_read_any_period(client):
    _workouts(3)
    only = ok("list_workouts", start="2026-09-30", end="2026-09-30")
    assert [w["start_date"][:10] for w in only["workouts"]] == ["2026-09-30"] and only["summary"]["count"] == 1
    client.post("/api/journal", json={"event_type": "headache", "category": "symptoms", "name": "Headache", "note": "After the long drive",
                                     "start": "2020-03-02T08:00:00Z"})
    journal = ok("get_health_journal", start="2020-03-01", end="2020-03-31")
    assert [e["note"] for e in journal["events"]] == ["After the long drive"]
    assert ok("get_health_journal", days=36500, search="long drive")["events"]
    assert not ok("get_health_journal", days=36500, search="nothing like this")["events"]


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------

def _assert_budget(text):
    assert len(text) <= mcp_server.RESULT_BUDGET, len(text)
    json.loads(text)


def test_a_long_list_comes_in_parts_of_whole_json(client):
    _workouts(600)
    seen, cursor, parts = [], None, 0
    while True:
        result = call("list_workouts", days=36500, limit=500, **({"cursor": cursor} if cursor else {}))
        text = result["content"][0]["text"]
        _assert_budget(text)
        page = json.loads(text)
        assert page["summary"]["count"] == 600      # the whole period, on every part
        seen += [w["start_date"] for w in page["workouts"]]
        parts += 1
        if not page["truncated"]:
            assert "next_cursor" not in page
            break
        assert page["rows_omitted"] == 600 - len(seen)
        cursor = page["next_cursor"]
    assert parts > 1 and len(seen) == 600 == len(set(seen)) and seen == sorted(seen, reverse=True)


def test_search_reaches_every_match(chart):
    for i in range(130):
        _note(f"r{i:03}", "x", when=f"2025-01-01T00:{i // 60:02}:{i % 60:02}Z", title=f"Pagination test {i}")
    seen, cursor = [], None
    while True:
        page = ok("search_records", query="pagination", limit=100, **({"cursor": cursor} if cursor else {}))
        seen += [r["id"] for r in page["records"]]
        if not page["truncated"]:
            break
        cursor = page["next_cursor"]
    assert len(seen) == 130 == len(set(seen))
    first = ok("search_records", query="pagination", limit=10)
    assert first["total_matches"] == 130 and first["rows_omitted"] == 120 and first["truncated"]


@pytest.mark.parametrize("tool, args", [
    ("list_workouts", {"days": 36500, "limit": 500}),
    ("get_training_summary", {"weeks": 104, "workout_type": "Running"}),
    ("search_records", {"limit": 100}),
    ("get_daily_metric", {"metric": "step_count", "days": 3650}),
    ("get_sleep_nights", {"days": 365}),
    ("get_health_summary", {}),
    ("list_lab_tests", {}),
    ("get_lab_trend", {"test": "blood pressure"}),
    ("compare_measures", {"a": "step_count", "b": "sleep_duration", "days": 365}),
    ("get_goals", {"days": 90}),
    ("get_insights", {}),
])
def test_nothing_goes_over_budget(chart, tool, args):
    _workouts(1500)
    for i in range(200):
        _note(f"big{i}", "word " * 2000, when=f"2025-02-01T00:{i // 60:02}:{i % 60:02}Z", details={"authors": ["Dr. Long"] * 200})
    _assert_budget(call(tool, **args)["content"][0]["text"])


def test_a_summary_too_long_for_its_budget_says_what_was_left_out(chart):
    out = mcp_server._run("get_health_summary", {}, budget=1500)
    assert not out.is_error and len(out.text) <= 1500
    body = json.loads(out.text)
    assert body["truncated"] is True and body["person"] and body["as_of"]


def test_asks_lookup_is_valid_json_at_its_own_budget(chart):
    _workouts(1500)
    text, is_error = ai._run_tool("list_workouts", {"days": 36500, "limit": 500}, _person_id())
    assert not is_error and len(text) <= ai.TOOL_RESULT_CHARS
    assert json.loads(text)["truncated"] is True


def test_workout_rows_leave_out_empty_fields(client):
    _workouts(1)
    row = ok("list_workouts", start="2026-10-01", end="2026-10-01")["workouts"][0]
    assert "step_count" not in row and "elevation_ascent_m" not in row and row["distance_m"] == 5000


def test_a_series_comes_newest_first_with_stats_over_the_whole_period(chart):
    result = ok("get_daily_metric", metric="sleep_duration", days=30)
    days = [d["day"] for d in result["days"]]
    assert days == sorted(days, reverse=True) and result["stats"]["count"] >= len(days) - 1
    nights = [n["day"] for n in ok("get_sleep_nights", days=14)["nights"]]
    assert nights == sorted(nights, reverse=True)
    results = [r["date"] for r in ok("get_lab_trend", test="A1c")["results"]]
    assert results == sorted(results, reverse=True)


# ---------------------------------------------------------------------------
# One record
# ---------------------------------------------------------------------------

def test_a_panel_shows_its_results(chart):
    panel = next(r for r in ok("search_records", category="reports", limit=100)["records"] if r["title"] == "Comprehensive metabolic panel")
    results = ok("get_record", record_id=panel["id"])["results"]
    assert len(results) >= 5 and all(r["test"] and r["result"] for r in results)


def test_a_long_note_can_be_read_to_its_end(chart):
    text = "Line of the note.\n" * 3000 + "THE END"
    _note("r_long", text[:4000], details={"full_text": text})
    parts, cursor = [], None
    while True:
        raw = call("get_record", record_id="r_long", **({"cursor": cursor} if cursor else {}))["content"][0]["text"]
        _assert_budget(raw)
        page = json.loads(raw)
        assert page["text_total_chars"] == len(text)
        parts.append(page["full_text"])
        assert "narrative" not in page      # the same words as the report's start
        if not page["truncated"]:
            break
        cursor = page["next_cursor"]
    assert len(parts) > 1 and "".join(parts) == text


def test_a_short_note_isnt_sent_twice(chart):
    _note("r_short", "Short note.", details={"full_text": "Short note."})
    page = ok("get_record", record_id="r_short")
    assert page["full_text"] == "Short note." and "narrative" not in page and page["truncated"] is False


def test_a_different_conclusion_is_kept_beside_the_report(chart):
    _note("r_two", "Impression: normal.", details={"full_text": "The whole report text."})
    page = ok("get_record", record_id="r_two")
    assert page["narrative"] == "Impression: normal." and page["full_text"] == "The whole report text."


def test_a_long_narrative_without_full_text_pages_too(chart):
    _note("r_narr", "n" * 45_000 + "THE END")
    first = ok("get_record", record_id="r_narr")
    assert first["truncated"] and len(first["narrative"]) < 20_000
    second = ok("get_record", record_id="r_narr", cursor=first["next_cursor"])
    assert second["text_from_char"] == len(first["narrative"])


# ---------------------------------------------------------------------------
# Demo data, and what descriptions promise
# ---------------------------------------------------------------------------

def test_every_tool_built_on_demo_data_says_so(chart):
    for tool, args in (("get_insights", {}), ("compare_measures", {"a": "step_count", "b": "sleep_duration"}), ("get_sleep_nights", {}),
                       ("get_training_summary", {}), ("get_goals", {}), ("get_daily_metric", {"metric": "step_count"}), ("get_health_summary", {})):
        result = ok(tool, **args)
        assert result.get("simulated") is True and result["simulated_sources"], tool
    assert ok("search_records", category="labs").get("simulated") is True      # the simulated hospital


def test_real_data_isnt_called_demo_data(client):
    _note("r_real", "text")
    assert "simulated" not in ok("search_records", category="notes")
    assert "simulated" not in ok("get_record", record_id="r_real")


def test_descriptions_match_what_the_tools_do(chart):
    tools = {t["name"]: t for t in mcp_server._tool_list()}
    profile = tools["search_records"]["inputSchema"]["properties"]["profile"]["description"]
    assert "default profile" not in profile
    assert "complete text" not in tools["get_record"]["description"]
    assert "'steps'" not in tools["list_wearable_metrics"]["description"]
    assert "step_count" in tools["list_wearable_metrics"]["description"]
    for name, tool in tools.items():
        for prop, schema in tool["inputSchema"]["properties"].items():
            assert schema.get("description") or schema.get("enum") or "minimum" in schema, f"{name}.{prop} says nothing"
    metrics = ok("list_wearable_metrics")["metrics"]
    assert {"metric", "label", "unit", "days"} <= set(metrics[0])
    assert ok("get_health_summary")["wearable_headline_30d"][0]["metric"] in {m["metric"] for m in metrics}


def test_compare_with_nothing_in_common_says_why(chart):
    result = ok("compare_measures", a="hrv_sdnn", b="step_count")
    assert result["n"] == 0 and "No day" in result["note"] and "step_count" in result["note"]


# ---------------------------------------------------------------------------
# Workouts several sources recorded, and what their totals cover
# ---------------------------------------------------------------------------

def _same_sessions(sources, count):
    profile = _person_id()
    start = date(2026, 10, 1)
    with db() as conn:
        for i in range(count):
            day = (start - timedelta(days=i)).isoformat()
            for source in sources:
                conn.execute("INSERT INTO workouts(id, profile_id, activity_type, name, start_date, end_date, duration_s, distance_m, source_name, avg_hr, created_at) "
                             "VALUES (?, ?, 'running', 'Running', ?, ?, 1800, 5000, ?, 150, 1)",
                             (f"w{source}{i}", profile, f"{day}T10:00:00Z", f"{day}T10:30:00Z", source))


@pytest.mark.parametrize("sources", [["Apple Watch", "WHOOP", "Oura"], ["Apple Watch", "WHOOP", "Oura", "Garmin", "Polar", "Fitbit"]])
def test_every_workout_is_reachable_however_many_sources_recorded_it(client, sources):
    _same_sessions(sources, 150)
    seen, cursor = [], None
    for _ in range(200):
        out = ok("list_workouts", days=36500, **({"cursor": cursor} if cursor else {}))
        seen += out["workouts"]
        assert out["workouts"], out
        cursor = out.get("next_cursor")
        if not cursor:
            break
    assert out["truncated"] is False and "rows_omitted" not in out
    assert out["summary"]["count"] == 150
    assert len({w["start_date"] for w in seen}) == 150 and len(seen) == 150
    assert all(len(w["also_recorded_by"]) == len(sources) - 1 for w in seen)


def test_a_session_at_the_limit_keeps_all_its_sources(client):
    _same_sessions(["Apple Watch", "WHOOP", "Oura", "Garmin"], 10)
    for limit in range(1, 11):
        rows = activity.list_workouts(_person_id(), None, limit)
        assert len(rows) == limit and all(len(w["also_recorded_by"]) == 3 for w in rows)


def test_a_page_with_nothing_on_it_doesnt_promise_more(client, monkeypatch):
    _same_sessions(["Apple Watch"], 3)
    monkeypatch.setattr(activity, "list_workouts", lambda *a, **k: [])
    out = ok("list_workouts", days=36500)
    assert out["workouts"] == [] and out["truncated"] is False and "next_cursor" not in out


def test_workout_totals_say_which_period_they_cover(client):
    _same_sessions(["Apple Watch"], 400)
    out = ok("list_workouts", start="2026-01-01", end="2026-06-30")
    assert out["summary"]["days"] == 181 and out["summary"]["count"] == 181
    assert out["summary"]["start"] == "2026-01-01" and out["summary"]["end"] == "2026-06-30"
    out = ok("list_workouts", end="2026-06-30", days=10)
    assert out["summary"]["days"] == 10 and out["summary"]["start"] == "2026-06-21" and out["summary"]["end"] == "2026-06-30"
    assert ok("list_workouts", days=30)["summary"]["days"] == 30


def test_the_workouts_page_gets_totals_for_its_window_too(client):
    _same_sessions(["Apple Watch"], 40)
    body = client.get("/api/biometrics/workouts", params={"start": "2026-09-01", "end": "2026-09-30"}).json()
    assert body["summary"]["days"] == 30 and body["summary"]["count"] == 30
    assert client.get("/api/biometrics/workouts", params={"days": 7}).json()["summary"]["days"] == 7


# ---------------------------------------------------------------------------
# Dates at the ends of the calendar
# ---------------------------------------------------------------------------

@pytest.fixture(params=["UTC", "Asia/Tokyo", "America/Los_Angeles"])
def anywhere(request, chart):
    settings.set("display.timezone", request.param)
    return chart


@pytest.mark.parametrize("args", [{"until": "9999-12-31"}, {"since": "0001-01-01"}, {"since": "0001-01-01", "until": "9999-12-31"},
                                  {"until": "0001-01-01"}, {"since": "9999-12-31"}, {"since": "0001-01-01T00:00:00+14:00"},
                                  {"until": "9999-12-31T23:59:59-12:00"}, {"until": "9999-12-31T23:59:59Z"}])
def test_records_with_a_date_at_the_edge_of_the_calendar(anywhere, args):
    r = anywhere.get("/api/records", params=args)
    assert r.status_code == 200, r.text
    assert ok("search_records", **args)["total_matches"] == r.json()["total"]


def test_no_upper_or_lower_bound_finds_everything(anywhere):
    everything = ok("search_records")["total_matches"]
    assert everything
    out = ok("search_records", until="9999-12-31", since="0001-01-01")
    assert out["total_matches"] == everything and out["adjusted"]["until"] and out["adjusted"]["since"]
    assert anywhere.get("/api/records", params={"until": "9999-12-31", "since": "0001-01-01"}).json()["total"] == everything
    assert ok("search_records", since="9999-12-31")["total_matches"] == 0
    assert ok("search_records", until="0001-01-01")["total_matches"] == 0


@pytest.mark.parametrize("tool, extra", [("get_sleep_nights", {}), ("get_daily_metric", {"metric": "step_count"}), ("list_workouts", {}),
                                         ("get_health_journal", {})])
@pytest.mark.parametrize("period", [{"end": "0001-01-01"}, {"end": "9999-12-31"}, {"end": "0001-01-01", "days": 30}, {"start": "0001-01-01"},
                                    {"start": "9999-12-31"}, {"start": "0001-01-01", "end": "9999-12-31"}, {"start": "0001-01-01", "end": "0001-01-02"},
                                    {"start": "9999-12-30", "end": "9999-12-31"}, {"end": "9999-12-31T23:59:59-12:00"}, {"end": "0001-01-01T00:00:00+14:00"}])
def test_a_period_at_the_edge_of_the_calendar_gets_an_answer_or_a_reason(anywhere, tool, extra, period):
    result = call(tool, **extra, **period)
    if result["isError"]:
        assert json.loads(result["content"][0]["text"])["code"] in ("period_too_long", "bad_range")


def test_an_end_with_no_limit_means_up_to_today(chart):
    today = ok("get_sleep_nights", days=30)
    far = ok("get_sleep_nights", days=30, end="9999-12-31")
    assert far["adjusted"]["end"] == today["end"] and far["nights"] == today["nights"] and today["nights"]


def test_a_start_after_today_is_said_to_be(chart):
    assert fails("get_sleep_nights", start="2200-01-01")["code"] == "bad_range"


@pytest.mark.parametrize("params", [{"start": "0001-01-01"}, {"end": "9999-12-31"}, {"start": "0001-01-01", "end": "9999-12-31"},
                                    {"end": "9999-12-31T23:59:59-12:00"}, {"start": "0001-01-01T00:00:00+14:00"}])
def test_the_workouts_and_journal_pages_take_edge_dates(anywhere, params):
    assert anywhere.get("/api/biometrics/workouts", params=params).status_code == 200
    assert anywhere.get("/api/biometrics/events", params=params).status_code == 200


# ---------------------------------------------------------------------------
# Numbers that aren't numbers
# ---------------------------------------------------------------------------

def _make_infinite(field="value_num"):
    with db() as conn:
        row = conn.execute("SELECT id FROM clinical_records WHERE category = 'labs' AND value_num IS NOT NULL LIMIT 1").fetchone()
        conn.execute(f"UPDATE clinical_records SET {field} = 9e999 WHERE id = ?", (row["id"],))
    return row["id"]


def test_an_infinite_value_doesnt_break_a_call(chart):
    record = _make_infinite()
    token = chart.post("/api/agents/tokens", json={"name": "t"}).json()["token"]
    r = chart.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_record", "arguments": {"record_id": record}}},
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == json.loads(result["content"][0]["text"])
    assert result["structuredContent"].get("value_num") is None


def test_the_result_is_valid_json_both_ways(chart):
    _make_infinite()
    reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_records", "arguments": {"category": "labs", "limit": 100}}})
    json.dumps(reply, allow_nan=False)


def test_an_infinite_value_isnt_shown_as_a_result(chart):
    rec = records.get_record(_make_infinite())
    rec["value_text"] = None
    assert "inf" not in str(mcp_server._lab_line(rec).get("result", ""))
    assert "inf" not in mcp_server._range({"ref_low": float("inf"), "ref_high": 5.0, "unit": "mg/dL"})


def test_a_portal_cant_store_an_infinite_value(client):
    assert fhir_normalize._num("1e999") is None and fhir_normalize._num(float("inf")) is None
    assert fhir_normalize._num(float("nan")) is None and fhir_normalize._num("5.5") == 5.5


def test_infinite_values_already_stored_are_cleared(chart):
    with db() as conn:
        conn.execute("DELETE FROM data_fixes WHERE name = ?", (records.NO_INFINITY_FIX,))
    record = _make_infinite()
    other = _make_infinite("ref_high")
    _make_infinite("value_norm")
    data_fixes.run_pending()
    assert records.get_record(record)["value_num"] is None and records.get_record(other)["ref_high"] is None
    with db() as conn:
        assert not conn.execute("SELECT 1 FROM clinical_records WHERE abs(value_norm) = 9e999 OR abs(ref_low) = 9e999").fetchone()
    assert chart.get("/api/records", params={"category": "labs", "limit": 1000}).status_code == 200
