"""
Local models: AI servers on this computer or another one on the home network.

Ollama, LM Studio, Jan, llama.cpp's ``llama-server``, vLLM and LocalAI all serve the OpenAI Chat Completions API, so the
assistant talks to them with the same adapter as OpenAI; only the address differs. This module finds servers running on
the usual ports, tidies up addresses people type, tells private addresses from public ones (plain HTTP is fine on a
home network, not across the internet) and estimates how much of a model's context window a summary can use.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

# Port, name and how the server labels its models ("owned_by"), most common first.
KNOWN_SERVERS: list[tuple[int, str]] = [
    (11434, "Ollama"),
    (1234, "LM Studio"),
    (1337, "Jan"),
    (8080, "llama.cpp"),
    (8000, "vLLM"),
]
OWNER_NAMES = {"llamacpp": "llama.cpp", "vllm": "vLLM", "localai": "LocalAI", "organization_owner": "LM Studio"}

# Tailscale gives each device an address in 100.64.0.0/10 (carrier-grade NAT space, never on the public internet).
TAILSCALE = ipaddress.ip_network("100.64.0.0/10")
PRIVATE_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".ts.net", ".localhost")

DEFAULT_CONTEXT_TOKENS = 8192
OLLAMA_DEFAULT_CONTEXT = 4096     # what Ollama uses when a model doesn't set num_ctx


def in_docker() -> bool:
    return Path("/.dockerenv").exists()


def normalize_base_url(text: str) -> str:
    """'studio.local:11434' → 'http://studio.local:11434/v1'. Keeps a path the person typed (e.g. '/api/v1')."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Enter the server's address, for example localhost:11434 or 192.168.1.20:1234.")
    if "://" not in text:
        text = f"http://{text}"
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("That doesn't look like a server address. Try something like studio.local:11434.")
    path = parts.path.rstrip("/")
    for suffix in ("/chat/completions", "/models"):   # pasted a full endpoint
        path = path.removesuffix(suffix)
    if not path:
        path = "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def root_url(base_url: str) -> str:
    """The server's root (without /v1), for the vendor's own endpoints such as Ollama's /api/show."""
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local or (addr.version == 4 and addr in TAILSCALE)


def is_private_host(host: str) -> bool:
    """True for this computer, the home network and Tailscale; false for anything reachable from the internet."""
    host = (host or "").strip("[]").lower()
    if not host:
        return False
    if host in ("localhost", "host.docker.internal") or host.endswith(PRIVATE_SUFFIXES) or "." not in host:
        return True
    if _private_ip(host):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(_private_ip(a) for a in addresses)


def describe_address(base_url: str) -> dict[str, Any]:
    parts = urlsplit(base_url)
    private = is_private_host(parts.hostname or "")
    return {"base_url": base_url, "private": private, "https": parts.scheme == "https",
            # Plain HTTP across the internet exposes the questions and health data on the way.
            "needs_confirmation": parts.scheme == "http" and not private}


def _headers(key: Optional[str]) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"} if key else {}


async def _models(client: httpx.AsyncClient, base_url: str, key: Optional[str] = None) -> tuple[str, list[dict[str, Any]]]:
    """(server name hint, models) from GET /models. Raises httpx errors when nothing answers."""
    resp = await client.get(f"{base_url}/models", headers=_headers(key))
    resp.raise_for_status()
    body = resp.json()
    items = body.get("data") if isinstance(body, dict) else body
    models = [{"id": m["id"]} for m in items or [] if isinstance(m, dict) and m.get("id")]
    owners = {str(m.get("owned_by") or "").lower() for m in items or [] if isinstance(m, dict)}
    hint = next((OWNER_NAMES[o] for o in owners if o in OWNER_NAMES), "")
    return hint, models


async def _ollama_sizes(client: httpx.AsyncClient, base_url: str) -> dict[str, int]:
    try:
        resp = await client.get(f"{root_url(base_url)}/api/tags")
        if resp.status_code == 200:
            return {m["name"]: m.get("size") or 0 for m in resp.json().get("models") or [] if m.get("name")}
    except (httpx.HTTPError, ValueError):
        pass
    return {}


async def _probe(client: httpx.AsyncClient, host: str, port: int, name: str) -> Optional[dict[str, Any]]:
    base_url = f"http://{host}:{port}/v1"
    try:
        hint, models = await _models(client, base_url)
    except (httpx.HTTPError, ValueError):
        return None
    if port == 11434:
        sizes = await _ollama_sizes(client, base_url)
        for m in models:
            if m["id"] in sizes:
                m["size"] = sizes[m["id"]]
    shown_host = "this computer" if host in ("127.0.0.1", "localhost", "host.docker.internal") else host
    return {"name": hint or name, "base_url": base_url, "where": shown_host, "models": models}


async def detect(skip_port: Optional[int] = None) -> list[dict[str, Any]]:
    """AI servers answering on the usual ports of this computer (from Docker, the computer running the container)."""
    host = "host.docker.internal" if in_docker() else "localhost"
    timeout = httpx.Timeout(2.0, connect=0.5)
    async with httpx.AsyncClient(timeout=timeout) as client:
        found = await asyncio.gather(*(_probe(client, host, port, name) for port, name in KNOWN_SERVERS
                                       if port != skip_port))
    return [f for f in found if f]


async def check(base_url: str, key: Optional[str] = None) -> dict[str, Any]:
    """Connects to a server the person named and lists its models."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as client:
        try:
            hint, models = await _models(client, base_url, key)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ValueError("The server wants an API key. Add the key it was started with.") from exc
            raise ValueError(f"The server answered with an error ({exc.response.status_code}). Check the address; "
                             "most servers expect it to end in /v1.") from exc
        except httpx.HTTPError as exc:
            raise ValueError(f"Nothing answered at {base_url} ({exc.__class__.__name__}). Check that the server is "
                             "running and allows connections from other devices.") from exc
        except ValueError as exc:
            raise ValueError("Something answered, but it isn't an OpenAI-compatible AI server.") from exc
        if urlsplit(base_url).port == 11434:
            sizes = await _ollama_sizes(client, base_url)
            for m in models:
                if m["id"] in sizes:
                    m["size"] = sizes[m["id"]]
    return {"name": hint, "models": models, **(await asyncio.to_thread(describe_address, base_url))}


async def context_tokens(base_url: str, model: str, key: Optional[str] = None) -> int:
    """The context window the server will actually give this model, when it says; otherwise a cautious guess."""
    root = root_url(base_url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
        # Ollama: num_ctx when the model sets one, otherwise Ollama's default (smaller than most models allow).
        try:
            resp = await client.post(f"{root}/api/show", json={"model": model})
            if resp.status_code == 200:
                params = resp.json().get("parameters") or ""
                for line in params.splitlines():
                    bits = line.split()
                    if len(bits) == 2 and bits[0] == "num_ctx" and bits[1].isdigit():
                        return int(bits[1])
                return OLLAMA_DEFAULT_CONTEXT
        except (httpx.HTTPError, ValueError):
            pass
        # LM Studio: the loaded context length.
        try:
            resp = await client.get(f"{root}/api/v0/models/{model}", headers=_headers(key))
            if resp.status_code == 200:
                info = resp.json()
                n = info.get("loaded_context_length") or info.get("max_context_length")
                if isinstance(n, int) and n > 0:
                    return n
        except (httpx.HTTPError, ValueError):
            pass
    return DEFAULT_CONTEXT_TOKENS
