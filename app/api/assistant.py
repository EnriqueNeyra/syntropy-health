"""The built-in assistant: the AI it runs on (local model, API key or agent on this computer), and chat."""

from __future__ import annotations

import json
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import resolve_profile
from app.core import auth, context
from app.services import ai, ai_agents, ai_local
from app.store import chats

# Setting up an AI (keys, servers) is for owners: it serves everyone on the server. Anyone can switch between the ones
# set up, and each person agrees for themselves before an online AI sees their records.
router = APIRouter(tags=["assistant"], dependencies=[Depends(auth.require_user)])
owner = Depends(auth.require_owner)


@router.get("/api/ai/config")
def get_config() -> dict:
    return ai.config()


class AIConfig(BaseModel):
    provider: str                              # a provider id, or "agent"
    model: Optional[str] = Field(None, max_length=200)
    api_key: Optional[str] = Field(None, max_length=500)
    base_url: Optional[str] = Field(None, max_length=500)       # local and other OpenAI-compatible servers
    label: Optional[str] = Field(None, max_length=60)           # e.g. "Ollama" or "Groq"
    allow_public_http: bool = False
    agent: Optional[str] = Field(None, max_length=20)           # with provider "agent"
    acknowledged: bool = False    # the person confirmed that an AI outside their network sees what it's asked about
    make_default: bool = True     # off: connect it without changing which AI Ask uses (unless none is set up yet)


@router.get("/api/ai/choices")
async def get_choices() -> dict:
    """Every AI that's set up, for Ask's model menu."""
    return await ai.choices()


@router.put("/api/ai/config")
async def put_config(req: AIConfig, principal: auth.Principal = Depends(auth.require_user)) -> dict:
    if not principal.owner and (req.api_key or req.base_url or req.allow_public_http or req.label
                                or not ai.is_set_up(req.provider, req.agent)):
        raise HTTPException(403, "Only the server's owner can set up an AI. You can choose between the ones set up.")
    if req.acknowledged:
        ai.acknowledge_cloud()
    try:
        # Checking whether an address is private may look up its name.
        return await run_in_threadpool(ai.save_config, req.provider, req.model, req.api_key, req.base_url, req.label,
                                       req.allow_public_http, req.agent, req.make_default)
    except ai.AIError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/ai/acknowledge")
async def acknowledge() -> dict:
    """Confirms that an AI outside the person's network sees their questions and what it looks up (asked once)."""
    ai.acknowledge_cloud()
    return {"ok": True}


@router.delete("/api/ai/config", dependencies=[owner])
async def turn_off() -> dict:
    ai.disable()
    return ai.config()


@router.delete("/api/ai/providers/{provider}", dependencies=[owner])
async def disconnect(provider: str) -> dict:
    """Forgets a provider's key, address and model list."""
    try:
        return ai.disconnect(provider)
    except ai.AIError as exc:
        raise HTTPException(404, str(exc))


class ModelsRequest(BaseModel):
    provider: Optional[str] = None           # defaults to the saved provider; unsaved values from the form win
    api_key: Optional[str] = Field(None, max_length=500)
    base_url: Optional[str] = Field(None, max_length=500)


@router.post("/api/ai/models", dependencies=[owner])
async def models(req: ModelsRequest) -> dict:
    try:
        return await ai.list_models(req.provider, req.api_key, req.base_url)
    except ai.AIError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/ai/local/detect")
async def detect_local(request: Request) -> dict:
    """AI servers running on this computer's usual ports (Ollama, LM Studio, Jan, llama.cpp, vLLM)."""
    return {"servers": await ai_local.detect(skip_port=request.url.port), "in_docker": ai_local.in_docker()}


class LocalCheck(BaseModel):
    base_url: str = Field(..., max_length=500)
    api_key: Optional[str] = Field(None, max_length=500)


@router.post("/api/ai/local/check", dependencies=[owner])
async def check_local(req: LocalCheck) -> dict:
    """Connects to a server the person typed in and lists its models."""
    try:
        url = ai_local.normalize_base_url(req.base_url)
        return await ai_local.check(url, req.api_key or ai.settings.get("ai.key.local"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/ai/agents")
async def list_agents(refresh: bool = False) -> dict:
    """Claude Code, Codex CLI and Gemini CLI: installed here or not, and signed in or not."""
    return await run_in_threadpool(ai_agents.detect, refresh)


@router.post("/api/ai/test", dependencies=[owner])
async def test() -> dict:
    try:
        return await ai.test_connection()
    except ai.AIError as exc:
        raise HTTPException(400, str(exc))


class ChatMessage(BaseModel):
    role: str
    content: str = Field(..., max_length=40_000)


class ChatRequest(BaseModel):
    profile_id: Optional[str] = None
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=200)


@router.post("/api/ai/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    """Streams newline-delimited JSON events: tool (a lookup started), answer, error, done. Asking only reads."""
    prof = resolve_profile(req.profile_id, "view")

    async def events() -> AsyncIterator[bytes]:
        async for event in ai.chat(prof["id"], [m.model_dump() for m in req.messages]):
            yield (json.dumps(event, ensure_ascii=False) + "\n").encode()

    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Saved conversations
# ---------------------------------------------------------------------------

CHAT_ID = "^c_[a-z0-9]{8,40}$"


def _account() -> Optional[str]:
    principal = context.principal()
    return principal.account_id if principal else None


class SavedMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field("", max_length=60_000)
    steps: list[str] = Field(default_factory=list, max_length=40)
    note: Optional[str] = Field(None, max_length=1000)
    by: Optional[str] = Field(None, max_length=300)


class ChatIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    messages: list[SavedMessage] = Field(..., max_length=200)


@router.get("/api/ai/chats")
def list_chats(profile: Optional[str] = None) -> dict:
    """This account's conversations about a person, newest first."""
    prof = resolve_profile(profile, "view")
    return {"chats": chats.list_chats(prof["id"], _account())}


@router.get("/api/ai/chats/{chat_id}")
def get_chat(chat_id: str, profile: Optional[str] = None) -> dict:
    prof = resolve_profile(profile, "view")
    chat = chats.get_chat(prof["id"], _account(), chat_id)
    if not chat:
        raise HTTPException(404, "Conversation not found")
    return chat


@router.put("/api/ai/chats/{chat_id}")
def save_chat(chat_id: str, req: ChatIn, profile: Optional[str] = None) -> dict:
    """Saves a conversation after each answer. Anyone who can see a person can keep their own conversations about them."""
    import re
    if not re.match(CHAT_ID, chat_id):
        raise HTTPException(400, "Bad conversation id.")
    prof = resolve_profile(profile, "view")
    try:
        return chats.save_chat(prof["id"], _account(), chat_id, req.title.strip(),
                               [m.model_dump(exclude_none=True) for m in req.messages])
    except PermissionError:
        raise HTTPException(404, "Conversation not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/api/ai/chats/{chat_id}")
def delete_chat(chat_id: str, profile: Optional[str] = None) -> dict:
    prof = resolve_profile(profile, "view")
    if not chats.delete_chat(prof["id"], _account(), chat_id):
        raise HTTPException(404, "Conversation not found")
    return {"ok": True}
