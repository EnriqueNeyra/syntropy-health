"""Who an AI connection can read, whose chart it answers from, and what the access log says about it."""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import mcp_server
from app.core import config
from app.store import agents
from tests.conftest import BASE, LOCAL_CLIENT, connect_institution
from tests.test_accounts import _ids, _join, _person, _signed_in_owner


def _rpc(client, token, name, args=None):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _ok(result):
    assert result["isError"] is False, result["content"][0]["text"]
    return json.loads(result["content"][0]["text"])


def _token(client, name="Claude", profile_id=None):
    return client.post("/api/agents/tokens", json={"name": name, "profile_id": profile_id}).json()


@pytest.fixture
def household(raw_client):
    """Alex (owner, with a simulated chart) and Maria (invited, with her own A1c). Nothing is shared yet."""
    client = _signed_in_owner(raw_client)
    assert client.put("/api/settings/developer", json={"enabled": True}).status_code == 200
    assert client.put("/api/settings/platforms/epic", json={"mode": "simulated"}).status_code == 200
    connect_institution(client, "epic-stanford-health-care")
    maria = _person(client, "Maria")
    maria_client, _ = _join(client, maria)
    assert maria_client.post("/api/labs", params={"profile": maria}, json={
        "collected": "2026-09-01", "results": [{"test": "Hemoglobin A1c", "loinc": "4548-4", "value": 7.7, "unit": "%"}]}).status_code == 200
    alex = _ids(client)["Alex"]["id"]
    record = _ok(_rpc(client, _token(client)["token"], "search_records", {"category": "conditions"}))["records"][0]
    return {"alex_client": client, "maria_client": maria_client, "alex": alex, "maria": maria, "record": record["id"]}


def _share_alex_with_maria(h, level="view"):
    accounts = h["alex_client"].get(f"/api/profiles/{h['alex']}/access").json()["accounts"]
    account = next(a for a in accounts if a["name"] == "Maria")["account_id"]
    assert h["alex_client"].put(f"/api/profiles/{h['alex']}/access", json={"account_id": account, "level": level}).status_code == 200


# ---------------------------------------------------------------------------
# Lookups by id
# ---------------------------------------------------------------------------

def test_a_members_token_cant_read_someone_elses_record_by_id(household):
    token = _token(household["maria_client"])["token"]
    assert _rpc(household["maria_client"], token, "get_record", {"record_id": household["record"]})["isError"]


def test_sharing_that_ends_ends_for_lookups_by_id_too(household):
    _share_alex_with_maria(household)
    client = household["maria_client"]
    everyone, only_alex = _token(client)["token"], _token(client, "Alex only", household["alex"])["token"]
    assert not _rpc(client, everyone, "get_record", {"record_id": household["record"]})["isError"]
    assert not _rpc(client, only_alex, "get_record", {"record_id": household["record"]})["isError"]

    _share_alex_with_maria(household, None)
    result = _rpc(client, everyone, "get_record", {"record_id": household["record"]})
    assert result["isError"] and "Record not found" in result["content"][0]["text"]
    # A token only for them ends with the sharing.
    gone = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_record", "arguments": {"record_id": household["record"]}}},
                       headers={"Authorization": f"Bearer {only_alex}"})
    assert gone.status_code == 401


def test_missing_and_forbidden_records_look_the_same(household):
    client = household["maria_client"]
    token = _token(client)["token"]
    forbidden = _rpc(client, token, "get_record", {"record_id": household["record"]})["content"][0]["text"]
    missing = _rpc(client, token, "get_record", {"record_id": "nope"})["content"][0]["text"]
    assert forbidden == missing


def test_a_token_whose_account_is_gone_reads_nothing(household):
    token = agents.create("Orphan", None, None)["token"]
    client = household["alex_client"]
    assert _rpc(client, token, "get_record", {"record_id": household["record"]})["isError"]
    assert _rpc(client, token, "get_health_summary")["isError"]
    assert _ok(_rpc(client, token, "list_profiles"))["people"] == []


def test_the_computers_own_connection_still_reads_by_id(household):
    # Neither limit set: stdio, on the computer holding the data.
    reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "get_record", "arguments": {"record_id": household["record"]}}})
    assert reply["result"]["isError"] is False


def test_the_assistant_stays_on_its_person():
    token = mcp_server.SCOPED_PROFILE.set("prf_someone")
    try:
        assert mcp_server.can_read("prf_someone") and not mcp_server.can_read("prf_other")
    finally:
        mcp_server.SCOPED_PROFILE.reset(token)


def _every_tool(record):
    return {"list_profiles": {}, "get_health_summary": {}, "search_records": {"query": "hypertension"}, "get_record": {"record_id": record},
            "list_lab_tests": {}, "get_lab_trend": {"test": "LDL"}, "list_wearable_metrics": {}, "get_daily_metric": {"metric": "step_count"},
            "list_workouts": {}, "get_health_journal": {}, "get_insights": {}, "compare_measures": {"a": "step_count", "b": "sleep_duration"},
            "get_sleep_nights": {}, "get_training_summary": {}, "get_goals": {}}


def test_the_matrix_covers_every_tool():
    assert set(_every_tool("x")) == set(mcp_server.TOOLS)


def test_a_token_limited_to_someone_ends_when_their_sharing_does(household):
    client = household["maria_client"]
    _share_alex_with_maria(household)
    token = _token(client, "Was shared", household["alex"])["token"]
    _share_alex_with_maria(household, None)
    for tool, args in _every_tool(household["record"]).items():
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}},
                        headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401, tool


@pytest.mark.parametrize("kind", ["member's own token", "token limited to the member"])
def test_no_tool_reaches_the_owners_chart_without_access(household, kind):
    client = household["maria_client"]
    token = _token(client, "Maria's", household["maria"] if "limited" in kind else None)["token"]
    for tool, args in _every_tool(household["record"]).items():
        for extra in ({}, {"profile": "Alex"}, {"profile": household["alex"]}):
            if tool == "get_record" and extra:
                extra = {}
            result = _rpc(client, token, tool, {**args, **extra})
            text = result["content"][0]["text"]
            assert "Essential hypertension" not in text and '"person": "Alex"' not in text, (kind, tool, extra)
            if extra and tool != "list_profiles":
                assert result["isError"], (kind, tool, extra)
            if tool == "list_profiles":
                assert "Alex" not in text


def test_with_access_every_tool_reads_the_chart_it_was_asked_for(household):
    _share_alex_with_maria(household)
    client = household["maria_client"]
    token = _token(client)["token"]
    for tool, args in _every_tool(household["record"]).items():
        if tool in ("list_profiles", "get_record"):
            continue
        for person in ("Alex", "Maria"):
            result = _rpc(client, token, tool, {**args, "profile": person})
            if result["isError"]:      # nothing by that name on this person's chart, which it says
                assert json.loads(result["content"][0]["text"])["code"] == "unknown_test", (tool, person)
                continue
            assert json.loads(result["content"][0]["text"])["person"] == person, (tool, person)


# ---------------------------------------------------------------------------
# Whose chart
# ---------------------------------------------------------------------------

def test_a_token_that_covers_several_people_has_to_say_who(household):
    _share_alex_with_maria(household)
    client = household["maria_client"]
    token = _token(client)["token"]
    assert [p["name"] for p in _ok(_rpc(client, token, "list_profiles"))["people"]] == ["Alex", "Maria"]

    for tool, args in (("get_health_summary", {}), ("get_lab_trend", {"test": "A1c"}), ("search_records", {"query": "a1c"})):
        result = _rpc(client, token, tool, args)
        assert result["isError"], tool
        body = json.loads(result["content"][0]["text"])
        assert body["code"] == "which_person" and set(body["valid"]) == {"Alex", "Maria"}

    mine = _ok(_rpc(client, token, "get_lab_trend", {"test": "A1c", "profile": "Maria"}))
    assert [p["value"] for p in mine["results"]] == [7.7] and mine["person"] == "Maria"


def test_a_token_that_covers_one_person_needs_no_name(household):
    client = household["maria_client"]
    token = _token(client)["token"]
    summary = _ok(_rpc(client, token, "get_health_summary"))
    assert summary["profile"]["name"] == "Maria" and summary["person"] == "Maria"
    # A share later doesn't quietly move this connection to someone else's chart.
    _share_alex_with_maria(household)
    assert _rpc(client, token, "get_health_summary")["isError"]


def test_list_profiles_marks_whose_token_it_is(household):
    _share_alex_with_maria(household)
    client = household["maria_client"]
    people = _ok(_rpc(client, _token(client)["token"], "list_profiles"))["people"]
    assert {p["name"]: p.get("you", False) for p in people} == {"Alex": False, "Maria": True}


def test_two_people_with_one_name_are_not_guessed_between(raw_client):
    client = _signed_in_owner(raw_client)
    first, second = _person(client, "Sam", "child"), _person(client, "Sam", "parent")
    token = _token(client)["token"]
    result = _rpc(client, token, "get_health_summary", {"profile": "Sam"})
    assert result["isError"] and json.loads(result["content"][0]["text"])["code"] == "which_person"
    assert _ok(_rpc(client, token, "get_health_summary", {"profile": second}))["profile"]["name"] == "Sam"
    assert first != second


def test_a_scoped_token_says_so_when_asked_about_someone_else(household):
    client = household["alex_client"]
    sam = _person(client, "Sam", "child")
    token = _token(client, "Sam only", sam)["token"]
    assert _rpc(client, token, "get_health_summary", {"profile": "Alex"})["isError"]
    assert _ok(_rpc(client, token, "get_health_summary"))["profile"]["name"] == "Sam"


def test_an_instance_with_several_people_and_no_token_has_to_say_who_too(household):
    reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_health_summary", "arguments": {}}})
    assert reply["result"]["isError"]
    assert json.loads(reply["result"]["content"][0]["text"])["code"] == "which_person"


# ---------------------------------------------------------------------------
# The access log
# ---------------------------------------------------------------------------

def _log(client, action_prefix="mcp."):
    return [e for e in client.get("/api/audit", params={"limit": 500}).json()["events"] if e["action"].startswith(action_prefix)]


def _rows(action_prefix="mcp."):
    conn = sqlite3.connect(str(config.db_path()))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action LIKE ? ORDER BY at", (action_prefix + "%",))]
    finally:
        conn.close()


def test_each_call_logs_person_tool_and_outcome(household):
    client = household["maria_client"]
    created = _token(client, "Maria Claude")
    _ok(_rpc(client, created["token"], "search_records", {"query": "a1c", "profile": "Maria"}))
    row = _rows("mcp.search_records")[-1]
    assert row["profile_id"] == household["maria"]
    assert row["actor"] == f"mcp:{created['id']}"
    detail = json.loads(row["detail"])
    assert detail["outcome"] == "ok" and detail["query"] == "a1c" and "profile" not in detail
    assert detail["ms"] >= 0 and detail["chars"] > 0

    # The record's own person, for a lookup by id.
    _ok(_rpc(household["alex_client"], _token(household["alex_client"])["token"], "get_record", {"record_id": household["record"]}))
    assert _rows("mcp.get_record")[-1]["profile_id"] == household["alex"]


def test_failed_calls_are_logged_without_their_arguments(household):
    client = household["maria_client"]
    token = _token(client)["token"]
    assert _rpc(client, token, "get_lab_trend", {"test": "HIV test nothing like this"})["isError"]
    row = _rows("mcp.get_lab_trend")[-1]
    detail = json.loads(row["detail"])
    assert row["profile_id"] == household["maria"]
    assert detail["outcome"] == "error:unknown_test" and "test" not in detail and "HIV" not in row["detail"]

    # Asking about someone out of reach leaves a trace with no person on it.
    assert _rpc(client, token, "get_health_summary", {"profile": "Alex"})["isError"]
    row = _rows("mcp.get_health_summary")[-1]
    assert row["profile_id"] is None and json.loads(row["detail"])["outcome"].startswith("error:")


def test_search_words_stay_with_the_person_who_searched(household):
    _share_alex_with_maria(household)
    alex_client, maria_client = household["alex_client"], household["maria_client"]
    token = _token(maria_client, "Maria Claude")["token"]
    _ok(_rpc(maria_client, token, "search_records", {"query": "hiv test", "profile": "Maria"}))
    _ok(_rpc(maria_client, token, "search_records", {"query": "alex hiv", "profile": "Alex"}))
    _ok(_rpc(maria_client, token, "list_profiles"))

    # Maria sees all of it, under the app's name, with her words.
    mine = [e for e in _log(maria_client, "mcp.search_records") if e["who"] == "Maria Claude"]
    assert {json.loads(e["detail"])["query"] for e in mine} == {"hiv test", "alex hiv"}

    # Alex sees that Maria's app read Alex's chart, and not what it searched for. Maria's own lookup isn't his business.
    seen = _log(alex_client)
    assert "hiv" not in json.dumps(seen).lower()
    on_alex = [e for e in seen if e["profile_id"] == household["alex"] and e["who"] == "Maria Claude"]
    assert len(on_alex) == 1 and json.loads(on_alex[0]["detail"]) == {"outcome": "ok"}
    assert not [e for e in seen if e["profile_id"] == household["maria"]]


def test_the_log_keeps_working_after_a_token_is_revoked(household):
    client = household["maria_client"]
    created = _token(client, "Maria Claude")
    _ok(_rpc(client, created["token"], "search_records", {"query": "a1c"}))
    assert client.delete(f"/api/agents/tokens/{created['id']}").status_code == 200
    entry = _log(client, "mcp.search_records")[-1]
    assert entry["who"] == "Maria Claude" and json.loads(entry["detail"])["query"] == "a1c"


def test_older_log_rows_stop_showing_search_words_to_the_owner(household):
    conn = sqlite3.connect(str(config.db_path()))
    conn.execute("INSERT INTO audit_log(at, actor, action, profile_id, detail) VALUES (?, 'mcp:Maria Claude', 'mcp.search_records', NULL, ?)",
                 (time.time(), json.dumps({"query": "hiv test"})))
    conn.commit()
    conn.close()
    alex = _log(household["alex_client"], "mcp.search_records")
    assert alex and "hiv" not in json.dumps(alex)
    # A token of that name makes it hers, unless someone else has one by the same name.
    _token(household["maria_client"], "Maria Claude")
    assert any("hiv" in (e["detail"] or "") for e in _log(household["maria_client"], "mcp.search_records"))
    _token(household["alex_client"], "Maria Claude")
    assert not any("hiv" in (e["detail"] or "") for e in _log(household["maria_client"], "mcp.search_records"))


def test_the_owners_own_token_and_stdio_lookups_stay_visible_to_them(household):
    client = household["alex_client"]
    _ok(_rpc(client, _token(client, "Alex Claude")["token"], "search_records", {"query": "lisinopril", "profile": "Alex"}))
    mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "search_records", "arguments": {"query": "metformin", "profile": "Alex"}}})
    words = {json.loads(e["detail"]).get("query") for e in _log(client, "mcp.search_records")}
    assert {"lisinopril", "metformin"} <= words


def test_an_ai_agent_started_by_the_app_logs_its_person_and_shows_its_words_only_to_who_asked(household):
    token = mcp_server.ACTOR.set("agent:claude")
    scope = mcp_server.SCOPED_PROFILE.set(household["maria"])
    try:
        mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "search_records", "arguments": {"query": "hiv test"}}})
    finally:
        mcp_server.ACTOR.reset(token)
        mcp_server.SCOPED_PROFILE.reset(scope)
    # The words are kept, and Maria, who didn't ask, learns that her record was read, not what for.
    row = _rows("mcp.search_records")[-1]
    assert row["profile_id"] == household["maria"] and "hiv" in row["detail"]
    shown = _log(household["maria_client"], "mcp.search_records")
    assert [e["who"] for e in shown] == ["agent:claude"] and "hiv" not in json.dumps(shown)


def test_logged_arguments_are_capped(household):
    client = household["maria_client"]
    token = _token(client)["token"]
    _ok(_rpc(client, token, "search_records", {"query": "x" * 50_000}))
    assert len(_rows("mcp.search_records")[-1]["detail"]) < 600


# ---------------------------------------------------------------------------
# The token check under a busy database
# ---------------------------------------------------------------------------

def _hold_write_lock(seconds):
    conn = sqlite3.connect(str(config.db_path()), check_same_thread=False, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    timer = threading.Timer(seconds, lambda: (conn.execute("ROLLBACK"), conn.close()))
    timer.start()
    return timer


def test_a_made_up_token_doesnt_wait_for_the_write_lock(household):
    client = TestClient(household["alex_client"].app, base_url=BASE, client=LOCAL_CLIENT)
    timer = _hold_write_lock(2)
    started = time.monotonic()
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Authorization": "Bearer sh_agent_madeup"})
    took = time.monotonic() - started
    timer.join()
    assert r.status_code == 401 and took < 1.5


def test_reading_works_while_something_else_holds_the_write_lock(household, monkeypatch):
    client = household["alex_client"]
    token = _token(client)["token"]
    # Noting "last used" happens after the reply has gone, which a test client waits for and a real one doesn't.
    monkeypatch.setattr(agents, "note_use", lambda *a: None)
    timer = _hold_write_lock(2)
    started = time.monotonic()
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Authorization": f"Bearer {token}"})
    took = time.monotonic() - started
    timer.join()
    assert r.status_code == 200 and r.json()["result"]["tools"] and took < 1.5


def test_noting_last_use_never_fails_a_request(household, monkeypatch):
    def locked():
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(agents, "db", locked)
    agents.note_use({"id": "agt_x", "last_used_at": 0}, "list_profiles")
    agents.note_use({"id": "agt_x", "last_used_at": 0}, None)


def test_a_read_that_finished_isnt_lost_when_the_log_cant_be_written(household, monkeypatch):
    monkeypatch.setattr(mcp_server, "audit", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    client = household["alex_client"]
    result = _rpc(client, _token(client)["token"], "list_profiles")
    assert result["isError"] is False


def test_last_used_and_last_call_are_still_noted(household):
    client = household["alex_client"]
    created = _token(client, "Claude")
    _ok(_rpc(client, created["token"], "list_profiles"))
    row = next(t for t in client.get("/api/agents/tokens").json()["tokens"] if t["id"] == created["id"])
    assert row["last_used_at"] and row["last_call"] == "list_profiles" and row["last_call_at"]

