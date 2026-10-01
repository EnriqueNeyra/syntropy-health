"""Shared plumbing for cloud wearable connectors (Oura, WHOOP)."""

from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.core import settings
from app.connectors.smart import SmartError, USER_AGENT


def redirect_uri() -> str:
    return settings.get("relay.wearable_redirect_uri")


def uses_local_exchange(provider: str, origin: str) -> bool:
    """True when this instance holds the client secret and is itself the registered redirect."""
    creds = settings.wearable_credentials(provider)
    return bool(creds["client_secret"]) and redirect_uri().startswith(origin)


def relay_base() -> str:
    parsed = urlparse(redirect_uri())
    return f"{parsed.scheme}://{parsed.netloc}"


async def exchange_code_local(token_url: str, provider: str, code: str, redirect: str) -> dict[str, Any]:
    creds = settings.wearable_credentials(provider)
    if not creds["client_id"] or not creds["client_secret"]:
        raise SmartError(f"{provider.title()} client credentials are not configured.", "auth")
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.post(token_url, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
            "client_id": creds["client_id"], "client_secret": creds["client_secret"],
        })
    if resp.status_code != 200:
        raise SmartError(f"{provider.title()} token exchange failed ({resp.status_code}).", "auth")
    return token_credentials(resp.json())


async def refresh_tokens(token_url: str, provider: str, credentials: dict[str, Any]) -> dict[str, Any]:
    """Refreshes locally when the secret is available, otherwise through the relay's broker."""
    refresh_token = credentials.get("refresh_token")
    if not refresh_token:
        raise SmartError(f"{provider.title()} access expired. Reconnect to continue.", "auth")
    creds = settings.wearable_credentials(provider)
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": USER_AGENT}) as client:
        try:
            if creds["client_secret"]:
                resp = await client.post(token_url, data={
                    "grant_type": "refresh_token", "refresh_token": refresh_token,
                    "client_id": creds["client_id"], "client_secret": creds["client_secret"],
                    **({"scope": "offline"} if provider == "whoop" else {}),
                })
            else:
                resp = await client.post(f"{relay_base()}/refresh", json={"provider": provider, "refresh_token": refresh_token})
        except httpx.HTTPError as exc:
            raise SmartError(f"Could not refresh {provider.title()} access: {exc}", "network") from exc
    if resp.status_code != 200:
        raise SmartError(f"{provider.title()} refused to refresh access ({resp.status_code}). Reconnect to continue.", "auth")
    return token_credentials(resp.json(), credentials)


def token_credentials(tokens: dict[str, Any], previous: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    creds = dict(previous or {})
    if not tokens.get("access_token"):
        raise SmartError("Token response did not include an access token.", "auth")
    creds["access_token"] = tokens["access_token"]
    if tokens.get("refresh_token"):
        creds["refresh_token"] = tokens["refresh_token"]
    expires_in = tokens.get("expires_in")
    creds["expires_at"] = time.time() + float(expires_in) if expires_in else None
    if tokens.get("scope"):
        creds["scope"] = tokens["scope"]
    return creds


def needs_refresh(credentials: dict[str, Any], skew: int = 120) -> bool:
    exp = credentials.get("expires_at")
    return bool(exp) and float(exp) - skew < time.time()
