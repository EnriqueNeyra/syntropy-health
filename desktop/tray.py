"""The menu bar item (Mac) and tray icon (Windows) that keep Syntropy Health reachable while its window is closed."""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any, Callable

from syntropy_desktop import APP_NAME, resource

Actions = dict[str, Callable[[], None]]


def _labels(state: dict[str, Any]) -> list[tuple[str, str, bool | None]]:
    """(action, title, checked) for each menu line; "" is a separator and "info" lines can't be chosen."""
    if state.get("joined"):
        # This Mac opens the household's server on another computer; it runs no server of its own.
        return [("show", f"Open {APP_NAME}", None), ("browser", "Open in Browser", None), ("", "", None),
                ("info", f"Connected to {state['joined']}", None), ("switch", "Use a Different Server…", None),
                ("", "", None), ("updates", "Check for Updates…", None), ("quit", f"Quit {APP_NAME}", None)]
    lines: list[tuple[str, str, bool | None]] = [
        ("show", f"Open {APP_NAME}", None),
        ("browser", "Open in Browser", None),
        ("", "", None),
        ("lan", "Let Your iPhone and Other Devices Connect", state["lan"]),
    ]
    if state.get("address"):
        lines.append(("info", f"    Address: {state['address']}", None))
    lines += [("login", "Open at Login", state["login"]), ("", "", None), ("updates", "Check for Updates…", None),
              ("quit", f"Quit {APP_NAME}", None)]
    return lines


class NoTray:
    """Linux and anything else: the window is the app; closing it quits."""

    def __init__(self, actions: Actions, *_: Any):
        self.actions = actions

    def start(self) -> None: ...
    def update(self, state: dict[str, Any]) -> None: ...
    def stop(self) -> None: ...
    def activate(self) -> None: ...

    def hidden_hint(self) -> None:
        self.actions["quit"]()


# ---------------------------------------------------------------------------
# Mac: NSStatusItem, plus Dock and Cmd-Q behaviour
# ---------------------------------------------------------------------------

class MacTray:
    def __init__(self, actions: Actions, state: Callable[[], dict[str, Any]], on_app_quit: Callable[[], None]):
        import objc
        from AppKit import NSObject
        from webview.platforms.cocoa import BrowserView

        self.actions, self.state = actions, state
        self.item = None

        # Clicking the Dock icon brings the window back; Cmd-Q and Quit in the Dock really quit (the close button
        # only hides the window).
        def reopen(_self: Any, _app: Any, _visible: bool) -> bool:
            threading.Thread(target=actions["show"], daemon=True).start()
            return True

        def terminate(_self: Any, _app: Any) -> int:
            on_app_quit()
            return 1   # NSTerminateNow

        objc.classAddMethods(BrowserView.AppDelegate, [
            objc.selector(reopen, selector=b"applicationShouldHandleReopen:hasVisibleWindows:", signature=b"Z@:@Z"),
            objc.selector(terminate, selector=b"applicationShouldTerminate:", signature=b"I@:@"),
        ])

        run = self._run

        class MenuTarget(NSObject):
            def choose_(self, sender: Any) -> None:
                run(str(sender.representedObject()))

        self.target = MenuTarget.alloc().init()

    def _run(self, action: str) -> None:
        # Off the main thread: actions may show a dialog or restart the server, which waits on the main thread.
        threading.Thread(target=self.actions[action], daemon=True).start()

    def start(self) -> None:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(self._build)

    def _build(self) -> None:
        from AppKit import NSImage, NSStatusBar, NSVariableStatusItemLength

        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        image = NSImage.alloc().initWithContentsOfFile_(str(resource("menubar@2x.png")))
        image.setSize_((18, 18))
        image.setTemplate_(True)
        self.item.button().setImage_(image)
        self.item.button().setToolTip_(APP_NAME)
        self._menu(self.state())
        window = self.item.button().window()
        logging.getLogger("syntropy.desktop").info("Menu bar item ready (window %s)", window.windowNumber() if window else None)

    def _menu(self, state: dict[str, Any]) -> None:
        from AppKit import NSMenu, NSMenuItem, NSOffState, NSOnState

        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        for action, title, checked in _labels(state):
            if not action:
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "choose:", "")
            item.setTarget_(self.target)
            item.setRepresentedObject_(action)
            if checked is not None:
                item.setState_(NSOnState if checked else NSOffState)
            if action == "info":
                item.setEnabled_(False)
            menu.addItem_(item)
        self.item.setMenu_(menu)

    def update(self, state: dict[str, Any]) -> None:
        from PyObjCTools import AppHelper
        if self.item:
            AppHelper.callAfter(self._menu, state)

    def activate(self) -> None:
        from AppKit import NSApp
        from PyObjCTools import AppHelper
        AppHelper.callAfter(lambda: NSApp.activateIgnoringOtherApps_(True))

    def stop(self) -> None:
        from AppKit import NSStatusBar
        from PyObjCTools import AppHelper
        if self.item:
            AppHelper.callAfter(NSStatusBar.systemStatusBar().removeStatusItem_, self.item)

    def hidden_hint(self) -> None: ...


# ---------------------------------------------------------------------------
# Windows: notification-area icon
# ---------------------------------------------------------------------------

class WindowsTray:
    def __init__(self, actions: Actions, state: Callable[[], dict[str, Any]], _on_app_quit: Callable[[], None]):
        import pystray
        from PIL import Image

        self.pystray = pystray
        self.actions, self.state_fn = actions, state
        self.state = state()
        self.hinted = False
        self.icon = pystray.Icon("SyntropyHealth", Image.open(resource("icon-256.png")), APP_NAME, menu=self._menu())

    def _menu(self) -> Any:
        items = []
        for action, title, checked in _labels(self.state):
            if not action:
                items.append(self.pystray.Menu.SEPARATOR)
            elif action == "info":
                items.append(self.pystray.MenuItem(title.strip(), None, enabled=False))
            else:
                items.append(self.pystray.MenuItem(
                    title, (lambda a: lambda: threading.Thread(target=self.actions[a], daemon=True).start())(action),
                    checked=(lambda c: (lambda _item: c))(checked) if checked is not None else None,
                    default=action == "show"))
        return self.pystray.Menu(*items)

    def start(self) -> None:
        threading.Thread(target=self.icon.run, daemon=True, name="tray").start()

    def update(self, state: dict[str, Any]) -> None:
        self.state = state
        self.icon.menu = self._menu()
        self.icon.update_menu()

    def activate(self) -> None: ...

    def stop(self) -> None:
        self.icon.stop()

    def hidden_hint(self) -> None:
        if not self.hinted:
            self.hinted = True
            self.icon.notify(f"{APP_NAME} is still running, so your iPhone can keep syncing. Quit it from this icon.",
                             APP_NAME)


def make_tray(actions: Actions, state: Callable[[], dict[str, Any]], on_app_quit: Callable[[], None]) -> Any:
    if sys.platform == "darwin":
        return MacTray(actions, state, on_app_quit)
    if sys.platform == "win32":
        return WindowsTray(actions, state, on_app_quit)
    return NoTray(actions)
