"""MCP access for the person's own AI agents: access tokens, and the Streamable HTTP endpoint."""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app import mcp_server
from app.api.deps import resolve_profile
from app.core import auth, context
from app.services import network
from app.store import accounts, agents, profiles

router = APIRouter(tags=["agents"], dependencies=[Depends(auth.require_user)])
mcp_router = APIRouter(tags=["mcp"])

MAX_BODY_BYTES = 1_000_000      # far more than any call needs


# ---------------------------------------------------------------------------
# Agent access tokens
# ---------------------------------------------------------------------------

@router.get("/api/agents/tokens")
def list_tokens() -> dict:
    """This account's tokens: each reads what the account can see (or only one of those people)."""
    principal = context.principal()
    return {"tokens": [t for t in agents.list_tokens() if not principal or t.get("account_id") == principal.account_id]}


class TokenRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    profile_id: Optional[str] = None      # None: every profile
    expires_days: Optional[Literal[30, 90, 365]] = None      # None: until it's revoked


@router.post("/api/agents/tokens")
def create_token(req: TokenRequest) -> dict:
    if req.profile_id:
        resolve_profile(req.profile_id, "view")
    principal = context.principal()
    return agents.create(req.name, req.profile_id, principal.account_id if principal else None, req.expires_days)


@router.get("/api/agents/local-command", dependencies=[Depends(auth.require_owner)])
def local_command(profile_id: Optional[str] = None, everyone: bool = False) -> dict:
    """How an MCP client on the computer holding the data starts the stdio server, with no token. It reads one person's
    records: the owner's own unless told otherwise, and everyone's only if that's what is asked for."""
    principal = context.principal()
    person = None if everyone else resolve_profile(profile_id, "view")["id"] if profile_id else (
        (principal.home if principal else None) or profiles.default_profile()["id"])
    if Path("/.dockerenv").exists():
        limit = ["-e", f"SYNTROPY_MCP_PROFILE={person}"] if person else []
        return {"command": "docker", "args": ["exec", "-i", *limit, "syntropy-health", "python", "-m", "app.mcp_server"]}
    return mcp_server.stdio_command(person)


@router.delete("/api/agents/tokens/{token_id}")
def revoke_token(token_id: str) -> dict:
    principal = context.principal()
    if not agents.revoke(token_id, principal.account_id if principal else None):
        raise HTTPException(404, "Token not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# MCP over Streamable HTTP (JSON responses)
# ---------------------------------------------------------------------------

def _agent(request: Request) -> dict[str, Any]:
    authz = request.headers.get("authorization", "")
    agent = agents.authenticate(authz[7:].strip() if authz.lower().startswith("bearer ") else None)
    if not agent:
        raise HTTPException(401, "An agent access token is required. Create one in Settings → AI.",
                            headers={"WWW-Authenticate": 'Bearer realm="syntropy-health"'})
    return agent


def _allowed(agent: dict[str, Any]) -> tuple[Optional[frozenset[str]], Optional[str]]:
    """Who a token may read: whoever its account can see now (sharing that ends ends here too). And whose it is."""
    account = accounts.get(agent.get("account_id"))
    if not account:
        return (frozenset() if accounts.any_accounts() else None), None
    return frozenset(accounts.access_for(account)), account["profile_id"]


def _handle_batch(batch: list[Any], agent: dict[str, Any]) -> list[dict[str, Any]]:
    allowed, home = _allowed(agent)
    scope = mcp_server.SCOPED_PROFILE.set(agent["profile_id"])
    who = mcp_server.ALLOWED_PROFILES.set(allowed)
    own = mcp_server.HOME_PROFILE.set(home)
    actor = mcp_server.ACTOR.set(f"mcp:{agent['id']}")
    gate = mcp_server.CALL_GATE.set(lambda: agents.wait_before_call(agent["id"]))
    try:
        return [r for r in (mcp_server.handle(m) if isinstance(m, dict) else mcp_server._error(None, -32600, "Invalid request")
                            for m in batch) if r is not None]
    finally:
        mcp_server.SCOPED_PROFILE.reset(scope)
        mcp_server.ALLOWED_PROFILES.reset(who)
        mcp_server.HOME_PROFILE.reset(own)
        mcp_server.ACTOR.reset(actor)
        mcp_server.CALL_GATE.reset(gate)


def _last_tool(batch: list[Any]) -> Optional[str]:
    for message in reversed(batch):
        params = message.get("params") if isinstance(message, dict) and message.get("method") == "tools/call" else None
        if isinstance(params, dict) and isinstance(params.get("name"), str):
            return params["name"][:100]
    return None


def _origin_allowed(request: Request) -> bool:
    """DNS rebinding: a web page reaching this server through a name of its own says so here. Apps send no Origin."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    try:
        host = urlsplit(origin).netloc
    except ValueError:
        return False
    return bool(host) and (host.lower() == request.headers.get("host", "").lower() or network.is_local_name(host))


def _rpc_failure(status: int, code: int, message: str, msg_id: Any = None, data: Any = None) -> JSONResponse:
    return JSONResponse(mcp_server._error(msg_id, code, message, data), status, headers={"Cache-Control": "no-store"})


def _decoded_name(value: str) -> Optional[str]:
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2], validate=True).decode()
        except (binascii.Error, ValueError):
            return None
    return value


def _header_mismatch(request: Request, message: dict[str, Any]) -> Optional[str]:
    """What's wrong with a 2026-07-28 request's headers, which have to say the same as its body so nothing in between can
    be told one thing while this server does another."""
    method, params = message.get("method"), message.get("params") or {}
    meta = params.get("_meta") if isinstance(params, dict) else None
    if meta is not None and not isinstance(meta, dict):
        return None      # no version to compare: handling the request refuses this, and says why
    version = (meta or {}).get(mcp_server.META_VERSION)
    sent = request.headers.get("mcp-protocol-version")
    if sent is None:
        return "The MCP-Protocol-Version header is missing."
    if sent != version:
        return f"Header mismatch: MCP-Protocol-Version header value '{sent}' does not match body value '{version}'"
    if request.headers.get("mcp-method") != method:
        return f"Header mismatch: Mcp-Method header value '{request.headers.get('mcp-method')}' does not match body value '{method}'"
    if method in ("tools/call", "prompts/get"):
        name = _decoded_name(request.headers.get("mcp-name", ""))
        if name != params.get("name"):
            return f"Header mismatch: Mcp-Name header value '{name}' does not match body value '{params.get('name')}'"
    return None


async def _read_body(request: Request) -> Optional[bytes]:
    """The body, or None if it's too long to be a real call. Without a Content-Length it still stops at the limit."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@mcp_router.post("/mcp")
async def mcp(request: Request) -> Response:
    """Read-only MCP tools for the person's own agents (Claude, Cursor, local agents...). Each token can be limited to
    one person; every call is recorded in the activity log with the token's name."""
    if not _origin_allowed(request):
        return _rpc_failure(403, -32600, "This Origin isn't allowed. Add its name to SYNTROPY_ALLOWED_HOSTS if it's yours.")
    agent = await run_in_threadpool(_agent, request)
    raw = await _read_body(request)
    if raw is None:
        return _rpc_failure(413, -32600, "This request is too large.")
    try:
        body = mcp_server.parse_message(raw)
    except (ValueError, RecursionError):
        return _rpc_failure(400, -32700, "Parse error")
    version = request.headers.get("mcp-protocol-version")
    if version is not None and version not in mcp_server.PROTOCOL_VERSIONS:
        reply = mcp_server.unsupported_version(body.get("id") if isinstance(body, dict) else None, version)
        return JSONResponse(reply, 400, headers={"Cache-Control": "no-store"})
    if isinstance(body, list):
        # Batches went away in 2025-06-18, and a client without the header is speaking 2025-03-26 at the newest.
        if version is not None and version != "2025-03-26":
            return _rpc_failure(400, -32600, f"Batches aren't part of protocol version {version}.")
        if not body:
            return _rpc_failure(400, -32600, "Invalid request: an empty batch.")
        if len(body) > 50:
            return _rpc_failure(400, -32600, "Batch too large")
    elif not isinstance(body, dict):
        return _rpc_failure(400, -32600, "Invalid request: the body has to be a JSON-RPC message.")
    batch = body if isinstance(body, list) else [body]
    modern = isinstance(body, dict) and mcp_server.is_modern(body)
    if modern:
        wrong = _header_mismatch(request, body)
        if wrong:
            return _rpc_failure(400, -32020, wrong, body.get("id"))
    elif version in mcp_server.MODERN_VERSIONS and isinstance(body, dict) and "id" in body and "method" in body:
        return _rpc_failure(400, -32602, f"Invalid params: _meta needs {mcp_server.META_VERSION} and {mcp_server.META_CAPABILITIES}.", body["id"])
    replies = await run_in_threadpool(_handle_batch, batch, agent)
    note = BackgroundTask(agents.note_use, agent, _last_tool(batch))
    if not replies:
        return Response(status_code=202, background=note)   # only notifications, or a client's answer
    single = None if isinstance(body, list) else replies[0]
    return JSONResponse(replies if single is None else single, mcp_server.http_status(single, modern),
                        headers={"Cache-Control": "no-store"}, background=note)


@mcp_router.get("/mcp")
def mcp_stream() -> Response:
    # No server-initiated messages: clients fall back to plain request/response.
    return Response(status_code=405, headers={"Allow": "POST"})
