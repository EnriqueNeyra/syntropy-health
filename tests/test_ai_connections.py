"""Local models, other OpenAI-compatible services, models without tools, agents on this computer and MCP last calls."""

import json
import stat
import sys
import textwrap

import httpx
import pytest

from app.services import ai, ai_agents, ai_local
from tests.test_assistant import _events, fake_model  # noqa: F401 - fixture


def test_addresses_are_tidied_and_classified():
    assert ai_local.normalize_base_url("studio.local:11434") == "http://studio.local:11434/v1"
    assert ai_local.normalize_base_url("http://192.168.1.20:1234/v1/chat/completions") == "http://192.168.1.20:1234/v1"
    assert ai_local.normalize_base_url("https://api.groq.com/openai/v1/") == "https://api.groq.com/openai/v1"
    with pytest.raises(ValueError):
        ai_local.normalize_base_url("")
    for private in ("localhost", "127.0.0.1", "192.168.1.20", "10.0.0.5", "100.101.102.103", "studio.local", "nas",
                    "host.docker.internal", "my-mac.tail1234.ts.net"):
        assert ai_local.is_private_host(private), private
    assert not ai_local.is_private_host("8.8.8.8")
    assert ai_local.describe_address("http://8.8.8.8:11434/v1")["needs_confirmation"]
    assert not ai_local.describe_address("https://8.8.8.8/v1")["needs_confirmation"]


def test_local_model_without_a_key(client, fake_model):
    calls, script = fake_model
    r = client.put("/api/ai/config", json={"provider": "local", "base_url": "localhost:11434", "model": "qwen3:8b",
                                           "label": "Ollama"}).json()
    assert r["configured"] and r["label"] == "Ollama" and r["base_url"] == "http://localhost:11434/v1"
    assert r["kind"] == "local" and r["address"]["private"]
    script.append({"choices": [{"message": {"content": "Hello from a local model."}}]})
    client.put("/api/ai/config", json={"provider": "local", "model": "qwen3:8b"})    # address kept
    events = _events(client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "Hi"}]}))
    assert events[-2]["text"] == "Hello from a local model."
    assert calls[-1]["url"] == "http://localhost:11434/v1/chat/completions" and "authorization" not in calls[-1]["headers"]
    assert [m["id"] for m in client.post("/api/ai/models", json={"provider": "local"}).json()["models"]] == ["model-a", "model-b"]


def test_plain_http_to_the_internet_needs_confirmation(client):
    body = {"provider": "custom", "base_url": "http://8.8.8.8:8000/v1", "model": "m"}
    r = client.put("/api/ai/config", json=body)
    assert r.status_code == 400 and "unencrypted" in r.json()["detail"]
    assert client.put("/api/ai/config", json={**body, "allow_public_http": True}).status_code == 200
    r = client.put("/api/ai/config", json={"provider": "custom", "base_url": "https://api.groq.com/openai/v1", "model": "m",
                                           "label": "Groq", "api_key": "gsk-test"}).json()
    assert r["configured"] and r["label"] == "Groq" and not r["address"]["private"]


def test_model_without_tools_answers_from_a_snapshot(client, monkeypatch):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if body.get("tools"):
            return httpx.Response(400, json={"error": {"message": "gemma3:4b does not support tools"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Your recent labs look fine."}}]})

    monkeypatch.setattr(ai, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    client.post("/api/connections/wearable", json={"provider": "oura", "mode": "simulated"})
    client.put("/api/ai/config", json={"provider": "custom", "base_url": "http://192.168.1.9:1234", "model": "gemma3:4b"})
    events = _events(client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "How are my labs?"}]}))
    assert [e["type"] for e in events] == ["note", "tool", "answer", "done"]
    assert events[-1]["mode"] == "snapshot" and events[2]["text"] == "Your recent labs look fine."
    system = requests[-1]["messages"][0]["content"]
    assert "Health data snapshot" in system and "wearables_latest_and_7_30_day_averages" in system and "tools" not in requests[-1]
    assert client.get("/api/ai/config").json()["tools"] is False
    requests.clear()
    _events(client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "And sleep?"}]}))
    assert len(requests) == 1        # remembered: straight to the snapshot


def test_snapshot_fits_the_budget(client):
    client.post("/api/connections/wearable", json={"provider": "oura", "mode": "simulated"})
    profile = client.get("/api/profiles").json()["profiles"][0]["id"]
    big = ai.summary_context(profile, 80_000)
    small = ai.summary_context(profile, 1500)
    assert len(small) <= 1600 and len(small) < len(big) and small.startswith("person:")
    assert "not_included_for_space" in small


# ---------------------------------------------------------------------------
# Agents on this computer, with stand-ins for the real tools
# ---------------------------------------------------------------------------

FAKE_CLAUDE = """
import json, subprocess, sys, os
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("2.9.0 (Claude Code)"); sys.exit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True})); sys.exit(0)
if args[:1] == ["--help"]:
    print("--tools --permission-mode dontAsk --no-session-persistence"); sys.exit(0)
prompt = sys.stdin.read()
json.dump({"args": args, "prompt": prompt, "cwd": os.getcwd(), "files": sorted(os.listdir("."))}, open(os.environ["FAKE_LOG"], "w"))
config = json.load(open(args[args.index("--mcp-config") + 1]))["mcpServers"]["syntropy"]
# Talk to Syntropy's MCP server the way the real tool would, and ask for another person's records.
server = subprocess.Popen([config["command"], *config["args"]], env={**os.environ, **config["env"]},
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
out, _ = server.communicate(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
    "name": "list_profiles", "arguments": {}}}) + "\\n", timeout=60)
people = json.loads(json.loads(out)["result"]["content"][0]["text"])
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__syntropy__list_profiles", "input": {}}]}}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "People: " + ", ".join(p["name"] for p in people)}))
"""

FAKE_CODEX = """
import json, sys, os
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli 0.130.0"); sys.exit(0)
if args[:2] == ["login", "status"]:
    print("Not logged in"); sys.exit(1)
if "--help" in args:
    print("--ephemeral --ignore-user-config"); sys.exit(0)
json.dump({"args": args, "prompt": sys.stdin.read()}, open(os.environ["FAKE_LOG"], "w"))
print(json.dumps({"type": "thread.started", "thread_id": "t"}))
print(json.dumps({"type": "turn.failed", "error": {"message": "401 Unauthorized: please log in"}}))
"""


@pytest.fixture
def fake_tools(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\n{textwrap.dedent(body)}")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(ai_agents, "_search_dirs", lambda: [str(bin_dir)])
    monkeypatch.setattr(ai_agents, "_detected", (0.0, []))
    log = tmp_path / "fake.json"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("WHOOP_CLIENT_SECRET", "must-not-leak")
    return log


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in tools are shell scripts")
def test_agents_are_detected(client, fake_tools):
    agents = {a["id"]: a for a in client.get("/api/ai/agents").json()["agents"]}
    assert agents["claude"]["installed"] and agents["claude"]["signed_in"] is True and agents["claude"]["version"] == "2.9.0"
    assert agents["codex"]["installed"] and agents["codex"]["signed_in"] is False
    assert not agents["gemini"]["installed"] and agents["gemini"]["optional"]


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in tools are shell scripts")
def test_claude_code_answers_with_only_syntropy_tools(client, fake_tools):
    client.post("/api/profiles", json={"name": "Sam", "relationship": "child"})
    assert client.put("/api/ai/config", json={"provider": "agent", "agent": "claude", "acknowledged": True}).json()["configured"]
    cfg = client.get("/api/ai/config").json()
    assert cfg["kind"] == "agent" and cfg["label"] == "Claude Code"
    events = _events(client.post("/api/ai/chat", json={"messages": [
        {"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello!"}, {"role": "user", "content": "Who's here?"}]}))
    assert [e["type"] for e in events] == ["tool", "answer", "done"]
    assert events[1]["text"] == "People: Alex"          # the MCP server was limited to the person being viewed
    assert events[-1]["mode"] == "agent"
    run = json.loads(fake_tools.read_text())
    args = run["args"]
    assert args[args.index("--tools") + 1] == "" and args[args.index("--allowedTools") + 1] == "mcp__syntropy"
    assert "--strict-mcp-config" in args and args[args.index("--permission-mode") + 1] == "dontAsk"
    assert "Alex" in args[args.index("--system-prompt") + 1]
    assert "Who's here?" in run["prompt"] and "Hello!" in run["prompt"]
    assert run["files"] == ["mcp.json"] and "syntropy-agent-" in run["cwd"]
    config = json.loads(json.dumps(args))
    assert "must-not-leak" not in json.dumps(config)
    log = client.get("/api/audit").json()["events"]
    assert any(e["actor"] == "agent:claude" and e["action"] == "mcp.list_profiles" for e in log)
    assert any(e["action"] == "ai.chat" and "agent:claude" in (e["detail"] or "") for e in log)


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in tools are shell scripts")
def test_codex_runs_read_only_and_explains_sign_in(client, fake_tools):
    client.put("/api/ai/config", json={"provider": "agent", "agent": "codex", "model": "gpt-5-codex", "acknowledged": True})
    events = _events(client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "Hi"}]}))
    assert events[0]["type"] == "error" and "isn't signed in" in events[0]["message"] and "codex login" in events[0]["message"]
    args = json.loads(fake_tools.read_text())["args"]
    assert args[:2] == ["exec", "--json"] and args[args.index("--sandbox") + 1] == "read-only" and args[-1] == "-"
    overrides = [args[i + 1] for i, a in enumerate(args) if a == "-c"]
    assert "features.shell_tool=false" in overrides and 'web_search="disabled"' in overrides
    assert 'mcp_servers.syntropy.default_tools_approval_mode="approve"' in overrides
    assert any(o.startswith("mcp_servers.syntropy.env={") and "SYNTROPY_MCP_PROFILE" in o for o in overrides)
    assert "--ignore-user-config" in args and args[args.index("--model") + 1] == "gpt-5-codex"


def test_agents_need_a_known_agent(client):
    assert client.put("/api/ai/config", json={"provider": "agent", "agent": "hal"}).status_code == 400


def test_mcp_tokens_remember_their_last_call(client):
    token = client.post("/api/agents/tokens", json={"name": "Cursor"}).json()
    headers = {"Authorization": f"Bearer {token['token']}"}
    client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "get_health_summary", "arguments": {}}}, headers=headers)
    [listed] = client.get("/api/agents/tokens").json()["tokens"]
    assert listed["last_call"] == "get_health_summary" and listed["last_call_at"]
