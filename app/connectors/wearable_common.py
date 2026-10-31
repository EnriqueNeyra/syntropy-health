"""Shared plumbing for cloud wearable connectors (Oura, WHOOP, Google Health)."""

from __future__ import annotations

import random
import re
import time
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.core import config, settings
from app.connectors.smart import SmartError, USER_AGENT, retry_after_seconds, temporary_refusal


class Refused(Exception):
    """A provider answered 403 to one data type. Why isn't known yet: that takes all the types together (see refusals)."""

    def __init__(self, endpoint: str = "", detail: str = ""):
        super().__init__(endpoint)
        self.endpoint = endpoint
        self.detail = detail


def refusals(name: str, refused: list[Refused], asked: int, granted: Optional[str], scopes: dict[str, str]) -> list[str]:
    """What to say about the data types a provider refused. All of them is an error, because counted as a success the next
    sync would start after the days this one didn't bring. Some of them is a warning, unless the person didn't grant that
    type when they connected, which is their choice and nothing to keep mentioning."""
    if refused and len(refused) >= asked:
        detail = next((r.detail for r in refused if r.detail), None)
        raise SmartError(detail or f"{name} refused access to everything Syntropy Health asked for. A membership may have expired, or "
                                   f"permission may have been taken back. Check your {name} account, or reconnect. What's missing "
                                   "is fetched once access is back.", "error")
    held = set((granted or "").split())
    out = []
    for r in refused:
        scope = scopes.get(r.endpoint)
        if granted and scope and scope not in held:
            continue
        out.append(f"{name} refused access to {r.endpoint.replace('_', ' ').replace('-', ' ')}. It will be fetched again until {name} allows it.")
    return out


def too_many_pages(name: str, what: str) -> SmartError:
    return SmartError(f"{name} has more {what} than Syntropy Health can fetch in one go, so this sync stopped rather than miss some. "
                      "It will try again.", "error")


def clock_offset_minutes(value: Any) -> Optional[int]:
    """"-05:00" to -300. Zero says nothing about where someone was, so it's left to their home zone."""
    if not isinstance(value, str) or len(value) != 6 or value[0] not in "+-":
        return None
    try:
        minutes = int(value[1:3]) * 60 + int(value[4:6])
    except ValueError:
        return None
    return (-minutes if value[0] == "-" else minutes) or None


def redirect_uri() -> str:
    return settings.get("relay.wearable_redirect_uri")


def wall_clock(d: date, hour: int, minute: int, tz: tzinfo) -> datetime:
    """``hour:minute`` on day ``d`` by the clock in ``tz``, as UTC. Simulated data keeps the person's own hours (asleep
    at night, waking in the morning), wherever they are."""
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz).astimezone(timezone.utc)


def simulated_wake(d: date, hour: int, minute: int, tz: tzinfo, seed: str) -> datetime:
    """When a simulated night ends: around ``hour:minute``, a little different each morning and later at weekends. The
    same for the same day and seed, so syncing again gives the same nights."""
    rng = random.Random(f"{seed}-{d.isoformat()}-wake")
    return wall_clock(d, hour, minute, tz) + timedelta(minutes=rng.randint(-25, 30) + (40 if d.weekday() >= 5 else 0))


def uses_local_exchange(provider: str, origin: str) -> bool:
    """True when this instance holds the client secret and is itself the registered redirect."""
    creds = settings.wearable_credentials(provider)
    return bool(creds["client_secret"]) and redirect_uri().startswith(origin)


def not_configured(name: str) -> str:
    """Why Oura or WHOOP has no client ID: a release build always has Syntropy's, so this one is from the source."""
    if config.official_build():
        return f"{name} is not configured. Add the {name} client ID in Settings → Wearables."
    return (f"This copy of Syntropy Health was built from the source code, so it can't use Syntropy Labs' {name} app. "
            f"Install Syntropy Health from health.syntropylabs.io/setup, or add your own {name} app in Settings → Wearables.")


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
    try:
        tokens = resp.json()
    except ValueError:
        tokens = None
    return token_credentials(tokens if isinstance(tokens, dict) else {})


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
                key = config.relay_key()
                resp = await client.post(f"{relay_base()}/refresh", json={"provider": provider, "refresh_token": refresh_token},
                                         headers={config.RELAY_KEY_HEADER: key} if key else None)
        except httpx.HTTPError as exc:
            raise SmartError(f"Couldn't reach {provider.title()} to renew access. Syntropy will try again.", "network") from exc
    if resp.status_code == 403 and not creds["client_secret"]:
        raise SmartError(f"Syntropy Labs' sign-in relay only serves up-to-date Syntropy Health releases. Update to the latest "
                         f"version, or add your own {provider.title()} app in Settings → Wearables.", "auth")
    if resp.status_code != 200:
        if temporary_refusal(resp) or _relay_passing_on_trouble(resp, bool(creds["client_secret"])):
            raise SmartError(f"{provider.title()} couldn't renew access just now ({resp.status_code}). Syntropy will try again.",
                             "network", retry_after=retry_after_seconds(resp))
        if provider == "google" and "invalid_grant" in resp.text:
            raise SmartError("Google ended this sign-in, so Google Health has to be reconnected. A Google Cloud project still in "
                             "Testing has its sign-ins end after 7 days. To stop that, publish the project's OAuth consent "
                             "screen (Google may ask for a review), or reconnect each week.", "auth")
        raise SmartError(f"{provider.title()} refused to refresh access ({resp.status_code}). Reconnect to continue.", "auth")
    try:
        tokens = resp.json()
    except ValueError:
        tokens = None
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise SmartError(f"{provider.title()} sent an answer without a new access token. Syntropy will try again.", "network")
    return token_credentials(tokens, credentials)


def _relay_passing_on_trouble(resp: httpx.Response, local: bool) -> bool:
    """Relays deployed before it reported the provider's status answer 400 refresh_failed for everything. Their text still
    tells an outage ("HTTP 503") or a provider's temporary error from a refusal."""
    if local or resp.status_code != 400:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    if not isinstance(body, dict) or body.get("error") != "refresh_failed":
        return False
    description = str(body.get("error_description") or "")
    return bool(re.fullmatch(r"HTTP (429|408|425|5\d\d)", description)) or description in ("temporarily_unavailable", "server_error", "slow_down")


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
