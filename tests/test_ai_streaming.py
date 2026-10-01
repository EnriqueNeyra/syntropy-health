"""Ask streams answers as they're written: Server-sent events from Anthropic and OpenAI-compatible servers, and the
partial messages of Claude Code."""

import json

import httpx

from app.services import ai, ai_agents
from tests.test_assistant import _events


def _sse(payloads) -> httpx.Response:
    body = "".join(f"data: {json.dumps(p) if not isinstance(p, str) else p}\n\n" for p in payloads)
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def _stream_with(monkeypatch, rounds):
    """A server answering each request with the next list of SSE payloads; returns the request bodies it received."""
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content or b"{}"))
        return _sse(rounds.pop(0))

    monkeypatch.setattr(ai, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return bodies


def _ask(client, question="How did I sleep?"):
    client.post("/api/ai/acknowledge")
    return _events(client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": question}]}))


def test_openai_compatible_answer_streams_and_tool_calls_are_assembled(client, monkeypatch):
    client.put("/api/ai/config", json={"provider": "openrouter", "api_key": "sk-or-test", "model": "some/model"})
    args = json.dumps({"metric": "sleep_duration", "days": 7})
    bodies = _stream_with(monkeypatch, [
        [{"choices": [{"delta": {"content": "Let me check."}}]},
         {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                                 "function": {"name": "get_daily_metric", "arguments": args[:10]}}]}}]},
         {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[10:]}}]}}]},
         "[DONE]"],
        [{"choices": [{"delta": {"content": "<think>hm"}}]}, {"choices": [{"delta": {"content": "m</think>You slept "}}]},
         {"choices": [{"delta": {"content": "about **7 h**."}}]}, "[DONE]"],
    ])
    events = _ask(client)
    kinds = [e["type"] for e in events]
    assert kinds == ["delta", "reset", "tool", "delta", "delta", "answer", "done"], kinds
    assert "".join(e["text"] for e in events[3:5]) == "You slept about **7 h**."
    assert events[5]["text"] == "You slept about **7 h**."
    assert all(b["stream"] is True for b in bodies)
    # The tool call went back to the model whole, with its arguments put together from the fragments.
    sent = bodies[1]["messages"][-2]
    assert sent["tool_calls"][0]["function"] == {"name": "get_daily_metric", "arguments": args}


def test_anthropic_answer_streams(client, monkeypatch):
    client.put("/api/ai/config", json={"provider": "anthropic", "api_key": "sk-ant-test", "model": "claude-test"})
    _stream_with(monkeypatch, [[
        {"type": "message_start", "message": {"content": []}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "list_workouts", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"days":'}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": " 30}"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_stop"},
    ], [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Five runs "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "this month."}},
        {"type": "content_block_stop", "index": 0},
    ]])
    events = _ask(client, "How much did I run?")
    assert [e["type"] for e in events] == ["tool", "delta", "delta", "answer", "done"]
    assert events[0]["label"].startswith("Reviewing workouts")
    assert events[3]["text"] == "Five runs this month."


def test_stream_errors_read_like_other_errors(client, monkeypatch):
    client.put("/api/ai/config", json={"provider": "openrouter", "api_key": "sk-or-test", "model": "some/model"})
    monkeypatch.setattr(ai, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error": {"message": "bad key"}}))))
    events = _ask(client)
    assert events[0]["type"] == "error" and "rejected the API key" in events[0]["message"]


def test_a_server_that_cannot_stream_is_asked_again_without(client, monkeypatch):
    client.put("/api/ai/config", json={"provider": "openrouter", "api_key": "sk-or-test", "model": "some/model"})
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body.get("stream"))
        if body.get("stream"):
            return httpx.Response(400, json={"error": {"message": "streaming is not supported with tools"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Fine."}}]})
    monkeypatch.setattr(ai, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    events = _ask(client)
    assert [e["type"] for e in events] == ["answer", "done"] and events[0]["text"] == "Fine."
    assert seen == [True, None]


def test_visible_text_hides_reasoning_as_it_arrives():
    v = ai._Visible()
    assert v.add("Hello <thi") == "Hello "
    assert v.add("nk>secret") == ""
    assert v.add("</think> world") == " world"


def test_claude_code_partial_messages_become_deltas():
    state: dict = {}
    parse = lambda ev: ai_agents._parse("claude", ev, state)
    stream = lambda e: {"type": "stream_event", "event": e, "parent_tool_use_id": None}
    assert parse(stream({"type": "message_start", "message": {}})) == []
    assert parse(stream({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Let me look."}})) \
        == [{"type": "delta", "text": "Let me look."}]
    # A second message (after a lookup) starts the shown text again.
    assert parse(stream({"type": "message_start", "message": {}})) == [{"type": "reset"}]
    assert parse(stream({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "You slept well."}})) \
        == [{"type": "delta", "text": "You slept well."}]
    # A subagent's text is never shown.
    assert parse({**stream({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "x"}}),
                  "parent_tool_use_id": "tu_1"}) == []
