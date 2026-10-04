"""
The built-in assistant, on the AI the person chooses.

Three ways to run it (Settings → AI), and the choice decides where data goes:
- a local model (Ollama, LM Studio, llama.cpp, ...) on this computer or another on the home network: nothing leaves it;
- the person's own API key with Anthropic, OpenAI, Google Gemini, OpenRouter, xAI, Mistral, Groq, DeepSeek, Together,
  Fireworks, Cerebras or any other OpenAI-compatible service;
- an agent installed on this computer (Claude Code, Codex CLI, Gemini CLI), on the person's existing plan
  (``ai_agents``).

Any number of these can be connected at once; one is the default, and Ask's model menu switches between them.

The assistant answers questions and coaches using the same read-only tools the MCP server exposes, limited to the
profile being viewed. A model that can't call tools answers from a compact summary of the person's data instead. Keys
are encrypted at rest. The provider receives the questions and only the data the model asks for (or the summary).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from app import mcp_server
from app.core import context, settings
from app.core.db import audit, db
from app.services import ai_agents, ai_local, insights
from app.store import accounts, activity, profiles

# api: the wire format ("anthropic", or "openai" for OpenAI-compatible Chat Completions).
# custom_url: the person gives the address. needs_key: False when a key is optional.
PROVIDERS: dict[str, dict[str, Any]] = {
    "anthropic": {"label": "Anthropic (Claude)", "api": "anthropic", "base_url": "https://api.anthropic.com",
                  "default_model": "claude-sonnet-5", "key_hint": "From console.anthropic.com → API keys."},
    "openai": {"label": "OpenAI", "api": "openai", "base_url": "https://api.openai.com/v1",
               "default_model": "", "key_hint": "From platform.openai.com → API keys."},
    "gemini": {"label": "Google Gemini", "api": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "default_model": "", "key_hint": "From aistudio.google.com → Get API key."},
    "openrouter": {"label": "OpenRouter", "api": "openai", "base_url": "https://openrouter.ai/api/v1",
                   "default_model": "", "key_hint": "From openrouter.ai → Keys. Reaches models from many providers."},
    "xai": {"label": "xAI (Grok)", "api": "openai", "base_url": "https://api.x.ai/v1", "default_model": "",
            "key_hint": "From console.x.ai → API keys."},
    "mistral": {"label": "Mistral", "api": "openai", "base_url": "https://api.mistral.ai/v1", "default_model": "",
                "key_hint": "From console.mistral.ai → API keys."},
    "groq": {"label": "Groq", "api": "openai", "base_url": "https://api.groq.com/openai/v1", "default_model": "",
             "key_hint": "From console.groq.com → API keys."},
    "deepseek": {"label": "DeepSeek", "api": "openai", "base_url": "https://api.deepseek.com/v1", "default_model": "",
                 "key_hint": "From platform.deepseek.com → API keys."},
    "together": {"label": "Together AI", "api": "openai", "base_url": "https://api.together.xyz/v1", "default_model": "",
                 "key_hint": "From api.together.ai → Settings → API keys."},
    "fireworks": {"label": "Fireworks AI", "api": "openai", "base_url": "https://api.fireworks.ai/inference/v1",
                  "default_model": "", "key_hint": "From fireworks.ai → Settings → API keys."},
    "cerebras": {"label": "Cerebras", "api": "openai", "base_url": "https://api.cerebras.ai/v1", "default_model": "",
                 "key_hint": "From cloud.cerebras.ai → API keys."},
    "custom": {"label": "Other OpenAI-compatible", "api": "openai", "base_url": "", "custom_url": True, "needs_key": False,
               "default_model": "", "key_hint": "The key from that service, if it needs one."},
    "local": {"label": "Local model", "api": "openai", "base_url": "", "custom_url": True, "needs_key": False,
              "local": True, "default_model": "", "key_hint": "Only if the server was started with one (LM Studio, vLLM --api-key)."},
}
KEY_PROVIDERS = tuple(k for k in PROVIDERS if k != "local")
# The model offered first when a provider is connected: the first listed (newest first) that matches.
SUGGESTED = {
    "anthropic": [r"sonnet"], "openai": [r"^gpt-\d+(\.\d+)?$", r"^gpt-\d"], "gemini": [r"^gemini-[\d.]+-pro$", r"pro"],
    "openrouter": [r"^anthropic/claude.*sonnet"], "xai": [r"^grok-\d+(?!.*(mini|image|vision))"],
    "mistral": [r"^mistral-large-latest$", r"large"], "groq": [r"70b", r"120b"], "deepseek": [r"^deepseek-chat$"],
    "together": [r"70B", r"70b"], "fireworks": [r"70b", r"deepseek"], "cerebras": [r"70b", r"120b"],
}
# Models a chat can't use (embeddings, speech, images, moderation…), left out of the model lists.
NOT_CHAT = re.compile(r"embed|whisper|tts|dall-e|davinci|babbage|moderation|transcribe|realtime|audio|imagen|image-|-image|"
                      r"veo|rerank|guard|computer-use|aqa|lyria|sora|search-preview|codex-mini", re.I)
MODEL_CACHE_S = 7 * 86400

MAX_TOOL_ROUNDS = 10
TOOL_RESULT_CHARS = 24_000
HISTORY_MESSAGES = 24
# The assistant reads one person's data; choosing people is the app's job, not the model's.
HIDDEN_TOOLS = {"list_profiles"}
# Assumed context window for hosted models answering without tools (all current ones allow far more).
HOSTED_CONTEXT_TOKENS = 32_000


class AIError(Exception):
    pass


class ToolsUnsupported(AIError):
    """The model (or the server running it) can't call tools."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _label(provider: str) -> str:
    if provider in ("local", "custom"):
        return settings.get(f"ai.label.{provider}") or PROVIDERS[provider]["label"]
    return PROVIDERS[provider]["label"]


def _resolve(provider: str, api_key: Optional[str] = None, base_url: Optional[str] = None) -> dict[str, Any]:
    """A provider's settings, with unsaved values from the form taking precedence."""
    spec = PROVIDERS[provider]
    model = settings.get(f"ai.model.{provider}") or spec["default_model"]
    return {"provider": provider, "label": _label(provider), "api": spec["api"],
            "base_url": base_url or (settings.get(f"ai.base_url.{provider}") if spec.get("custom_url") else spec["base_url"]) or "",
            "model": model, "key": api_key or settings.get(f"ai.key.{provider}"),
            "tools": tool_support(provider, model)}


def _can_configure() -> bool:
    principal = context.principal()
    return principal is None or principal.owner


def tool_support(provider: str, model: str) -> Optional[bool]:
    """Whether this model can look things up with tools: True, False, or None when not checked yet."""
    return (settings.get(f"ai.tools.{provider}") or {}).get(model)


def _remember_tools(provider: str, model: str, supported: bool) -> None:
    known = dict(settings.get(f"ai.tools.{provider}") or {})
    known[model] = supported
    settings.set(f"ai.tools.{provider}", dict(list(known.items())[-50:]))


def _configured(provider: str, cfg: dict[str, Any]) -> bool:
    spec = PROVIDERS[provider]
    return bool(cfg["model"] and cfg["base_url"] and (cfg["key"] or spec.get("needs_key") is False))


def config(include_key: bool = False) -> dict[str, Any]:
    provider = settings.get("ai.provider") or ""
    base = {"providers": _provider_list(), "agent_id": settings.get("ai.agent") or None,
            "cloud_ack": cloud_acknowledged(), "can_configure": _can_configure()}
    if provider == "agent":
        agent_id = settings.get("ai.agent") or ""
        spec = ai_agents.AGENTS.get(agent_id)
        if not spec:
            return {**base, "provider": None, "configured": False}
        model = settings.get(f"ai.agent_model.{agent_id}") or ""
        return {**base, "provider": "agent", "kind": "agent", "label": spec["label"], "model": model or "default model",
                "agent_model": model, "vendor": spec["vendor"], "plan": spec["plan"], "configured": True, "tools": True,
                "sends_elsewhere": True}
    if provider not in PROVIDERS:
        return {**base, "provider": None, "configured": False}
    cfg = _resolve(provider)
    key = cfg.pop("key")
    kind = "local" if provider == "local" else "key"
    out = {**base, **cfg, "kind": kind, "has_key": bool(key), "configured": _configured(provider, {**cfg, "key": key}),
           "model_name": next((m["name"] for m in cached_models(provider) if m["id"] == cfg["model"]), cfg["model"])}
    if cfg["base_url"] and PROVIDERS[provider].get("custom_url"):
        out["address"] = {"private": ai_local.is_private_host(urlsplit(cfg["base_url"]).hostname or "")}
    out["sends_elsewhere"] = not out.get("address", {}).get("private")     # a hosted provider, or a server on the internet
    if include_key:
        out["key"] = key
    return out


def _provider_list() -> list[dict[str, Any]]:
    out = []
    for k, v in PROVIDERS.items():
        spec = _resolve(k)
        out.append({"id": k, "label": _label(k), "default_model": v["default_model"], "key_hint": v["key_hint"],
                    "custom_url": bool(v.get("custom_url")), "needs_key": v.get("needs_key", True),
                    "saved_model": settings.get(f"ai.model.{k}") or "", "has_key": bool(spec["key"]),
                    "base_url": settings.get(f"ai.base_url.{k}") or v["base_url"], "tools": spec["tools"],
                    "connected": _configured(k, spec), "models": len(cached_models(k))})
    return out


def save_config(provider: str, model: Optional[str] = None, api_key: Optional[str] = None,
                base_url: Optional[str] = None, label: Optional[str] = None, allow_public_http: bool = False,
                agent: Optional[str] = None, make_default: bool = True) -> dict[str, Any]:
    """Saves a provider's settings. It becomes the default unless ``make_default`` is off and another AI already is."""
    if not make_default and not config().get("configured"):
        make_default = True
    if provider == "agent":
        if agent not in ai_agents.AGENTS:
            raise AIError("Choose Claude Code, Codex CLI or Gemini CLI.")
        if not make_default:
            if model is not None:
                settings.set(f"ai.agent_model.{agent}", model.strip() or None)
            return config()
        settings.set("ai.provider", "agent")
        settings.set("ai.agent", agent)
        if model is not None:
            settings.set(f"ai.agent_model.{agent}", model.strip() or None)
        return config()
    if provider not in PROVIDERS:
        raise AIError("Unknown provider.")
    spec = PROVIDERS[provider]
    if spec.get("custom_url"):
        if base_url is not None:
            try:
                url = ai_local.normalize_base_url(base_url)
            except ValueError as exc:
                raise AIError(str(exc)) from exc
            address = ai_local.describe_address(url)
            if address["needs_confirmation"] and not allow_public_http:
                raise AIError("That address is on the internet and uses plain HTTP, so your questions and health data "
                              "would travel unencrypted. Use HTTPS, a home-network or Tailscale address, or confirm to "
                              "send it anyway.")
            settings.set(f"ai.base_url.{provider}", url)
        if label is not None:
            settings.set(f"ai.label.{provider}", label.strip()[:60] or None)
        if not settings.get(f"ai.base_url.{provider}"):
            raise AIError("Enter the server's address first.")
    if make_default:
        settings.set("ai.provider", provider)
    if model is not None:
        settings.set(f"ai.model.{provider}", model.strip() or None)
        _remember_model(provider, model.strip())
    if api_key is not None:
        settings.set(f"ai.key.{provider}", api_key.strip() or None)
    return config()


def disable() -> None:
    settings.set("ai.provider", None)


def disconnect(provider: str) -> dict[str, Any]:
    """Forgets a provider's key, address and models. If it was the default, another connected one takes over."""
    if provider not in PROVIDERS:
        raise AIError("Unknown provider.")
    for key in ("key", "model", "base_url", "label", "models", "recent", "tools"):
        settings.set(f"ai.{key}.{provider}", None)
    if settings.get("ai.provider") == provider:
        settings.set("ai.provider", next((k for k in PROVIDERS if _configured(k, _resolve(k))), None))
    return config()


# Each person confirms, once, that an AI outside their network (a provider with a key, an agent's vendor, or a server
# on the internet) sees their questions and the records it looks up, under that company's terms. Asking about someone
# else who signs in needs their agreement, not the asker's; for someone who doesn't sign in (a child), whoever looks
# after them agrees.
def _ack_key(account_id: Optional[str]) -> str:
    return f"ai.cloud_ack.{account_id}" if account_id else "ai.cloud_ack"


def _whose_agreement(profile_id: Optional[str]) -> Optional[str]:
    principal = context.principal()
    mine = principal.account_id if principal else None
    if profile_id:
        theirs = accounts.for_profile(profile_id)
        if theirs:
            return theirs["id"]
    return mine


def cloud_acknowledged(profile_id: Optional[str] = None) -> bool:
    return bool(settings.get(_ack_key(_whose_agreement(profile_id))))


def acknowledge_cloud() -> None:
    principal = context.principal()
    key = _ack_key(principal.account_id if principal else None)
    if not settings.get(key):
        settings.set(key, time.time())
        with db() as conn:
            audit(conn, "user", "ai.provider_data_acknowledged", None)


CLOUD_ACK_NEEDED = ("Before Ask uses an AI outside your network, confirm that it will see your questions and the "
                    "health records it looks up to answer them.")


def _someone_elses_agreement(profile_id: str) -> Optional[str]:
    """The message when the person asked about signs in themselves and hasn't agreed (None otherwise)."""
    principal = context.principal()
    theirs = accounts.for_profile(profile_id)
    if not theirs or (principal and theirs["id"] == principal.account_id):
        return None
    return (f"{theirs['name']} hasn't agreed to an AI outside your network seeing their records. They can allow it "
            "the first time they use Ask themselves; until then, use a local model to ask about them.")


def _remember_model(provider: str, model: Optional[str]) -> None:
    """Models used with a provider recently, offered again in Ask's model menu."""
    if not model:
        return
    recent = [m for m in settings.get(f"ai.recent.{provider}") or [] if m != model]
    settings.set(f"ai.recent.{provider}", [model, *recent][:6])


def is_set_up(provider: str, agent: Optional[str] = None) -> bool:
    """Already set up by an owner, so anyone may switch to it (no new key or address)."""
    if provider == "agent":
        return (agent or "") in ai_agents.AGENTS
    if provider not in PROVIDERS:
        return False
    spec = _resolve(provider)
    return bool(spec["base_url"]) if provider == "local" else _configured(provider, spec)


# The model menu opens often and what it lists changes rarely: what was found is reused for a while, and once it's older
# the menu shows it anyway while it's looked up again in the background.
MENU_FRESH_S = 60
AGENTS_FRESH_S = 600
_local_found: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_background: set[asyncio.Task] = set()


def _in_background(coro: Any) -> None:
    task = asyncio.ensure_future(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _check_local(base_url: str, key: Optional[str]) -> list[dict[str, Any]]:
    try:
        found = [{"id": m["id"], "name": m["id"]} for m in (await ai_local.check(base_url, key))["models"]]
    except ValueError:
        found = []
    _local_found[base_url] = (time.time(), found)
    return found


async def _local_models(base_url: str, key: Optional[str]) -> list[dict[str, Any]]:
    """The local server's models, for the menu."""
    hit = _local_found.get(base_url)
    if hit and time.time() - hit[0] > MENU_FRESH_S:
        _in_background(_check_local(base_url, key))
    return hit[1] if hit else await _check_local(base_url, key)


async def _agents() -> dict[str, Any]:
    """The AI apps installed and signed in here, for the menu."""
    last = ai_agents.last_detected()
    if last and time.time() - last[0] > AGENTS_FRESH_S:
        _in_background(asyncio.to_thread(ai_agents.detect, True))
    return last[1] if last else await asyncio.to_thread(ai_agents.detect)


async def choices() -> dict[str, Any]:
    """What Ask's model menu offers: every AI that's connected, grouped, with all of its models (the saved and recently
    used ones first), plus the one in use."""
    cfg = config()
    groups: list[dict[str, Any]] = []

    def options(provider: str, listed: list[dict[str, Any]], pinned: list[str]) -> list[dict[str, Any]]:
        names = {m["id"]: m for m in listed}
        ids = list(dict.fromkeys([*[m for m in pinned if m], *names]))
        return [{"provider": provider, "model": m, "name": names.get(m, {}).get("name") or m,
                 "tools": tool_support(provider, m) if tool_support(provider, m) is not None else names.get(m, {}).get("tools"),
                 "pinned": m in pinned} for m in ids]

    local = _resolve("local")
    key_providers = [p for p in KEY_PROVIDERS if _configured(p, _resolve(p))]
    # Finding what's there (the local server's models, AI apps signed in here) takes seconds: all at once, and from
    # what was found a moment ago when there is that.
    found, listed, agents = await asyncio.gather(
        _local_models(local["base_url"], local["key"]) if local["base_url"] else asyncio.sleep(0, []),
        asyncio.gather(*(refresh_models(p) for p in key_providers)),
        _agents(),
    )
    if local["base_url"]:
        private = ai_local.is_private_host(urlsplit(local["base_url"]).hostname or "")
        groups.append({"id": "local", "kind": "local", "label": local["label"], "private": private, "reachable": bool(found),
                       "options": options("local", found, [local["model"]] if not found else
                                          [m for m in [local["model"], *(settings.get("ai.recent.local") or [])] if m in {f["id"] for f in found}])})
    for provider, models in zip(key_providers, listed):
        spec = _resolve(provider)
        groups.append({"id": provider, "kind": "key", "label": spec["label"], "private": False,
                       "options": options(provider, models, [spec["model"], *(settings.get(f"ai.recent.{provider}") or [])])})
    for agent in agents.get("agents", []):
        if not agent["installed"] or agent["signed_in"] is False:
            continue
        spec = ai_agents.AGENTS[agent["id"]]
        saved = settings.get(f"ai.agent_model.{agent['id']}") or ""
        models = list(dict.fromkeys(["", *([saved] if saved else []), *spec.get("models", [])]))
        groups.append({"id": f"agent:{agent['id']}", "kind": "agent", "agent": agent["id"], "label": agent["label"],
                       "vendor": agent["vendor"], "plan": agent["plan"],
                       "options": [{"provider": "agent", "agent": agent["id"], "model": m,
                                    "name": m[:1].upper() + m[1:] if m else "Default model", "pinned": True} for m in models]})
    current = None
    if cfg.get("configured"):
        current = ({"provider": "agent", "agent": cfg["agent_id"], "model": cfg["agent_model"] or ""}
                   if cfg["provider"] == "agent" else {"provider": cfg["provider"], "model": cfg["model"]})
    return {"current": current, "groups": groups}


# ---------------------------------------------------------------------------
# Talking to models
#
# The assistant keeps one conversation format and each adapter translates it to its provider's API:
#   {"role": "user" | "assistant", "content": str | [parts]}   parts: {"type": "text" | "image" | "pdf", ...}
#   {"role": "assistant", "raw": ...}      a reply with tool calls, sent back to the provider unchanged
#   {"role": "tools", "results": [...]}    tool results: {"id", "name", "content", "error"}
# ---------------------------------------------------------------------------

@dataclass
class Reply:
    text: str
    calls: list[dict[str, Any]] = field(default_factory=list)    # {"id", "name", "args"}
    raw: Any = None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0))


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:300]
    return str(err or body)[:300]


async def _request(client: httpx.AsyncClient, cfg: dict[str, Any], method: str, path: str, **kwargs: Any) -> Any:
    try:
        resp = await client.request(method, f"{cfg['base_url']}{path}", **kwargs)
    except httpx.HTTPError as exc:
        raise _unreachable(cfg, exc) from exc
    if resp.status_code < 400:
        return resp.json()
    _raise_for(resp, cfg)


def _unreachable(cfg: dict[str, Any], exc: Exception) -> AIError:
    if PROVIDERS[cfg["provider"]].get("custom_url"):
        return AIError(f"Couldn't reach {cfg['label']} at {cfg['base_url']} ({exc.__class__.__name__}). Check that it's "
                       "running and that this computer can reach it.")
    return AIError(f"Couldn't reach {cfg['label']} ({exc.__class__.__name__}). Check this computer's internet connection.")


def _raise_for(resp: httpx.Response, cfg: dict[str, Any]) -> None:
    """The provider's error (the response body must have been read) as the message the person sees."""
    detail = _error_text(resp)
    lowered = detail.lower()
    if resp.status_code in (401, 403):
        if not cfg.get("key"):
            raise AIError(f"{cfg['label']} wants an API key ({detail}).")
        raise AIError(f"{cfg['label']} rejected the API key ({detail}).")
    if "tool" in lowered and ("support" in lowered or "not" in lowered):
        raise ToolsUnsupported(f"Model '{cfg['model']}' can't use tools ({detail}).")
    if resp.status_code == 404 and "model" in lowered:
        raise AIError(f"Model '{cfg['model']}' was not found at {cfg['label']} ({detail}).")
    raise AIError(f"{cfg['label']} returned an error ({resp.status_code}): {detail}")


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


# Anthropic Messages API

def _anthropic_headers(cfg: dict[str, Any]) -> dict[str, str]:
    return {"x-api-key": cfg.get("key") or "", "anthropic-version": "2023-06-01"}


def _anthropic_part(part: dict[str, Any]) -> dict[str, Any]:
    if part["type"] == "image":
        return {"type": "image", "source": {"type": "base64", "media_type": part["media_type"], "data": part["data"]}}
    if part["type"] == "pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": part["data"]}}
    return {"type": "text", "text": part["text"]}


def _anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        if m["role"] == "tools":
            out.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": r["id"], "content": r["content"],
                                                     "is_error": r["error"]} for r in m["results"]]})
        elif "raw" in m:
            out.append({"role": "assistant", "content": m["raw"]})
        else:
            content = m["content"]
            out.append({"role": m["role"], "content": [_anthropic_part(p) for p in content] if isinstance(content, list) else content})
    return out


def _anthropic_body(cfg: dict[str, Any], system: str, messages: list[dict[str, Any]],
                    tools: Optional[list[dict[str, Any]]], max_tokens: int) -> dict[str, Any]:
    body: dict[str, Any] = {"model": cfg["model"], "max_tokens": max_tokens, "system": system,
                            "messages": _anthropic_messages(messages)}
    if tools:
        body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in tools]
    return body


async def _anthropic(client: httpx.AsyncClient, cfg: dict[str, Any], system: str, messages: list[dict[str, Any]],
                     tools: Optional[list[dict[str, Any]]], max_tokens: int) -> Reply:
    res = await _request(client, cfg, "POST", "/v1/messages", json=_anthropic_body(cfg, system, messages, tools, max_tokens),
                         headers=_anthropic_headers(cfg))
    return _anthropic_reply(res.get("content") or [])


def _anthropic_reply(blocks: list[dict[str, Any]]) -> Reply:
    return Reply(text="".join(b.get("text", "") for b in blocks if b.get("type") == "text"),
                 calls=[{"id": b["id"], "name": b["name"], "args": b.get("input") or {}} for b in blocks if b.get("type") == "tool_use"],
                 raw=blocks)


# OpenAI-compatible Chat Completions (OpenAI, Gemini, OpenRouter)

def _openai_part(part: dict[str, Any]) -> dict[str, Any]:
    if part["type"] == "image":
        return {"type": "image_url", "image_url": {"url": f"data:{part['media_type']};base64,{part['data']}"}}
    if part["type"] == "pdf":
        return {"type": "file", "file": {"filename": part["filename"], "file_data": f"data:application/pdf;base64,{part['data']}"}}
    return {"type": "text", "text": part["text"]}


def _openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "tools":
            out += [{"role": "tool", "tool_call_id": r["id"], "content": r["content"]} for r in m["results"]]
        elif "raw" in m:
            out.append(m["raw"])
        else:
            content = m["content"]
            out.append({"role": m["role"], "content": [_openai_part(p) for p in content] if isinstance(content, list) else content})
    return out


def _openai_body(cfg: dict[str, Any], system: str, messages: list[dict[str, Any]],
                 tools: Optional[list[dict[str, Any]]]) -> dict[str, Any]:
    body: dict[str, Any] = {"model": cfg["model"], "messages": _openai_messages(system, messages)}
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                           "parameters": t["schema"]}} for t in tools]
    return body


def _openai_headers(cfg: dict[str, Any]) -> dict[str, str]:
    headers = {"authorization": f"Bearer {cfg['key']}"} if cfg.get("key") else {}
    if cfg["provider"] == "openrouter":
        headers.update({"HTTP-Referer": "https://syntropylabs.io", "X-Title": "Syntropy Health"})
    return headers


async def _openai(client: httpx.AsyncClient, cfg: dict[str, Any], system: str, messages: list[dict[str, Any]],
                  tools: Optional[list[dict[str, Any]]], max_tokens: int) -> Reply:
    res = await _request(client, cfg, "POST", "/chat/completions", json=_openai_body(cfg, system, messages, tools),
                         headers=_openai_headers(cfg))
    return _openai_reply(((res.get("choices") or [{}])[0].get("message")) or {})


def _openai_reply(msg: dict[str, Any]) -> Reply:
    tool_calls = msg.get("tool_calls") or []
    return Reply(text=msg.get("content") or "",
                 calls=[{"id": c.get("id"), "name": (c.get("function") or {}).get("name") or "",
                         "args": _arguments((c.get("function") or {}).get("arguments"))} for c in tool_calls],
                 # Tool calls go back exactly as received: some providers attach data to them (e.g. Gemini's signatures).
                 raw={"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})


async def _complete(client: httpx.AsyncClient, cfg: dict[str, Any], system: str, messages: list[dict[str, Any]],
                    tools: Optional[list[dict[str, Any]]] = None, max_tokens: int = 4096) -> Reply:
    adapter = _anthropic if cfg["api"] == "anthropic" else _openai
    reply = await adapter(client, cfg, system, messages, tools, max_tokens)
    # Some reasoning models reached through OpenRouter inline their thinking.
    reply.text = re.sub(r"<think>.*?</think>", "", reply.text, flags=re.S).strip()
    return reply


# Streaming. Ask shows the answer as it's written: the adapters below send the same requests with "stream": true and
# yield ("delta", text) as text arrives, then ("reply", Reply) with exactly what the non-streaming adapters return. A
# server that answers with plain JSON instead (it ignores "stream") is read the same way as without streaming.

async def _sse(resp: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """The JSON payloads of a server-sent event stream."""
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except ValueError:
            continue
        if isinstance(payload, dict):
            yield payload


async def _stream_request(client: httpx.AsyncClient, cfg: dict[str, Any], path: str, body: dict[str, Any],
                          headers: dict[str, str], events, whole) -> AsyncIterator[tuple[str, Any]]:
    """POSTs a streaming request. `events(payloads)` turns the stream into ("delta" | "reply", …); `whole(json)` reads a
    plain JSON answer."""
    try:
        async with client.stream("POST", f"{cfg['base_url']}{path}", json={**body, "stream": True}, headers=headers) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                _raise_for(resp, cfg)
            if "text/event-stream" not in resp.headers.get("content-type", ""):
                await resp.aread()
                yield "reply", whole(resp.json())
                return
            async for item in events(_sse(resp)):
                yield item
    except httpx.HTTPError as exc:
        raise _unreachable(cfg, exc) from exc


async def _anthropic_events(payloads: AsyncIterator[dict[str, Any]]) -> AsyncIterator[tuple[str, Any]]:
    blocks: dict[int, dict[str, Any]] = {}
    partial: dict[int, str] = {}
    async for ev in payloads:
        kind = ev.get("type")
        if kind == "content_block_start":
            blocks[ev["index"]] = dict(ev.get("content_block") or {})
        elif kind == "content_block_delta":
            block, delta = blocks.setdefault(ev["index"], {"type": "text", "text": ""}), ev.get("delta") or {}
            if delta.get("type") == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
                yield "delta", delta.get("text", "")
            elif delta.get("type") == "input_json_delta":
                partial[ev["index"]] = partial.get(ev["index"], "") + delta.get("partial_json", "")
            elif delta.get("type") == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif delta.get("type") == "signature_delta":
                block["signature"] = delta.get("signature", "")
        elif kind == "content_block_stop" and ev.get("index") in partial:
            blocks[ev["index"]]["input"] = _arguments(partial.pop(ev["index"]))
        elif kind == "error":
            raise AIError(f"Anthropic stopped with an error: {(ev.get('error') or {}).get('message') or ev}")
    yield "reply", _anthropic_reply([blocks[i] for i in sorted(blocks)])


def _merge_call(into: dict[str, Any], part: dict[str, Any]) -> None:
    """Adds one streamed piece of a tool call: the arguments arrive in fragments, everything else once."""
    for key, value in part.items():
        if key == "index":
            continue
        if key == "arguments" and isinstance(value, str):
            into[key] = into.get(key, "") + value
        elif isinstance(value, dict):
            _merge_call(into.setdefault(key, {}), value)
        elif value not in (None, "") and not into.get(key):
            into[key] = value


async def _openai_events(payloads: AsyncIterator[dict[str, Any]]) -> AsyncIterator[tuple[str, Any]]:
    text, calls = "", {}
    async for chunk in payloads:
        if chunk.get("error"):
            err = chunk["error"]
            raise AIError(f"The model stopped with an error: {err.get('message') if isinstance(err, dict) else err}")
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str) and delta["content"]:
                text += delta["content"]
                yield "delta", delta["content"]
            for i, part in enumerate(delta.get("tool_calls") or []):
                _merge_call(calls.setdefault(part.get("index", i), {}), part)
    yield "reply", _openai_reply({"content": text, "tool_calls": [calls[i] for i in sorted(calls)]})


class _Visible:
    """What of a streamed reply may be shown so far: reasoning some models inline as <think>…</think> never is."""
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.raw, self.sent = "", ""

    def add(self, piece: str) -> Optional[str]:
        """The new text to show after `piece` arrives ("" when nothing yet), or None if what was shown must be redrawn."""
        self.raw += piece
        shown = re.sub(r"<think>.*?</think>", "", self.raw, flags=re.S)
        if self.OPEN in shown:
            shown = shown[:shown.index(self.OPEN)]
        # Hold back a tag that may be arriving ("<thi").
        for n in range(len(self.OPEN) - 1, 0, -1):
            if shown.endswith(self.OPEN[:n]):
                shown = shown[:-n]
                break
        shown = shown.lstrip()
        if not shown.startswith(self.sent):
            self.sent = shown
            return None
        new, self.sent = shown[len(self.sent):], shown
        return new


async def _complete_stream(client: httpx.AsyncClient, cfg: dict[str, Any], system: str, messages: list[dict[str, Any]],
                           tools: Optional[list[dict[str, Any]]] = None,
                           max_tokens: int = 4096) -> AsyncIterator[tuple[str, Any]]:
    """Like _complete, streamed: ("delta", text) to show, ("reset", None) when what was shown must be redrawn from
    later deltas, then ("reply", Reply)."""
    if cfg["api"] == "anthropic":
        stream = _stream_request(client, cfg, "/v1/messages", _anthropic_body(cfg, system, messages, tools, max_tokens),
                                 _anthropic_headers(cfg), _anthropic_events, lambda res: _anthropic_reply(res.get("content") or []))
    else:
        stream = _stream_request(client, cfg, "/chat/completions", _openai_body(cfg, system, messages, tools),
                                 _openai_headers(cfg), _openai_events,
                                 lambda res: _openai_reply(((res.get("choices") or [{}])[0].get("message")) or {}))
    visible = _Visible()
    try:
        first = await anext(stream)
    except AIError as exc:
        # A server that can't stream (some local servers with tools): the same request without streaming.
        if "stream" not in str(exc).lower():
            raise
        yield "reply", await _complete(client, cfg, system, messages, tools, max_tokens)
        return

    async def replay() -> AsyncIterator[tuple[str, Any]]:
        yield first
        async for item in stream:
            yield item

    async for kind, value in replay():
        if kind == "delta":
            new = visible.add(value)
            if new is None:
                yield "reset", None
                if visible.sent:
                    yield "delta", visible.sent
            elif new:
                yield "delta", new
        else:
            value.text = re.sub(r"<think>.*?</think>", "", value.text, flags=re.S).strip()
            yield "reply", value


def _stamp(value: Any) -> float:
    """A model's creation time as seconds (providers give epoch seconds or ISO dates), 0 when unknown."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _chat_models(provider: str, items: list[Any]) -> list[dict[str, Any]]:
    """The models a chat can use, newest first when the provider says when each was made, with readable names."""
    out: dict[str, dict[str, Any]] = {}
    for m in items:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or m.get("name") or "").removeprefix("models/")
        if not mid or NOT_CHAT.search(mid):
            continue
        if provider == "openai" and not re.match(r"(gpt-|o\d|chatgpt-)", mid):
            continue
        name = m.get("display_name") or m.get("name") or mid
        params = m.get("supported_parameters")
        out[mid] = {"id": mid, "name": str(name).removeprefix("models/"), "created": _stamp(m.get("created_at") or m.get("created")),
                    **({"tools": "tools" in params} if isinstance(params, list) else {})}
    models = list(out.values())
    if sum(1 for m in models if m["created"]) >= len(models) / 2:
        models.sort(key=lambda m: (-m["created"], m["id"]))
    else:
        models.sort(key=lambda m: m["id"].lower())
    return models


def suggest_model(provider: str, models: list[dict[str, Any]]) -> str:
    for part in SUGGESTED.get(provider, []):
        found = next((m["id"] for m in models if re.search(part, m["id"])), None)
        if found:
            return found
    return models[0]["id"] if models else ""


def cached_models(provider: str) -> list[dict[str, Any]]:
    """The provider's models as last listed (kept a week), for the model menus."""
    saved = settings.get(f"ai.models.{provider}") or {}
    return saved.get("models") or []


async def refresh_models(provider: str) -> list[dict[str, Any]]:
    """The saved provider's models, listed again when the list is more than a week old (the old list on failure)."""
    saved = settings.get(f"ai.models.{provider}") or {}
    if saved.get("models") and time.time() - saved.get("at", 0) < MODEL_CACHE_S:
        return saved["models"]
    try:
        return (await list_models(provider))["models"]
    except AIError:
        return saved.get("models") or []


async def list_models(provider: Optional[str] = None, api_key: Optional[str] = None,
                      base_url: Optional[str] = None) -> dict[str, Any]:
    """The provider's chat models and the one to suggest, using unsaved values from the form when given."""
    provider = provider or settings.get("ai.provider")
    if provider not in PROVIDERS:
        raise AIError("Choose a provider first.")
    if base_url:
        try:
            base_url = ai_local.normalize_base_url(base_url)
        except ValueError as exc:
            raise AIError(str(exc)) from exc
    cfg = _resolve(provider, api_key, base_url)
    if not cfg["base_url"]:
        raise AIError("Enter the server's address first.")
    if not cfg["key"] and PROVIDERS[provider].get("needs_key", True):
        raise AIError("Paste your API key first.")
    async with _client() as client:
        if cfg["api"] == "anthropic":
            data = await _request(client, cfg, "GET", "/v1/models", params={"limit": 100}, headers=_anthropic_headers(cfg))
        else:
            data = await _request(client, cfg, "GET", "/models",
                                  headers={"authorization": f"Bearer {cfg['key']}"} if cfg["key"] else {})
    items = data.get("data") if isinstance(data, dict) else data
    models = _chat_models(provider, items or [])
    if not base_url or base_url == _resolve(provider)["base_url"]:
        settings.set(f"ai.models.{provider}", {"at": time.time(), "models": models[:500]})
    return {"models": models, "suggested": suggest_model(provider, models)}


TEST_PROMPT = "This is a connection test. Call the get_health_summary tool once, with no arguments."


async def check_tools(client: httpx.AsyncClient, cfg: dict[str, Any]) -> bool:
    """Whether the model uses tools when asked to, remembered for this model. Sends the real tool list, so a provider
    that rejects one of them fails here rather than in the middle of a question."""
    try:
        reply = await _complete(client, cfg, "You are being tested. Follow the instruction exactly.",
                                [{"role": "user", "content": TEST_PROMPT}], _tools(), max_tokens=200)
        supported = bool(reply.calls)
    except ToolsUnsupported:
        supported = False
    _remember_tools(cfg["provider"], cfg["model"], supported)
    return supported


async def test_connection() -> dict[str, Any]:
    cfg = config(include_key=True)
    if not cfg.get("configured"):
        raise AIError("Finish setting up the assistant first.")
    started = time.time()
    if cfg["provider"] == "agent":
        answer = ""
        try:
            principal = context.principal()
            own = (principal.home if principal else None) or profiles.default_profile()["id"]
            async for event in ai_agents.run(cfg["agent_id"], own,
                                             "You are being tested. Reply with exactly: OK",
                                             [{"role": "user", "content": "Reply with exactly: OK"}], cfg["agent_model"] or None):
                if event["type"] == "answer":
                    answer = event["text"]
        except ai_agents.AgentError as exc:
            raise AIError(str(exc)) from exc
        return {"ok": True, "reply": answer[:100], "seconds": round(time.time() - started, 1), "model": cfg["label"],
                "tools": True}
    async with _client() as client:
        tools = await check_tools(client, cfg)
        reply = "" if tools else (await _complete(client, cfg, "Reply with exactly: OK", [{"role": "user", "content": "Ping"}],
                                                  None, max_tokens=16)).text
    return {"ok": True, "reply": reply[:100], "seconds": round(time.time() - started, 1), "model": cfg["model"], "tools": tools}


# ---------------------------------------------------------------------------
# The assistant
# ---------------------------------------------------------------------------

TOOL_LABELS = {
    "get_health_summary": "Reading the health summary",
    "search_records": "Searching records",
    "get_record": "Opening a record",
    "list_lab_tests": "Listing lab tests",
    "get_lab_trend": "Looking up lab results",
    "list_wearable_metrics": "Listing wearable metrics",
    "get_daily_metric": "Checking daily values",
    "list_workouts": "Reviewing workouts",
    "get_health_journal": "Reading the health journal",
    "get_insights": "Looking for patterns",
    "compare_measures": "Comparing",
    "get_sleep_nights": "Reviewing sleep night by night",
    "get_training_summary": "Reviewing training load",
    "get_goals": "Checking goals",
}


def _tools() -> list[dict[str, Any]]:
    out = []
    for name, (_, desc, props, req) in mcp_server.TOOLS.items():
        if name in HIDDEN_TOOLS:
            continue
        props = {k: v for k, v in props.items() if k != "profile"}
        out.append({"name": name, "description": desc,
                    "schema": {"type": "object", "properties": props, "required": [r for r in req if r != "profile"]}})
    return out


def _tool_label(name: str, args: dict[str, Any]) -> str:
    base = TOOL_LABELS.get(name, name.replace("_", " ").capitalize())
    detail = (args.get("test") or args.get("metric") or args.get("query") or args.get("category")
              or (f"{args['a']} and {args['b']}" if args.get("a") and args.get("b") else "") or args.get("workout_type") or "")
    if args.get("days"):
        detail = f"{detail}, {args['days']} days" if detail else f"{args['days']} days"
    return f"{base} ({str(detail).replace('_', ' ')})" if detail else base


def _run_tool(name: str, args: dict[str, Any], profile_id: str) -> tuple[str, bool]:
    if name in HIDDEN_TOOLS:
        return f"Unknown tool: {name}", True
    token = mcp_server.SCOPED_PROFILE.set(profile_id)
    try:
        text, is_error = mcp_server.call_tool(name, {k: v for k, v in args.items() if k != "profile"})
    finally:
        mcp_server.SCOPED_PROFILE.reset(token)
    if len(text) > TOOL_RESULT_CHARS:
        text = text[:TOOL_RESULT_CHARS] + "… [truncated: ask for a shorter date range or fewer items]"
    return text, is_error


def system_prompt(profile: dict[str, Any], snapshot: Optional[str] = None) -> str:
    """The assistant's instructions. With ``snapshot`` (for models that can't use tools) it answers from that instead."""
    tz = settings.timezone_name()
    try:
        today = datetime.now(ZoneInfo(tz))
    except Exception:  # noqa: BLE001
        today = datetime.now().astimezone()
    who = profile["name"]
    facts = [f"relationship to the account owner: {profile.get('relationship') or 'self'}"]
    if profile.get("birth_date"):
        facts.append(f"born {profile['birth_date']}")
    if profile.get("sex"):
        facts.append(f"sex {profile['sex']}")
    return f"""You are the health assistant inside Syntropy Health, a private, self-hosted personal health record. \
You help {who} ({'; '.join(facts)}) understand their own health data and act on it: answering questions, spotting \
trends, and coaching on sleep, training, recovery and habits.

Today is {today:%A, %B} {today.day}, {today.year} ({tz}).

How to work:
{LOOKUP_SNAPSHOT if snapshot else LOOKUP_TOOLS}
- Accuracy matters more than completeness. Only state numbers, dates and flags exactly as they appear in the \
data. Use the reference range and flag the source reported; when the range is "not reported by the lab", say \
so and don't give a typical range from memory. Never infer a finding (for example anemia or infection) the data \
doesn't show.
- For "anything out of range", use out_of_range_recent_labs and the flag on each result.
- Cite specifics: values with units, dates, and where they came from (the source name).
- Reply in the same language the person writes in.
- Be concise and practical. Use short paragraphs, bullet lists and small markdown tables when comparing numbers.
- Coaching: give specific, realistic next steps tied to their data, and explain the reasoning briefly.
- Daily wearable values already resolve overlapping devices (one source per day; "Combined" is the Apple Watch and \
iPhone merged). Sources marked "(simulated)" are demo data: say so and don't present them as real measurements.
- If the health summary reports an identity_conflict, the clinical records describe more than one person. Say so \
briefly, then still answer, naming which person (and source) each clinical result belongs to. Wearable data comes \
from the person's own devices and isn't affected.

You are not a clinician. Explain and summarize; don't diagnose, prescribe, or advise changing medication. When a \
value is out of range, changing quickly, or a symptom sounds urgent, say so plainly and suggest contacting their \
care team (or emergency services for emergencies).{SNAPSHOT_HEAD + snapshot if snapshot else ""}"""


LOOKUP_TOOLS = ("- Always look things up with the tools before answering; never guess values. Start with get_health_summary "
                "when you need context. Fetch only the date ranges you need.")
LOOKUP_SNAPSHOT = ("- You can't look anything up. All you know about their data is the snapshot at the end. If the answer "
                   "isn't in it, say what's missing and that a model that can look things up (Settings → AI) or "
                   "a narrower question would help. Never guess values.")
SNAPSHOT_HEAD = "\n\nHealth data snapshot (JSON, one section per line):\n"


def _safe(fn: Any) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a snapshot without one section is still useful
        return None


def summary_context(profile_id: str, budget_chars: int) -> str:
    """A compact snapshot of one person's data for models that can't call tools, most useful first, within budget."""
    token = mcp_server.SCOPED_PROFILE.set(profile_id)
    try:
        s = mcp_server.t_health_summary({})
    finally:
        mcp_server.SCOPED_PROFILE.reset(token)
    sections: list[tuple[str, Any]] = [("person", s["profile"])]
    if s["identity_conflict"]:
        sections += [("identity_conflict", s["identity_conflict"]), ("patient_records_name", s["patient"])]
    sections += [
        ("out_of_range_recent_labs", s["out_of_range_recent_labs"]), ("active_conditions", s["active_conditions"]),
        ("current_medications", s["active_medications"]), ("allergies", s["allergies"]),
        ("wearables_latest_and_7_30_day_averages", s["wearable_headline_30d"]), ("latest_vitals", s["latest_vitals"]),
        ("patterns_across_sources", _safe(lambda: [{k: i[k] for k in ("title", "text")} for i in insights.insights(profile_id, 10)])),
        ("workouts_last_30_days", activity.workout_summary(profile_id, 30)),
        ("health_journal_last_30_days", activity.event_summary(profile_id, 30)),
        ("recent_labs", s["recent_labs"]), ("recent_visits", s["recent_visits"]),
        ("connected_sources", s["connected_sources"]), ("record_counts", s["record_counts"]),
    ]
    lines: list[str] = []
    left_out: list[str] = []
    used = 0
    for name, value in sections:
        if value in (None, [], {}):
            continue
        line = f"{name}: {json.dumps(value, default=str, ensure_ascii=False, separators=(',', ':'))}"
        room = budget_chars - used
        if len(line) > room and isinstance(value, list):
            while value and len(line) > room:            # keep the first (most recent) items that fit
                value = value[:max(1, len(value) * 2 // 3)] if len(value) > 1 else []
                line = f"{name} (first {len(value)}): {json.dumps(value, default=str, ensure_ascii=False, separators=(',', ':'))}"
        if not value or len(line) > room:
            left_out.append(name)
            continue
        lines.append(line)
        used += len(line) + 1
    if left_out:
        lines.append(f"not_included_for_space: {', '.join(left_out)}")
    return "\n".join(lines)


async def _context_budget(cfg: dict[str, Any], turns: list[dict[str, Any]]) -> int:
    """Characters of snapshot the model's context window leaves room for (about 3 characters per token, cautiously)."""
    if cfg["provider"] == "local":
        tokens = await ai_local.context_tokens(cfg["base_url"], cfg["model"], cfg.get("key"))
    else:
        tokens = HOSTED_CONTEXT_TOKENS
    history = sum(len(t["content"]) for t in turns) // 3
    free = tokens - 1500 - 1200 - history      # the instructions, room for the reply, the conversation
    return max(2500, min(80_000, free * 3))


def _trim_for_small_context(turns: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    """The latest turns that fit, always keeping the question."""
    kept: list[dict[str, Any]] = []
    total = 0
    for t in reversed(turns):
        if kept and total + len(t["content"]) > max_chars:
            break
        kept.insert(0, t)
        total += len(t["content"])
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    return kept or turns[-1:]


async def _answer_with_tools(client: httpx.AsyncClient, cfg: dict[str, Any], system: str, turns: list[dict[str, Any]],
                             profile_id: str, used: list[str]) -> AsyncIterator[dict[str, Any]]:
    tools = _tools()
    messages: list[dict[str, Any]] = list(turns)
    for _ in range(MAX_TOOL_ROUNDS):
        reply, shown = None, False
        async for kind, value in _complete_stream(client, cfg, system, messages, tools):
            if kind == "reply":
                reply = value
            else:
                shown = shown or kind == "delta"
                yield {"type": kind} if kind == "reset" else {"type": "delta", "text": value}
        if not reply.calls:
            yield {"type": "answer", "text": reply.text}
            return
        if shown:
            yield {"type": "reset"}     # "Let me look that up…": the answer comes after the lookups
        messages.append({"role": "assistant", "raw": reply.raw})
        results = []
        for call in reply.calls:
            yield {"type": "tool", "name": call["name"], "label": _tool_label(call["name"], call["args"])}
            text, is_error = _run_tool(call["name"], call["args"], profile_id)
            used.append(call["name"])
            results.append({"id": call["id"], "name": call["name"], "content": text, "error": is_error})
        messages.append({"role": "tools", "results": results})
    yield {"type": "answer", "text": "I looked up a lot without reaching an answer. Try a narrower question."}


async def _answer_from_snapshot(client: httpx.AsyncClient, cfg: dict[str, Any], profile: dict[str, Any],
                                turns: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    budget = await _context_budget(cfg, turns)
    turns = _trim_for_small_context(turns, max(2000, budget // 3))
    yield {"type": "tool", "name": "snapshot", "label": "Reading a summary of your data"}
    snapshot = summary_context(profile["id"], budget)
    reply = None
    async for kind, value in _complete_stream(client, cfg, system_prompt(profile, snapshot), turns):
        if kind == "reply":
            reply = value
        else:
            yield {"type": kind} if kind == "reset" else {"type": "delta", "text": value}
    yield {"type": "answer", "text": reply.text}


async def chat(profile_id: str, history: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """Answers with the chosen AI, yielding progress events and finally the answer."""
    cfg = config(include_key=True)
    if not cfg.get("configured"):
        yield {"type": "error", "message": "Choose an AI for the assistant in Settings → AI first."}
        return
    profile = next((p for p in profiles.list_profiles() if p["id"] == profile_id), None)
    if not profile:
        yield {"type": "error", "message": "Unknown profile."}
        return
    if cfg.get("sends_elsewhere") and not cloud_acknowledged(profile_id):
        other = _someone_elses_agreement(profile_id)
        yield {"type": "error", "message": other or CLOUD_ACK_NEEDED, "code": "cloud_ack_other" if other else "cloud_ack"}
        return
    turns = [{"role": m["role"], "content": str(m["content"])[:20_000]} for m in history[-HISTORY_MESSAGES:]
             if m.get("role") in ("user", "assistant") and m.get("content")]
    if not turns or turns[-1]["role"] != "user":
        yield {"type": "error", "message": "Ask a question first."}
        return
    used: list[str] = []
    started = time.time()
    mode = "agent" if cfg["provider"] == "agent" else ("snapshot" if cfg.get("tools") is False else "tools")
    try:
        if mode == "agent":
            async for event in ai_agents.run(cfg["agent_id"], profile_id, system_prompt(profile), turns,
                                             cfg["agent_model"] or None):
                if event["type"] == "tool":
                    used.append(event["name"])
                    event = {"type": "tool", "name": event["name"], "label": _tool_label(event["name"], event["args"])}
                yield event
        else:
            async with _client() as client:
                if mode == "tools":
                    try:
                        async for event in _answer_with_tools(client, cfg, system_prompt(profile), turns, profile_id, used):
                            yield event
                    except ToolsUnsupported:
                        if used:
                            raise
                        _remember_tools(cfg["provider"], cfg["model"], False)
                        mode = "snapshot"
                        yield {"type": "note", "text": f"{cfg['model']} can't look things up, so it's answering from a "
                                                       "summary of your data."}
                if mode == "snapshot":
                    async for event in _answer_from_snapshot(client, cfg, profile, turns):
                        yield event
                elif cfg.get("tools") is None and used:
                    _remember_tools(cfg["provider"], cfg["model"], True)
    except (AIError, ai_agents.AgentError) as exc:
        yield {"type": "error", "message": str(exc)}
    finally:
        with db() as conn:
            audit(conn, "ai", "ai.chat", {"provider": f"agent:{cfg['agent_id']}" if mode == "agent" else cfg.get("provider"),
                                          "model": cfg.get("model"), "mode": mode, "tools": used,
                                          "seconds": round(time.time() - started, 1)}, profile_id)
    yield {"type": "done", "model": cfg["model"], "provider": cfg["label"], "mode": mode}


# ---------------------------------------------------------------------------
# Reading lab results from a report (PDF or photo)
# ---------------------------------------------------------------------------

EXTRACT_PROMPT = """Extract every lab test result from this lab report. Reply with only a JSON object, no prose:
{"collected": "YYYY-MM-DD or null", "lab": "laboratory or clinic name or null",
 "results": [{"test": "test name as printed", "loinc": "LOINC code if you are certain, else null",
   "value": number or null, "value_text": "the result as printed if it is not a plain number (e.g. 'Negative', '<0.5')",
   "unit": "unit as printed or null", "ref_low": number or null, "ref_high": number or null,
   "ref_text": "reference range as printed or null", "flag": "H, L, or null as printed", "date": "YYYY-MM-DD or null"}]}
Copy numbers exactly as printed. Skip headers, patient details and comments. If nothing is readable, return {"results": []}."""

MAX_REPORT_BYTES = 15 * 1024 * 1024


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:30]).strip()
    except Exception:  # noqa: BLE001 - scanned or unusual PDFs fall back to sending the file itself
        return ""


def _parse_json(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise AIError("The model didn't return any results. Try a clearer scan, or enter the results by hand.")
    try:
        data = json.loads(match.group(0))
    except ValueError as exc:
        raise AIError("The model's reply wasn't valid JSON. Try again, or enter the results by hand.") from exc
    if not isinstance(data.get("results"), list):
        raise AIError("The model didn't return any results.")
    return data


async def extract_lab_results(data: bytes, content_type: str, filename: str) -> dict[str, Any]:
    cfg = config(include_key=True)
    if not cfg.get("configured"):
        raise AIError("Reading reports uses your AI provider. Choose one in Settings → AI, or enter results by hand.")
    if cfg["provider"] == "agent":
        raise AIError("Reading reports needs a local model or an API key; agents can't read files here. Choose one in "
                      "Settings → AI, or enter results by hand.")
    if cfg.get("sends_elsewhere") and not cloud_acknowledged():
        raise AIError(CLOUD_ACK_NEEDED)
    if len(data) > MAX_REPORT_BYTES:
        raise AIError("That file is larger than 15 MB.")
    is_pdf = content_type == "application/pdf" or filename.lower().endswith(".pdf")
    if not is_pdf and not content_type.startswith("image/"):
        raise AIError("Upload a PDF or a photo (JPEG, PNG, HEIC converted to JPEG).")
    text = _pdf_text(data) if is_pdf else ""
    if text:
        report: dict[str, Any] = {"type": "text", "text": f"Lab report text:\n\n{text[:60_000]}"}
    else:
        report = {"type": "pdf" if is_pdf else "image", "media_type": content_type, "filename": filename or "report.pdf",
                  "data": base64.b64encode(data).decode()}
    async with _client() as client:
        reply = await _complete(client, cfg, "You transcribe lab reports into structured data.",
                                [{"role": "user", "content": [report, {"type": "text", "text": EXTRACT_PROMPT}]}], None, 8192)
    out = _parse_json(reply.text)
    out["read_as"] = "text" if text else ("document" if is_pdf else "image")
    return out
