"""
The Windows window's dealings with web pages, where pywebview's defaults would let a page do too much and leave a
person stranded on a sign-in page:

- The title bar names any other site, and a bar with a back button appears under it (the tray menu offers the same).
  pywebview turns off the browser's keys and menus outside debugging, so nothing else in the window can go back.
- A page can't start another program: the WebView2 event for external schemes is refused, and a link only leaves the
  window when a person clicked it (sites.py).
- pywebview's bridge, which any page can reach, is dropped. Only pages the app writes itself are listened to.

Written against pywebview's EdgeChrome class, given as an argument so the rules can be tested without Windows.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Optional

import sites

log = logging.getLogger("syntropy.desktop")

APP_NAME = "Syntropy Health"


def _text(value: Any) -> str:
    return str(value) if value is not None else ""


def guard_edge(edge_class: Any, module: Any, on_source: Callable[[str], None],
               on_own_message: Callable[[dict[str, Any]], None]) -> None:
    """Replaces what pywebview subscribes to WebView2's events. It looks each handler up when the view is ready, so this
    has to run before webview.start. ``module`` is webview.platforms.edgechromium."""
    module.js_bridge_call = sites.drop_bridge_call
    real_ready, real_source, real_notify = edge_class.on_webview_ready, edge_class.on_source_changed, edge_class.on_script_notify

    def on_new_window_request(_self: Any, _sender: Any, args: Any) -> None:
        args.set_Handled(True)
        url = _text(args.get_Uri())
        # Set by WebView2 only for a real click. A page's own a.click() or window.open() doesn't count.
        if getattr(args, "IsUserInitiated", True) is False:
            log.warning("Refused a link opened by a page without a click")
            return
        import webbrowser
        webbrowser.open(url)

    def on_source_changed(self: Any, sender: Any, args: Any) -> None:
        real_source(self, sender, args)
        on_source(_text(sender.Source))

    def on_script_notify(self: Any, sender: Any, args: Any) -> None:
        try:
            body = json.loads(_text(args.get_WebMessageAsJson()))
        except ValueError:
            return
        if isinstance(body, list) and body and body[0] == "_pywebviewAlert":      # alert() on a sign-in page
            real_notify(self, sender, args)
        elif isinstance(body, str):
            try:
                message = json.loads(body)
            except ValueError:
                return
            if isinstance(message, dict) and sites.is_own_page_token(message.pop("token", None)):
                message["_own_page"] = True
                threading.Thread(target=on_own_message, args=(message,), daemon=True).start()

    def on_webview_ready(self: Any, sender: Any, args: Any) -> None:
        real_ready(self, sender, args)
        if args.IsSuccess:
            try:
                sender.CoreWebView2.LaunchingExternalUriScheme += refuse_external_scheme
            except AttributeError:      # an older WebView2 runtime asks the person itself
                log.info("This WebView2 runtime has no external scheme event")

    edge_class.on_new_window_request = on_new_window_request
    edge_class.on_source_changed = on_source_changed
    edge_class.on_script_notify = on_script_notify
    edge_class.on_webview_ready = on_webview_ready


def refuse_external_scheme(_sender: Any, args: Any) -> None:
    args.Cancel = True
    log.warning("Refused a page's attempt to start another program")


class WindowsWindow:
    def __init__(self, window: Any, location: sites.Location, home_url: Callable[[], str],
                 on_request: Callable[[dict[str, Any]], None], on_away_changed: Callable[[], None]):
        self.window = window
        self.location = location
        self.home_url = home_url
        self.on_request = on_request
        self.on_away_changed = on_away_changed
        self.away = False
        self.bar: Any = None
        self.bar_label: Any = None
        self.lock = threading.Lock()

    def guard(self) -> None:
        import webview.platforms.winforms  # noqa: F401 - loads what EdgeChrome needs, in pywebview's own order
        from webview.platforms import edgechromium

        edgechromium.webbrowser = sites.only_when_clicked(lambda: True)     # on_new_window_request has already asked
        guard_edge(edgechromium.EdgeChrome, edgechromium, self.source_changed, self.on_request)

    def source_changed(self, url: str) -> None:
        # On WebView2's thread, which set_title has to wait for.
        threading.Thread(target=self.apply, args=(url,), daemon=True).start()

    def apply(self, url: str) -> None:
        with self.lock:
            away = self.location.is_away(url)
            changed = away != self.away
            self.away = away
            name: Optional[str] = sites.site_name(url) if away else None
            self.window.set_title(f"{name} - {APP_NAME}" if name else APP_NAME)
            try:
                self.show_bar(name)
            except Exception:  # noqa: BLE001 - the title and the tray menu still say where you are and go back
                log.exception("Couldn't show the back bar")
        if changed:
            self.on_away_changed()

    def go_home(self) -> None:
        self.window.load_url(f"{self.home_url().rstrip('/')}/#/sources")

    def show_bar(self, name: Optional[str]) -> None:
        from webview.platforms.winforms import BrowserView

        form = BrowserView.instances.get(self.window.uid)
        if form is None:
            return

        def update() -> Any:
            if self.bar is None:
                self.build_bar(form)
            self.bar_label.Text = f"You're on {name}" if name else ""
            self.bar.Visible = bool(name)

        from System import Func, Type
        form.Invoke(Func[Type](update))

    def build_bar(self, form: Any) -> None:
        import System.Windows.Forms as WinForms

        bar = WinForms.ToolStrip()
        bar.Dock = WinForms.DockStyle.Top
        bar.GripStyle = WinForms.ToolStripGripStyle.Hidden
        back = WinForms.ToolStripButton(f"‹ Back to {APP_NAME}")
        back.Click += lambda *_: threading.Thread(target=self.go_home, daemon=True).start()
        label = WinForms.ToolStripLabel("")
        label.Alignment = WinForms.ToolStripItemAlignment.Right
        bar.Items.Add(back)
        bar.Items.Add(label)
        bar.Visible = False
        # Added last, so it's docked first, and the web view fills what's left.
        form.Controls.Add(bar)
        self.bar, self.bar_label = bar, label
