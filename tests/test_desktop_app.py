"""The Mac and Windows app's start-up (desktop/syntropy_desktop.py), as far as it can be checked off those systems."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))
import syntropy_desktop  # noqa: E402


def test_windows_window_gets_no_png_icon(monkeypatch):
    # WinForms reads a window icon with System.Drawing.Icon, which throws on a PNG while creating the window: the
    # Windows app never opened. Without one, the window takes the icon built into the .exe.
    monkeypatch.setattr(syntropy_desktop.sys, "platform", "win32")
    assert syntropy_desktop.window_icon() == {}
    monkeypatch.setattr(syntropy_desktop.sys, "platform", "darwin")
    assert syntropy_desktop.window_icon() == {}           # the bundle's icon.icns
    monkeypatch.setattr(syntropy_desktop.sys, "platform", "linux")
    icon = Path(syntropy_desktop.window_icon()["icon"])
    assert icon.suffix == ".png" and icon.exists()


def test_webview2_is_only_needed_on_windows(monkeypatch):
    monkeypatch.setattr(syntropy_desktop.sys, "platform", "darwin")
    assert syntropy_desktop.webview2_installed()


def test_page_opens_in_the_browser_without_a_window(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNTROPY_DATA_DIR", str(tmp_path))
    opened = []
    monkeypatch.setattr(syntropy_desktop.webbrowser, "open", opened.append)
    app = syntropy_desktop.DesktopApp(background=True)
    app.browser_mode = True
    app.open_page("/#/settings/general/updates")
    app.show()
    assert opened == [f"{app.server.url}/#/settings/general/updates", app.server.url]


def _packaged_mac(monkeypatch, tmp_path, exe):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(syntropy_desktop.sys, "platform", "darwin")
    monkeypatch.setattr(syntropy_desktop.sys, "frozen", True, raising=False)
    monkeypatch.setattr(syntropy_desktop.sys, "executable", exe)


def test_mac_login_item_is_valid_and_follows_the_app(monkeypatch, tmp_path):
    import plistlib
    # A folder with "&" in its name used to make the login item's XML invalid, so it never opened.
    _packaged_mac(monkeypatch, tmp_path, "/Users/a/R&D/Syntropy Health.app/Contents/MacOS/Syntropy Health")
    syntropy_desktop.set_open_at_login(True)
    agent = plistlib.loads(syntropy_desktop._launch_agent().read_bytes())
    assert agent["ProgramArguments"] == [syntropy_desktop.sys.executable, "--background"]
    assert agent["AssociatedBundleIdentifiers"] == ["io.syntropyhealth.desktop"]

    # Moved into Applications: the login item opens the new place.
    moved = "/Applications/Syntropy Health.app/Contents/MacOS/Syntropy Health"
    monkeypatch.setattr(syntropy_desktop.sys, "executable", moved)
    syntropy_desktop.heal_open_at_login()
    assert syntropy_desktop._launch_agent_program() == moved

    # Opened from the disk image (or a temporary copy macOS made): left pointing at the installed one.
    for temporary in ("/Volumes/Syntropy Health/Syntropy Health.app/Contents/MacOS/Syntropy Health",
                      "/private/var/folders/x/AppTranslocation/1/d/Syntropy Health.app/Contents/MacOS/Syntropy Health"):
        monkeypatch.setattr(syntropy_desktop.sys, "executable", temporary)
        assert syntropy_desktop.runs_from_temporary_place()
        syntropy_desktop.heal_open_at_login()
        assert syntropy_desktop._launch_agent_program() == moved

    syntropy_desktop.set_open_at_login(False)
    assert not syntropy_desktop.open_at_login()


@pytest.mark.skipif(sys.platform == "win32", reason="a Mac LaunchAgent: launchctl, and a home folder from HOME")
def test_uninstalling_removes_only_this_apps_server_service(monkeypatch, tmp_path):
    import plistlib
    monkeypatch.setenv("HOME", str(tmp_path))
    ran = []
    monkeypatch.setattr(syntropy_desktop.subprocess, "run", lambda cmd, **_: ran.append(cmd))
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    plist = agents / "io.syntropyhealth.server.plist"
    bundle = Path("/Applications/Syntropy Health.app")

    plist.write_bytes(plistlib.dumps({"ProgramArguments": [str(Path.home() / ".local/bin/syntropy-health"), "serve"]}))
    syntropy_desktop.remove_server_service(bundle)          # installed with install.sh --server: not the app's
    assert plist.exists() and not ran

    plist.write_bytes(plistlib.dumps({"ProgramArguments": [str(bundle / "Contents/MacOS/Syntropy Health"), "serve"]}))
    syntropy_desktop.remove_server_service(bundle)
    assert not plist.exists() and ran[0][:2] == ["launchctl", "bootout"]
