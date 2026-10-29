"""
Runtime configuration.

Values are read from the environment (optionally seeded from a repo-local ``.env``
file) every time they are requested, so tests and the settings UI can change them
without restarting the process. User-editable settings live in the database
(see ``app.core.settings``); environment variables only provide defaults.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_DIR = REPO_ROOT / "app"

APP_NAME = "Syntropy Health"
APP_VERSION = "1.6.0"

DEFAULT_RELAY_URL = "https://health.syntropylabs.io/callback"
DEFAULT_WEARABLE_RELAY_URL = "https://syntropy-auth-relay.syntropylabs.workers.dev/callback"
RELAY_KEY_HEADER = "X-Syntropy-Key"


def _load_env_file() -> None:
    """Minimal .env loader (no dependency). Never overrides real environment values."""
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


_load_env_file()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def is_source_checkout() -> bool:
    """Running from a clone of the repository (./run.sh), rather than installed or packaged."""
    return (REPO_ROOT / "run.sh").exists() and not getattr(sys, "frozen", False)


def default_data_dir() -> Path:
    """./data in a clone of the repository; otherwise the usual place for app data on this system."""
    if is_source_checkout():
        return REPO_ROOT / "data"
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "syntropy-health"


def data_dir() -> Path:
    """Directory holding the database, encryption key and uploads."""
    explicit = env("SYNTROPY_DATA_DIR")
    if explicit:
        path = Path(explicit)
    elif env("SYNTROPY_DB_PATH"):
        path = Path(env("SYNTROPY_DB_PATH")).resolve().parent
    else:
        path = default_data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    explicit = env("SYNTROPY_DB_PATH")
    if explicit:
        p = Path(explicit)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    return data_dir() / "syntropy.db"


def timezone_name() -> str:
    return env("TZ") or "UTC"


def scheduler_enabled() -> bool:
    return env_bool("SYNTROPY_SCHEDULER", True)


def relay_key() -> str:
    """The key Syntropy Labs' release builds carry. The sign-in relay and the callback page serve only sign-ins that
    present it, and Syntropy Health's own registrations (EHR vendors, Oura, WHOOP) are offered only with it. It isn't
    in the source code: the release workflows write it to app/core/release_key.py (scripts/release_key.py), so a build
    from the source, such as a fork, has none and uses its own registrations. SYNTROPY_RELAY_KEY sets it in development."""
    return env("SYNTROPY_RELAY_KEY") or _built_in_relay_key()


def _built_in_relay_key() -> str:
    try:
        from app.core.release_key import KEY
    except ImportError:
        return ""
    return str(KEY).strip()


def official_build() -> bool:
    return bool(relay_key())


# Placeholder values ("your-client-id") are treated as unset.
PLACEHOLDER_PREFIXES = ("your-",)


def clean_credential(value: str | None) -> str:
    value = (value or "").strip()
    if not value or any(value.startswith(p) for p in PLACEHOLDER_PREFIXES):
        return ""
    return value
