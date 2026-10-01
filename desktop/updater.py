"""
Installing a new release in the Mac and Windows apps (app/services/updates.py decides when).

1. Download the release's app (the .dmg, or the Windows setup .exe) and its SHA256SUMS, over HTTPS from GitHub, and
   check the download against them. When this app is signed, the new one must be signed by the same developer too.
2. Automatic updates wait until the window is closed (the app keeps running in the menu bar or tray), so nobody's
   work is interrupted. Updates the owner asked for install straight away.
3. Mac: the new app is copied next to this one; a small script waits for this app to quit, swaps them and opens
   the new one. Windows: the setup program installs silently over this one (same folder, same choices) and opens
   the new version. Either way it opens the way it was: with its window, or in the background.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("syntropy.desktop.update")


class UpdateError(Exception):
    pass


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Syntropy-Health-updater"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)


def _expected_sha256(sums: str, name: str) -> Optional[str]:
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


class Updater:
    """Registered with the server (updates.set_installer) by the desktop app."""

    def __init__(self, folder: Path, window_hidden: Callable[[], bool], quit_app: Callable[[], None],
                 progress: Callable[[Optional[str], str], None]):
        self.folder = folder
        self.window_hidden = window_hidden
        self.quit_app = quit_app
        self.progress = progress

    # ------------------------------------------------------------------ entry point
    def __call__(self, release: dict[str, Any], automatic: bool) -> None:
        from app.services import updates

        name = updates.MAC_ASSET if sys.platform == "darwin" else updates.WINDOWS_ASSET
        assets = release.get("assets") or {}
        if name not in assets or updates.CHECKSUMS_ASSET not in assets:
            raise UpdateError(f"The {release['version']} release has no app for this computer yet. Download it from "
                              f"{release['url']}.")
        self.folder.mkdir(parents=True, exist_ok=True)
        for old in self.folder.iterdir():       # earlier downloads
            if old.is_file():
                old.unlink(missing_ok=True)
        file = self.folder / name
        sums = self.folder / updates.CHECKSUMS_ASSET
        log.info("Downloading Syntropy Health %s", release["version"])
        self.progress("downloading", "")
        _download(assets[updates.CHECKSUMS_ASSET], sums)
        _download(assets[name], file)
        expected = _expected_sha256(sums.read_text(errors="replace"), name)
        if not expected or _sha256(file) != expected:
            file.unlink(missing_ok=True)
            raise UpdateError("The download didn't match the release's checksum, so it wasn't installed.")

        if automatic and not self.window_hidden():
            self.progress("waiting", "")
            while not self.window_hidden():
                time.sleep(30)
        self.progress("installing", "")
        background = self.window_hidden()
        log.info("Installing Syntropy Health %s (reopening %s)", release["version"],
                 "in the background" if background else "with its window")
        if sys.platform == "darwin":
            self._install_mac(file, background)
        elif sys.platform == "win32":
            self._install_windows(file, background)
        else:
            raise UpdateError("Updates install themselves only in the Mac and Windows apps.")
        # Give the page a moment to show "Installing…", then quit so the new version can take this one's place.
        threading.Timer(1.0, self.quit_app).start()

    # ------------------------------------------------------------------ Mac
    @staticmethod
    def _team_id(app: Path) -> Optional[str]:
        out = _run(["codesign", "-dv", "--verbose=2", str(app)])
        for line in (out.stderr + out.stdout).splitlines():
            if line.startswith("TeamIdentifier=") and line.split("=", 1)[1] not in ("", "not set"):
                return line.split("=", 1)[1].strip()
        return None

    def _install_mac(self, dmg: Path, background: bool) -> None:
        exe = Path(sys.executable).resolve()
        bundle = exe.parents[2]           # …/Syntropy Health.app/Contents/MacOS/Syntropy Health
        if bundle.suffix != ".app":
            raise UpdateError("This copy of Syntropy Health isn't an app bundle, so it can't update itself.")
        if not os.access(bundle.parent, os.W_OK):
            raise UpdateError(f"Syntropy Health can't replace itself in {bundle.parent}. Download the new version and "
                              "drag it to Applications.")
        mount = Path(tempfile.mkdtemp(prefix="syntropy-update-"))
        staged = bundle.with_name(f".{bundle.name}.update")
        attach = _run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount), str(dmg)])
        if attach.returncode != 0:
            raise UpdateError(f"The download couldn't be opened ({attach.stderr.strip()}).")
        try:
            new = mount / bundle.name
            if not new.exists():
                raise UpdateError("The download doesn't contain the app.")
            if _run(["codesign", "--verify", "--deep", "--strict", str(new)]).returncode != 0:
                raise UpdateError("The new app's signature isn't valid, so it wasn't installed.")
            team = self._team_id(bundle)
            if team and self._team_id(new) != team:
                raise UpdateError("The new app isn't signed by the same developer, so it wasn't installed.")
            _run(["rm", "-rf", str(staged)])
            copy = _run(["ditto", str(new), str(staged)])
            if copy.returncode != 0:
                raise UpdateError(f"The new app couldn't be copied ({copy.stderr.strip()}).")
        finally:
            _run(["hdiutil", "detach", "-quiet", str(mount)])
        dmg.unlink(missing_ok=True)

        # After this app quits: swap in the new one (putting the old one back if that fails) and open it.
        app, old, q = shlex.quote(str(bundle)), shlex.quote(str(bundle.with_name(f".{bundle.name}.old"))), shlex.quote(str(staged))
        reopen = f"open -g {app} --args --background" if background else f"open {app}"
        script = f"""
while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.5; done
rm -rf {old}
if mv {app} {old} && mv {q} {app}; then rm -rf {old}; else [ -d {app} ] || mv {old} {app}; rm -rf {q}; fi
{reopen}
"""
        subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)

    # ------------------------------------------------------------------ Windows
    @staticmethod
    def _signer(path: Path) -> Optional[str]:
        """The certificate thumbprint of a validly signed file, else None."""
        ps = ("$s = Get-AuthenticodeSignature -LiteralPath $args[0]; "
              "if ($s.Status -eq 'Valid') { $s.SignerCertificate.Thumbprint }")
        out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps, str(path)])
        return out.stdout.strip() or None

    def _install_windows(self, setup: Path, background: bool) -> None:
        current = Path(sys.executable).resolve()
        signer = self._signer(current)
        if signer and self._signer(setup) != signer:
            raise UpdateError("The new version isn't signed by the same developer, so it wasn't installed.")
        # Inno Setup: no questions, close anything still using the app's files, then open the new version
        # (desktop/installer.iss runs the app when /RELAUNCH is given).
        args = [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
                f"/RELAUNCH={'background' if background else 'window'}"]
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(args, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
