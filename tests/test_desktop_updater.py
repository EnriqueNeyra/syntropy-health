"""The Mac and Windows apps' updater (desktop/updater.py): what it downloads is checked before anything is installed, it
leaves the person's window alone, and an update that doesn't take is undone and reported instead of retried."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "desktop"))
import updater  # noqa: E402

from app.core import db  # noqa: E402
from app.services import updates  # noqa: E402

APP = b"the new app"
SUMS_OK = f"{hashlib.sha256(APP).hexdigest()}  {updates.MAC_ASSET}\n"
DMG_URL, SUMS_URL, SIG_URL = "https://github.com/x/mac.dmg", "https://github.com/x/SHA256SUMS", "https://github.com/x/SHA256SUMS.sig"


def _release(sums: str = SUMS_OK, signature: bytes | None = None) -> dict:
    assets = {updates.MAC_ASSET: DMG_URL, updates.CHECKSUMS_ASSET: f"mem:{sums}"}
    if signature is not None:
        assets[updates.SIGNATURE_ASSET] = "sig:" + base64.b64encode(signature).decode()
    return {"version": "9.1.0", "url": "https://example.test/release", "assets": assets}


def _from_memory(url, dest, limit=updater.MAX_DOWNLOAD):
    if url == DMG_URL:
        dest.write_bytes(APP)
    elif url.startswith("mem:"):
        dest.write_text(url[4:])
    else:
        dest.write_bytes(base64.b64decode(url[4:]))


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """An updater on a "Mac" whose downloads come from memory and whose install steps only record what they got."""
    monkeypatch.setattr(updater, "_download", _from_memory)
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    monkeypatch.setattr(updater, "WAIT_TICK", 0.01)
    monkeypatch.setattr(updates, "auto_on", lambda: True)
    state = SimpleNamespace(hidden=False, staged=[], swapped=[], progress=[], quit=threading.Event(), discarded=[], on_stage=None)

    def stage(self, file):
        state.staged.append(file.read_bytes())
        self.staged = file
        if state.on_stage:
            state.on_stage()

    monkeypatch.setattr(updater.Updater, "_stage_mac", stage)
    monkeypatch.setattr(updater.Updater, "_swap_mac", lambda self, background: state.swapped.append(background))
    monkeypatch.setattr(updater.Updater, "_discard_staged", lambda self: state.discarded.append(True))

    def progress(s, _d):
        state.progress.append(s)

    return updater.Updater(tmp_path / "updates", lambda: state.hidden, state.quit.set, progress), state


def _soon(fn, delay=0.05):
    threading.Timer(delay, fn).start()


def test_installs_a_checked_download(mac):
    u, state = mac
    u(_release(), False)
    assert state.staged == [APP] and state.swapped == [False]
    assert state.progress == ["downloading", "installing"]
    assert state.quit.wait(3)
    attempt = json.loads((u.folder / updater.ATTEMPT).read_text())
    assert attempt["version"] == "9.1.0" and attempt["at"] <= time.time()


def test_automatic_updates_wait_for_the_window_to_close(mac):
    u, state = mac
    _soon(lambda: setattr(state, "hidden", True))
    u(_release(), True)
    assert state.progress == ["downloading", "waiting", "installing"]
    assert state.swapped == [True]       # reopens in the background, as it was


def test_turning_automatic_updates_off_cancels_the_wait(mac, monkeypatch):
    u, state = mac
    monkeypatch.setattr(updates, "auto_on", lambda: False)
    u(_release(), True)
    assert state.progress == ["downloading", "waiting", None]
    assert not state.staged and not state.swapped and not state.quit.is_set()


def test_asking_to_install_now_ends_the_wait(mac):
    u, state = mac
    _soon(u.install_now)
    u(_release(), True)
    assert state.swapped == [False]      # the window is open, so it comes back with its window
    assert state.quit.wait(3)


def test_reopening_the_window_while_it_prepares_goes_back_to_waiting(mac):
    u, state = mac
    state.hidden = True

    def reopened():
        state.hidden = False
        _soon(lambda: setattr(state, "hidden", True))

    state.on_stage = reopened
    u(_release(), True)
    assert state.progress == ["downloading", "installing", "waiting", "installing"]
    assert state.swapped == [True]


def test_reopening_and_turning_it_off_throws_the_prepared_copy_away(mac, monkeypatch):
    u, state = mac
    state.hidden = True

    def reopened():
        state.hidden = False
        monkeypatch.setattr(updates, "auto_on", lambda: False)

    state.on_stage = reopened
    u(_release(), True)
    assert state.discarded == [True] and not state.swapped and not state.quit.is_set()


def test_refuses_a_download_that_doesnt_match(mac):
    u, state = mac
    with pytest.raises(updater.UpdateError, match="checksum"):
        u(_release(f"{'0' * 64}  {updates.MAC_ASSET}\n"), False)
    with pytest.raises(updater.UpdateError, match="checksum"):
        u(_release("no entry for this file\n"), False)
    assert not state.staged and not state.quit.is_set()


def test_a_release_without_this_computers_app(mac):
    u, _ = mac
    with pytest.raises(updater.UpdateError, match="no app for this computer"):
        u({"version": "9.1.0", "url": "https://example.test/release", "assets": {}}, False)


# ---------------------------------------------------------------------------
# The release key
# ---------------------------------------------------------------------------

@pytest.fixture
def release_key(monkeypatch):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(updates, "RELEASE_PUBLIC_KEY", base64.b64encode(public).decode())
    return key


def test_a_signed_release_installs(mac, release_key):
    u, state = mac
    u(_release(signature=release_key.sign(SUMS_OK.encode())), False)
    assert state.staged == [APP]


def test_without_a_signature_nothing_installs_once_the_app_has_a_key(mac, release_key):
    u, state = mac
    with pytest.raises(updater.UpdateError, match="isn't signed"):
        u(_release(), False)
    assert not state.staged


@pytest.mark.parametrize("bad", [b"", b"x" * 64, None])
def test_a_wrong_signature_stops_it_before_the_app_is_even_downloaded(mac, release_key, bad):
    u, state = mac
    other = Ed25519PrivateKey.generate().sign(SUMS_OK.encode())
    with pytest.raises(updater.UpdateError, match="signature isn't valid"):
        u(_release(signature=bad if bad is not None else other), False)
    assert not state.staged and not (u.folder / updates.MAC_ASSET).exists()


def test_a_signature_for_other_checksums_is_refused(mac, release_key):
    u, _ = mac
    forged = f"{'0' * 64}  {updates.MAC_ASSET}\n"
    with pytest.raises(updater.UpdateError, match="signature isn't valid"):
        u(_release(forged, signature=release_key.sign(SUMS_OK.encode())), False)


def test_no_key_yet_means_no_signature_needed(mac):
    assert updates.RELEASE_PUBLIC_KEY == "" or pytest.skip("the key has been made")
    u, state = mac
    u(_release(), False)
    assert state.staged == [APP]


def test_a_broken_built_in_key_fails_closed():
    assert not updates.signature_is_valid(b"x", b"y" * 64, "not base64!")
    assert not updates.signature_is_valid(b"x", b"y" * 64, base64.b64encode(b"short").decode())


def test_the_key_maker_makes_a_key_that_works(capsys, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import new_release_key

    new_release_key.main()
    out = capsys.readouterr().out
    private = serialization.load_pem_private_key(re.search(r"-----BEGIN.*END PRIVATE KEY-----", out, re.S).group().encode(), None)
    public = re.search(r'RELEASE_PUBLIC_KEY = "(.+)"', out).group(1)
    assert updates.signature_is_valid(b"sums", private.sign(b"sums"), public)


def test_the_release_workflow_can_read_the_key_from_the_code():
    # desktop.yml reads this line with sed, and fails a release when the key's there and the secret isn't.
    text = (ROOT / "app/services/updates.py").read_text()
    assert re.search(r'^RELEASE_PUBLIC_KEY = "[^"]*"$', text, re.M)
    workflow = (ROOT / ".github/workflows/desktop.yml").read_text()
    assert "app/services/updates.py" in workflow and "openssl pkeyutl -sign -rawin" in workflow and "::error::The apps check" in workflow


# ---------------------------------------------------------------------------
# Where downloads may come from
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,ok", [
    ("https://github.com/EnriqueNeyra/syntropy-health/releases/download/v1/x.dmg", True),
    ("https://objects.githubusercontent.com/x", True), ("https://release-assets.githubusercontent.com/x", True),
    ("http://github.com/x", False), ("https://github.com.evil.example/x", False), ("https://evilgithub.com/x", False),
    ("https://evilgithubusercontent.com/x", False), ("https://example.test/x", False), ("file:///etc/passwd", False),
    ("https://[bad/", False), ("", False)])
def test_only_github_over_https(url, ok):
    assert updates.trusted_asset_url(url) is ok


@pytest.fixture
def local_site():
    """GitHub serves release files through a redirect."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/release/mac.dmg":
                self.send_response(302)
                self.send_header("Location", "/storage/mac.dmg")
                self.end_headers()
            elif self.path == "/storage/mac.dmg":
                self.send_response(200)
                self.send_header("Content-Length", str(len(APP)))
                self.end_headers()
                self.wfile.write(APP)
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_download_follows_the_release_redirect(tmp_path, local_site, monkeypatch):
    monkeypatch.setattr(updates, "trusted_asset_url", lambda url: True)
    updater._download(f"{local_site}/release/mac.dmg", tmp_path / "mac.dmg")
    assert (tmp_path / "mac.dmg").read_bytes() == APP
    with pytest.raises(updater.UpdateError, match="couldn't be downloaded"):
        updater._download(f"{local_site}/missing", tmp_path / "missing")


def test_download_refuses_an_address_elsewhere(tmp_path):
    with pytest.raises(updater.UpdateError, match="other than GitHub"):
        updater._download("http://example.test/mac.dmg", tmp_path / "mac.dmg")


def test_download_refuses_a_redirect_elsewhere(tmp_path, local_site, monkeypatch):
    monkeypatch.setattr(updates, "trusted_asset_url", lambda url: url.endswith("/release/mac.dmg"))
    with pytest.raises(updater.UpdateError, match="other than GitHub"):
        updater._download(f"{local_site}/release/mac.dmg", tmp_path / "mac.dmg")


def test_download_stops_at_the_limit(tmp_path, local_site, monkeypatch):
    monkeypatch.setattr(updates, "trusted_asset_url", lambda url: True)
    with pytest.raises(updater.UpdateError, match="far larger"):
        updater._download(f"{local_site}/release/mac.dmg", tmp_path / "mac.dmg", limit=3)


# ---------------------------------------------------------------------------
# Signatures on a Mac
# ---------------------------------------------------------------------------

def _done(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


@pytest.mark.parametrize("result,answer", [
    (_done(stderr="Identifier=x\nTeamIdentifier=RJ2YA2S2C8\n"), "RJ2YA2S2C8"),
    (_done(stderr="Identifier=x\nSignature=adhoc\nTeamIdentifier=not set\n"), None),
    (_done(1, stderr="/x: code object is not signed at all\n"), None)])
def test_who_signed_this_app(monkeypatch, result, answer):
    monkeypatch.setattr(updater, "_run", lambda cmd: result)
    assert updater.Updater._signed_by(Path("/x")) == answer


@pytest.mark.parametrize("result", [_done(1, stderr="codesign: something went wrong"), _done(stderr="TeamIdentifier=<a lie>\n"), _done(127)])
def test_if_codesign_cant_say_nothing_installs(monkeypatch, result):
    monkeypatch.setattr(updater, "_run", lambda cmd: result)
    if result.returncode == 0 and "lie" not in result.stderr:
        pytest.fail("bad test")
    with pytest.raises(updater.UpdateError, match="couldn't be read"):
        updater.Updater._signed_by(Path("/x"))


@pytest.fixture
def stage(monkeypatch, tmp_path):
    """_stage_mac with the system's tools stood in for: a bundle someone renamed, and a disk image holding the usual name."""
    bundle = tmp_path / "Apps" / "Renamed.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    exe = bundle / "Contents" / "MacOS" / "Syntropy Health"
    exe.write_text("")
    monkeypatch.setattr(updater.sys, "executable", str(exe))
    mount = tmp_path / "mount"
    (mount / updater.APP_BUNDLE).mkdir(parents=True)
    monkeypatch.setattr(updater.tempfile, "mkdtemp", lambda prefix: str(mount))
    ran = []
    answers = {"team": "RJ2YA2S2C8", "verify": 0, "require": 0}

    def run(cmd):
        ran.append(cmd)
        if cmd[0] == "codesign" and cmd[1] == "-dv":
            return _done(stderr=f"TeamIdentifier={answers['team']}\n")
        if cmd[0] == "codesign":
            return _done(answers["require"] if any(c.startswith("-R=") for c in cmd) else answers["verify"])
        if cmd[0] == "ditto":
            Path(cmd[2]).mkdir()
        return _done()

    monkeypatch.setattr(updater, "_run", run)
    u = updater.Updater(tmp_path / "updates", lambda: False, lambda: None, lambda s, d: None)
    dmg = tmp_path / "update.dmg"
    dmg.write_bytes(APP)
    return SimpleNamespace(u=u, dmg=dmg, ran=ran, answers=answers, bundle=bundle, mount=mount)


def test_a_signed_app_requires_the_same_developer_and_app(stage):
    stage.u._stage_mac(stage.dmg)
    require = next(c for cmd in stage.ran for c in cmd if c.startswith("-R="))
    assert 'identifier "io.syntropyhealth.desktop"' in require and 'leaf[subject.OU] = "RJ2YA2S2C8"' in require and "anchor apple generic" in require
    assert stage.u.staged == stage.bundle.with_name(".Renamed.app.update") and stage.u.staged.exists()


def test_a_renamed_app_still_finds_the_new_one(stage):
    # A bundle renamed Syntropy.app could never update, and blamed the download.
    stage.u._stage_mac(stage.dmg)
    assert ["ditto", str(stage.mount / updater.APP_BUNDLE), str(stage.u.staged)] in stage.ran


def test_another_developers_app_is_refused(stage):
    stage.answers["require"] = 3
    with pytest.raises(updater.UpdateError, match="same developer"):
        stage.u._stage_mac(stage.dmg)
    assert stage.u.staged is None
    assert any(cmd[:2] == ["hdiutil", "detach"] for cmd in stage.ran)


def test_a_broken_signature_is_refused(stage):
    stage.answers["verify"] = 1
    with pytest.raises(updater.UpdateError, match="isn't valid"):
        stage.u._stage_mac(stage.dmg)


def test_a_download_without_the_app_is_refused(stage):
    shutil.rmtree(stage.mount / updater.APP_BUNDLE)
    with pytest.raises(updater.UpdateError, match="doesn't contain"):
        stage.u._stage_mac(stage.dmg)


# ---------------------------------------------------------------------------
# Signatures on Windows
# ---------------------------------------------------------------------------

def _windows_signatures(monkeypatch, current, new):
    monkeypatch.setattr(updater.sys, "executable", "C:/App/Syntropy Health.exe")
    monkeypatch.setattr(updater.Updater, "_signature", staticmethod(lambda path: new if "setup" in str(path) else current))
    return updater.Updater(Path("."), lambda: True, lambda: None, lambda s, d: None)


@pytest.mark.parametrize("current,new", [(("NotSigned", None), ("NotSigned", None)), (("NotSigned", None), ("Valid", "AB")), (("Valid", "AB"), ("Valid", "AB"))])
def test_windows_signature_passes(monkeypatch, current, new):
    _windows_signatures(monkeypatch, current, new)._check_windows(Path("C:/x/setup.exe"))


@pytest.mark.parametrize("current,new", [
    (("Valid", "AB"), ("NotSigned", None)), (("Valid", "AB"), ("Valid", "CD")), (("Valid", "AB"), ("HashMismatch", "AB")), (("Valid", "AB"), ("Unknown", None)),
    (("Unknown", None), ("Valid", "AB")), (("HashMismatch", "AB"), ("HashMismatch", "AB"))])
def test_windows_signature_fails_closed(monkeypatch, current, new):
    # PowerShell returning nothing used to skip the check.
    with pytest.raises(updater.UpdateError):
        _windows_signatures(monkeypatch, current, new)._check_windows(Path("C:/x/setup.exe"))


@pytest.mark.parametrize("output,answer", [("Valid|AB12\n", ("Valid", "AB12")), ("NotSigned|\n", ("NotSigned", None)), ("", ("Unknown", None))])
def test_windows_signature_is_read(monkeypatch, output, answer):
    monkeypatch.setattr(updater, "_run", lambda cmd: _done(stdout=output))
    assert updater.Updater._signature(Path("C:/x")) == answer


def test_windows_update_starts_its_script_with_paths_as_parameters(monkeypatch, tmp_path):
    started = {}
    monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "O'Brien" / updater.WINDOWS_APP))
    monkeypatch.setattr(updater.subprocess, "Popen", lambda args, **kw: started.update(args=args, kw=kw))
    tmp_path.joinpath("updates").mkdir()
    u = updater.Updater(tmp_path / "updates", lambda: True, lambda: None, lambda s, d: None)
    u._start_windows(tmp_path / "setup.exe", True)
    args = started["args"]
    assert args[:2] == ["powershell", "-NoProfile"] and "-File" in args
    assert args[args.index("-Relaunch") + 1] == "background"
    assert args[args.index("-AppPid") + 1] == str(os.getpid())
    assert args[args.index("-AppDir") + 1] == str(tmp_path / "O'Brien")
    script = (tmp_path / "updates" / "apply-update.ps1").read_bytes()
    assert script.startswith(b"\xef\xbb\xbf")                       # PowerShell 5 needs the mark for anything not ASCII
    assert b"O'Brien" not in script


def test_windows_refuses_to_update_a_copy_setup_didnt_install(monkeypatch, tmp_path):
    monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "python.exe"))
    u = updater.Updater(tmp_path, lambda: True, lambda: None, lambda s, d: None)
    with pytest.raises(updater.UpdateError, match="wasn't installed"):
        u._start_windows(tmp_path / "setup.exe", False)


def test_windows_script_keeps_a_copy_and_goes_back():
    script = updater.WINDOWS_SCRIPT
    copy, setup, reopen = script.index("Copy-Over $AppDir $previous"), script.index("Start-Process -FilePath $Setup"), script.rindex("Open-App\n$waited")
    assert copy < setup < reopen
    assert "/LOG=" in script and "/RELAUNCH" not in script and "$run.ExitCode -ne 0" in script
    assert "'update-ok'" in script and "'update-bad'" in script and "'update-result.json'" in script
    assert "Copy-Over $previous $AppDir" in script and "'rolled-back'" in script
    assert (updater.OK_MARK, updater.BAD_MARK, updater.RESULT) == ("update-ok", "update-bad", "update-result.json")
    assert "LASTEXITCODE -lt 8" in script       # robocopy's way of saying it worked


def test_windows_installer_handles_updates():
    text = (ROOT / "desktop/installer.iss").read_text()
    assert "MinVersion=10.0" in text
    assert 'Type: filesandordirs; Name: "{app}\\_internal"' in text
    run_value = next(line for line in text.splitlines() if "Tasks: startup" in line)
    assert "Check: not WizardSilent" in run_value       # a silent update would turn Open at Login back on
    assert "function PrepareToInstall" in text and "procedure DeinitializeSetup" in text
    # Updates stop what runs from the app's folder; only uninstalling removes its server task.
    prepare = text[text.index("function PrepareToInstall"):text.index("procedure CurStepChanged")]
    assert "StopProcesses" in prepare and "Unregister" not in prepare and "StopWhatRunsFromApp" not in prepare
    # Versions already out there still pass /RELAUNCH.
    assert "Check: Relaunch('background')" in text and "Check: Relaunch('window')" in text


def test_a_release_with_a_bad_mac_signature_fails_the_workflow():
    workflow = (ROOT / ".github/workflows/apps.yml").read_text()
    assert 'certificate leaf[subject.OU] = "RJ2YA2S2C8"' in workflow
    assert "xcrun stapler validate" in workflow and "spctl -a -t open" in workflow


# ---------------------------------------------------------------------------
# Going back when an update doesn't take: the real script, with stand-ins for `open` and a real process for the app
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "darwin", reason="the Mac's swap script")
class TestMacSwap:
    @pytest.fixture
    def app(self, monkeypatch, tmp_path):
        monkeypatch.setattr(updater, "GONE_SECONDS", 4)
        monkeypatch.setattr(updater, "WATCH_SECONDS", 12)
        bundle = tmp_path / "Apps" / "Syntropy Health.app"
        exe = bundle / "Contents" / "MacOS" / "Syntropy Health"
        exe.parent.mkdir(parents=True)
        shutil.copy("/bin/cat", exe)        # a command line that's exactly the app's, as `running` looks for
        subprocess.run(["codesign", "--force", "--sign", "-", str(exe)], check=True, capture_output=True)   # or macOS kills the copy
        (bundle / "version").write_text("old")
        staged = bundle.with_name(".Syntropy Health.app.update")
        (staged / "Contents" / "MacOS").mkdir(parents=True)
        shutil.copy("/bin/cat", staged / "Contents" / "MacOS" / "Syntropy Health")
        subprocess.run(["codesign", "--force", "--sign", "-", str(staged / "Contents" / "MacOS" / "Syntropy Health")], check=True, capture_output=True)
        (staged / "version").write_text("new")
        folder = tmp_path / "updates"
        folder.mkdir()
        stubs = tmp_path / "bin"
        stubs.mkdir()
        opens = tmp_path / "opens.log"
        (stubs / "open").write_text(f'#!/bin/sh\necho "$@" >> {opens}\n')
        (stubs / "open").chmod(0o755)
        monkeypatch.setenv("PATH", f"{stubs}:{os.environ['PATH']}")
        monkeypatch.setattr(updater.sys, "executable", str(exe))
        gone = subprocess.Popen(["/usr/bin/true"])
        gone.wait()
        monkeypatch.setattr(updater.os, "getpid", lambda: gone.pid)     # this app has quit, as far as the script can tell
        u = updater.Updater(folder, lambda: True, lambda: None, lambda s, d: None)
        u.staged = staged
        launched = []
        yield SimpleNamespace(u=u, bundle=bundle, exe=exe, folder=folder, opens=opens, launched=launched)
        subprocess.run(["chflags", "-R", "nouchg", str(bundle.parent)], check=False)
        for proc in launched:
            proc.terminate()

    @staticmethod
    def _until(check, seconds=25):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if check():
                return True
            time.sleep(0.1)
        return False

    def _new_version_running(self, app):
        app.launched.append(subprocess.Popen([str(app.exe)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL))

    def test_success_throws_the_copy_away_once_the_new_version_says_so(self, app):
        app.u._swap_mac(False)
        assert self._until(lambda: app.opens.exists())
        assert (app.bundle / "version").read_text() == "new"
        old = app.bundle.with_name(".Syntropy Health.app.old")
        assert (old / "version").read_text() == "old"     # kept until it's known to work
        self._new_version_running(app)
        (app.folder / updater.OK_MARK).write_text("ok")
        assert self._until(lambda: not old.exists())
        assert not (app.folder / updater.RESULT).exists()
        assert (app.bundle / "version").read_text() == "new"
        assert app.launched[0].poll() is None

    def test_a_new_version_that_closes_is_replaced_by_the_old_one(self, app):
        app.u._swap_mac(True)
        assert self._until(lambda: (app.folder / updater.RESULT).exists())
        assert (app.bundle / "version").read_text() == "old"
        result = json.loads((app.folder / updater.RESULT).read_text())
        assert result["outcome"] == "rolled-back" and "closed as soon as it opened" in result["detail"]
        assert self._until(lambda: len(app.opens.read_text().splitlines()) == 2)
        lines = app.opens.read_text().splitlines()
        assert len(lines) == 2 and all(line.startswith("-g ") and line.endswith("--background") for line in lines)

    def test_a_new_version_that_cant_use_the_data_says_why(self, app):
        app.u._swap_mac(False)
        assert self._until(lambda: app.opens.exists())
        self._new_version_running(app)
        (app.folder / updater.BAD_MARK).write_text('Couldn\'t "upgrade" \\ the database\nat all')
        assert self._until(lambda: (app.folder / updater.RESULT).exists())
        result = json.loads((app.folder / updater.RESULT).read_text())
        assert result["outcome"] == "rolled-back" and "upgrade" in result["detail"]
        assert (app.bundle / "version").read_text() == "old"
        assert self._until(lambda: app.launched[0].poll() is not None, 5)      # the new version doesn't linger

    def test_a_locked_app_is_left_alone_and_reported(self, app):
        subprocess.run(["chflags", "uchg", str(app.bundle)], check=True)
        app.u._swap_mac(False)
        assert self._until(lambda: app.opens.exists())
        subprocess.run(["chflags", "nouchg", str(app.bundle)], check=True)
        result = json.loads((app.folder / updater.RESULT).read_text())
        assert result["outcome"] == "swap-failed" and "couldn't replace itself" in result["detail"]
        assert (app.bundle / "version").read_text() == "old"
        assert len(app.opens.read_text().splitlines()) == 1


def _attempt(folder: Path, version: str = "99.0.0", at: float | None = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / updater.ATTEMPT).write_text(json.dumps({"version": version, "from": "1.0.0", "at": time.time() if at is None else at}))


@pytest.fixture
def fresh_state(isolated_data):
    updates.set_progress(None)
    yield isolated_data / "updates"
    updates.set_progress(None)
    updates.set_installer(None)


def test_nothing_on_file_only_clears_old_installers(fresh_state):
    folder = fresh_state
    folder.mkdir()
    (folder / updates.WINDOWS_ASSET).write_bytes(b"old installer")
    (folder / "previous").mkdir()
    updater.review_last_attempt(folder)
    assert list(folder.iterdir()) == [] and updates.failed_update() is None


def test_a_swap_that_failed_is_reported_and_not_retried_for_a_day(fresh_state, client):
    folder = fresh_state
    _attempt(folder)
    (folder / updater.RESULT).write_text(json.dumps({"outcome": "swap-failed", "detail": "It couldn't replace itself: Operation not permitted"}))
    updater.review_last_attempt(folder)
    failed = updates.failed_update()
    assert failed["version"] == "99.0.0" and "Operation not permitted" in failed["detail"]
    assert updates.status()["progress"]["state"] == "failed" and "Operation not permitted" in updates.status()["progress"]["detail"]
    assert updates.failed_lately("99.0.0") and not updates.failed_lately("99.0.1")
    assert any("Operation not permitted" in p["text"] for p in updates.problems())
    assert not list(folder.iterdir())

    # A later run of the app, with its memory empty, doesn't start the loop again.
    updates.set_progress(None)
    from app.core import settings
    settings.set("updates.latest", {"version": "99.0.0", "url": "x", "notes": "", "published_at": None, "assets": {}})
    settings.set("updates.checked_at", time.time())
    settings.set("updates.auto", True)
    installs = []
    updates.set_installer(lambda release, automatic: installs.append(release))
    import asyncio
    asyncio.run(updates.tick())
    time.sleep(0.05)
    assert installs == []

    # A day later it tries again, and the owner can ask at any time.
    updates.note_failed_update("99.0.0", "x")
    failed = updates.failed_update()
    updates._failed_file().write_text(json.dumps({**failed, "at": time.time() - updates.RETRY_FAILED_AFTER - 1}))
    asyncio.run(updates.tick())
    assert _until_true(lambda: installs)


def _until_true(check, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return False


def test_an_attempt_with_no_word_from_its_script_still_counts_as_failed(fresh_state):
    # Windows: setup never ran or stopped early, and the app is on the old version: the comparison says so.
    folder = fresh_state
    _attempt(folder)
    updater.review_last_attempt(folder)
    assert "didn't finish installing" in updates.failed_update()["detail"]


def test_going_back_puts_the_database_as_it_was(fresh_state, monkeypatch):
    folder = fresh_state
    _attempt(folder, at=123.0)
    (folder / updater.RESULT).write_text(json.dumps({"outcome": "rolled-back", "detail": "The new version closed as soon as it opened."}))
    asked = []
    monkeypatch.setattr(db, "restore_pre_upgrade", lambda since: asked.append(since) or "the copy")
    updater.review_last_attempt(folder)
    assert asked == [123.0]
    assert "closed as soon" in updates.failed_update()["detail"]


def test_powershell_results_are_read(fresh_state):
    folder = fresh_state
    _attempt(folder)
    (folder / updater.RESULT).write_bytes(b"\xef\xbb\xbf" + json.dumps({"outcome": "stuck", "detail": "from PowerShell"}).encode())
    updater.review_last_attempt(folder)
    assert updates.failed_update()["detail"] == "from PowerShell"


def test_the_new_version_leaves_the_attempt_for_itself(fresh_state):
    folder = fresh_state
    _attempt(folder, version="0.0.1")      # this version or older: this is what was being installed
    updater.review_last_attempt(folder)
    assert (folder / updater.ATTEMPT).exists() and updates.failed_update() is None

    (folder / updates.WINDOWS_ASSET).write_bytes(b"used installer")
    updater.started_fine(folder)
    assert [p.name for p in folder.iterdir()] == [updater.OK_MARK]


def test_a_new_version_that_cant_work_asks_to_go_back(fresh_state):
    folder = fresh_state
    _attempt(folder, version="0.0.1")
    assert updater.started_badly(folder, "It couldn't upgrade") is True
    assert (folder / updater.BAD_MARK).read_text() == "It couldn't upgrade"


def test_with_nobody_to_go_back_to_it_stays_and_explains(fresh_state):
    folder = fresh_state
    assert updater.started_badly(folder, "x") is False                     # no attempt at all
    _attempt(folder, version="0.0.1", at=time.time() - updater.WATCH_SECONDS - 1)
    assert updater.started_badly(folder, "x") is False                     # its script has given up
    assert not (folder / updater.BAD_MARK).exists()


def test_an_old_version_cant_ask_to_go_back(fresh_state):
    folder = fresh_state
    _attempt(folder, version="99.0.0")
    assert updater.started_badly(folder, "x") is False


def test_installing_while_waiting_wakes_the_installer(fresh_state):
    from app.core import settings
    settings.set("updates.latest", {"version": "99.0.0", "url": "x", "notes": "", "published_at": None, "assets": {}})
    woken = []

    class Installer:
        def __call__(self, release, automatic):
            pass

        def install_now(self):
            woken.append(True)

    updates.set_installer(Installer())
    updates.set_progress("waiting")
    updates.install(automatic=True)
    assert not woken
    updates.install()
    assert woken == [True]


def test_asking_clears_the_failure(fresh_state):
    from app.core import settings
    settings.set("updates.latest", {"version": "99.0.0", "url": "x", "notes": "", "published_at": None, "assets": {}})
    updates.note_failed_update("99.0.0", "x")
    updates.set_installer(lambda release, automatic: None)
    updates.install()
    assert updates.failed_update() is None
