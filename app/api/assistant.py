"""The built-in assistant: the AI it runs on (local model, API key or agent on this computer), and chat."""

from __future__ import annotations

import json
import re
import time
import unicodedata
from contextlib import aclosing
from typing import AsyncIterator, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
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
def get_config(chat: bool = False) -> dict:
    """The server's default AI, or with ``chat`` the one this person's questions in Ask go to."""
    return ai.config(chat=chat)


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
        await run_in_threadpool(ai.acknowledge_cloud, provider=req.provider, agent=req.agent, base_url=req.base_url)
    try:
        # Checking whether an address is private may look up its name.
        return await run_in_threadpool(ai.save_config, req.provider, req.model, req.api_key, req.base_url, req.label,
                                       req.allow_public_http, req.agent, req.make_default)
    except ai.AIError as exc:
        raise HTTPException(400, str(exc))


class Acknowledgement(BaseModel):
    vendor: Optional[str] = Field(None, max_length=300)      # from the config or the model menu; absent: the AI in use now
    name: Optional[str] = Field(None, max_length=200)
    provider: Optional[str] = Field(None, max_length=40)     # instead of vendor, an AI being connected
    agent: Optional[str] = Field(None, max_length=20)
    base_url: Optional[str] = Field(None, max_length=500)
    summary: bool = False                                    # also sending a summary of the record, for a model with no tools


@router.post("/api/ai/acknowledge")
def acknowledge(req: Optional[Acknowledgement] = None) -> dict:
    """Confirms that this AI outside the person's network sees their questions and what it looks up (asked once for each)."""
    req = req or Acknowledgement()
    ai.acknowledge_cloud(req.vendor, req.name, req.summary, req.provider, req.agent, req.base_url)
    return {"ok": True}


@router.delete("/api/ai/acknowledge")
def withdraw(vendor: str) -> dict:
    """Takes back that agreement: nothing more goes to that AI for this person until they agree again."""
    ai.withdraw_cloud(vendor)
    return ai.config(chat=True)


@router.delete("/api/ai/config", dependencies=[owner])
def turn_off() -> dict:
    ai.disable()
    return ai.config()


@router.delete("/api/ai/providers/{provider}", dependencies=[owner])
def disconnect(provider: str) -> dict:
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
        return await ai_local.check(url, req.api_key or ai.saved_key_for("local", url))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/ai/local/context")
async def local_context() -> dict:
    """How much the local model in use has room for at once, for the warning in Settings."""
    cfg = ai.config()
    if cfg.get("provider") != "local" or not cfg.get("configured"):
        return {"tokens": None}
    return await ai_local.context_info(cfg["base_url"], cfg["model"], ai.saved_key_for("local"))


@router.get("/api/ai/agents")
async def list_agents(refresh: bool = False) -> dict:
    """Claude Code, Codex CLI and Gemini CLI: installed here or not, and signed in or not."""
    found = await run_in_threadpool(ai_agents.detect, refresh)
    principal = context.principal()
    if principal is not None and not principal.owner:      # a full path usually names the owner's account on this computer
        found = {**found, "agents": [{k: v for k, v in agent.items() if k != "path"} for agent in found.get("agents", [])]}
    return found


@router.post("/api/ai/test", dependencies=[owner])
async def test() -> dict:
    try:
        return await ai.test_connection()
    except ai.AIError as exc:
        raise HTTPException(400, str(exc))


class ChatMessage(BaseModel):
    role: str
    content: str = Field(..., max_length=40_000)


class ExpectedAI(BaseModel):
    provider: Optional[str] = Field(None, max_length=40)
    agent: Optional[str] = Field(None, max_length=20)
    model: Optional[str] = Field(None, max_length=200)
    vendor: Optional[str] = Field(None, max_length=300)


class ChatRequest(BaseModel):
    profile_id: Optional[str] = None
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=200)
    expect: Optional[ExpectedAI] = None      # the AI the page says this goes to: nothing is sent if it isn't that one


@router.post("/api/ai/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    """Streams newline-delimited JSON events: tool (a lookup started), answer, error, done. Asking only reads."""
    prof = resolve_profile(req.profile_id, "view")

    async def events() -> AsyncIterator[bytes]:
        expect = req.expect.model_dump(exclude_none=True) if req.expect else None
        # Closed as soon as the person stops or leaves: an agent's process ends and the question stops counting.
        async with aclosing(ai.chat(prof["id"], [m.model_dump() for m in req.messages], expect)) as answer:
            async for event in answer:
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
    stopped: Optional[bool] = None     # the person stopped it, so it's only part of an answer
    cut: Optional[bool] = None         # the model ran out of room


class ChatIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    messages: list[SavedMessage] = Field(..., max_length=200)
    title_set: bool = False            # the person named it before its first answer was saved


class ChatChange(BaseModel):
    title: Optional[str] = Field(None, max_length=120)
    pinned: Optional[bool] = None


@router.get("/api/ai/chats")
def list_chats(profile: Optional[str] = None, q: Optional[str] = Query(None, max_length=200)) -> dict:
    """This account's conversations about a person, newest first; with `q`, those with those words in their title or messages."""
    prof = resolve_profile(profile, "view")
    return {"chats": chats.search_chats(prof["id"], _account(), q) if q and q.strip() else chats.list_chats(prof["id"], _account())}


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
    if not re.match(CHAT_ID, chat_id):
        raise HTTPException(400, "Bad conversation id.")
    prof = resolve_profile(profile, "view")
    try:
        return chats.save_chat(prof["id"], _account(), chat_id, req.title.strip(),
                               [m.model_dump(exclude_none=True) for m in req.messages], title_set=req.title_set)
    except PermissionError:
        raise HTTPException(404, "Conversation not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/api/ai/chats/{chat_id}")
def change_chat(chat_id: str, req: ChatChange, profile: Optional[str] = None) -> dict:
    """Renames or pins a conversation."""
    prof = resolve_profile(profile, "view")
    title = req.title.strip() if req.title is not None else None
    if title == "":
        raise HTTPException(400, "A conversation needs a name.")
    chat = chats.update_chat(prof["id"], _account(), chat_id, title=title, pinned=req.pinned)
    if not chat:
        raise HTTPException(404, "Conversation not found")
    return chat


@router.get("/api/ai/chats/{chat_id}/export")
def export_chat(chat_id: str, profile: Optional[str] = None) -> Response:
    """A conversation as a Markdown file."""
    prof = resolve_profile(profile, "view")
    chat = chats.get_chat(prof["id"], _account(), chat_id)
    if not chat:
        raise HTTPException(404, "Conversation not found")
    # Only safe characters go in the header (see records' files); filename* keeps a title in another script.
    name = re.sub(r"[^\w -]+", "", chat["title"]).strip()[:60].strip() or "Conversation"
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().strip() or "Conversation"
    # Not text/markdown: the Mac app's window shows any text it can rather than saving it, and would leave the app for the file.
    return Response(chats.as_markdown(chat, prof["name"], time.strftime("%Y-%m-%d")), media_type="application/octet-stream",
                    headers={"Content-Disposition": f"""attachment; filename="{plain}.md"; filename*=UTF-8''{quote(name)}.md""",
                             "Cache-Control": "no-store"})


@router.delete("/api/ai/chats/{chat_id}")
def delete_chat(chat_id: str, profile: Optional[str] = None) -> dict:
    prof = resolve_profile(profile, "view")
    if not chats.delete_chat(prof["id"], _account(), chat_id):
        raise HTTPException(404, "Conversation not found")
    return {"ok": True}
