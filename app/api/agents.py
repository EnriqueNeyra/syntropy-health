"""MCP access for the person's own AI agents: access tokens, and the Streamable HTTP endpoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app import mcp_server
from app.api.deps import resolve_profile
from app.core import auth, context
from app.store import accounts, agents

router = APIRouter(tags=["agents"], dependencies=[Depends(auth.require_user)])
mcp_router = APIRouter(tags=["mcp"])


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


@router.post("/api/agents/tokens")
async def create_token(req: TokenRequest) -> dict:
    if req.profile_id:
        resolve_profile(req.profile_id, "view")
    principal = context.principal()
    return agents.create(req.name, req.profile_id, principal.account_id if principal else None)


@router.get("/api/agents/local-command", dependencies=[Depends(auth.require_owner)])
def local_command() -> dict:
    """How an MCP client on the computer holding the data starts the stdio server (no token; everyone's records)."""
    if Path("/.dockerenv").exists():
        return {"command": "docker", "args": ["exec", "-i", "syntropy-health", "python", "-m", "app.mcp_server"]}
    return mcp_server.stdio_command()


@router.delete("/api/agents/tokens/{token_id}")
async def revoke_token(token_id: str) -> dict:
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


def _allowed(agent: dict[str, Any]) -> Optional[frozenset[str]]:
    """Who a token may read: whoever its account can see now (sharing that ends ends here too)."""
    account = accounts.get(agent.get("account_id"))
    if not account:
        return frozenset() if accounts.any_accounts() else None
    return frozenset(accounts.access_for(account))


def _handle_batch(body: Any, agent: dict[str, Any]) -> list[dict[str, Any]]:
    scope = mcp_server.SCOPED_PROFILE.set(agent["profile_id"])
    allowed = mcp_server.ALLOWED_PROFILES.set(_allowed(agent))
    actor = mcp_server.ACTOR.set(f"mcp:{agent['name']}")
    try:
        batch = body if isinstance(body, list) else [body]
        replies = [r for r in (mcp_server.handle(m) for m in batch if isinstance(m, dict)) if r is not None]
        calls = [m for m in batch if isinstance(m, dict) and m.get("method") == "tools/call"]
        if calls:
            agents.record_call(agent["id"], str((calls[-1].get("params") or {}).get("name") or ""))
        return replies
    finally:
        mcp_server.SCOPED_PROFILE.reset(scope)
        mcp_server.ALLOWED_PROFILES.reset(allowed)
        mcp_server.ACTOR.reset(actor)


@mcp_router.post("/mcp")
async def mcp(request: Request) -> Response:
    """Read-only MCP tools for the person's own agents (Claude, Cursor, local agents...). Each token can be limited to
    one person; every call is recorded in the activity log with the token's name."""
    agent = _agent(request)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400)
    if isinstance(body, list) and len(body) > 50:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Batch too large"}}, 400)
    replies = await run_in_threadpool(_handle_batch, body, agent)
    if not replies:
        return Response(status_code=202)   # only notifications
    return JSONResponse(replies if isinstance(body, list) else replies[0], headers={"Cache-Control": "no-store"})


@mcp_router.get("/mcp")
def mcp_stream() -> Response:
    # No server-initiated messages: clients fall back to plain request/response.
    return Response(status_code=405, headers={"Allow": "POST"})
