"""
Model Context Protocol (MCP) server for Syntropy Health.

Lets an AI assistant you choose (Claude Desktop, Claude Code, or any MCP client) answer questions about your records.
The tools are read-only and every call is recorded in the activity log. Nothing is sent anywhere by this server; what
the assistant does with results is governed by that assistant.

Two ways in:
- Over HTTP at ``/mcp`` with an agent token (Settings → AI); see ``app/api/agents.py``.
- Over stdio on the machine that holds the data, with access to everyone's records:
      docker exec -i syntropy-health python -m app.mcp_server
  or, without Docker, ``python -m app.mcp_server`` with SYNTROPY_DATA_DIR pointing at the data directory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from contextvars import ContextVar
from typing import Any, Callable, Optional

from app.core import config
from app.core.db import audit, db
from app.services import insights
from app.store import activity, biometrics, connections, goals, lab_catalog, profiles, records, sleep, training

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

SLIM_FIELDS = ("id", "category", "title", "code", "code_system", "effective_at", "effective_end", "status", "value_text",
               "value_num", "unit", "value_norm", "unit_norm", "ref_low", "ref_high", "ref_text", "interpretation",
               "narrative", "source_name")


def _slim(rec: dict[str, Any], narrative_chars: int = 600) -> dict[str, Any]:
    out = {k: rec.get(k) for k in SLIM_FIELDS if rec.get(k) not in (None, "", [])}
    if out.get("narrative") and len(out["narrative"]) > narrative_chars:
        out["narrative"] = out["narrative"][:narrative_chars] + "…"
    details = {k: v for k, v in (rec.get("details") or {}).items() if v not in (None, "", [], {}) and k not in ("attachments", "result_refs", "full_text")}
    if details:
        out["details"] = details
    if len(rec.get("sources") or []) > 1:
        out["reported_by"] = [s.get("source_name") for s in rec["sources"]]
    return out


# Set by the HTTP endpoint (for agent tokens limited to one person) and by the built-in assistant: every tool then
# reads only that person's data, whatever profile the model asks for.
SCOPED_PROFILE: ContextVar[Optional[str]] = ContextVar("scoped_profile", default=None)
# Set for agent tokens: the people the account that made the token can see (None: everyone, for the stdio server run on
# the computer holding the data).
ALLOWED_PROFILES: ContextVar[Optional[frozenset[str]]] = ContextVar("allowed_profiles", default=None)
# Who is calling, for the activity log ("mcp", or "mcp:<token name>").
ACTOR: ContextVar[str] = ContextVar("actor", default="mcp")


def _people() -> list[dict[str, Any]]:
    allowed = ALLOWED_PROFILES.get()
    return [p for p in profiles.list_profiles() if allowed is None or p["id"] in allowed]


def _profile(args: dict[str, Any]) -> dict[str, Any]:
    scoped = SCOPED_PROFILE.get()
    if scoped:
        prof = next((p for p in _people() if p["id"] == scoped), None)
        if prof is None:
            raise ValueError("This profile no longer exists.")
        ref = args.get("profile")
        if ref and str(ref).lower() not in (prof["id"].lower(), prof["name"].lower()):
            raise ValueError(f"Access is limited to {prof['name']}'s records.")
        return prof
    ref = args.get("profile")
    people = _people()
    if ref:
        for p in people:
            if ref in (p["id"], p["name"]) or p["name"].lower() == str(ref).lower():
                return p
        raise ValueError(f"No profile named '{ref}'. Use list_profiles to see who is on this instance.")
    if ALLOWED_PROFILES.get() is None:
        return profiles.default_profile()
    if not people:
        raise ValueError("This token can't see anyone's records any more.")
    return people[0]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def t_list_profiles(_: dict[str, Any]) -> Any:
    scoped = SCOPED_PROFILE.get()
    return [{"id": p["id"], "name": p["name"], "relationship": p["relationship"], "birth_date": p["birth_date"],
             "record_count": p["record_count"], "default": p["is_default"]} for p in _people()
            if not scoped or p["id"] == scoped]


def t_health_summary(args: dict[str, Any]) -> Any:
    prof = _profile(args)
    s = records.summary(prof["id"])
    return {
        "profile": {"name": prof["name"], "birth_date": prof["birth_date"]},
        "patient": [{k: i.get(k) for k in ("full_name", "birth_date", "gender", "source_name")} for i in s["identities"]],
        # Non-empty when connected records describe different people: don't treat them as one person's history.
        "identity_conflict": s["identity_conflict"],
        "active_conditions": [_slim(c) for c in s["active_conditions"]],
        "active_medications": [_slim(m) for m in s["active_medications"]],
        "allergies": [_slim(a) for a in s["allergies"]],
        "out_of_range_recent_labs": [_lab_line(l) for l in s["flagged_labs"]],
        "recent_labs": [_lab_line(l) for l in s["recent_labs"]],
        "latest_vitals": [_lab_line(v) for v in s["latest_vitals"]],
        "recent_visits": [_slim(e) for e in s["recent_encounters"]],
        "record_counts": s["counts"],
        "connected_sources": [{"name": c["display_name"], "kind": c["kind"], "mode": c["mode"], "last_sync_at": c["last_sync_at"]}
                              for c in connections.list_for_profile(prof["id"])],
        "wearable_headline_30d": [{k: m[k] for k in ("label", "unit", "avg_7d", "avg_30d")} | {"latest": m["latest"]}
                                  | ({"simulated": True} if "(simulated)" in (m["latest"] or {}).get("source", "") else {})
                                  for m in biometrics.headline(prof["id"])],
        "note": "Wearable figures marked simulated are demo data, not real measurements.",
    }


def t_search_records(args: dict[str, Any]) -> Any:
    prof = _profile(args)
    res = records.list_records(prof["id"], args.get("category"), q=args.get("query"), since=args.get("since"),
                               until=args.get("until"), limit=min(int(args.get("limit", 25)), 100))
    return {"total_matches": res["total"], "records": [_slim(r) for r in res["items"]]}


def t_get_record(args: dict[str, Any]) -> Any:
    rec = records.get_record(str(args["record_id"]), SCOPED_PROFILE.get())
    if not rec:
        raise ValueError("Record not found.")
    out = _slim(rec, narrative_chars=20000)
    if rec.get("details", {}).get("full_text"):
        out["full_text"] = rec["details"]["full_text"][:20000]
    return out


# Said explicitly: when the field is just missing, smaller models fill in a "typical" range from memory.
NO_RANGE = "not reported by the lab"


def _range(rec: dict[str, Any]) -> str:
    if rec.get("ref_text"):
        return rec["ref_text"]
    lo, hi = rec.get("ref_low"), rec.get("ref_high")
    if lo is None and hi is None:
        return NO_RANGE
    return f"{'' if lo is None else f'{lo:g}'}–{'' if hi is None else f'{hi:g}'} {rec.get('unit') or ''}".strip()


def _lab_line(rec: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in {
        "test": rec.get("title"), "date": (rec.get("effective_at") or "")[:10], "result": rec.get("value_text"),
        "flag": rec.get("interpretation"), "reference_range": _range(rec), "source": rec.get("source_name"),
    }.items() if v not in (None, "")}


def t_list_lab_tests(args: dict[str, Any]) -> Any:
    # Compact on purpose: one copy of each value (as reported) keeps smaller models from misreading.
    prof = _profile(args)
    out = []
    for i in records.observation_catalog(prof["id"], args.get("category", "labs")):
        latest = i["latest"]
        out.append({k: v for k, v in {
            "test": i["title"], "code": i["code"], "panel": i.get("panel"), "results_on_record": i["count"], "trend": i["trend"],
            "latest_date": (latest.get("effective_at") or "")[:10], "latest_result": latest.get("value_text"),
            "flag": latest.get("interpretation"), "reference_range": _range(latest), "source": latest.get("source_name"),
        }.items() if v not in (None, "")})
    return out


def _resolve_code(prof_id: str, code_or_name: str, category: Optional[str]) -> str:
    catalog = records.observation_catalog(prof_id, category or "labs") + ([] if category else records.observation_catalog(prof_id, "vitals"))
    for item in catalog:
        if item["code"] == code_or_name:
            return item["code"]
    # Common names and abbreviations ("HbA1c", "LDL", "TSH") that the source lab may have titled differently.
    on_record = {item["code"] for item in catalog}
    for code in lab_catalog.loinc_codes(code_or_name):
        if code in on_record:
            return code
    needle = code_or_name.lower()
    matches = [item for item in catalog if needle in item["title"].lower()]
    if matches:
        # "cholesterol" should mean Total Cholesterol, not the first panel member that mentions it:
        # prefer the title that is closest to the query (fewest extra characters).
        return min(matches, key=lambda item: len(item["title"]))["code"]
    raise ValueError(f"No lab or vital matching '{code_or_name}'. Use list_lab_tests to see what is available.")


def t_lab_trend(args: dict[str, Any]) -> Any:
    prof = _profile(args)
    code = _resolve_code(prof["id"], str(args["test"]), args.get("category"))
    series = records.observation_series(prof["id"], code)
    return {"code": code, "title": series["title"], "unit": series["unit"],
            "reference_range": _range({"ref_low": series["ref_low"], "ref_high": series["ref_high"], "unit": series["unit"]}),
            "results": [{"date": p["t"], "value": p["v"], "flag": p.get("interpretation"), "source": p.get("source_name")} for p in series["points"]],
            "components": [{"name": c["name"], "unit": c["unit"], "results": [{"date": p["t"], "value": p["v"]} for p in c["points"]]}
                           for c in series["components"]]}


def t_list_metrics(args: dict[str, Any]) -> Any:
    return biometrics.available_metrics(_profile(args)["id"])


def t_daily_metric(args: dict[str, Any]) -> Any:
    prof = _profile(args)
    s = biometrics.daily_series(prof["id"], str(args["metric"]), days=min(int(args.get("days", 30)), 3650))
    days = [{"day": p["day"], "value": p["value"], "source": p["source"],
             **({"simulated": True} if "(simulated)" in p["source"] else {}),
             **({"so_far_today": True} if p.get("partial") else {})} for p in s["points"]]
    out = {"metric": s["metric"], "label": s["label"], "unit": s["unit"], "aggregation": s["aggregation"], "stats": s["stats"],
           "days": days}
    if any(d.get("so_far_today") for d in days):
        out["today_note"] = "Today's value is a running total so far, not a full day; the stats leave it out."
    if any(d.get("simulated") for d in days):
        out["note"] = ("Days marked simulated come from a demo connection, not a real device: mention this and don't "
                       "treat them as the person's measurements.")
    return out


def t_list_workouts(args: dict[str, Any]) -> Any:
    prof = _profile(args)
    days = min(int(args.get("days", 90)), 36500)
    return {"summary": activity.workout_summary(prof["id"], days),
            "workouts": activity.list_workouts(prof["id"], days, min(int(args.get("limit", 50)), 500))}


def t_health_journal(args: dict[str, Any]) -> Any:
    prof = _profile(args)
    days = min(int(args.get("days", 90)), 36500)
    events = activity.list_events(prof["id"], days, args.get("event_type"), args.get("category"), min(int(args.get("limit", 100)), 1000))
    return {"summary": activity.event_summary(prof["id"], days),
            "events": [{k: e[k] for k in ("event_type", "category", "name", "start_date", "end_date", "value", "value_label",
                                          "source_name")} for e in events]}


def t_insights(args: dict[str, Any]) -> Any:
    prof = _profile(args)
    return {"insights": [{k: i[k] for k in ("kind", "tone", "title", "text")} for i in insights.insights(prof["id"], 20)],
            "note": "Patterns and changes found across the person's sources. They describe what lined up, not causes."}


def t_compare(args: dict[str, Any]) -> Any:
    prof = _profile(args)
    c = insights.compare(prof["id"], str(args["a"]), str(args["b"]), min(int(args.get("days", 90)), 3650),
                         int(args.get("lag", 0)))
    return {**{k: c[k] for k in ("a", "b", "lag", "days", "n", "r", "strength", "split")},
            "pairs": c["pairs"][-120:],
            "note": "r is the Pearson correlation of a with b (b taken lag days later; negative lag: earlier). Correlation is "
                    "not causation."}


def t_sleep_nights(args: dict[str, Any]) -> Any:
    prof = _profile(args)
    n = sleep.nights(prof["id"], min(int(args.get("days", 30)), 365))
    for night in n["nights"]:
        night.pop("segments", None)
        night.pop("bed_min", None)
        night.pop("wake_min", None)
    return n


def t_training(args: dict[str, Any]) -> Any:
    prof = _profile(args)
    return training.summary(prof["id"], min(int(args.get("weeks", 12)), 104), args.get("workout_type"))


def t_goals(args: dict[str, Any]) -> Any:
    prof = _profile(args)
    return {"goals": goals.progress(prof["id"], min(int(args.get("days", 7)), 90))}


PROFILE_ARG = {"profile": {"type": "string", "description": "Profile name or id. Defaults to the instance's default profile."}}
CATEGORY_ENUM = records.CATEGORIES

TOOLS: dict[str, tuple[Callable[[dict[str, Any]], Any], str, dict[str, Any], list[str]]] = {
    "list_profiles": (t_list_profiles, "List the people whose health records are kept on this Syntropy Health instance.", {}, []),
    "get_health_summary": (t_health_summary, "Snapshot of a person's health: active problems, current medications, allergies, "
                           "out-of-range recent labs, latest vitals, recent visits, connected sources and 30-day wearable averages. "
                           "Start here.", dict(PROFILE_ARG), []),
    "search_records": (t_search_records, "Search clinical records (conditions, medications, labs, notes, visits, ...) by text, "
                       "category and date range. Results are merged across institutions.", {
        **PROFILE_ARG,
        "query": {"type": "string", "description": "Free-text search over titles, codes, values and notes."},
        "category": {"type": "string", "enum": CATEGORY_ENUM},
        "since": {"type": "string", "description": "ISO date lower bound (inclusive)."},
        "until": {"type": "string", "description": "ISO date upper bound (inclusive)."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
    }, []),
    "get_record": (t_get_record, "Full detail for one record, including the complete text of clinical notes and reports.",
                   {"record_id": {"type": "string"}}, ["record_id"]),
    "list_lab_tests": (t_list_lab_tests, "Every lab test (or vital sign) on record with its latest value, number of results and trend.", {
        **PROFILE_ARG, "category": {"type": "string", "enum": ["labs", "vitals"], "default": "labs"}}, []),
    "get_lab_trend": (t_lab_trend, "All results over time for one lab test or vital sign (by LOINC code or name, e.g. 'A1c', "
                      "'LDL', 'blood pressure'), in one normalized unit with its reference range.", {
        **PROFILE_ARG, "test": {"type": "string", "description": "LOINC code or part of the test name."},
        "category": {"type": "string", "enum": ["labs", "vitals"]}}, ["test"]),
    "list_wearable_metrics": (t_list_metrics, "Wearable/phone metrics available (steps, sleep, HRV, resting heart rate, recovery, ...).",
                              dict(PROFILE_ARG), []),
    "get_daily_metric": (t_daily_metric, "Day-by-day values of one wearable metric (one source per day, no double counting).", {
        **PROFILE_ARG, "metric": {"type": "string", "description": "Metric id from list_wearable_metrics, e.g. 'sleep_duration'."},
        "days": {"type": "integer", "minimum": 1, "maximum": 3650, "default": 30}}, ["metric"]),
    "list_workouts": (t_list_workouts, "Workouts from Apple Health (type, time, duration, distance, energy, heart rate) with "
                      "totals by workout type.", {
        **PROFILE_ARG, "days": {"type": "integer", "minimum": 1, "maximum": 36500, "default": 90},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50}}, []),
    "get_health_journal": (t_health_journal, "Logged symptoms (with severity), cycle tracking, heart-rate and noise notifications, "
                           "mindful sessions, ECG classifications and State of Mind entries from Apple Health.", {
        **PROFILE_ARG, "days": {"type": "integer", "minimum": 1, "maximum": 36500, "default": 90},
        "category": {"type": "string", "enum": list(activity.EVENT_CATEGORIES)},
        "event_type": {"type": "string", "description": "e.g. 'headache', 'menstrual_flow', 'ecg', 'state_of_mind'."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100}}, []),
    "get_insights": (t_insights, "Patterns across every source: wearable numbers moving away from the person's normal, how "
                     "sleep lines up with check-in mood, energy and stress, symptoms against sleep, training load jumps, "
                     "labs moving further out of range, changes after a medication started, goals kept.", dict(PROFILE_ARG), []),
    "compare_measures": (t_compare, "Compare two daily measures (wearable metric ids, 'journal:mood', 'journal:energy', "
                         "'journal:stress', 'workouts:minutes', 'workouts:load') day by day, with their correlation. Use "
                         "lag=1 to pair a day with the next (e.g. training load and the next morning's HRV).", {
        **PROFILE_ARG, "a": {"type": "string"}, "b": {"type": "string"},
        "days": {"type": "integer", "minimum": 7, "maximum": 3650, "default": 90},
        "lag": {"type": "integer", "minimum": -7, "maximum": 7, "default": 0}}, ["a", "b"]),
    "get_sleep_nights": (t_sleep_nights, "Night-by-night sleep: bedtime, wake time, time asleep and in bed, efficiency and "
                         "stages (deep, core/light, REM, awake), with averages and how regular the schedule was.", {
        **PROFILE_ARG, "days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30}}, []),
    "get_training_summary": (t_training, "Weekly workout volume and heart-rate-weighted training load, this week's load against "
                             "the four weeks before, heart-rate zones, and for one workout type its progress (pace, "
                             "heart rate) and best efforts.", {
        **PROFILE_ARG, "weeks": {"type": "integer", "minimum": 1, "maximum": 104, "default": 12},
        "workout_type": {"type": "string", "description": "A workout name from list_workouts, e.g. 'Running'."}}, []),
    "get_goals": (t_goals, "The person's daily goals (e.g. sleep, steps) and how many recent days met them.", {
        **PROFILE_ARG, "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 7}}, []),
}

INSTRUCTIONS = (
    "Syntropy Health holds a person's own medical records (from their patient portals) and wearable data, stored locally. "
    "Use get_health_summary first (and get_insights for patterns across sources), then search_records / get_lab_trend / "
    "get_daily_metric / get_sleep_nights / list_workouts / get_training_summary / get_health_journal / compare_measures "
    "for detail. Values carry reference "
    "ranges and interpretation flags from the source lab. You are not a clinician: explain and summarize, cite dates and "
    "sources, and suggest discussing concerns with the person's care team rather than diagnosing. Only state values "
    "that appear in tool results; if identity_conflict is non-empty, the clinical records belong to more than one "
    "person, so say which person each result belongs to. Data marked simulated is demo data, not real measurements."
)


# ---------------------------------------------------------------------------
# JSON-RPC over stdio
# ---------------------------------------------------------------------------

def _tool_list() -> list[dict[str, Any]]:
    return [{"name": name, "description": desc,
             "inputSchema": {"type": "object", "properties": props, "required": req, "additionalProperties": False},
             "annotations": {"readOnlyHint": True, "openWorldHint": False}}
            for name, (_, desc, props, req) in TOOLS.items()]


def call_tool(name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Runs one tool. Returns the JSON result, or an error message the model can act on, and whether it failed."""
    if name not in TOOLS:
        return f"Unknown tool: {name}", True
    fn, _, _, required = TOOLS[name]
    missing = [a for a in required if a not in args]
    if missing:
        return f"Missing required argument: {', '.join(missing)}", True
    try:
        payload = fn(args)
    except (ValueError, LookupError, TypeError) as exc:
        return str(exc), True
    return json.dumps(payload, default=str, ensure_ascii=False), False


def handle(message: dict[str, Any]) -> Optional[dict[str, Any]]:
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}
    if msg_id is None:            # notification
        return None
    try:
        if method == "initialize":
            requested = params.get("protocolVersion")
            result: Any = {
                "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "syntropy-health", "version": config.APP_VERSION},
                "instructions": INSTRUCTIONS,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _tool_list()}
        elif method == "tools/call":
            name = params.get("name")
            if name not in TOOLS:
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
            args = params.get("arguments") or {}
            text, is_error = call_tool(name, args)
            if not is_error:
                with db() as conn:
                    audit(conn, ACTOR.get(), f"mcp.{name}", {k: v for k, v in args.items() if k != "profile"})
            result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        else:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    except Exception as exc:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": str(exc)}}


def stdio_command(profile_id: Optional[str] = None, actor: Optional[str] = None) -> dict[str, Any]:
    """How a program on this computer starts the stdio server: this Python (or the packaged app) with the data
    directory. ``profile_id`` limits it to one person and ``actor`` names the caller in the activity log."""
    if getattr(sys, "frozen", False):          # the Mac or Windows app: its executable runs the server with "mcp"
        exe = Path(sys.executable)
        console = exe.with_name("syntropy-health.exe")    # Windows: the console build next to the app has stdio
        cmd: dict[str, Any] = {"command": str(console if console.exists() else exe), "args": ["mcp"], "env": {}}
    else:
        cmd = {"command": sys.executable, "args": ["-m", "app.mcp_server"], "env": {"PYTHONPATH": str(config.REPO_ROOT)}}
    cmd["env"]["SYNTROPY_DATA_DIR"] = str(config.data_dir())
    if profile_id:
        cmd["env"]["SYNTROPY_MCP_PROFILE"] = profile_id
    if actor:
        cmd["env"]["SYNTROPY_MCP_ACTOR"] = actor
    return cmd


def main() -> None:
    # Set by stdio_command for agents started by the built-in assistant: one person's records, named in the log.
    if config.env("SYNTROPY_MCP_PROFILE"):
        SCOPED_PROFILE.set(config.env("SYNTROPY_MCP_PROFILE"))
    if config.env("SYNTROPY_MCP_ACTOR"):
        ACTOR.set(config.env("SYNTROPY_MCP_ACTOR"))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}) + "\n")
            sys.stdout.flush()
            continue
        batch = message if isinstance(message, list) else [message]
        responses = [r for r in (handle(m) for m in batch if isinstance(m, dict)) if r is not None]
        if responses:
            sys.stdout.write(json.dumps(responses if isinstance(message, list) else responses[0]) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
