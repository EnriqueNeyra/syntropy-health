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

# Platforms that authenticate via SMART on FHIR. Each has a production and a sandbox client ID: Syntropy Health's own
# registration where there is one (public clients' IDs aren't secret), else one from Settings or the environment
# (``env`` for production, ``sandbox_env`` for the vendor's sandbox).
EHR_PLATFORMS: dict[str, dict[str, Any]] = {
    "epic": {"label": "Epic (MyChart)", "env": "EPIC_CLIENT_ID", "sandbox_env": "EPIC_SANDBOX_CLIENT_ID", "default_mode": "sandbox",
             "default_client_ids": {"sandbox": "280d55bb-e8a2-45b0-8a7b-2e3826ef5e9c",
                                    "production": "eb7944d2-58e9-4805-bae2-8c68fd70cfdf"}},
    # Oracle's Code Console and eCW's healow portal each issue one client ID, for the sandbox and production alike.
    "cerner": {"label": "Oracle Health (Cerner)", "env": "CERNER_CLIENT_ID", "sandbox_env": "CERNER_SANDBOX_CLIENT_ID",
               "default_client_ids": {"sandbox": "bdb450fd-487c-49cd-8ab9-117cc89d55d6",
                                      "production": "bdb450fd-487c-49cd-8ab9-117cc89d55d6"}},
    "athena": {"label": "athenahealth", "env": "ATHENA_CLIENT_ID", "sandbox_env": "ATHENA_SANDBOX_CLIENT_ID",
               "default_client_ids": {"sandbox": "0oa145t7o43Uco1k7298", "production": "0oa14c6ry32JpSFDP298"}},
    "healow": {"label": "eClinicalWorks (healow)", "env": "HEALOW_CLIENT_ID", "sandbox_env": "HEALOW_SANDBOX_CLIENT_ID",
               "default_client_ids": {"sandbox": "_OhOmZAdes002quTJLmoDt6ESQVyUItZvH7WuYuhFWQ",
                                      "production": "_OhOmZAdes002quTJLmoDt6ESQVyUItZvH7WuYuhFWQ"}},
    # Not registered yet: their health systems are listed but can't be connected until a client ID is set.
    "trubridge": {"label": "TruBridge", "env": "TRUBRIDGE_CLIENT_ID", "sandbox_env": "TRUBRIDGE_SANDBOX_CLIENT_ID"},
    "greenway": {"label": "Greenway", "env": "GREENWAY_CLIENT_ID", "sandbox_env": "GREENWAY_SANDBOX_CLIENT_ID"},
    "modmed": {"label": "ModMed", "env": "MODMED_CLIENT_ID", "sandbox_env": "MODMED_SANDBOX_CLIENT_ID"},
    "nextgen": {"label": "NextGen", "env": "NEXTGEN_CLIENT_ID", "sandbox_env": "NEXTGEN_SANDBOX_CLIENT_ID"},
    "practicefusion": {"label": "Practice Fusion", "env": "PRACTICEFUSION_CLIENT_ID",
                       "sandbox_env": "PRACTICEFUSION_SANDBOX_CLIENT_ID"},
    "medhost": {"label": "MEDHOST", "env": "MEDHOST_CLIENT_ID", "sandbox_env": "MEDHOST_SANDBOX_CLIENT_ID"},
    # A public test server: offered only in developer mode.
    "smart-health-it": {"label": "SMART Health IT", "env": "", "sandbox_env": "", "developer_only": True},
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
    "developer.mode": False,    # off: health systems connect for real; on: each platform's own mode (simulator, sandbox)
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

def developer_mode() -> bool:
    return bool(get("developer.mode"))


def set_developer_mode(on: bool) -> None:
    set("developer.mode", bool(on) or None)


def _client_id(platform: str, mode: str) -> tuple[str, Optional[str]]:
    """The client ID for one mode and where it came from (settings, env, default)."""
    if mode not in ("sandbox", "production"):
        return "", None
    meta = EHR_PLATFORMS[platform]
    env_name = meta["env"] if mode == "production" else meta["sandbox_env"]
    for value, source in ((get(f"platform.{platform}.{mode}_client_id"), "settings"),
                          (config.env(env_name) if env_name else "", "env"),
                          (meta.get("default_client_ids", {}).get(mode, ""), "default")):
        if value := config.clean_credential(value):
            return value, source
    return "", None


def migrate_client_ids() -> None:
    """Before 1.2, one client ID served every mode; it moves to the mode it was saved for (a built-in ID is dropped).
    Left as it was, a sandbox ID outranked Syntropy's production one and sent health systems a test client."""
    for platform, meta in EHR_PLATFORMS.items():
        legacy = config.clean_credential(get(f"platform.{platform}.client_id"))
        if legacy and legacy not in meta.get("default_client_ids", {}).values():
            slot = "production" if get(f"platform.{platform}.mode") == "production" else "sandbox"
            if not get(f"platform.{platform}.{slot}_client_id"):
                set(f"platform.{platform}.{slot}_client_id", legacy)
        set(f"platform.{platform}.client_id", None)


def platform_config(platform: str, mode: Optional[str] = None) -> dict[str, Any]:
    """The platform's settings; ``mode`` picks the client ID for a mode other than the current one (a reconnect keeps
    its connection's mode). Outside developer mode every platform is in production."""
    meta = EHR_PLATFORMS.get(platform)
    if meta is None:
        raise KeyError(platform)
    developer = developer_mode()
    dev_mode = get(f"platform.{platform}.mode") or meta.get("default_mode", "simulated")
    mode = mode or (dev_mode if developer else "production")
    client_ids = {m: dict(zip(("client_id", "source"), _client_id(platform, m))) for m in ("sandbox", "production")}
    client_id, source = _client_id(platform, mode)
    if platform == "smart-health-it" and mode != "simulated":
        client_id, source = client_id or "syntropy-health", source or "default"
    developer_only = bool(meta.get("developer_only"))
    return {
        "platform": platform,
        "label": meta["label"],
        "mode": mode,
        "developer_mode": developer,
        "developer_mode_setting": dev_mode,
        "client_id": client_id,
        "client_id_source": source,
        "client_ids": client_ids,
        "developer_only": developer_only,
        # Whether choosing one of its health systems can start a sign-in.
        "available": mode == "simulated" or (bool(client_id) and (developer or not developer_only)),
        "production_ready": mode == "production" and bool(client_id),
    }


def set_platform_config(platform: str, mode: Optional[str] = None, sandbox_client_id: Optional[str] = None,
                        production_client_id: Optional[str] = None) -> dict[str, Any]:
    """``mode`` is the platform's mode while developer mode is on."""
    if platform not in EHR_PLATFORMS:
        raise KeyError(platform)
    if mode is not None:
        if mode not in CONNECTION_MODES:
            raise ValueError(f"Unknown mode '{mode}'")
        set(f"platform.{platform}.mode", mode)
    for slot, value in (("sandbox", sandbox_client_id), ("production", production_client_id)):
        if value is not None:
            set(f"platform.{platform}.{slot}_client_id", value.strip() or None)
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
