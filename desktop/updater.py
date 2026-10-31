"""
Installing a new release in the Mac and Windows apps (app/services/updates.py decides when).

1. Download the release's app (the .dmg, or the Windows setup .exe) and its SHA256SUMS, over HTTPS from GitHub only, and
   check the download against them. Once the app has a release key built in (updates.RELEASE_PUBLIC_KEY), SHA256SUMS
   must carry that key's signature. When this app is signed, the new one must be signed by the same developer too. A
   check that can't be made counts as failed.
2. Automatic updates wait until the window is closed (the app keeps running in the menu bar or tray), so nobody's
   work is interrupted. Turning them off during the wait cancels it, and asking to install now ends it. Updates the owner
   asked for install straight away.
3. A small script outlives this app. It waits for it to quit, keeps a copy of the program, puts in the new one (Mac:
   swaps the app; Windows: runs the setup program silently, same folder, same choices) and opens it the way it was: with
   its window, or in the background.
4. It then waits for the new version to say it started (started_fine, once its server is up). If that doesn't happen
   the script puts the copy back and opens it. That version, seeing the update didn't take, restores the database from the
   copy the new version made before upgrading it, tells the owner, and doesn't try the same release for a day
   (review_last_attempt).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from app.services import updates
from app.services.updates import InstallFailed

log = logging.getLogger("syntropy.desktop.update")

APP_BUNDLE = "Syntropy Health.app"         # its name in the disk image, whatever someone renamed their copy to
WINDOWS_APP = "Syntropy Health.exe"
BUNDLE_ID = "io.syntropyhealth.desktop"
MAX_DOWNLOAD = 1 << 30
WAIT_TICK = 30.0

ATTEMPT = "update-attempt.json"            # what's being installed: written just before this app quits
RESULT = "update-result.json"              # written by the script when the update didn't take
OK_MARK = "update-ok"                      # the new version started: the script can throw away its copy
BAD_MARK = "update-bad"                    # the new version can't work with this data: the script puts the copy back
# How long the script waits for the new version, which may be copying and upgrading a database of several GB first.
WATCH_SECONDS = 1500
GONE_SECONDS = 20
# Kinds of start-up trouble that going back fixes: the old version still works with data this one couldn't upgrade.
GO_BACK_KINDS = {"upgrade_failed", "no_copy", "other"}


class UpdateError(InstallFailed):
    pass


def _check_host(request: httpx.Request) -> None:
    if not updates.trusted_asset_url(str(request.url)):
        raise UpdateError("The download pointed somewhere other than GitHub, so it wasn't installed.")


def _download(url: str, dest: Path, limit: int = MAX_DOWNLOAD) -> None:
    # httpx brings its own certificate authorities (certifi). urllib relies on the system's OpenSSL ones, which the
    # packaged Mac app doesn't have, so every HTTPS download there failed certificate verification.
    headers = {"User-Agent": "Syntropy-Health-updater"}
    try:
        # The hook sees each hop, so a redirect can't leave GitHub or drop to plain http either.
        with httpx.Client(timeout=60.0, follow_redirects=True, event_hooks={"request": [_check_host]}) as client:
            with client.stream("GET", url, headers=headers) as resp, open(dest, "wb") as out:
                resp.raise_for_status()
                total = 0
                for chunk in resp.iter_bytes(1 << 20):
                    total += len(chunk)
                    if total > limit:
                        raise UpdateError("The download is far larger than any release, so it wasn't installed.")
                    out.write(chunk)
    except httpx.HTTPError as exc:
        raise UpdateError("The new version couldn't be downloaded. Check the internet connection and try again.") from exc


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
    # On Windows, keep a console window from flashing up over the app (PowerShell checks the signatures).
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300, creationflags=flags)


# ---------------------------------------------------------------------------
# What the next start makes of the last attempt
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        # PowerShell writes UTF-8 with a byte-order mark.
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _clear_leftovers(folder: Path, keep: tuple[str, ...] = ()) -> None:
    """Installers and the like. The script may be reading its own file, which is fine where it matters: a Mac's stays
    readable, and Windows has read the whole of its."""
    if not folder.is_dir():
        return
    for old in folder.iterdir():
        if old.is_file() and old.name not in keep:
            try:
                old.unlink()
            except OSError:
                log.info("Couldn't remove %s yet", old)


def _trying(folder: Path) -> Optional[dict[str, Any]]:
    """The attempt, if this is the version it was installing, and its script may still be waiting to hear."""
    attempt = _read_json(folder / ATTEMPT)
    if not attempt or not isinstance(attempt.get("version"), str) or updates.is_newer(attempt["version"]):
        return None
    return attempt if time.time() - float(attempt.get("at") or 0) < WATCH_SECONDS else None


def _log_setup_tail(setup_log: Path) -> None:
    """The setup program's own account of what went wrong, into desktop.log: its file goes with the other leftovers."""
    try:
        for line in setup_log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]:
            log.error("setup: %s", line)
    except OSError:
        pass


def _remove_old_copies(folder: Path) -> None:
    """Copies a script never got to remove, if the computer was restarted while it waited. Nothing is waiting by now."""
    shutil.rmtree(folder / "previous", ignore_errors=True)
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        bundle = Updater._bundle()
        if bundle.suffix == ".app":
            for name in (f".{bundle.name}.old", f".{bundle.name}.update"):
                _run(["rm", "-rf", str(bundle.with_name(name))])


def review_last_attempt(folder: Path) -> None:
    """Before this version's server starts. Still on the old version with an attempt on file means it didn't take."""
    attempt = _read_json(folder / ATTEMPT)
    if not attempt or not isinstance(attempt.get("version"), str):
        _clear_leftovers(folder)
        _remove_old_copies(folder)
        return
    version = attempt["version"]
    if not updates.is_newer(version):
        return                  # this is the new version: started_fine or started_badly says how it went
    result = _read_json(folder / RESULT) or {}
    detail = str(result.get("detail") or "The new version didn't finish installing.")[:500]
    log.error("The update to %s didn't take: %s", version, detail)
    _log_setup_tail(folder / "setup.log")
    if result.get("outcome") == "rolled-back":
        _restore_database(float(attempt.get("at") or 0))
    try:
        updates.note_failed_update(version, detail)
    except OSError:
        log.exception("Couldn't note that the update failed")
    updates.set_progress("failed", detail)
    _clear_leftovers(folder)


def _restore_database(since: float) -> None:
    """The new version may have upgraded the database before it stopped, and this version refuses a newer one."""
    from app.core import db
    try:
        restored = db.restore_pre_upgrade(since)
        if restored:
            log.warning("Put the database back as it was before the update, from %s", restored)
    except OSError:
        log.exception("Couldn't put the database back as it was before the update")


def started_fine(folder: Path) -> None:
    if (folder / ATTEMPT).exists():
        if _trying(folder):
            log.info("The update worked")
            try:
                (folder / OK_MARK).write_text("ok")
            except OSError:
                log.exception("Couldn't tell the update script that it worked")
        _clear_leftovers(folder, keep=(OK_MARK,))
        updates.clear_failed_update()


def started_badly(folder: Path, detail: str) -> bool:
    """Whether this is a new version that its update script will replace with the old one (the caller should then quit)."""
    if not _trying(folder):
        return False
    try:
        (folder / BAD_MARK).write_text(detail)
    except OSError:
        log.exception("Couldn't tell the update script to go back")
        return False
    return True


# ---------------------------------------------------------------------------
# Windows: everything after this app quits, run by PowerShell. Paths come as parameters, never written into the script.
# ---------------------------------------------------------------------------

WINDOWS_SCRIPT = r"""
param([int]$AppPid, [string]$Setup, [string]$AppDir, [string]$Relaunch, [string]$Folder, [int]$WatchSeconds, [int]$GoneSeconds)
$exe = Join-Path $AppDir 'Syntropy Health.exe'
$previous = Join-Path $Folder 'previous'
$ok = Join-Path $Folder 'update-ok'
$bad = Join-Path $Folder 'update-bad'
$log = Join-Path $Folder 'setup.log'
function Note($outcome, $detail) {
  @{ outcome = $outcome; detail = $detail } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Folder 'update-result.json') -Encoding UTF8
}
function Open-App {
  if ($Relaunch -eq 'background') { Start-Process -FilePath $exe -ArgumentList '--background' } else { Start-Process -FilePath $exe }
}
function Copy-Over($from, $to) {
  robocopy $from $to /MIR /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
  return ($LASTEXITCODE -lt 8)
}
function Stop-App {
  Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith("$AppDir\", 'OrdinalIgnoreCase') } | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 2
}
function Go-Back($detail) {
  Stop-App
  if (Copy-Over $previous $AppDir) {
    Note 'rolled-back' $detail
    Remove-Item -LiteralPath $previous -Recurse -Force -ErrorAction SilentlyContinue
  } else {
    Note 'stuck' "$detail The earlier version couldn't be put back either: install it again from the releases page. A copy is in $previous."
  }
  Open-App
  exit 1
}

Wait-Process -Id $AppPid -ErrorAction SilentlyContinue
if (-not (Copy-Over $AppDir $previous)) {
  Note 'not-started' "Couldn't keep a copy of the program in $previous first, so nothing was changed. Check that the disk has room."
  Open-App
  exit 1
}
$setupArgs = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS', "/LOG=`"$log`"")
$run = Start-Process -FilePath $Setup -ArgumentList $setupArgs -Wait -PassThru
if ($run.ExitCode -ne 0) { Go-Back "The setup program stopped with code $($run.ExitCode)." }

Open-App
$waited = 0
$gone = 0
while ($waited -lt $WatchSeconds) {
  if (Test-Path -LiteralPath $ok) {
    Remove-Item -LiteralPath $ok -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $previous -Recurse -Force -ErrorAction SilentlyContinue
    exit 0
  }
  if (Test-Path -LiteralPath $bad) { Go-Back ((Get-Content -LiteralPath $bad -Raw) -replace '\s+', ' ') }
  if (Get-Process | Where-Object { $_.Path -eq $exe }) { $gone = 0 } else { $gone += 2 }
  if ($gone -ge $GoneSeconds) { Go-Back 'The new version closed as soon as it opened.' }
  Start-Sleep -Seconds 2
  $waited += 2
}
Go-Back 'The new version took too long to start.'
"""


class Updater:
    """Registered with the server (updates.set_installer) by the desktop app."""

    def __init__(self, folder: Path, window_hidden: Callable[[], bool], quit_app: Callable[[], None],
                 progress: Callable[[Optional[str], str], None]):
        self.folder = folder
        self.window_hidden = window_hidden
        self.quit_app = quit_app
        self.progress = progress
        self.now = threading.Event()
        self.staged: Optional[Path] = None
        self.saying: Optional[str] = None

    def install_now(self) -> None:
        """The owner asked, while this was waiting for the window to close."""
        self.now.set()

    # ------------------------------------------------------------------ entry point
    def __call__(self, release: dict[str, Any], automatic: bool) -> None:
        name = updates.MAC_ASSET if sys.platform == "darwin" else updates.WINDOWS_ASSET
        assets = release.get("assets") or {}
        if name not in assets or updates.CHECKSUMS_ASSET not in assets:
            raise UpdateError(f"The {release['version']} release has no app for this computer yet. Download it from "
                              f"{release['url']}.")
        self.now.clear()
        self.folder.mkdir(parents=True, exist_ok=True)
        _clear_leftovers(self.folder)       # earlier downloads
        file = self.folder / name
        log.info("Downloading Syntropy Health %s", release["version"])
        self._say("downloading")
        self._check_checksums(release, assets, name, file)

        if not self._go_ahead(automatic):
            return
        self._say("installing")
        if sys.platform == "darwin":
            self._stage_mac(file)
        elif sys.platform == "win32":
            self._check_windows(file)
        else:
            raise UpdateError("Updates install themselves only in the Mac and Windows apps.")
        # Staging takes a while, long enough for someone to open the window again.
        if not self._go_ahead(automatic):
            self._discard_staged()
            return
        self._say("installing")
        background = self.window_hidden()
        log.info("Installing Syntropy Health %s (reopening %s)", release["version"],
                 "in the background" if background else "with its window")
        self._note_attempt(release)
        if sys.platform == "darwin":
            self._swap_mac(background)
        else:
            self._start_windows(file, background)
        # Give the page a moment to show "Installing…", then quit so the new version can take this one's place.
        threading.Timer(1.0, self.quit_app).start()

    def _check_checksums(self, release: dict[str, Any], assets: dict[str, str], name: str, file: Path) -> None:
        sums = self.folder / updates.CHECKSUMS_ASSET
        _download(assets[updates.CHECKSUMS_ASSET], sums, limit=1 << 20)
        if updates.RELEASE_PUBLIC_KEY:
            if updates.SIGNATURE_ASSET not in assets:
                raise UpdateError(f"The {release['version']} release isn't signed, so it wasn't installed.")
            signature = self.folder / updates.SIGNATURE_ASSET
            _download(assets[updates.SIGNATURE_ASSET], signature, limit=1 << 12)
            if not updates.signature_is_valid(sums.read_bytes(), signature.read_bytes()):
                raise UpdateError(f"The {release['version']} release's signature isn't valid, so it wasn't installed.")
            # The signature covers the wheel's name, which holds the version: an old signed release can't pose as a new one.
            if _expected_sha256(sums.read_text(errors="replace"), updates.wheel_name(release["version"])) is None:
                raise UpdateError(f"The signed files aren't those of version {release['version']}, so it wasn't installed.")
        _download(assets[name], file)
        expected = _expected_sha256(sums.read_text(errors="replace"), name)
        if not expected or _sha256(file) != expected:
            file.unlink(missing_ok=True)
            raise UpdateError("The download didn't match the release's checksum, so it wasn't installed.")

    def _go_ahead(self, automatic: bool) -> bool:
        """True when it's time to go on: the owner asked, or the window is closed. False if automatic updates have been
        turned off since this started, including while it downloaded with the window closed."""
        if not automatic:
            return True
        while not self.now.is_set():
            if not updates.auto_on():
                log.info("Automatic updates were turned off, so the update isn't installing")
                self._say(None)
                return False
            if self.window_hidden():
                return True
            self._say("waiting")
            self.now.wait(WAIT_TICK)
        return True

    def _say(self, state: Optional[str]) -> None:
        if state != self.saying:
            self.saying = state
            self.progress(state, "")

    def _note_attempt(self, release: dict[str, Any]) -> None:
        from app.core.config import APP_VERSION
        (self.folder / ATTEMPT).write_text(json.dumps({"version": release["version"], "from": APP_VERSION, "at": time.time()}))

    # ------------------------------------------------------------------ Mac
    @staticmethod
    def _bundle() -> Path:
        return Path(sys.executable).resolve().parents[2]      # …/Syntropy Health.app/Contents/MacOS/Syntropy Health

    @staticmethod
    def _signed_by(app: Path) -> Optional[str]:
        """The developer's Team ID, or None for an app with no developer's signature (a build made on this computer). If
        codesign can't say which, nothing is installed."""
        out = _run(["codesign", "-dv", "--verbose=2", str(app)])
        text = out.stderr + out.stdout
        if out.returncode != 0:
            if "not signed at all" in text:
                return None
            raise UpdateError("This app's own signature couldn't be read, so the new one can't be checked against it.")
        team = re.search(r"^TeamIdentifier=(.+)$", text, re.M)
        if not team or team.group(1).strip() == "not set":
            return None
        if not re.fullmatch(r"[A-Z0-9]{10}", team.group(1).strip()):
            raise UpdateError("This app's own signature couldn't be read, so the new one can't be checked against it.")
        return team.group(1).strip()

    def _stage_mac(self, dmg: Path) -> None:
        bundle = self._bundle()
        if bundle.suffix != ".app":
            raise UpdateError("This copy of Syntropy Health isn't an app bundle, so it can't update itself.")
        if not os.access(bundle.parent, os.W_OK):
            raise UpdateError(f"Syntropy Health can't replace itself in {bundle.parent}. Download the new version and "
                              "drag it to Applications.")
        team = self._signed_by(bundle)
        mount = Path(tempfile.mkdtemp(prefix="syntropy-update-"))
        staged = bundle.with_name(f".{bundle.name}.update")
        attach = _run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount), str(dmg)])
        if attach.returncode != 0:
            raise UpdateError(f"The download couldn't be opened ({attach.stderr.strip()}).")
        try:
            new = mount / APP_BUNDLE
            if not new.exists():
                raise UpdateError("The download doesn't contain the app.")
            if _run(["codesign", "--verify", "--deep", "--strict", str(new)]).returncode != 0:
                raise UpdateError("The new app's signature isn't valid, so it wasn't installed.")
            if team:
                requirement = f'anchor apple generic and identifier "{BUNDLE_ID}" and certificate leaf[subject.OU] = "{team}"'
                if _run(["codesign", "--verify", "--deep", "--strict", f"-R={requirement}", str(new)]).returncode != 0:
                    raise UpdateError("The new app isn't signed by the same developer, so it wasn't installed.")
            _run(["rm", "-rf", str(staged)])
            copy = _run(["ditto", str(new), str(staged)])
            if copy.returncode != 0:
                raise UpdateError(f"The new app couldn't be copied ({copy.stderr.strip()}).")
        finally:
            _run(["hdiutil", "detach", "-quiet", str(mount)])
        dmg.unlink(missing_ok=True)
        self.staged = staged

    def _discard_staged(self) -> None:
        if self.staged:
            _run(["rm", "-rf", str(self.staged)])
            self.staged = None

    def _swap_mac(self, background: bool) -> None:
        bundle = self._bundle()
        assert self.staged is not None
        reopen = 'open -g "$app" --args --background' if background else 'open "$app"'
        script = f"""
app={shlex.quote(str(bundle))}
old={shlex.quote(str(bundle.with_name(f".{bundle.name}.old")))}
staged={shlex.quote(str(self.staged))}
exe={shlex.quote(str(Path(sys.executable).resolve()))}
ok={shlex.quote(str(self.folder / OK_MARK))}
bad={shlex.quote(str(self.folder / BAD_MARK))}
result={shlex.quote(str(self.folder / RESULT))}
note() {{
  clean=$(printf '%s' "$2" | tr -d '"\\\\' | tr '\\n\\r\\t' '   ')
  printf '{{"outcome":"%s","detail":"%s"}}\\n' "$1" "$clean" > "$result.tmp" && mv "$result.tmp" "$result"
}}
reopen() {{ {reopen}; }}
running() {{ ps -axo command= | grep -Fx -e "$exe" -e "$exe --background" > /dev/null; }}

while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.5; done
rm -rf "$old"
if ! why=$(mv "$app" "$old" 2>&1); then
  rm -rf "$staged"; note swap-failed "It couldn't replace itself: $why"; reopen; exit 1
fi
if ! why=$(mv "$staged" "$app" 2>&1); then
  mv "$old" "$app"; rm -rf "$staged"; note swap-failed "It couldn't put the new version in place: $why"; reopen; exit 1
fi
reopen

waited=0; gone=0; detail="The new version took too long to start."
while [ "$waited" -lt {WATCH_SECONDS} ]; do
  if [ -e "$ok" ]; then rm -rf "$old"; rm -f "$ok"; exit 0; fi
  if [ -e "$bad" ]; then detail=$(cat "$bad"); break; fi
  if running; then gone=0; else gone=$((gone + 2)); fi
  if [ "$gone" -ge {GONE_SECONDS} ]; then detail="The new version closed as soon as it opened."; break; fi
  sleep 2; waited=$((waited + 2))
done
ps -axo pid=,command= | grep -F -e " $exe" | grep -v -e "$exe [a-z]" | while read -r pid _; do kill "$pid" 2>/dev/null; done
sleep 2
rm -rf "$app"
mv "$old" "$app"
note rolled-back "$detail"
reopen
exit 1
"""
        path = self.folder / "apply-update.sh"
        path.write_text(script)
        # From a file: run from an argument, the script's own command line would hold the app's path, and "running" would
        # always find it.
        subprocess.Popen(["/bin/sh", str(path)], start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)

    # ------------------------------------------------------------------ Windows
    @staticmethod
    def _signature(path: Path) -> tuple[str, Optional[str]]:
        """(status, certificate thumbprint). The status is PowerShell's, "NotSigned" or "Valid" among others, or "Unknown"
        when it gave no answer."""
        ps = ("$s = Get-AuthenticodeSignature -LiteralPath $args[0]; "
              "\"$($s.Status)|$($s.SignerCertificate.Thumbprint)\"")
        out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps, str(path)])
        status, _, thumbprint = out.stdout.strip().partition("|")
        return status or "Unknown", thumbprint or None

    def _check_windows(self, setup: Path) -> None:
        status, signer = self._signature(Path(sys.executable).resolve())
        if status == "NotSigned":       # no developer's signature to compare with: the checksum above is the check
            return
        if status != "Valid":
            raise UpdateError("This app's own signature couldn't be read, so the new one can't be checked against it.")
        new_status, new_signer = self._signature(setup)
        if new_status != "Valid" or new_signer != signer:
            raise UpdateError("The new version isn't signed by the same developer, so it wasn't installed.")

    def _start_windows(self, setup: Path, background: bool) -> None:
        current = Path(sys.executable).resolve()
        if current.name != WINDOWS_APP:
            raise UpdateError("This copy of Syntropy Health wasn't installed by its setup program, so it can't update itself.")
        script = self.folder / "apply-update.ps1"
        script.write_text(WINDOWS_SCRIPT, encoding="utf-8-sig")     # with the mark, or PowerShell 5 misreads the file
        args = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script),
                "-AppPid", str(os.getpid()), "-Setup", str(setup), "-AppDir", str(current.parent),
                "-Relaunch", "background" if background else "window", "-Folder", str(self.folder),
                "-WatchSeconds", str(WATCH_SECONDS), "-GoneSeconds", str(GONE_SECONDS)]
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(args, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
