"""
Agents on this machine: the assistant runs on an AI agent the person already has installed and signed in to, so
questions use their existing Claude, ChatGPT or Gemini plan.

Syntropy starts the vendor's own, unmodified command-line tool as a subprocess, in an empty temporary folder, with
Syntropy's read-only MCP server (limited to one person) as its only tool source: no shell, no file edits, no web. It
never reads or copies the tool's sign-in files and never calls the vendor's servers itself; signing in happens in the
tool's own flow, as the vendors' terms require.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from app import mcp_server
from app.services.ai_local import in_docker

SERVER = "syntropy"          # the MCP server's name inside the agent
RUN_TIMEOUT_S = 300
STATUS_TIMEOUT_S = 15

AGENTS: dict[str, dict[str, Any]] = {
    "claude": {
        "label": "Claude Code", "vendor": "Anthropic", "binary": "claude", "plan": "Claude",
        # Offered in Ask's model menu (the tool's own aliases, which follow its latest models); any --model value works.
        "models": ["sonnet", "opus", "haiku"],
        "install": "Install it from claude.com/claude-code.",
        "sign_in": "Run claude in a terminal and sign in, then come back.",
        "usage": "Counts toward your Claude plan's usage limits, like using Claude Code yourself.",
        "terms": "Questions fall under Anthropic's consumer terms and your Claude privacy settings (for example, "
                 "whether chats may be used to improve Claude), not API terms.",
    },
    "codex": {
        "label": "Codex CLI", "vendor": "OpenAI", "binary": "codex", "plan": "ChatGPT",
        "install": "Install it from developers.openai.com/codex.",
        "sign_in": "Run codex login in a terminal and sign in, then come back.",
        "usage": "Counts toward your ChatGPT plan's Codex usage.",
        "terms": "Questions fall under OpenAI's consumer terms and your ChatGPT data controls (for example, whether "
                 "chats may be used to train models), not API terms.",
    },
    "gemini": {
        "label": "Gemini CLI", "vendor": "Google", "binary": "gemini", "plan": "Google", "optional": True,
        "install": "Install it from geminicli.com.",
        "sign_in": "Run gemini in a terminal and sign in, then come back.",
        "usage": "Counts toward your Gemini CLI quota. Google recommends an AI Studio API key for apps like this; "
                 "that's the main Google option under Your own key.",
        "terms": "Questions fall under Google's terms for Gemini CLI and your Google account's data settings, not "
                 "API terms.",
    },
}


class AgentError(Exception):
    pass


# ---------------------------------------------------------------------------
# Finding the tools
# ---------------------------------------------------------------------------

def _search_dirs() -> list[str]:
    """PATH plus the usual install folders: apps started from the Dock or Start menu get a short PATH."""
    home = Path.home()
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    extra = [home / ".local/bin", home / ".claude/local", home / ".npm-global/bin", home / ".bun/bin",
             home / ".volta/bin", home / ".cargo/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin")]
    extra += sorted((home / ".nvm/versions/node").glob("*/bin"), reverse=True)
    if sys.platform == "win32":
        appdata, local = os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")
        extra += [Path(appdata) / "npm", Path(local) / "Programs/claude", home / ".local/bin",
                  Path(local) / "Microsoft/WinGet/Links", Path(os.environ.get("ProgramFiles", "")) / "nodejs"]
    out: list[str] = []
    for d in [*dirs, *map(str, extra)]:
        if d and d not in out:
            out.append(d)
    return out


def find_binary(name: str) -> Optional[str]:
    return shutil.which(name, path=os.pathsep.join(_search_dirs()))


def _command(path: str) -> list[str]:
    """How to start a tool. npm's Windows shims (.cmd) are run through Node directly, which avoids cmd.exe's quoting."""
    if sys.platform == "win32" and path.lower().endswith((".cmd", ".bat")):
        try:
            text = Path(path).read_text(errors="ignore")
        except OSError:
            text = ""
        match = re.search(r'"%(?:~)?dp0%?\\?([^"]+?\.(?:js|mjs|cjs))"', text)
        node = shutil.which("node", path=os.pathsep.join([str(Path(path).parent), *_search_dirs()]))
        if match and node:
            return [node, str(Path(path).parent / match.group(1))]
        return ["cmd", "/d", "/s", "/c", path]
    return [path]


def _env(binary: str) -> dict[str, str]:
    """The person's environment (the tool needs HOME and its own settings), without Syntropy's own secrets."""
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("SYNTROPY_") or k.endswith(("_CLIENT_SECRET", "_SECRET")))}
    env["PATH"] = os.pathsep.join([str(Path(binary).parent), *_search_dirs()])
    env.setdefault("NO_COLOR", "1")
    return env


def _run_quick(args: list[str], binary: str, timeout: float = STATUS_TIMEOUT_S) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=_env(binary),
                              stdin=subprocess.DEVNULL, cwd=tempfile.gettempdir(),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


_help_cache: dict[tuple[str, float, str], str] = {}


def _help(binary: str, *sub: str) -> str:
    """A tool's --help text, to use only the flags this version has."""
    try:
        key = (binary, os.path.getmtime(binary), " ".join(sub))
    except OSError:
        return ""
    if key not in _help_cache:
        _help_cache[key] = _run_quick([*_command(binary), *sub, "--help"], binary)[1]
    return _help_cache[key]


def _signed_in(agent_id: str, binary: str) -> Optional[bool]:
    """Asks the tool itself (never reads its credential files). None when the tool can't say."""
    cmd = _command(binary)
    if agent_id == "claude":
        code, out = _run_quick([*cmd, "auth", "status", "--json"], binary)
        try:
            data = json.loads(out[out.index("{"):out.rindex("}") + 1])
            return bool(data.get("loggedIn", data.get("logged_in")))
        except (ValueError, AttributeError):
            return None if code != 0 or not out else "not logged in" not in out.lower()
    if agent_id == "codex":
        code, out = _run_quick([*cmd, "login", "status"], binary)
        if code == -1:
            return None
        return code == 0 and "not logged in" not in out.lower()
    return None


_detected: tuple[float, list[dict[str, Any]]] = (0.0, [])


def detect(refresh: bool = False) -> dict[str, Any]:
    """The agents installed here, whether each is signed in, and what using it means."""
    global _detected
    if in_docker():
        return {"available": False, "reason": "Agents run on the computer itself, not inside Docker. Use the Mac or "
                "Windows app, or run Syntropy Health without Docker, to use them.", "agents": []}
    if not refresh and time.time() - _detected[0] < 60:
        return {"available": True, "agents": _detected[1]}
    agents = []
    for agent_id, spec in AGENTS.items():
        binary = find_binary(spec["binary"])
        entry = {"id": agent_id, **{k: spec[k] for k in ("label", "vendor", "plan", "install", "sign_in", "usage", "terms")}, "models": spec.get("models", []),
                 "optional": bool(spec.get("optional")), "installed": bool(binary), "path": binary, "version": None,
                 "signed_in": None}
        if binary:
            code, out = _run_quick([*_command(binary), "--version"], binary)
            match = re.search(r"\d+\.\d+(?:\.\d+)?", out)
            entry["version"] = match.group(0) if code == 0 and match else None
            entry["signed_in"] = _signed_in(agent_id, binary)
        agents.append(entry)
    _detected = (time.time(), agents)
    return {"available": True, "agents": agents}


# ---------------------------------------------------------------------------
# Running a question
# ---------------------------------------------------------------------------

def _transcript(turns: list[dict[str, Any]]) -> str:
    *earlier, latest = turns
    parts = []
    if earlier:
        parts.append("Conversation so far:\n\n" + "\n\n".join(
            f"{'Person' if t['role'] == 'user' else 'You'}: {t['content']}" for t in earlier))
    parts.append(f"The person's new message:\n\n{latest['content']}")
    return "\n\n".join(parts)


AGENT_NOTE = (f"\n\nYou're answering inside the Syntropy Health app. Your only tools are the {SERVER} tools, which read "
              "this person's records; you have no shell, files or web. Reply with the answer only, in markdown.")


def _claude(binary: str, work: Path, mcp: dict[str, Any], system: str, model: Optional[str]) -> list[str]:
    help_text = _help(binary)
    (work / "mcp.json").write_text(json.dumps({"mcpServers": {SERVER: {"type": "stdio", **mcp}}}))
    args = [*_command(binary), "-p", "--output-format", "stream-json", "--verbose",
            "--strict-mcp-config", "--mcp-config", str(work / "mcp.json"),
            "--allowedTools", f"mcp__{SERVER}", "--system-prompt", system + AGENT_NOTE]
    if "--tools" in help_text:
        args += ["--tools", ""]          # none of Claude Code's own tools
    else:
        args += ["--disallowedTools", "Bash,Edit,Write,MultiEdit,NotebookEdit,Read,Glob,Grep,WebFetch,WebSearch,Task"]
    if "dontAsk" in help_text:
        args += ["--permission-mode", "dontAsk"]    # anything not allowed above is refused, never asked
    if "--no-session-persistence" in help_text:
        args.append("--no-session-persistence")
    if "--include-partial-messages" in help_text:
        args.append("--include-partial-messages")  # the answer as it's written, shown in Ask while it streams
    if model:
        args += ["--model", model]
    return args


def _toml(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {_toml(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return json.dumps(value)        # a JSON string is a valid TOML basic string


def _codex(binary: str, work: Path, mcp: dict[str, Any], model: Optional[str]) -> list[str]:
    help_text = _help(binary, "exec")
    over = {f"mcp_servers.{SERVER}.command": mcp["command"], f"mcp_servers.{SERVER}.args": mcp["args"],
            f"mcp_servers.{SERVER}.env": mcp["env"],
            # exec can't ask for approval, so without this every MCP call is cancelled.
            f"mcp_servers.{SERVER}.default_tools_approval_mode": "approve",
            "features.shell_tool": False, "web_search": "disabled"}
    args = [*_command(binary), "exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only", "--cd", str(work)]
    for key, value in over.items():
        args += ["-c", f"{key}={'false' if value is False else _toml(value)}"]
    if "--ephemeral" in help_text:
        args.append("--ephemeral")
    if "--ignore-user-config" in help_text:
        args.append("--ignore-user-config")     # none of the person's other MCP servers or settings
    if model:
        args += ["--model", model]
    return [*args, "-"]                          # the prompt comes on stdin


def _gemini(binary: str, work: Path, mcp: dict[str, Any], model: Optional[str]) -> list[str]:
    help_text = _help(binary)
    (work / ".gemini").mkdir()
    (work / ".gemini/settings.json").write_text(json.dumps({"mcpServers": {SERVER: {**mcp, "trust": True}}}))
    # Everything is refused except Syntropy's tools.
    (work / "policy.toml").write_text(f'[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 1\n\n'
                                      f'[[rule]]\nmcpName = "{SERVER}"\ndecision = "allow"\npriority = 900\n')
    args = [*_command(binary), "-p", " ", "--output-format", "stream-json", "--allowed-mcp-server-names", SERVER]
    if "--skip-trust" in help_text:
        args.append("--skip-trust")
    if "--policy" in help_text:
        args += ["--policy", str(work / "policy.toml")]
    if model:
        args += ["--model", model]
    return args


def _short_tool(name: str) -> str:
    """'mcp__syntropy__get_daily_metric' or 'syntropy__get_daily_metric' → 'get_daily_metric'."""
    return name.rsplit("__", 1)[-1]


def _signin_error(agent_id: str, text: str) -> Optional[str]:
    lowered = text.lower()
    if any(s in lowered for s in ("not logged in", "please run /login", "login required", "not signed in", "401",
                                  "unauthorized", "authentication", "please log in", "invalid api key")):
        return f"{AGENTS[agent_id]['label']} isn't signed in. {AGENTS[agent_id]['sign_in']}"
    return None


def _parse(agent_id: str, event: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
    """One line of the tool's JSON output → assistant events. `state` collects the answer text."""
    kind = event.get("type")
    out: list[dict[str, Any]] = []
    if agent_id == "claude":
        if kind == "stream_event" and not event.get("parent_tool_use_id"):
            ev = event.get("event") or {}
            if ev.get("type") == "message_start":
                # Each message after a lookup starts over: only the last one is the answer.
                if state.pop("drafted", False):
                    out.append({"type": "reset"})
            elif ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                text = str(ev["delta"].get("text") or "")
                if text:
                    state["drafted"] = True
                    out.append({"type": "delta", "text": text})
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    out.append({"type": "tool", "name": _short_tool(block.get("name", "")), "args": block.get("input") or {}})
        elif kind == "result":
            text = str(event.get("result") or "")
            if event.get("is_error") or str(event.get("subtype", "")).startswith("error"):
                state["error"] = text or str(event.get("subtype"))
            else:
                state["answer"] = text
    elif agent_id == "codex":
        item = event.get("item") or {}
        if kind == "item.started" and item.get("type") == "mcp_tool_call":
            out.append({"type": "tool", "name": _short_tool(str(item.get("tool", ""))), "args": item.get("arguments") or {}})
        elif kind == "item.completed" and item.get("type") == "agent_message":
            state["answer"] = str(item.get("text") or "")
        elif kind == "turn.failed":
            state["error"] = str((event.get("error") or {}).get("message") or "The turn failed.")
        elif kind == "error":
            state.setdefault("notices", []).append(str(event.get("message") or ""))
    elif agent_id == "gemini":
        if kind == "tool_use":
            out.append({"type": "tool", "name": _short_tool(str(event.get("tool_name", ""))), "args": event.get("parameters") or {}})
        elif kind == "message" and event.get("role") == "assistant":
            text = str(event.get("content") or "")
            if not event.get("delta") and state.get("answer"):
                out.append({"type": "reset"})
            state["answer"] = (state.get("answer", "") if event.get("delta") else "") + text
            if text:
                out.append({"type": "delta", "text": text})
        elif kind == "result" and event.get("status") not in (None, "success"):
            state["error"] = str((event.get("error") or {}).get("message") or event.get("status"))
        elif kind == "error":
            state["error"] = str(event.get("message") or "Gemini CLI reported an error.")
    return out


async def run(agent_id: str, profile_id: str, system: str, turns: list[dict[str, Any]],
              model: Optional[str] = None) -> AsyncIterator[dict[str, Any]]:
    """Asks the agent; yields {"type": "tool", "name", "args"} as it looks things up, {"type": "delta", "text"} (and
    {"type": "reset"}) as the answer is written, where the tool reports that, then {"type": "answer"}."""
    if agent_id not in AGENTS:
        raise AgentError("Unknown agent.")
    if in_docker():
        raise AgentError(detect()["reason"])
    spec = AGENTS[agent_id]
    binary = find_binary(spec["binary"])
    if not binary:
        raise AgentError(f"{spec['label']} isn't installed on this computer. {spec['install']}")
    work = Path(tempfile.mkdtemp(prefix="syntropy-agent-"))
    mcp = mcp_server.stdio_command(profile_id, actor=f"agent:{agent_id}")
    if agent_id == "claude":
        args, prompt = _claude(binary, work, mcp, system, model), _transcript(turns)
    elif agent_id == "codex":
        args, prompt = _codex(binary, work, mcp, model), system + AGENT_NOTE + "\n\n" + _transcript(turns)
    else:
        args, prompt = _gemini(binary, work, mcp, model), system + AGENT_NOTE + "\n\n" + _transcript(turns)

    # A thread reads the tool's output so this works on every event loop (Windows' default can't run subprocesses
    # under every server configuration).
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[Optional[str]] = asyncio.Queue()
    stderr_tail: list[str] = []
    try:
        proc = subprocess.Popen(args, cwd=work, env=_env(binary), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise AgentError(f"Couldn't start {spec['label']} ({exc}).") from exc

    def pump() -> None:
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError:
            pass
        for line in proc.stdout:
            loop.call_soon_threadsafe(lines.put_nowait, line)
        loop.call_soon_threadsafe(lines.put_nowait, None)

    def drain_stderr() -> None:
        for line in proc.stderr:
            stderr_tail.append(line)
            del stderr_tail[:-40]

    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=drain_stderr, daemon=True).start()
    state: dict[str, Any] = {}
    deadline = time.monotonic() + RUN_TIMEOUT_S
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AgentError(f"{spec['label']} took longer than {RUN_TIMEOUT_S // 60} minutes. Try a narrower question.")
            try:
                line = await asyncio.wait_for(lines.get(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
            if line is None:
                break
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            for out in _parse(agent_id, event, state):
                yield out
        await asyncio.get_running_loop().run_in_executor(None, proc.wait)
        if state.get("answer") and not state.get("error"):
            yield {"type": "answer", "text": state["answer"].strip()}
            return
        detail = state.get("error") or " ".join(state.get("notices", [])) or "".join(stderr_tail[-8:]).strip()
        raise AgentError(_signin_error(agent_id, detail) or
                         f"{spec['label']} stopped without an answer{f': {detail[:400]}' if detail else '.'}")
    finally:
        if proc.poll() is None:
            proc.kill()
        shutil.rmtree(work, ignore_errors=True)
