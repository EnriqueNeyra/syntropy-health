"""Shared request helpers for routers."""

from __future__ import annotations

from typing import Any, Optional

from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.core import context
from app.store import profiles


def request_origin(request: Request) -> str:
    """Canonical scheme://host[:port] of this instance as seen by the browser (proxy aware)."""
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost:8000").split(",")[0].strip()
    return f"{proto}://{host}"


def phone_origin(request: Request) -> str:
    """The address to give the iPhone app. A page opened as localhost (the Mac and Windows apps' own window) would
    hand the phone an address that means the phone itself; when this server also listens on the home network
    (``SYNTROPY_LISTEN_HOST`` is set by ``syntropy-health serve`` and the desktop apps), give it that address instead."""
    origin = request_origin(request)
    parts = urlsplit(origin)
    if parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        return origin
    from app.services import network
    home = network.phone_address(parts.port)
    return f"{parts.scheme}://{home}{f':{parts.port}' if parts.port else ''}" if home else origin


def resolve_profile(profile_id: Optional[str], level: Optional[str] = None) -> dict[str, Any]:
    """The person a request is about, if the signed-in account may see them. Without an id, the account's own.

    ``level`` is what the request needs: 'view' or 'manage'. Unset, reading needs 'view' and changing needs 'manage'.
    Someone the account can't see is "not found", the same as someone who doesn't exist."""
    principal = context.principal()
    if principal is None:            # the scheduler, the command line and other code outside a request
        try:
            return profiles.resolve(profile_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
    pid = profile_id or principal.home or next(iter(principal.access), None)
    check_access(pid, level)
    prof = profiles.get_profile(pid) if pid else None
    if not prof:
        raise HTTPException(404, f"Profile '{profile_id}' not found" if profile_id else "Profile not found")
    return prof


def check_access(profile_id: Optional[str], level: Optional[str] = None) -> None:
    """For a record, source, device or other item that belongs to a person: may this request read (or change) it?"""
    principal = context.principal()
    if principal is None:
        return
    need = level or ("manage" if principal.writing else "view")
    have = principal.level(profile_id)
    if not have:
        raise HTTPException(404, "Not found")
    if need == "manage" and have != "manage":
        name = (profiles.get_profile(profile_id or "") or {}).get("name", "this person")
        raise HTTPException(403, f"You can see {name}'s data but not change it.")


def visible_profile_ids() -> Optional[set[str]]:
    """Who the signed-in account can see (None outside a request: everyone)."""
    principal = context.principal()
    return None if principal is None else set(principal.access)
