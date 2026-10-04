"""
Syntropy Health for Mac and Windows.

The app runs the same server as a home-server install, on this computer, and shows it in a native window (WebKit on a
Mac, Edge WebView2 on Windows). Closing the window keeps the server running, with a menu bar (Mac) or tray (Windows)
icon, so the iPhone app can keep syncing and automations keep running. From that icon:

- Let your iPhone and other devices connect: listen on the home network instead of only this computer.
- Open at login: start in the background when you log in.
- Check for updates (see updater.py; Settings → General → Updates has the choices).
- Quit.

A Mac can instead join the household's server running on another computer (chosen on first launch): then it runs no
server of its own, and the window opens that one, where each person signs in as themselves.

Run with a command (``serve``, ``mcp``, ``backup``, ``info``...) it behaves like the ``syntropy-health`` command
instead, without a window; that's how AI apps start its MCP server and how ``service install`` runs it.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional

CLI_COMMANDS = {"serve", "service", "backup", "update", "info", "mcp", "version", "accounts", "dev", "-h", "--help"}
APP_NAME = "Syntropy Health"
FIRST_PORT = 8000
log = logging.getLogger("syntropy.desktop")


def resource(name: str) -> Path:
    """A file shipped with the app (icons), from the bundle or from this folder when run from source."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / "icons" / name


# ---------------------------------------------------------------------------
# Preferences and single instance
# ---------------------------------------------------------------------------

class Prefs:
    def __init__(self, data: Path):
        self.path = data / "desktop.json"
        try:
            self.values: dict[str, Any] = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.values = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value
        self.path.write_text(json.dumps(self.values, indent=2))


class SingleInstance:
    """The first copy listens on a private local socket; a second copy asks it to show its window, then exits."""

    def __init__(self, data: Path, on_show: Callable[[], None]):
        self.file = data / "desktop.instance"
        self.on_show = on_show

    def signal_existing(self) -> bool:
        try:
            info = json.loads(self.file.read_text())
            with socket.create_connection(("127.0.0.1", info["port"]), timeout=2) as s:
                s.sendall(f"{info['token']} show\n".encode())
                return s.recv(16).startswith(b"ok")
        except (OSError, ValueError, KeyError):
            return False

    def listen(self) -> None:
        token = secrets.token_hex(16)
        on_show = self.on_show

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                line = self.rfile.readline(200).decode(errors="ignore").split()
                if line == [token, "show"]:
                    on_show()
                    self.wfile.write(b"ok\n")

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self.file.write_text(json.dumps({"port": server.server_address[1], "token": token, "pid": os.getpid()}))
        if os.name != "nt":
            self.file.chmod(0o600)
        threading.Thread(target=server.serve_forever, daemon=True, name="instance").start()


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

def port_free(host: str, port: int) -> bool:
    # Something already answering on this computer (a Docker install, another copy with other data) means taken...
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # ...but connections left in TIME_WAIT by this app's last run don't: moving to a new port would break the
        # phone's pairing. (Like uvicorn's own bind. Windows needs no flag, and there it would allow sharing a port.)
        if sys.platform != "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


class Server:
    def __init__(self) -> None:
        self.server = None
        self.thread: Optional[threading.Thread] = None
        self.host = "127.0.0.1"
        self.port = FIRST_PORT

    @property
    def url(self) -> str:
        """The address the window and "Open in Browser" use."""
        return f"http://localhost:{self.port}"

    @property
    def check_url(self) -> str:
        # The server always listens on 127.0.0.1; "localhost" may try ::1 first.
        return f"http://127.0.0.1:{self.port}"

    def start(self, host: str, port: int) -> None:
        import uvicorn
        from app.main import app

        self.host, self.port = host, port
        # Pairing then gives the iPhone this computer's address, and syntropyhealth.local is announced.
        os.environ["SYNTROPY_LISTEN_HOST"], os.environ["SYNTROPY_LISTEN_PORT"] = host, str(port)
        cfg = uvicorn.Config(app, host=host, port=port, loop="asyncio", http="h11", ws="none", lifespan="on",
                             proxy_headers=True, access_log=False, log_config=None)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="server")
        self.thread.start()
        # Wait for this server to be listening (uvicorn sets `started` once bound), not just for something to answer
        # on the port: another program there would otherwise pass for it.
        deadline = time.time() + 60
        while time.time() < deadline and self.thread.is_alive():
            if self.server.started:
                return
            time.sleep(0.1)
        raise RuntimeError(f"The server didn't start on port {port}. See {log_path()}.")

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(15)

    def restart(self, host: str) -> None:
        self.stop()
        self.start(host, self.port)

    def status(self) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(f"{self.check_url}/api/status", timeout=3) as r:
                return json.loads(r.read())
        except (OSError, ValueError):
            return {}


def choose_port(saved: Optional[int]) -> int:
    """The last port used, else 8000, else the next free one (a Docker install may already have 8000)."""
    for port in ([saved] if saved else []) + list(range(FIRST_PORT, FIRST_PORT + 100)):
        if port and port_free("0.0.0.0", port):
            return port
    raise RuntimeError("No free port between 8000 and 8099.")


# ---------------------------------------------------------------------------
# Open at login
# ---------------------------------------------------------------------------

LOGIN_LABEL = "io.syntropyhealth.desktop"


def _launch_agent() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LOGIN_LABEL}.plist"


def open_at_login() -> bool:
    if sys.platform == "darwin":
        return _launch_agent().exists()
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                winreg.QueryValueEx(key, APP_NAME)
                return True
        except OSError:
            return False
    return False


def set_open_at_login(on: bool) -> None:
    exe = sys.executable
    if sys.platform == "darwin":
        path = _launch_agent()
        if on:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LOGIN_LABEL}</string>
  <key>ProgramArguments</key><array><string>{exe}</string><string>--background</string></array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
</dict></plist>
""")
        else:
            path.unlink(missing_ok=True)
    elif sys.platform == "win32":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe}" --background')
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------

def user_agent() -> str:
    """The page recognises the desktop app by "SyntropyHealthDesktop/<version> (Mac|Windows)" (see theme.js)."""
    from app.core.config import APP_VERSION
    if sys.platform == "darwin":
        base = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/18.0 Safari/605.1.15")
        return f"{base} SyntropyHealthDesktop/{APP_VERSION} (Mac)"
    base = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 "
            "Safari/537.36 Edg/130.0.0.0")
    return f"{base} SyntropyHealthDesktop/{APP_VERSION} (Windows)"


def dark_mode() -> bool:
    """Whether the system is in dark mode, for the window's color before the page draws."""
    if sys.platform == "darwin":
        try:
            from AppKit import NSUserDefaults
            return NSUserDefaults.standardUserDefaults().stringForKey_("AppleInterfaceStyle") == "Dark"
        except ImportError:
            return False
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        except OSError:
            return False
    return False


def log_path() -> Path:
    from app.core import config
    return config.data_dir() / "desktop.log"


def setup_logging() -> None:
    handler = RotatingFileHandler(log_path(), maxBytes=5_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    stream = handler.stream
    sys.stdout = sys.stderr = stream        # a windowed app has no console


class DesktopApp:
    def __init__(self, background: bool):
        from app.core import config

        self.data = config.data_dir()
        self.prefs = Prefs(self.data)
        self.server = Server()
        self.window = None
        self.tray = None
        self.quitting = False
        self.background = background
        self.switching = threading.Lock()
        self.mac = None            # mac_window.MacWindow on a Mac
        self.hidden = background   # the window is closed (the app runs on in the menu bar or tray)
        self.browser_mode = False  # Windows without WebView2: pages open in the default browser instead

    # ----------------------------------------------------------- state shown in the menu
    @property
    def lan(self) -> bool:
        return self.server.host == "0.0.0.0"

    def lan_address(self) -> Optional[str]:
        from app.services import network
        if network.advertiser.name:
            return f"http://{network.advertiser.name}:{self.server.port}"
        found = network.home_addresses()
        return f"http://{found[0]}:{self.server.port}" if found else None

    @property
    def joined(self) -> Optional[str]:
        """The household server this Mac opens instead of running its own (None: it's the server)."""
        return self.prefs.get("server") if self.prefs.get("role") == "join" else None

    def menu_state(self) -> dict[str, Any]:
        if self.joined:
            return {"joined": self.prefs.get("server_name") or urllib.parse.urlsplit(self.joined).hostname}
        return {"lan": self.lan, "login": open_at_login(), "address": self.lan_address() if self.lan else None}

    @property
    def home_url(self) -> str:
        return self.joined or self.server.url

    # ----------------------------------------------------------- actions
    def show(self) -> None:
        if self.browser_mode:
            return self.open_browser()
        self.hidden = False
        if self.window:
            if self.mac:
                self.mac.shown = True
            self.window.show()
            self.window.restore()
            if self.tray:
                self.tray.activate()

    def open_browser(self) -> None:
        webbrowser.open(self.home_url)

    def open_page(self, path: str) -> None:
        """Show one of the app's own pages (e.g. "/#/settings/security"): in the window, else in the browser."""
        if self.browser_mode or not self.window:
            webbrowser.open(self.server.url + path)
            return
        self.show()
        self.window.load_url(self.server.url + path)

    # ----------------------------------------------------------- joining another computer's server
    def on_request(self, msg: dict[str, Any]) -> None:
        """What the page asks the app to do. Joining is only taken from this Mac's own server (its setup screen)."""
        kind = msg.get("type")
        own = msg.get("_origin", "").rstrip("/") in (f"http://localhost:{self.server.port}", f"http://127.0.0.1:{self.server.port}")
        if kind == "join" and own and not self.joined and isinstance(msg.get("url"), str):
            self.join(msg["url"], str(msg.get("name") or "")[:80] or None)
        elif kind == "retry" and self.joined:
            self.open_joined()
        elif kind == "switch" and self.joined:
            self.switch_server()

    def join(self, url: str, name: Optional[str]) -> None:
        """This Mac opens the household's server from now on; the server it started for the choice stops (nothing
        was set up in it)."""
        log.info("Joining the server at %s", url)
        self.prefs.set("role", "join")
        self.prefs.set("server", url)
        self.prefs.set("server_name", name)
        network_controller(None)
        update_installer(None)
        self.server.stop()
        self.open_joined()
        self.refresh_menu()

    def open_joined(self) -> None:
        if not self.window or not self.joined:
            return
        if reachable(self.joined):
            self.window.load_url(self.joined)
        else:
            self.window.load_html(unreachable_page(self.menu_state()["joined"], self.joined))
            self.show()

    def switch_server(self) -> None:
        """Back to the first-launch choice: set up here, or join another server."""
        log.info("Leaving the server at %s", self.joined)
        self.prefs.set("role", None)
        self.prefs.set("server", None)
        self.prefs.set("server_name", None)
        self.start_own_server()
        if self.window:
            self.window.load_url(self.server.url)
            self.show()
        self.refresh_menu()

    def start_own_server(self) -> None:
        network_controller(self.request_lan)
        update_installer(self.updater())
        host = "0.0.0.0" if self.prefs.get("lan") else "127.0.0.1"
        port = choose_port(self.prefs.get("port"))
        self.prefs.set("port", port)
        self.server.start(host, port)
        log.info("Serving %s on %s:%s (data in %s)", APP_NAME, host, port, self.data)

    def toggle_lan(self) -> None:
        """From the menu. Without a password, Settings → Security explains the choice instead of just opening up."""
        if self.joined:
            return
        if not self.lan and not self.server.status().get("auth_required"):
            self.open_page("/#/settings/security")      # the page's CSP blocks evaluate_js
            return
        self.set_lan(not self.lan)
        if self.window:
            try:
                self.window.load_url(self.window.get_current_url() or self.server.url)
            except Exception:  # noqa: BLE001
                self.window.load_url(self.server.url)

    def set_lan(self, on: bool) -> None:
        """Listen on the home network and Tailscale as well as this computer (restarts the server)."""
        with self.switching:        # the menu and Settings could both ask at once
            if on != self.lan:
                try:
                    self.server.restart("0.0.0.0" if on else "127.0.0.1")
                except RuntimeError:
                    log.exception("Couldn't switch network access %s; staying as it was", "on" if on else "off")
                    self.server.start("127.0.0.1" if on else "0.0.0.0", self.server.port)
                    return self.refresh_menu()
                self.prefs.set("lan", on)
                log.info("Network access %s", "on" if on else "off")
        self.refresh_menu()
        # syntropyhealth.local is announced a few seconds after the server starts; show it in the menu then.
        threading.Timer(6, self.refresh_menu).start()

    def request_lan(self, on: bool) -> None:
        """From Settings or onboarding (a request to this server): switch after the answer has gone out."""
        threading.Timer(0.5, self.set_lan, [on]).start()

    # ----------------------------------------------------------- updates
    def updater(self) -> Any:
        from app.services import updates
        from updater import Updater
        return Updater(self.data / "updates", lambda: self.hidden, self.quit, updates.set_progress)

    def check_for_updates(self) -> None:
        """From the menu. With its own server, Settings shows the answer (and the choices); a Mac that opens another
        computer's server asks here."""
        from app.core.config import APP_VERSION
        from app.services import updates
        if not self.joined:
            try:
                updates.check()
            except Exception:  # noqa: BLE001 - the page shows what's known
                log.exception("Update check failed")
            self.open_page("/#/settings/general/updates")
            return
        try:
            release = updates.fetch_latest()
        except updates.CheckFailed as exc:
            return self.message("Couldn't check for updates", str(exc))
        if not release or not updates.is_newer(release["version"]):
            return self.message("You're up to date", f"{APP_NAME} {APP_VERSION} is the latest version.")
        self.show()
        if self.window and self.window.create_confirmation_dialog(
                f"{APP_NAME} {release['version']} is available",
                f"You have {APP_VERSION}. Install the new version now? {APP_NAME} opens again when it's done."):
            try:
                self.updater()(release, False)
            except Exception as exc:  # noqa: BLE001
                log.exception("Update failed")
                self.message("Couldn't install the update", str(exc))

    def message(self, title: str, text: str) -> None:
        if not self.window:
            return alert(title, text)
        self.show()
        self.window.create_confirmation_dialog(title, text)

    def toggle_login(self) -> None:
        set_open_at_login(not open_at_login())
        self.refresh_menu()

    def quit(self) -> None:
        self.quitting = True
        if not self.joined:
            self.server.stop()
        if self.tray:
            self.tray.stop()
        if self.window:
            self.window.destroy()

    def refresh_menu(self) -> None:
        if self.tray:
            self.tray.update(self.menu_state())

    def on_closing(self) -> bool:
        """The window's close button hides it; the server keeps running until Quit."""
        if self.quitting:
            return True
        self.hidden = True
        self.window.hide()
        if self.tray:
            self.tray.hidden_hint()
        return False

    # ----------------------------------------------------------- run
    def run(self) -> None:
        import webview

        instance = SingleInstance(self.data, self.show)
        if instance.signal_existing():
            return
        instance.listen()

        mac = sys.platform == "darwin"
        if not mac and self.joined:          # joining is offered on a Mac; elsewhere this computer is the server
            self.prefs.set("role", None)
        if not self.joined:
            self.start_own_server()
        first_url = self.server.url
        if self.joined:
            first_url = self.joined if reachable(self.joined) else "about:blank"

        # On a Mac the window stays hidden until the page has drawn (mac_window shows it), so it never flashes blank.
        self.window = webview.create_window(APP_NAME, first_url, width=1280, height=860, min_size=(900, 600),
                                            hidden=self.background or mac, text_select=True,
                                            background_color="#0b0b0e" if dark_mode() else "#f6f7f9")
        self.window.events.closing += self.on_closing

        actions = {"show": self.show, "browser": self.open_browser, "lan": self.toggle_lan,
                   "login": self.toggle_login, "quit": self.quit, "switch": self.switch_server,
                   "updates": self.check_for_updates}
        from tray import make_tray
        self.tray = make_tray(actions, self.menu_state, self.on_app_quit)
        if mac:
            from mac_window import MacWindow
            self.mac = MacWindow(self.window, background=self.background, prefs=self.prefs, tray_actions=actions,
                                 dock_menu_state=self.menu_state, on_request=self.on_request,
                                 home_url=lambda: self.home_url)

        def started() -> None:
            self.tray.start()
            if self.mac:
                self.mac.install()
            if self.joined and first_url == "about:blank":      # the server's computer is off or away
                self.open_joined()

        webview.start(started, private_mode=False, storage_path=str(self.data / "webview"), user_agent=user_agent(),
                      **window_icon())
        self.server.stop()

    def run_in_browser(self) -> None:
        """Windows without the WebView2 runtime: the server and tray icon run as usual, and the app opens in the
        default browser instead of its own window."""
        instance = SingleInstance(self.data, self.open_browser)
        if instance.signal_existing():
            return
        instance.listen()
        self.prefs.set("role", None)
        self.start_own_server()
        actions = {"show": self.show, "browser": self.open_browser, "lan": self.toggle_lan,
                   "login": self.toggle_login, "quit": self.quit, "switch": self.switch_server,
                   "updates": self.check_for_updates}
        from tray import make_tray
        self.tray = make_tray(actions, self.menu_state, self.on_app_quit)
        self.browser_mode = True
        self.hidden = True          # no window to interrupt: automatic updates install when they're ready
        if not self.background:
            self.open_browser()
        self.tray.icon.run()        # until Quit
        self.server.stop()

    def on_app_quit(self) -> None:
        """Quit from the Dock or Cmd-Q (Mac)."""
        self.quitting = True
        self.server.stop()


def network_controller(fn: Optional[Callable[[bool], None]]) -> None:
    from app.services import network
    network.set_controller(fn)


def update_installer(fn: Optional[Callable[[dict, bool], None]]) -> None:
    from app.services import updates
    updates.set_installer(fn)


def window_icon() -> dict[str, str]:
    """The icon argument for webview.start, for Linux only.

    Windows: without one, the window takes the icon built into the .exe. Given a file, WinForms reads it with
    System.Drawing.Icon, which throws on a PNG while it creates the window, so the app never opened.
    Mac: the bundle's icon.icns is the Dock icon; passing one here would replace it with the full-bleed window icon,
    which looks oversized next to other apps."""
    if sys.platform in ("darwin", "win32"):
        return {}
    return {"icon": str(resource("icon-256.png"))}


WEBVIEW2_RUNTIME = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_DOWNLOAD = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def webview2_installed() -> bool:
    """Whether the Microsoft Edge WebView2 runtime the window needs is installed (built into Windows 11 and most
    Windows 10 PCs; without it pywebview falls back to Internet Explorer, which can't run the app)."""
    if sys.platform != "win32":
        return True
    import winreg
    places = [(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_RUNTIME}"),
              (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_RUNTIME}"),
              (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_RUNTIME}")]
    for root, path in places:
        try:
            with winreg.OpenKey(root, path) as key:
                version = str(winreg.QueryValueEx(key, "pv")[0])
        except OSError:
            continue
        if version and version != "0.0.0.0":
            return True
    return False


def alert(title: str, text: str) -> None:
    """A message box with no window of the app's own (Windows), for when the window itself can't open."""
    log.info("%s: %s", title, text)
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40 | 0x10000)      # MB_ICONINFORMATION | MB_SETFOREGROUND


def reachable(url: str) -> bool:
    import httpx   # its own certificate authorities: the packaged Mac app has none for urllib, so https:// failed

    try:
        r = httpx.get(f"{url.rstrip('/')}/api/wearables/pair", timeout=4.0, follow_redirects=True)
        return r.json().get("service") == "syntropy-health"
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


def unreachable_page(name: str, url: str) -> str:
    """Shown in the window when the household's server doesn't answer (its computer is asleep, or this Mac is away)."""
    import html
    return f"""<!doctype html><meta charset="utf-8"><title>{APP_NAME}</title>
<style>
  :root {{ color-scheme: light dark; font: 14px -apple-system, system-ui, sans-serif; }}
  body {{ margin: 0; height: 100vh; display: grid; place-items: center; background: Canvas; color: CanvasText; }}
  main {{ max-width: 420px; padding: 24px; text-align: center; }}
  h1 {{ font-size: 20px; margin: 0 0 8px; }} p {{ opacity: .7; line-height: 1.45; }}
  button {{ font: inherit; padding: 7px 14px; margin: 6px 4px 0; border-radius: 7px; border: 1px solid #8884; background: none; color: inherit; }}
  button.primary {{ background: #d6204a; border-color: #d6204a; color: white; }}
</style>
<main><h1>Can't reach {html.escape(name)}</h1>
<p>Syntropy Health runs on {html.escape(name)} ({html.escape(url)}). Make sure that computer is on and awake, and that this
Mac is on the same network (or on Tailscale).</p>
<button class="primary" onclick="send('retry')">Try Again</button><button onclick="send('switch')">Use a Different Server…</button></main>
<script>function send(t) {{ window.webkit.messageHandlers.syntropyDesktop.postMessage(JSON.stringify({{type: t}})); }}</script>"""


def main() -> None:
    argv = [a for a in sys.argv[1:] if not a.startswith("-psn_")]      # macOS may add a process serial number
    if argv and argv[0] in CLI_COMMANDS:
        if sys.stdout is None and argv[0] != "serve":   # the windowed Windows app, run with a command: no console
            sys.stdout = sys.stderr = open(os.devnull, "w")     # (serve keeps its own log: see cli.serve)
        from app.cli import main as cli_main
        cli_main(argv)
        return
    setup_logging()
    background = "--background" in argv
    try:
        desktop = DesktopApp(background=background)
        if webview2_installed():
            desktop.run()
            return
        log.warning("The Microsoft Edge WebView2 runtime isn't installed; opening in the browser instead")
        if not background:
            threading.Thread(target=alert, daemon=True, args=(
                APP_NAME, f"{APP_NAME} is open in your web browser, because this PC doesn't have the Microsoft Edge "
                          f"WebView2 Runtime its window needs. Install it from {WEBVIEW2_DOWNLOAD} (free, from "
                          f"Microsoft), then quit {APP_NAME} from its notification-area icon and open it again.")).start()
        desktop.run_in_browser()
    except Exception as exc:  # noqa: BLE001 - leave a trace for support; a windowed app can't print
        log.exception("Syntropy Health stopped")
        alert(f"{APP_NAME} couldn't start", f"{exc}\n\nDetails are in {log_path()}.")
        raise


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))
    main()
