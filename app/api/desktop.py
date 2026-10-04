"""For the Mac app's first launch: set up a server on this Mac, or join the household's server that already runs on
another computer. Only the app's own window (this computer) asks, and only before this server is set up: once it is,
the Mac is the server and there's nothing to join."""

from __future__ import annotations

import ipaddress
from typing import Optional
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.core import auth
from app.services import network

router = APIRouter(prefix="/api/desktop", tags=["desktop"], include_in_schema=False)


def _only_before_setup_here(request: Request) -> None:
    try:
        local = ipaddress.ip_address((request.client.host if request.client else "").split("%")[0]).is_loopback
    except ValueError:
        local = False
    if not local or auth.is_setup_complete():
        raise HTTPException(404, "Not found")
    if request.headers.get(auth.CSRF_HEADER, "").lower() != auth.CSRF_VALUE:
        raise HTTPException(403, "Missing X-Requested-With: syntropy header.")


@router.get("/discover")
async def discover(request: Request) -> dict:
    """Syntropy Health servers on the home network."""
    _only_before_setup_here(request)
    return {"servers": await run_in_threadpool(network.discover)}


class ProbeRequest(BaseModel):
    url: str = Field(..., max_length=300)
    address: Optional[str] = Field(None, max_length=300)     # the same server by IP, if its name doesn't resolve


def _normalize(raw: str) -> Optional[str]:
    raw = raw.strip().rstrip("/")
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    port = parts.port or (8000 if parts.scheme == "http" and parts.port is None else None)
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname       # an IPv6 address keeps its brackets
    return f"{parts.scheme}://{host}{f':{port}' if port else ''}"


@router.post("/probe")
async def probe(req: ProbeRequest, request: Request) -> dict:
    """Is a Syntropy Health server answering at this address? The page can't ask another address itself (its
    Content-Security-Policy keeps it to this server)."""
    _only_before_setup_here(request)
    tried = [u for u in (_normalize(req.url), _normalize(req.address or "")) if u]
    if not tried:
        raise HTTPException(400, "Enter an address like http://192.168.1.20:8000.")
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        for url in tried:
            try:
                info = (await client.get(f"{url}/api/wearables/pair")).json()
            except (httpx.HTTPError, ValueError):
                continue
            if isinstance(info, dict) and info.get("service") == "syntropy-health":
                return {"ok": True, "url": url, "version": info.get("version")}
    raise HTTPException(404, "No Syntropy Health server answered there. Check the address, and that the server lets "
                             "other devices connect (on that computer: Settings → Security).")
