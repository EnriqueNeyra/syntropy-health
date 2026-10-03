"""
User-editable instance settings persisted in ``app_settings``.

Secrets (client secrets, the password hash) are encrypted before storage.
Environment variables provide first-run defaults for credentials.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from app.core import config, security
from app.core.db import db

# Platforms that authenticate via SMART on FHIR.
EHR_PLATFORMS: dict[str, dict[str, Any]] = {
    # Syntropy Health's open.epic registration. Public clients' IDs aren't secret; an ID in Settings or the environment wins.
    "epic": {"label": "Epic (MyChart)", "env": "EPIC_CLIENT_ID", "vendor_sandbox": True, "default_mode": "production",
             "default_client_ids": {"sandbox": "280d55bb-e8a2-45b0-8a7b-2e3826ef5e9c",
                                    "production": "eb7944d2-58e9-4805-bae2-8c68fd70cfdf"}},
    "cerner": {"label": "Oracle Health (Cerner)", "env": "CERNER_CLIENT_ID", "vendor_sandbox": True},
    "athena": {"label": "athenahealth", "env": "ATHENA_CLIENT_ID", "vendor_sandbox": True},
    "healow": {"label": "eClinicalWorks (healow)", "env": "HEALOW_CLIENT_ID", "vendor_sandbox": True},
    "va": {"label": "VA Lighthouse", "env": "VA_CLIENT_ID", "vendor_sandbox": True},
    "smart-health-it": {"label": "SMART Health IT", "env": "", "vendor_sandbox": True},
}

CONNECTION_MODES = ("simulated", "sandbox", "production")

# Client IDs of the Syntropy apps registered with Oura and WHOOP. Their secrets live only on the
# relay worker (see cloudflare-worker/), which performs the code exchange for these IDs. Google Health has
# no Syntropy app yet (its scopes need Google's restricted-scope review), so it needs your own OAuth client.
WEARABLES: dict[str, dict[str, str]] = {
    "oura": {"label": "Oura Ring", "id_env": "OURA_CLIENT_ID", "secret_env": "OURA_CLIENT_SECRET",
             "default_client_id": "386fc575-d55b-431e-acbe-5d1bd8da3036"},
    "whoop": {"label": "WHOOP", "id_env": "WHOOP_CLIENT_ID", "secret_env": "WHOOP_CLIENT_SECRET",
              "default_client_id": "91dbd202-060d-477e-a9b7-3aedd1def98c"},
    "google": {"label": "Google Health", "id_env": "GOOGLE_HEALTH_CLIENT_ID", "secret_env": "GOOGLE_HEALTH_CLIENT_SECRET",
               "default_client_id": ""},
}

SECRET_KEYS = {"auth.password_hash"}


def _is_secret(key: str) -> bool:
    """Encrypted at rest: the password hash, OAuth client secrets and AI provider API keys."""
    return key in SECRET_KEYS or key.endswith(".client_secret") or key.startswith("ai.key.")

DEFAULTS: dict[str, Any] = {
    "setup.completed": False,
    "auth.required": True,
    "relay.redirect_uri": config.DEFAULT_RELAY_URL,
    "relay.wearable_redirect_uri": config.DEFAULT_WEARABLE_RELAY_URL,
    "sync.ehr_interval_hours": 24,
    "sync.wearable_interval_hours": 4,
    "display.timezone": None,
    "display.units": None,      # "metric" or "us"; unset follows the browser's region
    "updates.check": True,      # ask GitHub once a day whether a newer release is out
    "updates.auto": False,      # and install it (the Mac and Windows apps, and the Linux installer's timer)
}


def _raw_get(key: str) -> Optional[str]:
    with db() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def get(key: str, default: Any = None) -> Any:
    raw = _raw_get(key)
    if raw is None:
        return DEFAULTS.get(key, default)
    value = json.loads(raw)
    if _is_secret(key):
        return security.decrypt_json(value)
    return value


def set(key: str, value: Any) -> None:  # noqa: A001 - mirrors dict API
    stored: Any = value
    if value is not None and _is_secret(key):
        stored = security.encrypt_json(value)
    with db() as conn:
        if value is None:
            conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, json.dumps(stored), time.time()),
            )


def delete_prefix(prefix: str) -> None:
    """Removes a key and every key under it (``prefix`` and ``prefix.*``)."""
    with db() as conn:
        conn.execute("DELETE FROM app_settings WHERE key = ? OR key LIKE ? ESCAPE '\\'",
                     (prefix, prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ".%"))


def timezone_name() -> str:
    return get("display.timezone") or config.timezone_name()


# ---------------------------------------------------------------------------
# EHR platform configuration
# ---------------------------------------------------------------------------

def platform_config(platform: str, mode: Optional[str] = None) -> dict[str, Any]:
    """The platform's settings; ``mode`` picks the built-in client ID for a mode other than the current one."""
    meta = EHR_PLATFORMS.get(platform)
    if meta is None:
        raise KeyError(platform)
    mode = mode or get(f"platform.{platform}.mode") or meta.get("default_mode", "simulated")
    env_client = config.clean_credential(config.env(meta["env"])) if meta["env"] else ""
    saved_client = config.clean_credential(get(f"platform.{platform}.client_id"))
    default_client = meta.get("default_client_ids", {}).get(mode, "")
    client_id = saved_client or env_client or default_client
    if platform == "smart-health-it":
        client_id = client_id or "syntropy-health"
    return {
        "platform": platform,
        "label": meta["label"],
        "mode": mode,
        "client_id": client_id,
        "client_id_source": ("settings" if saved_client else "env" if env_client else "default" if default_client else None),
        "production_ready": mode == "production" and bool(client_id),
    }


def set_platform_config(platform: str, mode: Optional[str] = None, client_id: Optional[str] = None) -> dict[str, Any]:
    if platform not in EHR_PLATFORMS:
        raise KeyError(platform)
    if mode is not None:
        if mode not in CONNECTION_MODES:
            raise ValueError(f"Unknown mode '{mode}'")
        set(f"platform.{platform}.mode", mode)
    if client_id is not None:
        set(f"platform.{platform}.client_id", client_id.strip() or None)
    return platform_config(platform)


def wearable_credentials(provider: str) -> dict[str, str]:
    meta = WEARABLES[provider]
    client_id = (config.clean_credential(get(f"wearable.{provider}.client_id"))
                 or config.clean_credential(config.env(meta["id_env"])) or meta["default_client_id"])
    secret = config.clean_credential(get(f"wearable.{provider}.client_secret")) or config.clean_credential(config.env(meta["secret_env"]))
    return {"client_id": client_id, "client_secret": secret}


def set_wearable_credentials(provider: str, client_id: Optional[str] = None, client_secret: Optional[str] = None) -> None:
    if provider not in WEARABLES:
        raise KeyError(provider)
    if client_id is not None:
        set(f"wearable.{provider}.client_id", client_id.strip() or None)
    if client_secret is not None:
        set(f"wearable.{provider}.client_secret", client_secret.strip() or None)
