"""
Updates: whether a newer release of Syntropy Health is out, and how this installation gets it.

Once a day the server asks GitHub for the latest published release. Nothing about the person or their records is
sent; GitHub sees the request like any other. The owner is told in Alerts and in Settings → General, with the way
this installation updates:

- The Mac and Windows apps download the release, check it, install it and open again: when the owner asks, or on
  their own while the window is closed, with "Install updates automatically" on (see desktop/updater.py).
- The Linux installer's daily timer installs it when that setting is on (``syntropy-health update --due``).
- Docker, other installs and clones of the repository are given the command to run.

Checks can be turned off in Settings, or for the whole installation with SYNTROPY_UPDATE_CHECK=false.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from app.core import config, settings

log = logging.getLogger("syntropy.updates")

REPO = "EnriqueNeyra/syntropy-health"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"

CHECK_EVERY = 24 * 3600
TICK_SECONDS = 3600
RETRY_FAILED_AFTER = 24 * 3600

# Files the release workflow attaches under fixed names (.github/workflows/desktop.yml).
MAC_ASSET = "Syntropy-Health-mac-arm64.dmg"
WINDOWS_ASSET = "Syntropy-Health-windows-x64-setup.exe"
CHECKSUMS_ASSET = "SHA256SUMS"
REQUIREMENTS_ASSET = "Syntropy-Health-requirements.txt"

LINUX_VENV = Path("/opt/syntropy-health/venv")
LINUX_TIMER = Path("/etc/systemd/system/syntropy-health-update.timer")


class CheckFailed(Exception):
    """GitHub couldn't be reached, or answered with something unexpected."""


class InstallFailed(Exception):
    """An installer's reason for not installing, written for the person (anything else is logged, not shown)."""


# ---------------------------------------------------------------------------
# Versions and releases
# ---------------------------------------------------------------------------

def version_tuple(version: str) -> tuple[int, int, int]:
    """"v1.2" → (1, 2, 0). Only the leading numbers count ("1.3.0rc1" → (1, 3, 0))."""
    numbers: list[int] = []
    for piece in version.strip().lstrip("vV").split(".")[:3]:
        digits = re.match(r"\d+", piece)
        if not digits:
            break
        numbers.append(int(digits.group()))
    return tuple((numbers + [0, 0, 0])[:3])  # type: ignore[return-value]


def is_newer(latest: str, current: str = config.APP_VERSION) -> bool:
    return version_tuple(latest) > version_tuple(current)


def _release(data: dict[str, Any]) -> dict[str, Any]:
    tag = str(data["tag_name"])
    return {
        "version": tag.lstrip("vV"),
        "tag": tag,
        "url": data.get("html_url") or f"{RELEASES_URL}/tag/{tag}",
        "notes": (data.get("body") or "")[:4000],
        "published_at": data.get("published_at"),
        "assets": {a["name"]: a["browser_download_url"] for a in data.get("assets") or []
                   if isinstance(a, dict) and a.get("name") and a.get("browser_download_url")},
    }


def fetch_latest() -> Optional[dict[str, Any]]:
    """The latest published release (drafts and pre-releases aren't), or None before the first one."""
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.get(LATEST_API, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "Syntropy-Health-update-check"})
    except httpx.HTTPError as exc:
        raise CheckFailed("GitHub couldn't be reached.") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise CheckFailed(f"GitHub answered {resp.status_code}.")
    try:
        return _release(resp.json())
    except (ValueError, KeyError, TypeError) as exc:
        raise CheckFailed("GitHub's answer wasn't understood.") from exc


def _wheel_name(version: str) -> str:
    return f"syntropy_health-{version}-py3-none-any.whl"


def _download(url: str) -> bytes:
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Syntropy-Health-update"})
    except httpx.HTTPError as exc:
        raise InstallFailed("GitHub couldn't be reached. Try again later.") from exc
    if resp.status_code != 200:
        raise InstallFailed(f"GitHub answered {resp.status_code} for {url.rsplit('/', 1)[-1]}. Try again later.")
    return resp.content


def fetch_checksums(version: str) -> dict[str, str]:
    """File name → SHA-256, from the SHA256SUMS the release workflow publishes with every release."""
    sums = _download(f"https://github.com/{REPO}/releases/download/v{version}/{CHECKSUMS_ASSET}").decode("utf-8", "replace")
    found = (line.split(None, 1) for line in sums.splitlines() if line.strip())
    return {name.strip().lstrip("*"): digest.lower() for digest, name in found}


def fetch_requirements(version: str, checksums: dict[str, str], folder: Path) -> Optional[Path]:
    """The exact dependencies a release was built and tested with, checked, so that the same version number means the
    same software everywhere. None for a release from before they were published."""
    expected = checksums.get(REQUIREMENTS_ASSET)
    if not expected:
        return None
    content = _download(f"https://github.com/{REPO}/releases/download/v{version}/{REQUIREMENTS_ASSET}")
    if hashlib.sha256(content).hexdigest() != expected:
        raise InstallFailed(f"The dependency list of version {version} doesn't match its checksum, so nothing was installed.")
    path = folder / REQUIREMENTS_ASSET
    path.write_bytes(content)
    return path


def package_source(release: dict[str, Any], checksums: dict[str, str]) -> str:
    """What pip or uv installs for a release: its wheel, which they check against its checksum. Never its source archive,
    which lacks the relay key the release workflow builds into the wheel (config.relay_key), so it would lose Syntropy's
    sign-in registrations."""
    name = _wheel_name(release["version"])
    wheel, digest = release.get("assets", {}).get(name), checksums.get(name)
    if not wheel or not digest:
        raise InstallFailed(f"Version {release['version']} has no package for this computer yet. Try again later.")
    return f"syntropy-health @ {wheel}#sha256={digest}"


def package_source_for(version: str, checksums: dict[str, str]) -> str:
    """A release's wheel by its version, to go back to one (scripts/install.sh builds the same address)."""
    name = _wheel_name(version)
    if not checksums.get(name):
        raise InstallFailed(f"Version {version} has no package for this computer.")
    return f"syntropy-health @ https://github.com/{REPO}/releases/download/v{version}/{name}#sha256={checksums[name]}"


# ---------------------------------------------------------------------------
# An update that didn't start: kept in a file, not the database, because whoever runs the update may be root, and the
# database was just put back. With it the nightly timer doesn't retry the same release, and the owner is told.
# ---------------------------------------------------------------------------

def _failed_file() -> Path:
    return config.data_dir() / "update-failed.json"


def note_failed_update(version: Optional[str], detail: str) -> None:
    path = _failed_file()
    path.write_text(json.dumps({"version": version, "at": time.time(), "detail": detail}))
    try:
        owner = config.data_dir().stat()
        os.chown(path, owner.st_uid, owner.st_gid)
    except (OSError, AttributeError):       # not root, or Windows
        pass


def failed_update() -> Optional[dict[str, Any]]:
    try:
        return json.loads(_failed_file().read_text())
    except (OSError, ValueError):
        return None


def clear_failed_update() -> None:
    _failed_file().unlink(missing_ok=True)


def problems() -> list[dict[str, str]]:
    failed = failed_update()
    if not failed or (failed.get("version") and not is_newer(failed["version"])) or time.time() - failed.get("at", 0) > 30 * 86400:
        return []      # since then, that version or a newer one has been installed, or it's old news
    return [{"id": f"update-failed-{failed.get('version')}", "title": f"The update to {failed.get('version') or 'a new version'} didn't start",
             "text": f"Syntropy Health went back to the version that worked, so nothing is lost. What happened: {failed.get('detail')}"}]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def checks_allowed() -> bool:
    """False when whoever runs the server turned checks off for good (SYNTROPY_UPDATE_CHECK=false)."""
    return config.env_bool("SYNTROPY_UPDATE_CHECK", True)


def checks_on() -> bool:
    return checks_allowed() and bool(settings.get("updates.check"))


def auto_on() -> bool:
    return checks_on() and bool(settings.get("updates.auto"))


def check() -> dict[str, Any]:
    """Asks GitHub now and remembers the answer (or that it couldn't)."""
    try:
        latest = fetch_latest()
    except CheckFailed as exc:
        settings.set("updates.error", str(exc))
    else:
        settings.set("updates.latest", latest)
        settings.set("updates.error", None)
    settings.set("updates.checked_at", time.time())
    return status()


# ---------------------------------------------------------------------------
# How this installation updates
# ---------------------------------------------------------------------------

Installer = Callable[[dict[str, Any], bool], None]     # (release, automatic) → downloads, installs, reopens
_installer: Optional[Installer] = None
_progress: dict[str, Any] = {}
_lock = threading.Lock()


def set_installer(fn: Optional[Installer]) -> None:
    """The Mac and Windows apps install updates themselves."""
    global _installer
    _installer = fn


def set_progress(state: Optional[str], detail: str = "") -> None:
    """downloading, waiting (for the window to close), installing, or failed."""
    global _progress
    _progress = {"state": state, "detail": detail, "at": time.time()} if state else {}


def method() -> dict[str, Any]:
    """How this installation gets a new version: installed by the app itself, or a command to run."""
    if _installer:
        return {"kind": "app", "installs": True, "automatic": True}
    if getattr(sys, "frozen", False):       # the app's program, started as a service rather than as the app
        return {"kind": "app", "installs": False, "automatic": False}
    if config.env("SYNTROPY_INSTALL") == "docker":
        return {"kind": "docker", "installs": False, "automatic": False,
                "command": "docker compose pull && docker compose up -d"}
    if config.is_source_checkout():
        return {"kind": "source", "installs": False, "automatic": False,
                "command": "git pull, then start Syntropy Health again"}
    if Path(sys.prefix).resolve() == LINUX_VENV:
        return {"kind": "linux", "installs": False, "automatic": LINUX_TIMER.exists(),
                "command": "sudo syntropy-health update"}
    return {"kind": "package", "installs": False, "automatic": False, "command": "syntropy-health update"}


def status() -> dict[str, Any]:
    latest = settings.get("updates.latest")
    available = bool(latest) and is_newer(latest["version"])
    return {
        "current": config.APP_VERSION,
        "latest": {k: latest[k] for k in ("version", "url", "notes", "published_at")} if latest else None,
        "available": available,
        "checked_at": settings.get("updates.checked_at"),
        "error": settings.get("updates.error"),
        "check": checks_on(),
        "check_allowed": checks_allowed(),
        "auto": auto_on(),
        "method": method(),
        "progress": _progress or None,
        "releases_url": RELEASES_URL,
    }


def install(automatic: bool = False) -> dict[str, Any]:
    """Starts installing the latest release in the Mac or Windows app. The app closes and opens again when done."""
    latest = settings.get("updates.latest")
    if not latest or not is_newer(latest["version"]):
        raise ValueError("Syntropy Health is up to date.")
    if not _installer:
        raise RuntimeError("This installation doesn't install updates itself.")
    with _lock:
        if _progress.get("state") not in (None, "failed"):
            return status()
        set_progress("downloading")
    threading.Thread(target=_run_installer, args=(_installer, latest, automatic), daemon=True, name="update").start()
    return status()


def _run_installer(installer: Installer, release: dict[str, Any], automatic: bool) -> None:
    try:
        installer(release, automatic)
    except Exception as exc:  # noqa: BLE001 - shown in Settings; the app keeps running the version it has
        log.exception("Couldn't install Syntropy Health %s", release.get("version"))
        detail = str(exc) if isinstance(exc, InstallFailed) else "Something went wrong; the log has the details."
        set_progress("failed", detail)


# ---------------------------------------------------------------------------
# Background checks
# ---------------------------------------------------------------------------

_task: Optional[asyncio.Task] = None


async def tick() -> None:
    if not checks_on():
        return
    if time.time() - float(settings.get("updates.checked_at") or 0) >= CHECK_EVERY:
        await asyncio.to_thread(check)
    if _progress.get("state") == "failed" and time.time() - _progress["at"] >= RETRY_FAILED_AFTER:
        set_progress(None)
    if auto_on() and _installer and not _progress and status()["available"]:
        install(automatic=True)


async def _loop() -> None:
    await asyncio.sleep(60)     # let the server finish starting
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001
            log.exception("Update check failed")
        await asyncio.sleep(TICK_SECONDS)


def start() -> None:
    global _task
    if checks_allowed() and (_task is None or _task.done()):
        _task = asyncio.get_running_loop().create_task(_loop())


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _task = None
