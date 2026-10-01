"""The assistant (with the person's own API key), manual lab results, and MCP access for external agents."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.services import ai
from tests.conftest import BASE, connect_institution


@pytest.fixture
def fake_model(monkeypatch):
    """Answers like an OpenAI-compatible or Anthropic server; records every request it receives."""
    calls = []
    script: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        calls.append({"url": str(request.url), "body": body, "headers": dict(request.headers)})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "model-b"}, {"id": "model-a"}]})
        return httpx.Response(200, json=script.pop(0))

    monkeypatch.setattr(ai, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return calls, script


def _events(resp) -> list[dict]:
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def test_chat_with_openai_compatible_model_uses_tools(client, fake_model):
    calls, script = fake_model
    for provider in ("oura",):
        client.post("/api/connections/wearable", json={"provider": provider, "mode": "simulated"})
    r = client.put("/api/ai/config", json={"provider": "openrouter", "model": "qwen/qwen3-235b"})
    assert not r.json()["configured"]                               # a key is required
    r = client.put("/api/ai/config", json={"provider": "openrouter", "api_key": "sk-or-test"})
    assert r.json()["configured"] and r.json()["sends_elsewhere"] and not r.json()["cloud_ack"]
    # A provider outside the network: asked to confirm it sees the data, once, before the first question.
    first = _events(client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "How did I sleep?"}]}))
    assert first[0]["type"] == "error" and first[0]["code"] == "cloud_ack" and calls == []
    assert client.post("/api/ai/acknowledge").json() == {"ok": True}
    script.extend([
        {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
            "function": {"name": "get_daily_metric", "arguments": json.dumps({"metric": "sleep_duration", "days": 7})}}]}}]},
        {"choices": [{"message": {"role": "assistant", "content": "<think>hmm</think>You slept about **7 h** a night."}}]},
    ])
    resp = client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "How did I sleep this week?"}]})
    events = _events(resp)
    assert [e["type"] for e in events] == ["tool", "answer", "done"]
    assert "sleep duration" in events[0]["label"]
    assert events[1]["text"] == "You slept about **7 h** a night."
    first = calls[0]["body"]
    assert first["model"] == "qwen/qwen3-235b" and "Alex" in first["messages"][0]["content"]
    assert calls[0]["url"].startswith("https://openrouter.ai/") and calls[0]["headers"]["authorization"] == "Bearer sk-or-test"
    assert not any(t["function"]["name"] == "list_profiles" for t in first["tools"])
    assert all("profile" not in t["function"]["parameters"]["properties"] for t in first["tools"])
    tool_msg = calls[1]["body"]["messages"][-1]
    assert tool_msg["role"] == "tool" and json.loads(tool_msg["content"])["metric"] == "sleep_duration"
    log = client.get("/api/audit").json()
    assert any(e["action"] == "ai.chat" for e in log["events"])


def test_keys_and_addresses_are_required(client, fake_model):
    calls, _ = fake_model
    for needs_address in ("local", "custom"):
        r = client.put("/api/ai/config", json={"provider": needs_address, "model": "x"})
        assert r.status_code == 400 and "address" in r.json()["detail"]
    assert client.put("/api/ai/config", json={"provider": "ollama", "model": "x"}).status_code == 400
    assert client.post("/api/ai/models", json={"provider": "openai"}).status_code == 400     # no key yet
    # The form's unsaved key is used to list models, and listing saves nothing.
    assert [m["id"] for m in client.post("/api/ai/models", json={"provider": "openai", "api_key": "sk-new"}).json()["models"]] == []
    assert calls[-1]["headers"]["authorization"] == "Bearer sk-new"
    assert not client.get("/api/ai/config").json()["configured"]


def test_connection_test_checks_tool_use(client, fake_model):
    calls, script = fake_model
    client.put("/api/ai/config", json={"provider": "openai", "model": "gpt-test", "api_key": "sk-test", "acknowledged": True})
    script.append({"choices": [{"message": {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                   "function": {"name": "get_health_summary", "arguments": "{}"}}]}}]})
    r = client.post("/api/ai/test").json()
    assert r["tools"] is True and client.get("/api/ai/config").json()["tools"] is True
    assert {t["function"]["name"] for t in calls[0]["body"]["tools"]} >= {"get_health_summary", "get_lab_trend"}


def test_chat_with_anthropic_and_key_is_never_returned(client, fake_model):
    calls, script = fake_model
    client.put("/api/ai/config", json={"provider": "anthropic", "api_key": "sk-ant-secret", "acknowledged": True})
    cfg = client.get("/api/ai/config").json()
    assert cfg["configured"] and cfg["model"] == "claude-sonnet-5" and "sk-ant-secret" not in json.dumps(cfg)
    script.extend([
        {"content": [{"type": "tool_use", "id": "t1", "name": "get_health_summary", "input": {}}], "stop_reason": "tool_use"},
        {"content": [{"type": "text", "text": "Here is your summary."}], "stop_reason": "end_turn"},
    ])
    events = _events(client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "Summarize"}]}))
    assert events[-2] == {"type": "answer", "text": "Here is your summary."}
    assert calls[0]["headers"]["x-api-key"] == "sk-ant-secret"
    result = calls[1]["body"]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1" and not result["is_error"]
    assert [m["id"] for m in client.post("/api/ai/models", json={}).json()["models"]] == ["model-a", "model-b"]


def test_chat_reports_provider_errors(client, monkeypatch):
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "invalid x-api-key"}})
    monkeypatch.setattr(ai, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    client.put("/api/ai/config", json={"provider": "anthropic", "api_key": "bad", "acknowledged": True})
    events = _events(client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "Hi"}]}))
    assert events[0]["type"] == "error" and "rejected the API key" in events[0]["message"]


def test_manual_lab_results(client):
    r = client.post("/api/labs", json={"collected": "2026-09-01", "lab": "Quest Diagnostics", "results": [
        {"test": "Hemoglobin A1c", "loinc": "4548-4", "value": 5.4, "unit": "%", "ref_high": 5.6},
        {"test": "LDL cholesterol (calculated)", "loinc": "13457-7", "value": 162, "unit": "mg/dL", "ref_high": 99, "flag": "H"},
        {"test": "Hepatitis C antibody", "value_text": "Non-reactive"}]})
    assert r.status_code == 200, r.text
    assert r.json()["records"] == 3
    labs = {i["title"]: i for i in client.get("/api/records", params={"category": "labs"}).json()["items"]}
    assert labs["LDL cholesterol (calculated)"]["interpretation"] == "high"
    assert labs["Hemoglobin A1c"]["effective_at"].startswith("2026-09-01")
    assert any(l["title"].startswith("LDL") for l in client.get("/api/summary").json()["flagged_labs"])
    trend = client.get("/api/observations/series", params={"code": "4548-4"}).json()
    assert trend["points"][0]["v"] == 5.4
    assert client.post("/api/labs", json={"collected": "2026-09-01", "results": [{"test": "Nothing"}]}).status_code == 400
    rid = labs["Hemoglobin A1c"]["id"]
    assert client.delete(f"/api/labs/{rid}").status_code == 200
    ehr = connect_institution(client, "epic-stanford-health-care")
    portal = client.get("/api/records", params={"category": "labs", "connection": ehr["connection_id"]}).json()["items"][0]
    assert client.delete(f"/api/labs/{portal['id']}").status_code == 404   # portal records aren't editable
    assert any(c["name"] == "Hemoglobin A1c" for c in client.get("/api/labs/catalog").json()["common"])


def test_read_lab_report_with_model(client, fake_model):
    calls, script = fake_model
    client.put("/api/ai/config", json={"provider": "openai", "model": "gpt-test", "api_key": "sk-test", "acknowledged": True})
    script.append({"choices": [{"message": {"content": '```json\n{"collected": "2026-08-30", "lab": "LabCorp", "results": '
                                           '[{"test": "Ferritin", "value": 48, "unit": "ng/mL", "ref_low": 30, "ref_high": 400}]}\n```'}}]})
    files = {"file": ("report.jpg", b"\xff\xd8\xff fake jpeg", "image/jpeg")}
    out = client.post("/api/labs/extract", files=files, data={"method": "ai"}).json()
    assert out["results"][0]["loinc"] == "2276-4" and out["read_as"] == "image"
    part = calls[0]["body"]["messages"][1]["content"][0]
    assert part["type"] == "image_url" and part["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert client.post("/api/labs/extract", files={"file": ("a.txt", b"hi", "text/plain")}).status_code == 400


def test_mcp_over_http_with_scoped_tokens(client):
    connect_institution(client, "epic-stanford-health-care")
    other = client.post("/api/profiles", json={"name": "Sam", "relationship": "child"}).json()
    anon = TestClient(client.app, base_url=BASE)
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    assert anon.post("/mcp", json=init).status_code == 401

    full = client.post("/api/agents/tokens", json={"name": "Claude Desktop"}).json()["token"]
    sam_only = client.post("/api/agents/tokens", json={"name": "Coach bot", "profile_id": other["id"]}).json()
    auth = {"Authorization": f"Bearer {full}"}
    r = anon.post("/mcp", json=init, headers=auth)
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "syntropy-health"
    assert anon.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=auth).status_code == 202
    tools = anon.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=auth).json()["result"]["tools"]
    assert {"get_health_summary", "get_lab_trend", "list_workouts"} <= {t["name"] for t in tools}
    call = lambda name, args, h: anon.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                        "params": {"name": name, "arguments": args}}, headers=h).json()["result"]
    summary = json.loads(call("get_health_summary", {}, auth)["content"][0]["text"])
    assert summary["profile"]["name"] == "Alex" and summary["active_conditions"]
    record_id = json.loads(call("search_records", {"category": "conditions"}, auth)["content"][0]["text"])["records"][0]["id"]

    # A token limited to Sam can't see Alex, even when asking by name or by record id.
    scoped = {"Authorization": f"Bearer {sam_only['token']}"}
    assert [p["name"] for p in json.loads(call("list_profiles", {}, scoped)["content"][0]["text"])] == ["Sam"]
    assert call("get_health_summary", {"profile": "Alex"}, scoped)["isError"]
    assert json.loads(call("get_health_summary", {}, scoped)["content"][0]["text"])["profile"]["name"] == "Sam"
    assert call("get_record", {"record_id": record_id}, scoped)["isError"]
    assert any(e["actor"] == "mcp:Coach bot" for e in client.get("/api/audit").json()["events"])

    assert client.delete(f"/api/agents/tokens/{sam_only['id']}").status_code == 200
    assert anon.post("/mcp", json=init, headers=scoped).status_code == 401
    assert [t["name"] for t in client.get("/api/agents/tokens").json()["tokens"]] == ["Claude Desktop"]
    assert anon.post(f"/mcp?token={full}", json=init).status_code == 401   # tokens in URLs end up in logs
    unknown = anon.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "drop_tables"}},
                        headers=auth).json()
    assert unknown["error"]["code"] == -32602
    assert call("get_lab_trend", {}, auth)["isError"]   # missing required argument
    local = client.get("/api/agents/local-command").json()
    assert local["args"][-2:] == ["-m", "app.mcp_server"]


def test_lab_lookup_understands_common_abbreviations(client):
    client.post("/api/labs", json={"collected": "2026-09-01", "results": [
        {"test": "Hemoglobin A1C", "loinc": "4548-4", "value": 5.1, "unit": "%"},
        {"test": "Low Density Lipoprotein Cholesterol", "loinc": "18262-6", "value": 85, "unit": "mg/dL", "ref_high": 99}]})
    from app import mcp_server
    trend = lambda test: json.loads(mcp_server.call_tool("get_lab_trend", {"test": test})[0])
    a1c = trend("HbA1c")
    assert a1c["code"] == "4548-4" and a1c["results"][0]["value"] == 5.1
    assert a1c["reference_range"] == mcp_server.NO_RANGE      # said outright, so models don't invent one
    assert trend("LDL")["code"] == "18262-6" and trend("ldl-c")["reference_range"] == "–99 mg/dL"
    text, failed = mcp_server.call_tool("get_lab_trend", {"test": "TSH"})
    assert failed and "list_lab_tests" in text


def test_model_lists_keep_chat_models_and_suggest_one(client, monkeypatch):
    listed = {"data": [
        {"id": "gpt-5.1", "created": 300}, {"id": "gpt-5.1-mini", "created": 310}, {"id": "text-embedding-3-large", "created": 400},
        {"id": "whisper-1", "created": 100}, {"id": "gpt-image-1", "created": 500}, {"id": "o4-mini", "created": 200},
        {"id": "tts-1-hd", "created": 50}, {"id": "babbage-002", "created": 10}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=listed)
    monkeypatch.setattr(ai, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    r = client.post("/api/ai/models", json={"provider": "openai", "api_key": "sk-x"}).json()
    assert [m["id"] for m in r["models"]] == ["gpt-5.1-mini", "gpt-5.1", "o4-mini"]      # newest first, chat models only
    assert r["suggested"] == "gpt-5.1"
    listed["data"] = [{"id": "claude-haiku-9", "display_name": "Claude Haiku 9", "created_at": "2026-05-01T00:00:00Z"},
                      {"id": "claude-sonnet-9", "display_name": "Claude Sonnet 9", "created_at": "2026-06-01T00:00:00Z"}]
    r = client.post("/api/ai/models", json={"provider": "anthropic", "api_key": "sk-ant"}).json()
    assert r["models"][0]["name"] == "Claude Sonnet 9" and r["suggested"] == "claude-sonnet-9"


def test_several_providers_connected_at_once(client, fake_model):
    """Connecting another provider keeps the default unless asked; Ask's menu offers every model of each one; and
    disconnecting the default hands over to another connected provider."""
    client.put("/api/ai/config", json={"provider": "groq", "api_key": "gsk-1", "model": "llama-70b", "make_default": False})
    assert client.get("/api/ai/config").json()["provider"] == "groq"           # the first one is the default anyway
    client.post("/api/ai/models", json={"provider": "groq"})                     # lists model-a, model-b (cached)
    client.put("/api/ai/config", json={"provider": "mistral", "api_key": "ms-1", "model": "mistral-large-latest",
                                       "make_default": False})
    cfg = client.get("/api/ai/config").json()
    assert cfg["provider"] == "groq" and cfg["base_url"] == "https://api.groq.com/openai/v1"
    connected = {p["id"]: p for p in cfg["providers"] if p["connected"]}
    assert set(connected) == {"groq", "mistral"} and connected["groq"]["models"] == 2
    groups = {g["id"]: g for g in client.get("/api/ai/choices").json()["groups"]}
    assert [o["model"] for o in groups["groq"]["options"]] == ["llama-70b", "model-a", "model-b"]
    assert groups["groq"]["options"][0]["pinned"] and not groups["groq"]["options"][1]["pinned"]
    assert client.delete("/api/ai/providers/groq").json()["provider"] == "mistral"
    assert not client.get("/api/ai/config").json()["providers"][[p["id"] for p in cfg["providers"]].index("groq")]["has_key"]
    assert client.delete("/api/ai/providers/nope").status_code == 404
