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
import functools
import hashlib
import json
import logging
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
from app.core import context
from app.services.ai_local import in_docker

log = logging.getLogger("syntropy.ai")

SERVER = "syntropy"          # the MCP server's name inside the agent
RUN_TIMEOUT_S = 300
STATUS_TIMEOUT_S = 15

AGENTS: dict[str, dict[str, Any]] = {
    "claude": {
        "label": "Claude Code", "vendor": "Anthropic", "binary": "claude", "plan": "Claude",
        # Offered in Ask's model menu (the tool's own aliases, which follow its latest models); any --model value works.
        "models": ["sonnet", "opus", "haiku"],
        "install": "Install it from claude.com/claude-code.",
        # The Claude app's own Claude Code keeps its own sign-in, so someone using that daily can still find this one signed out.
        "sign_in": "Run claude auth login in a terminal and sign in, then come back. Being signed in to the Claude app "
                   "doesn't sign in the claude command.",
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
                 "API terms. If you keep your own instructions for Gemini CLI (~/.gemini/GEMINI.md), current versions "
                 "send them with every question too: Ask can't leave them out.",
    },
}


class AgentError(Exception):
    pass


# A model name is passed on the tool's command line, and only a plain name may reach it: no flags, no shell characters.
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\[\]-]{0,100}$")


def valid_model(name: str) -> bool:
    return bool(MODEL_NAME.match(name))


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
# Looking starts each tool two or three times. One look at a time, and one asked for is skipped if the last was this recent.
_detecting = threading.Lock()
DETECT_FRESH_S = 60
REFRESH_MIN_S = 10


def last_detected() -> Optional[tuple[float, dict[str, Any]]]:
    """When the agents were last looked for, and what was found; None before the first look (or inside Docker)."""
    if in_docker() or not _detected[0]:
        return None
    return _detected[0], {"available": True, "agents": _detected[1]}


def available(agent_id: str) -> bool:
    """Whether this agent could answer here: known, installed, and not known to be signed out."""
    if agent_id not in AGENTS or in_docker() or not find_binary(AGENTS[agent_id]["binary"]):
        return False
    return not any(a["id"] == agent_id and a.get("signed_in") is False for a in _detected[1])


def detect(refresh: bool = False) -> dict[str, Any]:
    """The agents installed here, whether each is signed in, and what using it means."""
    if in_docker():
        return {"available": False, "reason": "Agents run on the computer itself, not inside Docker. Use the Mac or "
                "Windows app, or run Syntropy Health without Docker, to use them.", "agents": []}

    def recent() -> bool:
        return time.time() - _detected[0] < (REFRESH_MIN_S if refresh else DETECT_FRESH_S)

    if recent():
        return {"available": True, "agents": _detected[1]}
    with _detecting:
        if recent():            # someone else just looked
            return {"available": True, "agents": _detected[1]}
        return {"available": True, "agents": _look()}


def _look() -> list[dict[str, Any]]:
    global _detected
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
    return agents


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


def _claude(binary: str, work: Path, mcp: dict[str, Any], system: str, model: Optional[str]) -> tuple[list[str], bool]:
    """The command line, and whether it carries the instructions. They name the person, so they don't go on a command line
    anyone on this computer can read: in a private file where the tool can read one, otherwise with the question."""
    help_text = _help(binary)
    (work / "mcp.json").write_text(json.dumps({"mcpServers": {SERVER: {"type": "stdio", **mcp}}}))
    args = [*_command(binary), "-p", "--output-format", "stream-json", "--verbose",
            "--strict-mcp-config", "--mcp-config", str(work / "mcp.json"),
            "--allowedTools", f"mcp__{SERVER}"]
    in_file = "--system-prompt-file" in help_text or "--system-prompt[-file]" in help_text
    if in_file:
        (work / "instructions.txt").write_text(system + AGENT_NOTE, encoding="utf-8")
        args += ["--system-prompt-file", str(work / "instructions.txt")]
    if "--tools" in help_text:
        args += ["--tools", ""]          # none of Claude Code's own tools
    else:
        args += ["--disallowedTools", "Bash,Edit,Write,MultiEdit,NotebookEdit,Read,Glob,Grep,WebFetch,WebSearch,Task"]
    if "dontAsk" in help_text:
        args += ["--permission-mode", "dontAsk"]    # anything not allowed above is refused, never asked
    if "--no-session-persistence" in help_text:
        args.append("--no-session-persistence")
    if "--setting-sources" in help_text:
        # Not the person's own settings, which hold their CLAUDE.md instructions (sent with every question otherwise)
        # and hooks. Signing in doesn't depend on them. This empty folder has no project or local settings.
        args += ["--setting-sources", "project,local"]
    if "--include-partial-messages" in help_text:
        args.append("--include-partial-messages")  # the answer as it's written, shown in Ask while it streams
    if model:
        args += ["--model", model]
    return args, in_file


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


NO_CONTEXT_FILE = "SYNTROPY-NO-CONTEXT.md"


def _gemini(binary: str, work: Path, mcp: dict[str, Any], model: Optional[str]) -> list[str]:
    help_text = _help(binary)
    (work / ".gemini").mkdir()
    # A context file that doesn't exist, for the person's own GEMINI.md instructions. Versions that replace the name with
    # this one leave them out; 0.66 adds it beside GEMINI.md and reads that anyway, so the terms say so.
    (work / ".gemini/settings.json").write_text(json.dumps({"mcpServers": {SERVER: {**mcp, "trust": True}},
                                                            "context": {"fileName": NO_CONTEXT_FILE}}))
    # Everything is refused except Syntropy's tools.
    (work / "policy.toml").write_text(f'[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 1\n\n'
                                      f'[[rule]]\nmcpName = "{SERVER}"\ndecision = "allow"\npriority = 900\n')
    args = [*_command(binary), "-p", " ", "--output-format", "stream-json", "--allowed-mcp-server-names", SERVER]
    if "--skip-trust" in help_text:
        args.append("--skip-trust")
    if "--extensions" in help_text:
        args += ["--extensions", "none"]         # nor the context files, servers and hooks of the person's extensions
    # The policy is what limits it to Syntropy's read-only tools, so it doesn't run without one.
    if "--policy" not in help_text:
        raise AgentError("This version of Gemini CLI can't be limited to read-only lookups, so Ask won't run it. Update "
                         f"Gemini CLI and try again. {AGENTS['gemini']['install']}")
    args += ["--policy", str(work / "policy.toml")]
    if model:
        args += ["--model", model]
    return args


# Gemini CLI has no switch to stop it keeping a transcript of each session (for 30 days, by default). It keeps them in
# a folder of its own for each working folder: ~/.gemini/tmp/<name>/chats, beside that session's logs, with a history
# folder in ~/.gemini/history/<name>. A ".project_root" file in each names the working folder, and ~/.gemini/projects.json
# lists the names. Before version 0.2x the name was the working folder's SHA-256 and there was no ".project_root".
WORK_PREFIX = "syntropy-agent-"


def _gemini_dirs() -> list[Path]:
    """Where Gemini CLI keeps what it writes: in ~/.gemini (or under GEMINI_CLI_HOME), or ~/.cache/.gemini when it runs
    in the Mac's sandbox."""
    home = Path(os.environ.get("GEMINI_CLI_HOME") or Path.home())
    return [home / ".gemini", home / ".cache" / ".gemini"]


def _same_path(path: str) -> str:
    """A path as Gemini CLI compares them (it lowercases them on Windows)."""
    return os.path.normcase(os.path.abspath(path))


def _was_ours(project: str) -> bool:
    """Whether Gemini CLI's folder was made for one of Syntropy's working folders that's been removed: the folder of a
    question that has finished, here or before, and never one that's still being answered."""
    temp = {_same_path(tempfile.gettempdir()), _same_path(os.path.realpath(tempfile.gettempdir()))}
    path = _same_path(project)
    return (os.path.dirname(path) in temp and os.path.basename(path).startswith(WORK_PREFIX)
            and not os.path.exists(path))


def _forget_gemini_runs(work: Path) -> None:
    """Removes what Gemini CLI kept of this question and of any earlier one asked here: transcripts, logs and history,
    and their entries in its list of folders."""
    hashed = {hashlib.sha256(str(p).encode()).hexdigest() for p in (work, os.path.realpath(work))}
    for base in _gemini_dirs():
        for sub in ("tmp", "history"):
            try:
                folders = [f for f in (base / sub).iterdir() if f.is_dir()]
            except OSError:
                continue
            for folder in folders:
                try:
                    project = (folder / ".project_root").read_text(encoding="utf-8").strip()
                except OSError:
                    project = None
                if (project and _was_ours(project)) or (project is None and folder.name in hashed):
                    shutil.rmtree(folder, ignore_errors=True)
        _unlist(base / "projects.json")


def _unlist(registry: Path) -> None:
    """Takes Syntropy's finished folders out of Gemini CLI's list, under the same lock Gemini CLI takes (a folder named
    after the file). If Gemini CLI holds it for longer than a moment, they stay listed: a name, nothing of the question."""
    if not registry.exists():
        return
    lock = registry.with_name(registry.name + ".lock")
    for _ in range(40):
        try:
            lock.mkdir()
            break
        except FileExistsError:
            time.sleep(0.05)
        except OSError:
            return
    else:
        return
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
        projects = data["projects"]
        keep = {path: name for path, name in projects.items() if not _was_ours(path)}
        if len(keep) < len(projects):
            fresh = registry.with_name(f"{registry.name}.{os.getpid()}.syntropy.tmp")
            fresh.write_text(json.dumps({**data, "projects": keep}, indent=2), encoding="utf-8")
            os.replace(fresh, registry)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def _stop(proc: subprocess.Popen) -> None:
    """Stops the tool and what it started. Gemini CLI answers in a copy of itself that it starts: asked to stop, it passes
    that on, and on Windows, where it can't be asked, the whole tree is ended."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def _finish(agent_id: str, proc: subprocess.Popen, work: Path) -> None:
    """After a question, answered or not: the tool is stopped, then nothing it was given or wrote is left behind."""
    _stop(proc)
    shutil.rmtree(work, ignore_errors=True)
    if agent_id == "gemini":
        try:
            _forget_gemini_runs(work)
        except Exception:  # noqa: BLE001 - the answer stands; the log says what was left
            log.warning("Couldn't remove what Gemini CLI kept of a question", exc_info=True)


def _short_tool(name: str) -> str:
    """'mcp__syntropy__get_daily_metric' or 'syntropy__get_daily_metric' → 'get_daily_metric'."""
    return name.rsplit("__", 1)[-1]


def _signin_error(agent_id: str, text: str) -> Optional[str]:
    lowered = text.lower()
    # "authenticat" covers Claude Code's "Failed to authenticate: OAuth session expired and could not be refreshed".
    if any(s in lowered for s in ("not logged in", "please run /login", "login required", "not signed in", "401",
                                  "unauthorized", "authenticat", "session expired", "please log in", "invalid api key")):
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


def _start(agent_id: str, profile_id: str, system: str, turns: list[dict[str, Any]], model: Optional[str],
           actor: str) -> tuple[subprocess.Popen, Path, str]:
    """Starts the tool in a new empty folder: (the process, the folder, what goes on its standard input)."""
    if in_docker():
        raise AgentError(detect()["reason"])
    spec = AGENTS[agent_id]
    binary = find_binary(spec["binary"])
    if not binary:
        raise AgentError(f"{spec['label']} isn't installed on this computer. {spec['install']}")
    work = Path(tempfile.mkdtemp(prefix=WORK_PREFIX))
    if not profile_id:
        raise AgentError("Choose whose records to ask about.")
    mcp = mcp_server.stdio_command(profile_id, actor=actor)
    mcp["args"] = [*mcp["args"], "--profile", profile_id]       # besides its environment: either limits it to one person
    prompt = system + AGENT_NOTE + "\n\n" + _transcript(turns)
    try:
        if agent_id == "claude":
            args, in_file = _claude(binary, work, mcp, system, model)
            if in_file:
                prompt = _transcript(turns)
        elif agent_id == "codex":
            args = _codex(binary, work, mcp, model)
        else:
            args = _gemini(binary, work, mcp, model)
    except (AgentError, OSError):
        shutil.rmtree(work, ignore_errors=True)
        raise
    try:
        proc = subprocess.Popen(args, cwd=work, env=_env(binary), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise AgentError(f"Couldn't start {spec['label']} ({exc}).") from exc
    return proc, work, prompt


def _stop_unused(agent_id: str, starting: asyncio.Future) -> None:
    if starting.cancelled() or starting.exception() is not None:
        return
    proc, work, _ = starting.result()
    threading.Thread(target=_finish, args=(agent_id, proc, work), daemon=True).start()


async def run(agent_id: str, profile_id: str, system: str, turns: list[dict[str, Any]],
              model: Optional[str] = None) -> AsyncIterator[dict[str, Any]]:
    """Asks the agent; yields {"type": "tool", "name", "args"} as it looks things up, {"type": "delta", "text"} (and
    {"type": "reset"}) as the answer is written, where the tool reports that, then {"type": "answer"}."""
    if agent_id not in AGENTS:
        raise AgentError("Unknown agent.")
    if model and not valid_model(model):
        raise AgentError(f"The model saved for {AGENTS[agent_id]['label']} isn't a model name. Choose another in Ask's model menu.")
    spec = AGENTS[agent_id]
    principal = context.principal()
    # Named in the access log, so what was looked up shows to the account that asked.
    actor = f"agent:{agent_id}:{principal.account_id}" if principal and principal.account_id else f"agent:{agent_id}"
    # Beside the server, which keeps answering: finding the tool and reading its --help start other programs.
    starting = asyncio.ensure_future(asyncio.to_thread(_start, agent_id, profile_id, system, turns, model, actor))
    try:
        proc, work, prompt = await asyncio.shield(starting)
    except asyncio.CancelledError:
        # The person stopped before it started: it's stopped once it has.
        starting.add_done_callback(functools.partial(_stop_unused, agent_id))
        raise

    # A thread reads the tool's output so this works on every event loop (Windows' default can't run subprocesses
    # under every server configuration).
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[Optional[str]] = asyncio.Queue()
    stderr_tail: list[str] = []

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
        # In a thread, which finishes even if this is stopped again while it waits.
        await asyncio.to_thread(_finish, agent_id, proc, work)
