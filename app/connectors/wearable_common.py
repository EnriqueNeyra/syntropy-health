"""Shared plumbing for cloud wearable connectors (Oura, WHOOP, Google Health)."""

from __future__ import annotations

import json
import random
import time
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.core import config, data_fixes, settings
from app.connectors.smart import SmartError, USER_AGENT

# Who can renew through a relay at all. Google Health can't: its renewals only ever go to Google, with the person's secret.
RELAY_PROVIDERS = ("oura", "whoop")


def _saved_redirect(provider: str) -> str:
    saved = settings.get(f"wearable.{provider}.redirect_uri") or ""
    return "" if saved == config.DEFAULT_WEARABLE_RELAY_URL else saved     # the relay would use Syntropy's secret, not theirs


def _syntropys_app(provider: str, client_id: str) -> bool:
    default = settings.WEARABLES[provider]["default_client_id"]
    return bool(default) and client_id in ("", default)


def redirect_uri(provider: str, origin: str = "") -> str:
    """Where the provider sends the browser after consent. Syntropy's apps are registered with the relay and nothing
    else, whatever anyone saved. A person's own app uses the address they registered, or else this server's /callback."""
    creds = settings.wearable_credentials(provider)
    if _syntropys_app(provider, creds["client_id"]):
        return config.DEFAULT_WEARABLE_RELAY_URL
    own = _saved_redirect(provider)
    return own or (f"{origin}/callback" if origin and creds["client_secret"] else config.DEFAULT_WEARABLE_RELAY_URL)


def wall_clock(d: date, hour: int, minute: int, tz: tzinfo) -> datetime:
    """``hour:minute`` on day ``d`` by the clock in ``tz``, as UTC. Simulated data keeps the person's own hours (asleep
    at night, waking in the morning), wherever they are."""
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz).astimezone(timezone.utc)


def simulated_wake(d: date, hour: int, minute: int, tz: tzinfo, seed: str) -> datetime:
    """When a simulated night ends: around ``hour:minute``, a little different each morning and later at weekends. The
    same for the same day and seed, so syncing again gives the same nights."""
    rng = random.Random(f"{seed}-{d.isoformat()}-wake")
    return wall_clock(d, hour, minute, tz) + timedelta(minutes=rng.randint(-25, 30) + (40 if d.weekday() >= 5 else 0))


def not_configured(name: str) -> str:
    """Why Oura or WHOOP has no client ID: a release build always has Syntropy's, so this one is from the source."""
    if config.official_build():
        return f"{name} is not configured. Add the {name} client ID in Settings → Wearables."
    return (f"This copy of Syntropy Health was built from the source code, so it can't use Syntropy Labs' {name} app. "
            f"Install Syntropy Health from health.syntropylabs.io/setup, or add your own {name} app in Settings → Wearables.")


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def own_relay_origin(address: Optional[str]) -> Optional[str]:
    """The origin of a relay someone runs themselves, or None if this address can't be one. It gets refresh tokens, so
    https only, and Syntropy's hosts hold no secret for that person's app."""
    try:
        parsed = urlparse((address or "").strip())
        if parsed.scheme != "https" or not parsed.hostname:
            return None
    except ValueError:
        return None
    origin = f"https://{parsed.netloc.lower()}"
    return None if origin in (_origin(config.DEFAULT_WEARABLE_RELAY_URL), _origin(config.DEFAULT_RELAY_URL)) else origin


def refresh_broker(provider: str) -> Optional[str]:
    """Who renews tokens for an app whose secret isn't on this server: the relay for Syntropy's apps. For someone's own
    Oura or WHOOP app, only the relay they named for this in Settings. Never a redirect setting, which says where a
    browser goes and can be any host. None when nobody can."""
    if provider not in RELAY_PROVIDERS:
        return None
    if _syntropys_app(provider, settings.wearable_credentials(provider)["client_id"]):
        return _origin(config.DEFAULT_WEARABLE_RELAY_URL)
    return own_relay_origin(settings.get(f"wearable.{provider}.relay_url"))


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
    broker = refresh_broker(provider) if not creds["client_secret"] else None
    if not creds["client_secret"] and not broker:
        raise SmartError(f"{provider.title()} access can't be renewed without your app's client secret. Add it in "
                         "Settings → Wearables, then reconnect.", "auth")
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
                resp = await client.post(f"{broker}/refresh", json={"provider": provider, "refresh_token": refresh_token},
                                         headers={config.RELAY_KEY_HEADER: key} if key else None)
        except httpx.HTTPError as exc:
            raise SmartError(f"Could not refresh {provider.title()} access: {exc}", "network") from exc
    if resp.status_code >= 500 or resp.status_code == 429:
        raise SmartError(f"{provider.title()} isn't answering right now. The connection is fine, and it will be tried again at the next sync.", "network")
    if resp.status_code == 403 and not creds["client_secret"] and _syntropys_app(provider, creds["client_id"]):
        raise SmartError(f"Syntropy Labs' sign-in relay only serves up-to-date Syntropy Health releases. Update to the latest "
                         f"version, or add your own {provider.title()} app in Settings → Wearables.", "auth")
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


def _move_wearable_redirect(conn: Any, cursor: Any, size: int) -> None:
    """The one redirect setting all wearables once shared becomes per provider, kept only for those with their own app:
    set up for Google, it had also pulled Oura and WHOOP off the relay they're registered with. Renewals used to follow
    it too, so an app with no secret here keeps renewing at an https address it named, now as that app's own relay."""
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'relay.wearable_redirect_uri'").fetchone()
    if row:
        saved = json.loads(row["value"])
        if saved and saved != config.DEFAULT_WEARABLE_RELAY_URL:
            for provider in settings.WEARABLES:
                creds = settings.wearable_credentials(provider)
                if creds["client_secret"] or not _syntropys_app(provider, creds["client_id"]):
                    conn.execute("INSERT OR IGNORE INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)",
                                 (f"wearable.{provider}.redirect_uri", row["value"], time.time()))
                    relay = own_relay_origin(saved) if provider in RELAY_PROVIDERS and not creds["client_secret"] else None
                    if relay:
                        conn.execute("INSERT OR IGNORE INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)",
                                     (f"wearable.{provider}.relay_url", json.dumps(relay), time.time()))
        conn.execute("DELETE FROM app_settings WHERE key = 'relay.wearable_redirect_uri'")
    return None


data_fixes.register("wearable-redirect-per-provider", _move_wearable_redirect)
