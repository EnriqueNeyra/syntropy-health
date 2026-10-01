"""MCP server: protocol handshake and tools against real synced data."""

import json
import subprocess
import sys

from app import mcp_server
from tests.conftest import connect_institution


def call(name, **arguments):
    resp = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    result = resp["result"]
    return json.loads(result["content"][0]["text"]) if not result["isError"] else result


def test_handshake_and_tool_list():
    init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["capabilities"]["tools"] is not None
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert {"get_health_summary", "search_records", "get_lab_trend", "get_daily_metric"} <= {t["name"] for t in tools}
    assert mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"] == -32601


def test_tools_answer_questions(client):
    connect_institution(client, "epic-stanford-health-care")
    client.post("/api/connections/wearable", json={"provider": "oura", "mode": "simulated"})
    summary = call("get_health_summary")
    assert any(c["title"] == "Essential hypertension" for c in summary["active_conditions"])
    assert summary["wearable_headline_30d"]
    trend = call("get_lab_trend", test="A1c")
    assert trend["code"] == "4548-4" and len(trend["results"]) >= 3
    found = call("search_records", query="lisinopril")
    assert found["total_matches"] >= 1
    sleep = call("get_daily_metric", metric="sleep_duration", days=14)
    assert len(sleep["days"]) >= 13
    err = call("get_lab_trend", test="unobtainium")
    assert err["isError"] is True
    audit = client.get("/api/audit").json()["events"]
    assert any(e["action"] == "mcp.get_lab_trend" for e in audit)


def test_stdio_transport(isolated_data):
    proc = subprocess.run(
        [sys.executable, "-m", "app.mcp_server"],
        input=json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}) + "\n",
        capture_output=True, text=True, timeout=60,
        env={"SYNTROPY_DATA_DIR": str(isolated_data), "PATH": "/usr/bin:/bin"},
    )
    out = json.loads(proc.stdout.strip())
    assert out["id"] == 7 and out["result"]["tools"]
