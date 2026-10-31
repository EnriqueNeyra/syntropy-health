"""The MCP wire: versions, headers, Origin, and what comes back for requests that aren't right. Older clients keep working."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app import mcp_server
from app.api import agents as agents_api
from app.core.db import read
from app.store import agents
from tests.conftest import BASE, LOCAL_CLIENT

MODERN = "2026-07-28"
META = {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {}}


@pytest.fixture
def api(client):
    token = client.post("/api/agents/tokens", json={"name": "Claude"}).json()["token"]
    anon = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    anon.headers["Authorization"] = f"Bearer {token}"
    anon.token_value = token
    return anon


def _msg(method, msg_id=1, **params):
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}


def _modern_headers(method, name=None):
    return {"MCP-Protocol-Version": MODERN, "Mcp-Method": method, **({"Mcp-Name": name} if name else {})}


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("asked, answered", [
    ("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"), ("2025-03-26", "2025-03-26"), ("2024-11-05", "2024-11-05"),
    ("2026-07-28", "2025-11-25"), ("1900-01-01", "2025-11-25"),
])
def test_the_handshake_settles_on_a_version(api, asked, answered):
    r = api.post("/mcp", json=_msg("initialize", protocolVersion=asked, capabilities={}, clientInfo={"name": "t", "version": "1"}))
    assert r.status_code == 200 and r.json()["result"]["protocolVersion"] == answered
    result = r.json()["result"]
    assert "ttlMs" not in result and "resultType" not in result and result["capabilities"]["tools"]


@pytest.mark.parametrize("version", ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"])
def test_older_clients_keep_working_after_the_handshake(api, version):
    h = {"MCP-Protocol-Version": version}
    assert api.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h).status_code == 202
    r = api.post("/mcp", json=_msg("tools/list"), headers=h)
    assert r.status_code == 200 and r.json()["result"]["tools"] and "ttlMs" not in r.json()["result"]
    r = api.post("/mcp", json=_msg("tools/call", name="list_profiles", arguments={}), headers=h)
    assert r.status_code == 200 and r.json()["result"]["isError"] is False


def test_a_client_that_sends_no_version_header_works(api):
    assert api.post("/mcp", json=_msg("tools/list")).status_code == 200


@pytest.mark.parametrize("bad", ["1900-01-01", "garbage", ""])
def test_an_unsupported_version_header_is_refused_with_what_is_supported(api, bad):
    r = api.post("/mcp", json=_msg("tools/list"), headers={"MCP-Protocol-Version": bad})
    assert r.status_code == 400
    error = r.json()["error"]
    assert error["code"] == -32022 and "2025-06-18" in error["data"]["supported"] and error["data"]["requested"] == bad


# ---------------------------------------------------------------------------
# 2026-07-28: no handshake, everything on each request
# ---------------------------------------------------------------------------

def test_discover(api):
    r = api.post("/mcp", json=_msg("server/discover", _meta=META), headers=_modern_headers("server/discover"))
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["resultType"] == "complete" and set(mcp_server.PROTOCOL_VERSIONS) == set(result["supportedVersions"])
    assert result["capabilities"]["tools"] is not None and result["instructions"]
    assert result["cacheScope"] == "private" and result["ttlMs"] >= 0     # the instructions name who this connection reads
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "syntropy-health"


def test_modern_tool_calls(api):
    tools = api.post("/mcp", json=_msg("tools/list", _meta=META), headers=_modern_headers("tools/list")).json()["result"]
    assert tools["resultType"] == "complete" and tools["ttlMs"] > 0 and tools["cacheScope"] == "public" and tools["tools"]
    r = api.post("/mcp", json=_msg("tools/call", name="list_profiles", arguments={}, _meta=META), headers=_modern_headers("tools/call", "list_profiles"))
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["resultType"] == "complete" and result["isError"] is False and result["structuredContent"]["people"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]


def test_a_modern_request_without_its_required_fields_is_refused(api):
    r = api.post("/mcp", json=_msg("tools/list", _meta={"io.modelcontextprotocol/protocolVersion": MODERN}), headers=_modern_headers("tools/list"))
    assert r.status_code == 400 and r.json()["error"]["code"] == -32602
    r = api.post("/mcp", json=_msg("server/discover"), headers=_modern_headers("server/discover"))
    assert r.status_code == 400 and r.json()["error"]["code"] in (-32602, -32020)


def test_an_unsupported_version_in_the_body_says_what_is_supported(api):
    meta = {**META, "io.modelcontextprotocol/protocolVersion": "2031-01-01"}
    r = api.post("/mcp", json=_msg("tools/list", _meta=meta), headers={**_modern_headers("tools/list"), "MCP-Protocol-Version": "2031-01-01"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32022 and MODERN in r.json()["error"]["data"]["supported"]


def test_an_unknown_method_is_a_404_in_the_modern_style_and_a_200_in_the_old(api):
    r = api.post("/mcp", json=_msg("nope/nothing", _meta=META), headers=_modern_headers("nope/nothing"))
    assert r.status_code == 404 and r.json()["error"]["code"] == -32601
    r = api.post("/mcp", json=_msg("nope/nothing"), headers={"MCP-Protocol-Version": "2025-06-18"})
    assert r.status_code == 200 and r.json()["error"]["code"] == -32601


@pytest.mark.parametrize("headers", [
    {},
    {"MCP-Protocol-Version": "2025-06-18", "Mcp-Method": "tools/call", "Mcp-Name": "list_profiles"},
    {"MCP-Protocol-Version": MODERN, "Mcp-Name": "list_profiles"},
    {"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/list", "Mcp-Name": "list_profiles"},
    {"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/call"},
    {"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/call", "Mcp-Name": "get_goals"},
    {"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/call", "Mcp-Name": "=?base64?notbase64!?="},
])
def test_headers_have_to_match_the_body(api, headers):
    r = api.post("/mcp", json=_msg("tools/call", name="list_profiles", arguments={}, _meta=META), headers=headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == -32020
    assert r.json()["id"] == 1


def test_a_base64_name_header_is_decoded(api):
    r = api.post("/mcp", json=_msg("tools/call", name="list_profiles", arguments={}, _meta=META),
                 headers={"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/call", "Mcp-Name": "=?base64?bGlzdF9wcm9maWxlcw==?="})
    assert r.status_code == 200 and r.json()["result"]["isError"] is False


# ---------------------------------------------------------------------------
# Origin, Host
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("origin", ["https://evil.example", "null", "file://", "http://evil.example:8000"])
def test_a_page_from_elsewhere_is_refused(api, origin):
    r = api.post("/mcp", json=_msg("tools/list"), headers={"Origin": origin})
    assert r.status_code == 403 and r.json()["error"]["code"] and "id" in r.json()


@pytest.mark.parametrize("origin", [BASE, "http://localhost:3000", "http://127.0.0.1:8000", "http://syntropyhealth.local:8000", "http://192.168.1.20:8000"])
def test_local_origins_pass(api, origin):
    assert api.post("/mcp", json=_msg("tools/list"), headers={"Origin": origin}).status_code == 200


def test_the_same_origin_passes_whatever_it_is_called(api):
    # This instance has a password, so the name itself is fine (the check below is for one without).
    r = api.post("/mcp", json=_msg("tools/list"), headers={"Origin": "https://health.example.org", "Host": "health.example.org"})
    assert r.status_code == 200


def test_an_allowed_host_passes(api, monkeypatch):
    monkeypatch.setenv("SYNTROPY_ALLOWED_HOSTS", "health.example.org")
    assert api.post("/mcp", json=_msg("tools/list"), headers={"Origin": "https://health.example.org"}).status_code == 200


def test_the_origin_check_comes_before_the_token(client):
    anon = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    assert anon.post("/mcp", json=_msg("tools/list"), headers={"Origin": "https://evil.example"}).status_code == 403


def test_without_a_password_mcp_only_answers_to_local_names(raw_client):
    assert raw_client.post("/api/setup", json={"profile_name": "Alex", "accept_terms": True}, headers={"X-Requested-With": "syntropy"}).status_code == 200
    raw_client.headers["X-Requested-With"] = "syntropy"
    token = raw_client.post("/api/agents/tokens", json={"name": "Claude"}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    assert raw_client.post("/mcp", json=_msg("tools/list"), headers={**h, "Host": "evil.example"}).status_code == 421
    assert raw_client.post("/mcp", json=_msg("tools/list"), headers=h).status_code == 200


# ---------------------------------------------------------------------------
# Requests that aren't right
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", ["5", '"hi"', "[]", "null", "true"])
def test_a_body_that_isnt_a_message_is_a_400(api, body):
    r = api.post("/mcp", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32600


def test_unparseable_bodies_are_a_400(api):
    for body in (b"{not json", b'{"a":"\xff"}', b"[" * 200_000):
        r = api.post("/mcp", content=body, headers={"Content-Type": "application/json"})
        assert r.status_code == 400 and r.json()["error"]["code"] == -32700


def test_a_clients_answer_is_accepted_and_ignored(api):
    assert api.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "result": {}}).status_code == 202
    assert api.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "error": {"code": 1, "message": "x"}}).status_code == 202


@pytest.mark.parametrize("message, code", [
    ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "x"}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 5}}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": ["x"]}}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": 5}, -32600),
    ({"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"}, -32600),
    ({"jsonrpc": "2.0", "id": None, "method": "ping"}, -32600),
    ({"jsonrpc": "2.0", "id": True, "method": "ping"}, -32600),
])
def test_malformed_requests_get_a_protocol_error(api, message, code):
    r = api.post("/mcp", json=message)
    assert r.status_code == 200 and r.json()["error"]["code"] == code


def test_a_bad_arguments_type_is_a_tool_error_the_model_can_read(api):
    r = api.post("/mcp", json=_msg("tools/call", name="get_goals", arguments=[1]))
    assert r.status_code == 200 and r.json()["result"]["isError"] is True


def test_internal_failures_dont_show_their_words(api, monkeypatch):
    monkeypatch.setattr(mcp_server, "_answer", lambda *_: (_ for _ in ()).throw(RuntimeError("secret path /home/x")))
    error = api.post("/mcp", json=_msg("tools/list")).json()["error"]
    assert error["code"] == -32603 and "secret" not in error["message"]


def test_batches_only_where_they_were_part_of_the_protocol(api):
    two = [_msg("tools/list"), _msg("ping", 2)]
    assert len(api.post("/mcp", json=two).json()) == 2
    assert len(api.post("/mcp", json=two, headers={"MCP-Protocol-Version": "2025-03-26"}).json()) == 2
    r = api.post("/mcp", json=two, headers={"MCP-Protocol-Version": "2025-06-18"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32600
    mixed = api.post("/mcp", json=[_msg("ping"), 5]).json()
    assert [m.get("error", {}).get("code") for m in mixed] == [None, -32600]
    assert api.post("/mcp", json=[_msg("ping")] * 51).status_code == 400


def test_a_body_that_is_too_long_is_refused(api):
    r = api.post("/mcp", json=_msg("tools/call", name="get_goals", arguments={"junk": "x" * 2_000_000}))
    assert r.status_code == 413
    with read() as conn:
        assert (conn.execute("SELECT max(length(detail)) FROM audit_log").fetchone()[0] or 0) < 1000


def test_unknown_arguments_dont_reach_the_log(api):
    assert api.post("/mcp", json=_msg("tools/call", name="get_goals", arguments={"junk": "x" * 500_000})).json()["result"]["isError"]
    with read() as conn:
        assert conn.execute("SELECT max(length(detail)) FROM audit_log").fetchone()[0] < 1000


def test_unknown_addresses_under_well_known_are_a_plain_404(client):
    for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        r = client.get(path)
        assert r.status_code == 404 and r.headers["content-type"].startswith("application/json"), path


def test_a_mistyped_token_is_told_where_to_get_one(client):
    r = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT).post("/mcp", json=_msg("tools/list"), headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and "Settings" in r.json()["detail"] and r.headers["www-authenticate"].startswith("Bearer")


# ---------------------------------------------------------------------------
# What's declared
# ---------------------------------------------------------------------------

def _check_against(schema, value, path="$"):
    """Enough of JSON Schema for what's declared here: a client validates structuredContent against outputSchema, and a
    wrong declaration would make it throw the result away."""
    expected = schema.get("type")
    kinds = {"object": dict, "array": list, "string": str, "boolean": bool, "number": (int, float), "integer": int}
    if expected:
        assert isinstance(value, kinds[expected]), f"{path} is {type(value).__name__}, declared {expected}"
    if expected == "object":
        for key in schema.get("required", []):
            assert key in value, f"{path}.{key} is missing"
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                _check_against(sub, value[key], f"{path}.{key}")


def test_tools_declare_titles_hints_and_output(client):
    tools = mcp_server._tool_list()
    assert len(tools) == len(mcp_server.TOOLS)
    for tool in tools:
        assert tool["title"] and tool["annotations"]["title"] == tool["title"]
        assert tool["annotations"]["readOnlyHint"] is True and tool["annotations"]["idempotentHint"] is True
        assert tool["annotations"]["destructiveHint"] is False and tool["annotations"]["openWorldHint"] is False
        assert tool["outputSchema"]["type"] == "object"


def test_results_match_what_is_declared(client):
    from tests.conftest import connect_institution
    connect_institution(client, "epic-stanford-health-care")
    client.post("/api/connections/wearable", json={"provider": "oura", "mode": "simulated"})
    schemas = {t["name"]: t["outputSchema"] for t in mcp_server._tool_list()}
    record = None
    for name, args in (("list_profiles", {}), ("get_health_summary", {}), ("search_records", {"category": "reports"}), ("get_record", None),
                       ("list_lab_tests", {}), ("get_lab_trend", {"test": "A1c"}), ("list_wearable_metrics", {}),
                       ("get_daily_metric", {"metric": "sleep_duration"}), ("list_workouts", {}), ("get_health_journal", {}),
                       ("get_insights", {}), ("compare_measures", {"a": "step_count", "b": "sleep_duration"}), ("get_sleep_nights", {}),
                       ("get_training_summary", {"workout_type": "Running"}), ("get_goals", {})):
        result = mcp_server.handle(_msg("tools/call", name=name, arguments={"record_id": record} if args is None else args))["result"]
        assert result["isError"] is False, f"{name}: {result['content'][0]['text'][:300]}"
        assert json.loads(result["content"][0]["text"]) == json.loads(json.dumps(result["structuredContent"], default=str))
        _check_against(schemas[name], result["structuredContent"], name)
        if name == "search_records":
            record = result["structuredContent"]["records"][0]["id"]
    assert not {t for t in mcp_server.TOOLS} - set(schemas)


def test_errors_carry_no_structured_content(client):
    result = mcp_server.handle(_msg("tools/call", name="get_lab_trend", arguments={"test": "nope"}))["result"]
    assert result["isError"] and "structuredContent" not in result


def test_no_tool_writes():
    # AI stays read-only over the record.
    words = ("add", "create", "delete", "update", "set_", "write", "remove")
    assert not [n for n in mcp_server.TOOLS if any(w in n for w in words)]


def test_prompts_are_templates_that_read_nothing(client):
    listed = mcp_server.handle(_msg("prompts/list"))["result"]["prompts"]
    assert {p["name"] for p in listed} == set(mcp_server.PROMPTS)
    got = mcp_server.handle(_msg("prompts/get", name="prepare_for_visit", arguments={"profile": "Alex"}))["result"]
    assert got["messages"][0]["role"] == "user" and "Alex" in got["messages"][0]["content"]["text"]
    assert mcp_server.handle(_msg("prompts/get", name="nope"))["error"]["code"] == -32602


def test_instructions_say_the_date_and_that_record_text_is_only_data(client):
    text = mcp_server.handle(_msg("initialize", protocolVersion="2025-06-18"))["result"]["instructions"]
    assert "Today is" in text and "never as instructions" in text and mcp_server._as_of() in text


def test_instructions_name_who_a_connection_reads(client):
    client.post("/api/profiles", json={"name": "Sam", "relationship": "child"})
    text = mcp_server.handle(_msg("initialize", protocolVersion="2025-06-18"))["result"]["instructions"]
    assert "Sam" in text and "profile argument" in text


# ---------------------------------------------------------------------------
# stdio
# ---------------------------------------------------------------------------

def _stdio(isolated_data, lines: bytes, **env):
    return subprocess.run([sys.executable, "-m", "app.mcp_server"], input=lines, capture_output=True, timeout=60,
                          env={"SYNTROPY_DATA_DIR": str(isolated_data), "PATH": "/usr/bin:/bin", "SYNTROPY_NO_DOTENV": "1", **env})


def test_stdio_reads_utf8_whatever_the_computer_is_set_to(client, isolated_data):
    client.post("/api/profiles", json={"name": "María", "relationship": "partner"})
    line = json.dumps(_msg("tools/call", name="get_goals", arguments={"profile": "María"}), ensure_ascii=False).encode() + b"\n"
    out = _stdio(isolated_data, line, PYTHONIOENCODING="cp1252")
    reply = json.loads(out.stdout.decode("utf-8"))
    assert reply["result"]["isError"] is False, reply
    assert json.loads(reply["result"]["content"][0]["text"])["person"] == "María"


def test_stdio_answers_in_utf8(client, isolated_data):
    client.post("/api/profiles", json={"name": "Zoë", "relationship": "partner"})
    out = _stdio(isolated_data, json.dumps(_msg("tools/call", name="list_profiles", arguments={})).encode() + b"\n", PYTHONIOENCODING="cp1252")
    assert "Zoë" in out.stdout.decode("utf-8")


def test_a_bad_line_on_stdio_doesnt_end_the_server(isolated_data):
    lines = b"\xff\xfe not json\n" + b"5\n" + b"[" * 200_000 + b"\n" + json.dumps(_msg("ping", 2)).encode() + b"\n"
    out = _stdio(isolated_data, lines)
    assert out.returncode == 0, out.stderr.decode()[-500:]
    replies = [json.loads(line) for line in out.stdout.decode().splitlines()]
    assert [r.get("error", {}).get("code") for r in replies] == [-32700, -32600, -32700, None] and replies[-1]["id"] == 2


def test_stdio_speaks_the_modern_style_too(isolated_data):
    out = _stdio(isolated_data, json.dumps(_msg("server/discover", _meta=META)).encode() + b"\n")
    assert json.loads(out.stdout)["result"]["supportedVersions"]


def test_stdio_probe_without_meta_falls_back_cleanly(isolated_data):
    out = _stdio(isolated_data, json.dumps(_msg("server/discover")).encode() + b"\n")
    assert json.loads(out.stdout)["error"]["code"] == -32602


def test_stdio_limited_by_name(client, isolated_data):
    client.post("/api/profiles", json={"name": "Sam", "relationship": "child"})
    out = _stdio(isolated_data, json.dumps(_msg("tools/call", name="list_profiles", arguments={})).encode() + b"\n", SYNTROPY_MCP_PROFILE="sam")
    people = json.loads(json.loads(out.stdout)["result"]["content"][0]["text"])["people"]
    assert [p["name"] for p in people] == ["Sam"]


def test_stdio_limited_to_no_one_reads_nothing_rather_than_everyone(client, isolated_data):
    out = _stdio(isolated_data, json.dumps(_msg("tools/call", name="get_goals", arguments={})).encode() + b"\n", SYNTROPY_MCP_PROFILE="nobody")
    assert json.loads(out.stdout)["result"]["isError"] is True


def test_the_offered_setup_reads_the_owners_own_records_unless_told_otherwise(client):
    from app.store import profiles
    alex = profiles.default_profile()["id"]
    sam = client.post("/api/profiles", json={"name": "Sam", "relationship": "child"}).json()["id"]
    assert client.get("/api/agents/local-command").json()["env"]["SYNTROPY_MCP_PROFILE"] == alex
    assert client.get("/api/agents/local-command", params={"profile_id": sam}).json()["env"]["SYNTROPY_MCP_PROFILE"] == sam
    assert "SYNTROPY_MCP_PROFILE" not in client.get("/api/agents/local-command", params={"everyone": True}).json()["env"]
    assert client.get("/api/agents/local-command", params={"profile_id": "prf_nobody"}).status_code == 404


def test_the_offered_setup_in_docker_says_who_too(client, monkeypatch):
    from pathlib import Path
    real = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self: str(self) == "/.dockerenv" or real(self))
    local = client.get("/api/agents/local-command").json()
    assert local["command"] == "docker" and local["args"][-3:] == ["python", "-m", "app.mcp_server"]
    assert any(a.startswith("SYNTROPY_MCP_PROFILE=") for a in local["args"])
    assert not any(a.startswith("SYNTROPY_MCP_PROFILE=") for a in client.get("/api/agents/local-command", params={"everyone": True}).json()["args"])


def test_the_cli_takes_a_person(client, isolated_data):
    client.post("/api/profiles", json={"name": "Sam", "relationship": "child"})
    line = json.dumps(_msg("tools/call", name="list_profiles", arguments={})).encode() + b"\n"
    out = subprocess.run([sys.executable, "-m", "app.cli", "mcp", "--profile", "Sam"], input=line, capture_output=True, timeout=60,
                         env={**os.environ, "SYNTROPY_DATA_DIR": str(isolated_data)})
    assert out.returncode == 0, out.stderr.decode()[-400:]
    assert [p["name"] for p in json.loads(json.loads(out.stdout)["result"]["content"][0]["text"])["people"]] == ["Sam"]


# ---------------------------------------------------------------------------
# Limits on a token
# ---------------------------------------------------------------------------

def test_tool_calls_are_limited_per_token_and_say_when_to_retry(api, client, monkeypatch):
    monkeypatch.setattr(agents, "CALLS_PER_MINUTE", 3)
    call = _msg("tools/call", name="list_profiles", arguments={})
    for _ in range(3):
        assert api.post("/mcp", json=call).json()["result"]["isError"] is False
    refused = api.post("/mcp", json=call)
    assert refused.status_code == 200 and refused.json()["result"]["isError"] is True
    body = json.loads(refused.json()["result"]["content"][0]["text"])
    assert body["code"] == "too_many_calls" and 1 <= body["retry_after_seconds"] <= 60
    # Other tokens aren't held up, and listing tools still works.
    other = client.post("/api/agents/tokens", json={"name": "Other"}).json()["token"]
    assert client.post("/mcp", json=call, headers={"Authorization": f"Bearer {other}"}).json()["result"]["isError"] is False
    assert api.post("/mcp", json=_msg("tools/list")).status_code == 200


def test_a_flood_leaves_one_log_row_not_thousands(api, monkeypatch):
    monkeypatch.setattr(agents, "CALLS_PER_MINUTE", 1)
    call = _msg("tools/call", name="list_profiles", arguments={})
    for _ in range(20):
        api.post("/mcp", json=call)
    with read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'mcp.%'").fetchone()[0] == 2


def test_a_batch_counts_each_call(api, monkeypatch):
    monkeypatch.setattr(agents, "CALLS_PER_MINUTE", 2)
    replies = api.post("/mcp", json=[_msg("tools/call", i, name="list_profiles", arguments={}) for i in range(1, 5)]).json()
    assert [r["result"]["isError"] for r in replies] == [False, False, True, True]


def test_the_limit_window_moves_on(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(agents, "_clock", lambda: now[0])
    monkeypatch.setattr(agents, "CALLS_PER_MINUTE", 2)
    assert agents.wait_before_call("t") == (0, False)
    assert agents.wait_before_call("t") == (0, False)
    wait, first = agents.wait_before_call("t")
    assert wait == 60 and first is True
    assert agents.wait_before_call("t")[1] is False
    now[0] += 61
    assert agents.wait_before_call("t") == (0, False)


@pytest.mark.parametrize("days", [30, 90, 365])
def test_a_token_can_expire(client, days):
    made = client.post("/api/agents/tokens", json={"name": "Short", "expires_days": days}).json()
    assert made["expires_at"] > made["created_at"]
    assert abs(made["expires_at"] - made["created_at"] - days * 86400) < 5
    listed = next(t for t in client.get("/api/agents/tokens").json()["tokens"] if t["id"] == made["id"])
    assert listed["expires_at"] == made["expires_at"]


def test_an_expired_token_stops_working(client):
    from app.core.db import db
    made = client.post("/api/agents/tokens", json={"name": "Short", "expires_days": 30}).json()
    h = {"Authorization": f"Bearer {made['token']}"}
    assert client.post("/mcp", json=_msg("tools/list"), headers=h).status_code == 200
    with db() as conn:
        conn.execute("UPDATE agent_tokens SET expires_at = 1 WHERE id = ?", (made["id"],))
    assert client.post("/mcp", json=_msg("tools/list"), headers=h).status_code == 401


def test_tokens_dont_expire_unless_asked(client):
    made = client.post("/api/agents/tokens", json={"name": "Long"}).json()
    assert made["expires_at"] is None


def test_only_the_offered_expiry_choices_are_accepted(client):
    assert client.post("/api/agents/tokens", json={"name": "x", "expires_days": 7}).status_code == 422


def test_the_body_limit_is_far_above_any_real_call():
    assert agents_api.MAX_BODY_BYTES >= 100_000


# ---------------------------------------------------------------------------
# Text that JSON can spell but isn't text
# ---------------------------------------------------------------------------

LONE = '"\\ud800"'


def _raw(method, msg_id="1", **params):
    return ('{"jsonrpc":"2.0","id":%s,"method":"%s","params":{%s}}' % (msg_id, method, ",".join(f'"{k}":{v}' for k, v in params.items()))).encode()


def test_a_lone_surrogate_doesnt_end_the_stdio_server(isolated_data):
    lines = [_raw("ping"), _raw("tools/call", "2", name=LONE), _raw("ping", LONE), _raw("prompts/get", "4", name=LONE),
             _raw("tools/call", "5", name='"search_records"', arguments='{"query":%s}' % LONE),
             _raw("ping", "6")]
    out = _stdio(isolated_data, b"\n".join(lines) + b"\n")
    assert out.returncode == 0, out.stderr.decode()[-500:]
    replies = [json.loads(line) for line in out.stdout.decode().splitlines()]
    assert len(replies) == 6 and replies[-1]["id"] == 6 and replies[-1]["result"] == {}
    assert replies[1]["error"]["code"] == -32602
    assert replies[4]["result"]["isError"] is False


@pytest.mark.parametrize("body", [
    _raw("tools/call", name=LONE),
    _raw("ping", LONE),
    _raw("prompts/get", name=LONE),
    _raw("tools/call", name='"get_daily_metric"', arguments='{"metric":%s}' % LONE),
    _raw("tools/call", name='"search_records"', arguments='{"since":%s}' % LONE),
    _raw("tools/call", name='"search_records"', arguments='{"query":%s}' % LONE),
    _raw("tools/call", name='"get_lab_trend"', arguments='{"test":%s}' % LONE),
    _raw("tools/call", name='"get_record"', arguments='{"record_id":%s}' % LONE),
    _raw("tools/call", name='"get_goals"', arguments='{"profile":%s}' % LONE),
    _raw("tools/call", name='"get_goals"', arguments='{%s:1}' % LONE),
    _raw(r"\ud800"),
])
def test_a_lone_surrogate_over_http_gets_a_proper_answer(api, body):
    r = api.post("/mcp", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    reply = r.json()
    assert "error" in reply or "result" in reply


def test_a_lone_surrogate_where_a_header_has_to_match_gets_a_proper_answer(api):
    body = _raw("tools/call", name=LONE, _meta=json.dumps(META))
    r = api.post("/mcp", content=body, headers=_modern_headers("tools/call", "x"))
    assert r.status_code == 400 and r.json()["error"]["code"] == -32020


def test_real_characters_outside_the_basic_plane_survive(api):
    body = _raw("tools/call", name='"get_goals"', arguments='{"profile":"\\ud83d\\ude00"}')
    r = api.post("/mcp", content=body, headers={"Content-Type": "application/json"})
    assert "\U0001F600" in json.loads(r.json()["result"]["content"][0]["text"])["message"]


@pytest.mark.parametrize("meta", ["x", 5, 0, "", [], True, ["a"]])
def test_discover_with_meta_that_isnt_an_object_is_refused_in_words(api, meta):
    r = api.post("/mcp", json=_msg("server/discover", _meta=meta), headers=_modern_headers("server/discover"))
    assert r.status_code == 400 and r.json()["error"]["code"] == -32602


@pytest.mark.parametrize("params", ["x", 5, ["a"]])
def test_discover_with_params_that_arent_an_object_is_refused_in_words(api, params):
    r = api.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": params}, headers=_modern_headers("server/discover"))
    assert r.status_code == 400 and r.json()["error"]["code"] in (-32602, -32020)


@pytest.mark.parametrize("name", [["a"], {"a": 1}, 5])
def test_a_prompt_name_that_isnt_text_is_refused_in_words(api, name):
    r = api.post("/mcp", json=_msg("prompts/get", name=name))
    assert r.status_code == 200 and r.json()["error"]["code"] == -32602
