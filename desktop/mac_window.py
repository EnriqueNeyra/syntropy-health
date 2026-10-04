"""
The Mac window: Syntropy Health's interface drawn edge to edge in a native window, the way Mac apps look and behave.

- The title bar is transparent and the sidebar shows the system's sidebar material, with the window buttons in it, as in
  Mail or Notes. The page leaves room for them and says where the window can be dragged (html[data-shell=mac] in
  app.css, and desktopBridge in app.js).
- A full menu bar with the usual shortcuts: Settings (⌘,), New Chat (⌘N), Find (⌘F), the sections (⌘1–⌘8), Back and
  Forward (⌘[ ⌘]), Reload (⌘R) and text size (⌘+ ⌘− ⌘0). Right-clicking the Dock icon offers the menu bar item's choices.
- Swiping with two fingers goes back and forward, and the window remembers its size and place.
- The app is in the Dock (and ⌘-Tab) only while its window is open. Closed, it runs on in the menu bar alone, like
  other menu bar apps; opening the window from there puts it back in the Dock.
- Another site in the window (a health system's sign-in) gets an ordinary title bar naming the site, with a
  "Syntropy Health" button that returns to the app: those pages don't leave room for the window buttons or offer a way
  back.

The page talks to the window through a WebKit message handler, "syntropyDesktop": which theme it shows (so the sidebar
material and title bar match it), its title (for the Window menu and Mission Control), when it's ready to be seen, and
when the top of the window is dragged. pywebview's own JavaScript bridge can't be used: it builds functions with
new Function(), which the page's Content-Security-Policy forbids. The window runs scripts in the page with
evaluateJavaScript, which that policy doesn't restrict.
"""

from __future__ import annotations

import json
import logging
import threading
import webbrowser
from typing import Any, Callable, Optional

import AppKit
import objc
from Foundation import NSObject
from PyObjCTools import AppHelper

log = logging.getLogger("syntropy.desktop")

HANDLER = "syntropyDesktop"
HELP_URL = "https://health.syntropylabs.io/docs/"
# (title, route) for ⌘1 onwards, in the sidebar's order.
SECTIONS = [("Overview", "overview"), ("Ask", "ask"), ("Trends", "trends"), ("Workouts", "workouts"),
            ("Journal", "journal"), ("Timeline", "timeline"), ("Records", "records"), ("Sources", "sources")]
ZOOM_STEPS = [0.75, 0.85, 0.9, 1.0, 1.1, 1.2, 1.35, 1.5]

WKScriptMessageHandler = objc.protocolNamed("WKScriptMessageHandler")


class _Bridge(NSObject, protocols=[WKScriptMessageHandler]):
    """Receives the page's messages (on the main thread)."""

    def initWithCallback_(self, callback):
        self = objc.super(_Bridge, self).init()
        if self is not None:
            self.callback = callback
        return self

    def userContentController_didReceiveScriptMessage_(self, _controller, message):
        try:
            data = json.loads(str(message.body()))       # the page sends JSON text
            if isinstance(data, dict):
                # Where the message came from: some requests are only taken from this Mac's own server.
                origin = message.frameInfo().securityOrigin()
                data["_origin"] = f"{origin.protocol()}://{origin.host()}:{origin.port()}"
                self.callback(data)
        except Exception:  # noqa: BLE001 - a bad message must never take the app down
            log.exception("Couldn't handle a message from the page")


class _MenuTarget(NSObject):
    def initWithActions_(self, actions):
        self = objc.super(_MenuTarget, self).init()
        if self is not None:
            self.actions = actions
        return self

    def perform_(self, sender):
        # Menu items carry their action as the represented object, buttons as their identifier.
        key = sender.representedObject() if sender.respondsToSelector_(b"representedObject") else sender.identifier()
        action = self.actions.get(str(key))
        if action:
            action()

    def validateMenuItem_(self, item):
        return True


class _URLObserver(NSObject):
    """Tells the window each time the web view's address changes (key-value observing of WKWebView.URL)."""

    def initWithCallback_(self, callback):
        self = objc.super(_URLObserver, self).init()
        if self is not None:
            self.callback = callback
        return self

    def observeValueForKeyPath_ofObject_change_context_(self, _path, _obj, _change, _context):
        try:
            self.callback()
        except Exception:  # noqa: BLE001
            log.exception("Couldn't follow the window's address")


def _origin(scheme: Any, host: Any, port: Any) -> tuple[str, str, int]:
    scheme = str(scheme or "").lower()
    port = int(port or 0) or {"http": 80, "https": 443}.get(scheme, 0)
    return scheme, str(host or "").lower(), port


def _url_origin(url: Any) -> Optional[tuple[str, str, int]]:
    """The origin of an NSURL; None for pages with none of their own (about:blank, the offline page)."""
    if url is None or str(url.scheme() or "").lower() not in ("http", "https"):
        return None
    return _origin(url.scheme(), url.host(), url.port())


def _set_in_dock(on: bool) -> None:
    # A "regular" app has a Dock icon, a menu bar and a place in ⌘-Tab; an "accessory" one has none of them, but its
    # menu bar item and windows keep working.
    app = AppKit.NSApplication.sharedApplication()
    policy = AppKit.NSApplicationActivationPolicyRegular if on else AppKit.NSApplicationActivationPolicyAccessory
    if app.activationPolicy() != policy:
        app.setActivationPolicy_(policy)


class MacWindow:
    """Dresses pywebview's window. Everything here runs on the main thread."""

    def __init__(self, window: Any, *, background: bool, prefs: Any, tray_actions: dict[str, Callable[[], None]],
                 dock_menu_state: Callable[[], dict[str, Any]],
                 on_request: Optional[Callable[[dict[str, Any]], None]] = None,
                 home_url: Optional[Callable[[], str]] = None):
        self.window = window                   # the pywebview Window
        self.background = background
        self.prefs = prefs
        self.tray_actions = tray_actions
        self.dock_menu_state = dock_menu_state
        self.on_request = on_request           # what the app itself acts on (joining a server...)
        self.ns_window = None
        self.webview = None
        self.shown = False
        self.zoom = float(prefs.get("zoom", 1.0) or 1.0)
        self.home_url = home_url                            # Syntropy Health's own pages (this Mac's or a joined server)
        self.away = False                                   # showing another site
        self.home_bar = None
        if background:              # opened at login: only the menu bar item, from the start (this is the main thread)
            _set_in_dock(False)

    # ----------------------------------------------------------------- setup
    def install(self) -> None:
        AppHelper.callAfter(self._install)

    def _install(self) -> None:
        from webview.platforms.cocoa import BrowserView

        inst = BrowserView.instances.get(self.window.uid)
        if inst is None:
            AppHelper.callLater(0.05, self._install)
            return
        win, wk = inst.window, inst.webview
        self.ns_window, self.webview = win, wk

        # Listen to the page first: it says when it's ready to be shown.
        self.bridge = _Bridge.alloc().initWithCallback_(self._on_message)
        wk.configuration().userContentController().addScriptMessageHandler_name_(self.bridge, HANDLER)

        # Edge to edge: a transparent title bar over the content, the window buttons in a taller bar (a toolbar with no
        # items, as in Mail), no title text.
        # pywebview paints the title bar's background (so it doesn't follow the window color); clear it.
        for view in win.contentView().superview().subviews():
            if "Titlebar" in type(view).__name__ and view.respondsToSelector_(b"setBackgroundColor:"):
                view.setBackgroundColor_(AppKit.NSColor.clearColor())
        win.setStyleMask_(win.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView)
        win.setTitlebarAppearsTransparent_(True)
        win.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        toolbar = AppKit.NSToolbar.alloc().initWithIdentifier_("SyntropyHealth")
        toolbar.setShowsBaselineSeparator_(False)
        win.setToolbar_(toolbar)
        win.setToolbarStyle_(AppKit.NSWindowToolbarStyleUnified)

        # The sidebar material behind a transparent web view; the page paints everything but its sidebar.
        container = AppKit.NSView.alloc().initWithFrame_(win.contentView().frame())
        container.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        effect = AppKit.NSVisualEffectView.alloc().initWithFrame_(container.bounds())
        effect.setMaterial_(AppKit.NSVisualEffectMaterialSidebar)
        effect.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        effect.setState_(AppKit.NSVisualEffectStateFollowsWindowActiveState)
        effect.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        container.addSubview_(effect)
        wk.removeFromSuperview()
        wk.setFrame_(container.bounds())
        wk.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        wk.setValue_forKey_(False, "drawsBackground")
        container.addSubview_(wk)
        win.setContentView_(container)
        win.makeFirstResponder_(wk)

        wk.setAllowsBackForwardNavigationGestures_(True)
        if self.zoom != 1.0:
            wk.setPageZoom_(self.zoom)
        # Size and place are remembered between launches (and restored now, when saved before).
        win.setFrameAutosaveName_("Syntropy Health")

        self._menus()
        self._dock_menu()
        self.url_observer = _URLObserver.alloc().initWithCallback_(self._location_changed)
        wk.addObserver_forKeyPath_options_context_(self.url_observer, "URL", 0, None)
        # The page normally says it's ready well before this.
        AppHelper.callLater(4.0, self._show_if_hidden)

    def _show_if_hidden(self) -> None:
        if not self.shown and not self.background:
            self.show()

    def show(self) -> None:
        self.shown = True
        _set_in_dock(True)
        self.ns_window.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)

    def in_dock(self, on: bool) -> None:
        """In the Dock while the window is open, only in the menu bar while it's closed (from any thread)."""
        AppHelper.callAfter(_set_in_dock, on)

    # ----------------------------------------------------------------- the page
    def _on_message(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "ready":
            if not self.shown and not self.background:
                self.show()
        elif kind == "theme":
            name = AppKit.NSAppearanceNameDarkAqua if msg.get("theme") == "dark" else AppKit.NSAppearanceNameAqua
            self.ns_window.setAppearance_(AppKit.NSAppearance.appearanceNamed_(name))
        elif kind == "title":
            self.ns_window.setTitle_(str(msg.get("title") or "Syntropy Health")[:200])
        elif kind == "drag":
            event = AppKit.NSApp.currentEvent()
            if event is not None and event.type() in (AppKit.NSEventTypeLeftMouseDown, AppKit.NSEventTypeLeftMouseDragged):
                self.ns_window.performWindowDragWithEvent_(event)
        elif kind == "titlebar-double-click":
            action = AppKit.NSUserDefaults.standardUserDefaults().stringForKey_("AppleActionOnDoubleClick") or "Maximize"
            if action == "Minimize":
                self.ns_window.miniaturize_(None)
            elif action != "None":
                self.ns_window.zoom_(None)
        elif self.on_request:
            threading.Thread(target=self.on_request, args=(msg,), daemon=True).start()

    # ----------------------------------------------------------------- other sites
    def _home(self) -> set[tuple[str, str, int]]:
        url = AppKit.NSURL.URLWithString_(self.home_url()) if self.home_url else None
        home = _url_origin(url)
        if home is None:
            return set()
        if home[1] in ("localhost", "127.0.0.1"):      # this Mac's server answers to both names
            return {(home[0], name, home[2]) for name in ("localhost", "127.0.0.1")}
        return {home}

    def _location_changed(self) -> None:
        origin = _url_origin(self.webview.URL())
        home = self._home()
        away = bool(home) and origin is not None and origin not in home
        if away:
            self.ns_window.setTitle_(origin[1])        # where you are, as a browser would show it
        if away == self.away:
            return
        self.away = away
        win = self.ns_window
        if away:
            # An ordinary title bar: the page starts below it instead of under the window buttons.
            win.setStyleMask_(win.styleMask() & ~AppKit.NSWindowStyleMaskFullSizeContentView)
            win.setTitlebarAppearsTransparent_(False)
            win.setTitleVisibility_(AppKit.NSWindowTitleVisible)
            win.addTitlebarAccessoryViewController_(self._home_bar())
        else:
            if self.home_bar is not None:
                self.home_bar.removeFromParentViewController()
            win.setStyleMask_(win.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView)
            win.setTitlebarAppearsTransparent_(True)
            win.setTitleVisibility_(AppKit.NSWindowTitleHidden)
            win.setTitle_("Syntropy Health")           # until the page names itself again

    def _home_bar(self) -> Any:
        if self.home_bar is None:
            button = AppKit.NSButton.buttonWithTitle_target_action_("‹ Syntropy Health", self.menu_target, "perform:")
            button.setIdentifier_("home")
            button.setBezelStyle_(AppKit.NSBezelStyleRounded)
            button.setToolTip_("Leave this page and go back to Syntropy Health")
            button.sizeToFit()
            holder = AppKit.NSView.alloc().initWithFrame_(((0, 0), (button.frame().size.width + 12, 30)))
            button.setFrameOrigin_((6, (30 - button.frame().size.height) / 2))
            holder.addSubview_(button)
            self.home_bar = AppKit.NSTitlebarAccessoryViewController.alloc().init()
            self.home_bar.setView_(holder)
            self.home_bar.setLayoutAttribute_(AppKit.NSLayoutAttributeLeft)
        return self.home_bar

    def go_home(self) -> None:
        """Back to the last of Syntropy Health's own pages in this window's history, else its start page."""
        wk, home = self.webview, self._home()
        for item in reversed(list(wk.backForwardList().backList() or [])):
            if _url_origin(item.URL()) in home:
                wk.goToBackForwardListItem_(item)
                return
        if self.home_url:
            url = AppKit.NSURL.URLWithString_(self.home_url().rstrip("/") + "/#/sources")
            wk.loadRequest_(AppKit.NSURLRequest.requestWithURL_(url))

    def run_js(self, script: str) -> None:
        if self.webview is not None:
            self.webview.evaluateJavaScript_completionHandler_(script, None)

    def go(self, route: str) -> None:
        self.show()
        self.run_js(f"location.hash = {json.dumps(route)}")

    def set_zoom(self, step: Optional[int]) -> None:
        if step is None:
            self.zoom = 1.0
        else:
            i = min(range(len(ZOOM_STEPS)), key=lambda k: abs(ZOOM_STEPS[k] - self.zoom))
            self.zoom = ZOOM_STEPS[max(0, min(len(ZOOM_STEPS) - 1, i + step))]
        self.webview.setPageZoom_(self.zoom)
        self.prefs.set("zoom", self.zoom)

    # ----------------------------------------------------------------- menus
    def _menus(self) -> None:
        actions: dict[str, Callable[[], None]] = {
            "settings": lambda: self.go("#/settings"),
            "new-chat": lambda: self.go("#/ask?new=1"),
            "add-data": lambda: self.go("#/sources"),
            "summary": lambda: self.go("#/report"),
            "find": lambda: (self.show(), self.run_js(
                "(document.querySelector('.content input[type=search], #top-search input') || {focus(){}}).focus()")),
            "back": lambda: self.webview.goBack_(None),
            "home": self.go_home,
            "forward": lambda: self.webview.goForward_(None),
            "reload": lambda: self.webview.reload_(None),
            "zoom-in": lambda: self.set_zoom(1),
            "zoom-out": lambda: self.set_zoom(-1),
            "zoom-reset": lambda: self.set_zoom(None),
            "help": lambda: webbrowser.open(HELP_URL),
            # These may restart the server, which waits on the main thread: run them beside it.
            "browser": lambda: threading.Thread(target=self.tray_actions["browser"], daemon=True).start(),
            "lan": lambda: threading.Thread(target=self.tray_actions["lan"], daemon=True).start(),
            "switch": lambda: threading.Thread(target=self.tray_actions["switch"], daemon=True).start(),
            "updates": lambda: threading.Thread(target=self.tray_actions["updates"], daemon=True).start(),
        }
        for _, route in SECTIONS:
            actions[f"go-{route}"] = (lambda r=route: self.go(f"#/{r}"))
        self.menu_target = _MenuTarget.alloc().initWithActions_(actions)

        def item(title: str, action: Optional[str] = None, key: str = "", mods: int = AppKit.NSEventModifierFlagCommand,
                 selector: Optional[str] = None) -> Any:
            it = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, selector or ("perform:" if action else None), key)
            it.setKeyEquivalentModifierMask_(mods)
            if action:
                it.setTarget_(self.menu_target)
                it.setRepresentedObject_(action)
            return it

        def menu(title: str, items: list[Any]) -> Any:
            m = AppKit.NSMenu.alloc().initWithTitle_(title)
            for it in items:
                m.addItem_(it if it is not None else AppKit.NSMenuItem.separatorItem())
            top = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
            top.setSubmenu_(m)
            return top, m

        cmd, shift, opt, ctrl = (AppKit.NSEventModifierFlagCommand, AppKit.NSEventModifierFlagShift,
                                 AppKit.NSEventModifierFlagOption, AppKit.NSEventModifierFlagControl)
        main = AppKit.NSMenu.alloc().initWithTitle_("Main")
        services = AppKit.NSMenu.alloc().initWithTitle_("Services")
        services_item = item("Services")
        services_item.setSubmenu_(services)
        app_top, _ = menu("Syntropy Health", [
            item("About Syntropy Health", selector="orderFrontStandardAboutPanel:"),
            item("Check for Updates…", "updates"), None,
            item("Settings…", "settings", ","), None,
            services_item, None,
            item("Hide Syntropy Health", key="h", selector="hide:"),
            item("Hide Others", key="h", mods=cmd | opt, selector="hideOtherApplications:"),
            item("Show All", selector="unhideAllApplications:"), None,
            item("Quit Syntropy Health", key="q", selector="terminate:"),
        ])
        AppKit.NSApp.setServicesMenu_(services)
        file_top, _ = menu("File", [
            item("New Chat", "new-chat", "n"),
            item("Add Data…", "add-data", "n", cmd | shift),
            item("Visit Summary", "summary", "p", cmd | shift), None,
            item("Open in Browser", "browser", "o", cmd | shift), None,
            item("Close Window", key="w", selector="performClose:"),
        ])
        edit_top, _ = menu("Edit", [
            item("Undo", key="z", selector="undo:"), item("Redo", key="z", mods=cmd | shift, selector="redo:"), None,
            item("Cut", key="x", selector="cut:"), item("Copy", key="c", selector="copy:"),
            item("Paste", key="v", selector="paste:"), item("Select All", key="a", selector="selectAll:"), None,
            item("Find…", "find", "f"),
        ])
        view_top, _ = menu("View", [
            item("Reload", "reload", "r"), None,
            item("Actual Size", "zoom-reset", "0"), item("Zoom In", "zoom-in", "="), item("Zoom Out", "zoom-out", "-"), None,
            item("Enter Full Screen", key="f", mods=cmd | ctrl, selector="toggleFullScreen:"),
        ])
        go_top, _ = menu("Go", [
            item("Back", "back", "["), item("Forward", "forward", "]"),
            item("Back to Syntropy Health", "home", "h", cmd | shift), None,
            *[item(title, f"go-{route}", str(i + 1)) for i, (title, route) in enumerate(SECTIONS)], None,
            item("Settings", "settings", ""),
        ])
        window_top, window_menu = menu("Window", [
            item("Minimize", key="m", selector="performMiniaturize:"), item("Zoom", selector="performZoom:"), None,
            item("Bring All to Front", selector="arrangeInFront:"),
        ])
        help_top, help_menu = menu("Help", [item("Syntropy Health Help", "help", "?")])
        for top in (app_top, file_top, edit_top, view_top, go_top, window_top, help_top):
            main.addItem_(top)
        AppKit.NSApp.setMainMenu_(main)
        AppKit.NSApp.setWindowsMenu_(window_menu)
        AppKit.NSApp.setHelpMenu_(help_menu)

    def _dock_menu(self) -> None:
        """Right-clicking the Dock icon: the network switch and Open in Browser, like the menu bar item."""
        owner = self

        def dock_menu(_self: Any, _app: Any) -> Any:
            state = owner.dock_menu_state()
            m = AppKit.NSMenu.alloc().init()
            if state.get("joined"):
                switch = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Use a Different Server…", "perform:", "")
                switch.setTarget_(owner.menu_target)
                switch.setRepresentedObject_("switch")
                m.addItem_(switch)
                return m
            lan = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Let Your iPhone and Other Devices Connect", "perform:", "")
            lan.setTarget_(owner.menu_target)
            lan.setRepresentedObject_("lan")
            lan.setState_(AppKit.NSControlStateValueOn if state.get("lan") else AppKit.NSControlStateValueOff)
            m.addItem_(lan)
            browser = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Open in Browser", "perform:", "")
            browser.setTarget_(owner.menu_target)
            browser.setRepresentedObject_("browser")
            m.addItem_(browser)
            return m

        from webview.platforms.cocoa import BrowserView
        objc.classAddMethods(BrowserView.AppDelegate, [
            objc.selector(dock_menu, selector=b"applicationDockMenu:", signature=b"@@:@"),
        ])
