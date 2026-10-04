"""The Mac and Windows app's start-up (desktop/syntropy_desktop.py), as far as it can be checked off those systems."""

from __future__ import annotations

import sys
from pathlib import Path

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
