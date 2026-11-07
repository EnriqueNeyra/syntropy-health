"""
Syntropy Health for Mac and Windows. AGPL-3.0-only, with the additional terms in the NOTICE file.

The app runs the same server as a home-server install, on this computer, and shows it in a native window (WebKit on a
Mac, Edge WebView2 on Windows). Closing the window keeps the server running, with a menu bar (Mac) or tray (Windows)
icon, so the iPhone app can keep syncing and automations keep running. From that icon:

- Let your iPhone and other devices connect: listen on the home network instead of only this computer.
- Open at login: start in the background when you log in.
- Check for updates (see updater.py; Settings → General → Updates has the choices).
- Quit.

A Mac can instead join the household's server running on another computer (chosen on first launch): then it runs no
server of its own, and the window opens that one, where each person signs in as themselves.

Run with a command (``serve``, ``mcp``, ``backup``, ``restore``, ``info``...) it behaves like the ``syntropy-health`` command
instead, without a window; that's how AI apps start its MCP server and how ``service install`` runs it.
"""

from __future__ import annotations

import errno
import io
import json
import logging
import os
import plistlib
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional

import own_pages
import sites

CLI_COMMANDS = {"serve", "service", "backup", "restore", "update", "info", "mcp", "version", "accounts", "key", "dev", "-h", "--help"}
APP_NAME = "Syntropy Health"
FIRST_PORT = 8000
HOME_PORT_WAIT = 4.0        # seconds the saved port may stay busy (the last run closing, say) before using another
STOP_WAIT = 25              # seconds to wait for the server: its 5 for open requests, then its shutdown steps
START_WAIT = 1440           # seconds to wait for it to start, within how long an update's script waits (updater.WATCH_SECONDS)
log = logging.getLogger("syntropy.desktop")


def resource(name: str) -> Path:
    """A file shipped with the app (icons), from the bundle or from this folder when run from source."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / "icons" / name


# ---------------------------------------------------------------------------
# Preferences and single instance
# ---------------------------------------------------------------------------

def write_whole(path: Path, text: str) -> None:
    """A crash or a full disk leaves the old file, not half a new one."""
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Prefs:
    def __init__(self, data: Path):
        self.path = data / "desktop.json"
        try:
            values = json.loads(self.path.read_text())
        except (OSError, ValueError):
            values = {}
        self.values: dict[str, Any] = values if isinstance(values, dict) else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def number(self, key: str, default: float, low: float, high: float) -> float:
        value = self.values.get(key)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high else default

    def port(self) -> Optional[int]:
        value = self.values.get("port")
        return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535 else None

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value
        try:
            write_whole(self.path, json.dumps(self.values, indent=2))
        except OSError:
            log.exception("Couldn't save the app's preferences")


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
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
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
        write_whole(self.file, json.dumps({"port": server.server_address[1], "token": token, "pid": os.getpid()}))
        if os.name != "nt":
            self.file.chmod(0o600)
        threading.Thread(target=server.serve_forever, daemon=True, name="instance").start()


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

def _answering(family: int, address: str, port: int) -> bool:
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            return probe.connect_ex((address, port)) == 0
    except OSError:         # this computer has no IPv6
        return False


def port_free(host: str, port: int) -> bool:
    # Something already answering on this computer (a Docker install, another copy with other data) means taken. On
    # either loopback address: the window opens "localhost", which tries ::1 first...
    if _answering(socket.AF_INET, "127.0.0.1", port) or _answering(socket.AF_INET6, "::1", port):
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


def _listening_socket(family: int, host: str, port: int) -> socket.socket:
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s.bind((host, port))
        s.setblocking(False)
    except OSError:
        s.close()
        raise
    return s


def listening_sockets(host: str, port: int) -> list[socket.socket]:
    """The server's own address and ::1: the window opens "localhost", so nothing else may take that one afterwards and
    answer in the app's place."""
    sockets = [_listening_socket(socket.AF_INET, host, port)]
    try:
        sockets.append(_listening_socket(socket.AF_INET6, "::1", port))
    except OSError as exc:      # no IPv6 here, which leaves nothing to take over either
        log.info("Not listening on ::1: %s", exc)
    return sockets


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
                             proxy_headers=True, access_log=False, log_config=None, timeout_graceful_shutdown=5)
        sockets = listening_sockets(host, port)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": sockets}, daemon=True, name="server")
        self.thread.start()
        # Wait for this server to be listening (uvicorn sets `started` once bound), not just for something to answer
        # on the port: another program there would otherwise pass for it.
        # Long enough for the first start after an update: copying and upgrading a database of several GB takes minutes.
        deadline = time.time() + START_WAIT
        while time.time() < deadline and self.thread.is_alive():
            if self.server.started:
                self.note_serving()
                return
            time.sleep(0.1)
        if self.thread.is_alive():
            raise RuntimeError(f"It's still getting ready after {START_WAIT // 60} minutes, which is longer than even a large upgrade takes.")
        raise RuntimeError("The server stopped while it was starting.")

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def note_serving(self) -> None:
        """As `syntropy-health serve` does, so that `syntropy-health restore` knows not to replace the database under it."""
        from app import cli
        try:
            cli.note_serving(self.port)
        except OSError as exc:
            log.warning("Couldn't note that the server is running: %s", exc)

    def stop(self) -> bool:
        """Whether the server has really finished: until then its scheduler and announcements are still its own, and a
        new server started beside it would share them and leave none when this one ended."""
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(STOP_WAIT)
        if not self.running:
            from app import cli
            cli.note_stopped()
        return not self.running

    def restart(self, host: str) -> None:
        if not self.stop():
            raise RuntimeError("The server is still finishing what it was doing.")
        self.start(host, self.port)

    def problem(self) -> Optional[tuple[str, str]]:
        """(kind, text): up, but the data can't be used, as app.main's start-up found."""
        from app.main import app
        text = getattr(app.state, "problem", None)
        return (getattr(app.state, "problem_kind", None) or "unavailable", text) if text else None

    def status(self) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(f"{self.check_url}/api/status", timeout=3) as r:
                return json.loads(r.read())
        except (OSError, ValueError):
            return {}


def choose_port(saved: Optional[int]) -> int:
    """The saved port, given a moment if it's busy, else 8000, else the next free one (a Docker install may already have
    8000). Only start_own_server saves one: that port is what the phone was paired with."""
    if saved:
        deadline = time.monotonic() + HOME_PORT_WAIT
        while True:
            if port_free("0.0.0.0", saved):
                return saved
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
    for port in range(FIRST_PORT, FIRST_PORT + 100):
        if port != saved and port_free("0.0.0.0", port):
            return port
    raise OSError(errno.EADDRINUSE, "No free port between 8000 and 8099")


def program_on_port(port: int) -> Optional[str]:
    """The name of what's listening there, if the system will say."""
    try:
        if sys.platform == "win32":
            ps = (f"$c = Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue | Select-Object -First 1; "
                  "if ($c) { (Get-Process -Id $c.OwningProcess).ProcessName }")
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True,
                                 timeout=10, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            return out.strip() or None
        out = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fc"], capture_output=True, text=True,
                             timeout=10, check=False).stdout
        return next((line[1:] for line in out.splitlines() if line.startswith("c")), None)
    except (OSError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------------------
# Open at login
# ---------------------------------------------------------------------------

LOGIN_LABEL = "io.syntropyhealth.desktop"      # also the Mac app's bundle identifier


def _launch_agent() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LOGIN_LABEL}.plist"


def _login_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--background"]
    return [sys.executable, str(Path(__file__).resolve()), "--background"]


def _launch_agent_program() -> Optional[str]:
    """The program the Mac's login item opens."""
    try:
        return plistlib.loads(_launch_agent().read_bytes())["ProgramArguments"][0]
    except (OSError, ValueError, KeyError, IndexError, plistlib.InvalidFileException):
        return None


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
    if sys.platform == "darwin":
        path = _launch_agent()
        if on:
            agent: dict[str, Any] = {"Label": LOGIN_LABEL, "ProgramArguments": _login_command(), "RunAtLoad": True,
                                     "ProcessType": "Interactive"}
            if getattr(sys, "frozen", False):        # listed under the app's name in Settings → General → Login Items
                agent["AssociatedBundleIdentifiers"] = [LOGIN_LABEL]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(plistlib.dumps(agent))
        else:
            path.unlink(missing_ok=True)
    elif sys.platform == "win32":
        import winreg
        # Created if it isn't there yet (a new Windows account), as `syntropy-health service install` does.
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(_login_command()))
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except OSError:
                    pass


def runs_from_temporary_place() -> bool:
    """A Mac app opened from its disk image, or from Downloads before it was moved (macOS then runs it from a
    temporary copy): a login item pointing there would stop working."""
    exe = sys.executable
    return sys.platform == "darwin" and getattr(sys, "frozen", False) and (
        "/AppTranslocation/" in exe or (exe.startswith("/Volumes/") and _read_only_disk(exe)))


def _read_only_disk(path: str) -> bool:
    """A disk image is mounted read-only. An external drive someone keeps their apps on isn't, and is no temporary place."""
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except (OSError, AttributeError):
        return True


def heal_open_at_login() -> None:
    """The app was moved after Open at Login was turned on (from Downloads into Applications, say): open this copy."""
    if (sys.platform == "darwin" and getattr(sys, "frozen", False) and not runs_from_temporary_place()
            and open_at_login() and _launch_agent_program() != sys.executable):
        log.info("Pointing Open at Login at %s", sys.executable)
        set_open_at_login(True)


# ---------------------------------------------------------------------------
# Uninstalling (Mac; Windows has its own uninstaller, desktop/installer.iss, which asks the same question)
# ---------------------------------------------------------------------------

def app_bundle() -> Optional[Path]:
    """The Syntropy Health.app this is running from (None when run from source)."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    from Foundation import NSBundle
    bundle = NSBundle.mainBundle()
    if bundle.bundleIdentifier() != LOGIN_LABEL:
        return None
    return Path(bundle.bundlePath())


def remove_server_service(bundle: Path) -> None:
    """`Syntropy Health service install` (the command-line server, run from inside this app) starts with the Mac too;
    it can't outlive the app. One installed from elsewhere (install.sh --server) is left alone."""
    from app.cli import LAUNCHD_LABEL
    path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    try:
        program = Path(plistlib.loads(path.read_bytes())["ProgramArguments"][0])
    except (OSError, ValueError, KeyError, IndexError, plistlib.InvalidFileException):
        return
    if program.is_relative_to(bundle):
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True, check=False)
        path.unlink(missing_ok=True)


def move_to_trash(path: Path) -> bool:
    from Foundation import NSURL, NSFileManager
    ok, _where, error = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(str(path)), None, None)
    if not ok:
        log.warning("Couldn't move %s to the Trash: %s", path, error)
    return bool(ok)


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


def where_details_are() -> str:
    try:
        return f"Details are in {log_path()}."
    except OSError:
        return "The data folder itself can't be used, so no details could be saved."


class _LogWriter(io.TextIOBase):
    """sys.stdout and sys.stderr of a windowed app, which has no console. They go through logging rather than holding the
    log file, which is closed each time it rolls over: anything printed after that would raise an error."""

    logging_now = threading.local()

    def __init__(self, level: int):
        super().__init__()
        self.level = level
        self.pending = ""

    def writable(self) -> bool:
        return True

    def _log(self, line: str) -> None:
        # What logging itself prints when it can't write the log file (a full disk) comes back here, and would go round
        # until a RecursionError reached whatever had called log.*, such as the code about to say what's wrong.
        if getattr(self.logging_now, "yes", False):
            return
        self.logging_now.yes = True
        try:
            logging.getLogger("syntropy.desktop.output").log(self.level, "%s", line)
        finally:
            self.logging_now.yes = False

    def write(self, text: str) -> int:
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line.strip():
                self._log(line)
        return len(text)

    def flush(self) -> None:
        if self.pending.strip():
            self._log(self.pending)
        self.pending = ""


def setup_logging() -> None:
    """To desktop.log. Without a usable data folder, to standard error, and start-up then says what's wrong."""
    try:
        handler: logging.Handler = RotatingFileHandler(log_path(), maxBytes=5_000_000, backupCount=2, encoding="utf-8")
    except OSError as exc:
        handler = logging.StreamHandler(sys.stderr or io.StringIO())
        print(f"Couldn't open the log file: {exc}", file=sys.stderr or io.StringIO())
    else:
        sys.stdout, sys.stderr = _LogWriter(logging.INFO), _LogWriter(logging.ERROR)
        logging.raiseExceptions = False     # a log file it can't write: nowhere to say so, and nothing else needs to stop
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def describe_failure(exc: BaseException) -> tuple[str, str]:
    """(kind, text) for whatever stopped the app starting: what's wrong and what to do, in words."""
    from app.core.sqlite import sqlite3

    from app.core import config, db

    if isinstance(exc, db.StartupProblem):
        return exc.kind, str(exc)
    if isinstance(exc, sqlite3.Error):
        problem = db.unusable(exc, config.db_path())
        return problem.kind, str(problem)
    if isinstance(exc, (FileExistsError, NotADirectoryError)):
        return "unavailable", (f"{APP_NAME} keeps its data in a folder, but {exc.filename} isn't one. Move that file somewhere "
                               "else, then open the app again.")
    if isinstance(exc, OSError):
        where = exc.filename or "its data folder"
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            return "unavailable", f"The disk is full, so {APP_NAME} can't save anything to {where}. Free up some space, then try again."
        if exc.errno == errno.EADDRINUSE:
            return "unavailable", (f"Every port {APP_NAME} can use is taken by other programs. Close some of them, then try again.")
        if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            return "unavailable", (f"{APP_NAME} isn't allowed to write to {where}. Check that folder's permissions, and that its "
                                   "disk isn't read-only, then try again.")
        if exc.errno in (errno.ENOENT, errno.EIO, errno.ENOTCONN):
            return "unavailable", (f"{APP_NAME} can't get to {where}. If it's on an external drive, connect the drive, then try again.")
        return "unavailable", f"{APP_NAME} can't use its data folder: {exc}."
    return "other", f"{exc} If it keeps happening, reinstall the app: your data is kept."


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
        self.recovering = threading.Lock()
        self.starting = False      # the window shows the starting page until the server answers
        self.finishing = False     # waiting for the old server's last job, to start it again
        self.location = sites.Location(lambda: self.home_url, own_page=True)
        self.moved_from: Optional[int] = None    # the saved port, while another program holds it
        self.mac = None            # mac_window.MacWindow on a Mac
        self.win = None            # windows_window.WindowsWindow on Windows
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

    @property
    def away(self) -> bool:
        return bool((self.mac or self.win) and (self.mac or self.win).away)

    def menu_state(self) -> dict[str, Any]:
        if self.joined:
            return {"joined": self.prefs.get("server_name") or urllib.parse.urlsplit(self.joined).hostname, "away": self.away}
        return {"lan": self.lan, "login": open_at_login(), "address": self.lan_address() if self.lan else None,
                "away": self.away, "port_note": self.port_note()}

    def port_note(self) -> Optional[str]:
        if self.finishing:
            return "Still finishing a job, back when it's done"
        return f"Using port {self.server.port}: {self.moved_from} is busy" if self.moved_from else None

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
                self.mac.in_dock(True)
            self.window.show()
            self.window.restore()
            if self.tray:
                self.tray.activate()

    def open_browser(self) -> None:
        if self.starting:
            return self.show()
        webbrowser.open(self.home_url)

    def open_page(self, path: str) -> None:
        """Show one of the app's own pages (e.g. "/#/settings/security"): in the window, else in the browser."""
        if self.starting:
            return self.show()
        if self.browser_mode or not self.window:
            webbrowser.open(self.server.url + path)
            return
        self.show()
        self.window.load_url(self.server.url + path)

    # ----------------------------------------------------------- joining another computer's server
    def on_request(self, msg: dict[str, Any]) -> None:
        """What the page asks the app to do. Joining is only taken from this Mac's own server (its setup screen), and what
        recovers from a start-up problem only from the page saying so, which the app wrote itself."""
        kind = msg.get("type")
        if msg.get("_own_page") and kind in ("problem-retry", "problem-restore", "problem-fresh", "problem-folder",
                                             "problem-latest", "problem-quit", "problem-unlock", "go-home"):
            return self.on_own_page_request(kind)
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
            self.show_own_page(own_pages.unreachable_page(self.menu_state()["joined"], self.joined))
            self.show()

    def show_own_page(self, page: str) -> None:
        self.location.showing_own_page()
        # WebView2 loads this when it's finally ready, which could otherwise put the starting page back over this one.
        self.window.html = page
        self.window.load_html(page)

    def on_load_failed(self, url: str, code: int, reason: str) -> None:
        """The window couldn't open a page (Mac: WebKit otherwise leaves it blank, or on the page it was on)."""
        if self.quitting or sites.origin(url) is None:      # a page trying an app's own scheme simply carries on
            return
        if sites.origin(url) in sites.home_origins(self.home_url):
            if self.joined:
                if code == ATS_REFUSED:
                    reason = (f"This Mac only opens {self.joined} over a secure connection. Use its Tailscale name, or its "
                              "address on your home network, or an https:// address.")
                self.show_own_page(own_pages.unreachable_page(self.menu_state()["joined"], self.joined, reason))
                self.show()
            return
        self.show_own_page(own_pages.load_failed_page(sites.site_name(url), reason))

    def switch_server(self) -> None:
        """Back to the first-launch choice: set up here, or join another server."""
        log.info("Leaving the server at %s", self.joined)
        self.prefs.set("role", None)
        self.prefs.set("server", None)
        self.prefs.set("server_name", None)
        self.show()
        self.bring_up_own_server()

    def start_own_server(self) -> None:
        import updater

        network_controller(self.request_lan)
        update_installer(self.updater())
        # A problem got past later (the key unlocked with a password, a backup restored) is a new version that works.
        when_working(lambda: updater.started_fine(self.data / "updates"))
        host = "0.0.0.0" if self.prefs.get("lan") else "127.0.0.1"
        saved = self.prefs.port()
        port = choose_port(saved)
        if saved is None:
            self.prefs.set("port", port)
        self.moved_from = saved if saved not in (None, port) else None
        self.server.start(host, port)
        log.info("Serving %s on %s:%s (data in %s)", APP_NAME, host, port, self.data)
        self.note_moved_port()

    def note_moved_port(self) -> None:
        from app.services import network
        if self.moved_from:
            holder = program_on_port(self.moved_from)
            log.warning("Port %s is busy%s, so using %s until the next start", self.moved_from,
                        f" ({holder})" if holder else "", self.server.port)
            network.set_moved_port(self.moved_from, self.server.port, holder)
        else:
            network.set_moved_port(None, None, None)

    def bring_up_own_server(self) -> None:
        """Runs beside the window, which shows the starting page: the first start after an update can take minutes."""
        import updater

        self.starting = True
        problem = None
        watching = threading.Event()
        watcher = threading.Thread(target=self.show_encrypting, args=(watching,), daemon=True, name="encrypting-page")
        watcher.start()
        try:
            updater.review_last_attempt(self.data / "updates")     # before the server: it may put the database back
            self.start_own_server()
            problem = self.server.problem()
        except Exception as exc:  # noqa: BLE001 - whatever it was, the window says so
            log.exception("The server didn't start")
            problem = describe_failure(exc)
        watching.set()
        watcher.join(5)     # so it can't put its page back over what's shown next
        self.starting = False
        if problem:
            log.error("Can't start: %s", problem[1])
            # A new version that can't work with this data: its update script goes back to the one that can.
            if problem[0] in updater.GO_BACK_KINDS and updater.started_badly(self.data / "updates", problem[1]):
                return self.quit()
            self.show_problem(*problem)
        else:
            updater.started_fine(self.data / "updates")
            if self.window:
                self.window.load_url(self.server.url)
        self.refresh_menu()

    def show_encrypting(self, finished: threading.Event) -> None:
        """The start page, with how far along it is, while the server encrypts a database from before this version."""
        from app.core import encryption

        shown = None
        while not finished.wait(1.0):
            now = encryption.progress()
            if now and now != shown and self.window:
                shown = now
                self.show_own_page(own_pages.encrypting_page(now["stage"], now.get("percent")))

    def show_problem(self, kind: str, text: str) -> None:
        """Even when opened at login, with no window: nothing else will tell them, and the phone has stopped syncing."""
        from app.core import keystore

        if self.window:
            self.show_own_page(own_pages.problem_page(text, kind, str(self.data), str(log_path()), can_unlock=bool(keystore.recovery_entries())))
            self.show()

    # ----------------------------------------------------------- getting past a start-up problem
    def on_own_page_request(self, kind: str) -> None:
        if kind == "problem-folder":
            return reveal_folder(self.data)
        if kind == "problem-latest":
            from app.services import updates
            webbrowser.open(updates.RELEASES_URL)
            return None
        if kind == "problem-quit":
            return self.quit()
        if kind == "go-home":
            return self.go_home()
        if kind == "problem-unlock":        # the server's own page, which only it can answer (see app.main.unlock)
            return self.window.load_url(f"{self.server.url}/") if self.window else None
        if not self.recovering.acquire(blocking=False):     # a second click while the first is still going
            return None
        try:
            if kind == "problem-retry":
                self.start_again()
            elif kind == "problem-restore":
                self.restore_backup()
            elif kind == "problem-fresh":
                self.start_fresh()
        finally:
            self.recovering.release()

    def stopped_for_recovery(self) -> bool:
        """Whether nothing is using the database any more. A server still working on it (a very long upgrade) mustn't have
        its files moved, or a second one started beside it."""
        from app.core import db
        if not self.server.stop():
            self.show_problem("other", f"{APP_NAME} is still finishing what it was doing with your data. Quit, wait a few "
                                       "minutes, then open it again.")
            return False
        db.reset_migration_cache()
        return True

    def start_again(self) -> None:
        self.starting = True
        self.show_own_page(own_pages.starting_page())
        ready = self.stopped_for_recovery()
        self.starting = ready
        if ready:
            self.bring_up_own_server()

    def restore_backup(self) -> None:
        import webview
        import recovery
        from app.core import config, db

        start_in = config.db_path().parent / db.PRE_UPGRADE_DIR
        chosen = self.window.create_file_dialog(
            webview.FileDialog.OPEN, directory=str(start_in if start_in.is_dir() else self.data),
            file_types=("Syntropy Health backups (*.zip;*.db)", "All files (*.*)"))
        if not chosen:
            return
        backup = Path(chosen[0] if isinstance(chosen, (list, tuple)) else chosen)
        if not self.window.create_confirmation_dialog(
                "Restore this backup?", f"{APP_NAME} will start from {backup.name}, and anything added since it was made won't be "
                                        "there. The current database is kept, in a folder beside it."):
            return
        if not self.stopped_for_recovery():
            return
        self.show_own_page(own_pages.working_page("Restoring your backup…", "It's copied and checked first, which takes a "
                                                  "while with a lot of data. Your current database isn't changed until that's done."))
        try:
            aside = recovery.restore(backup, config.db_path())
        except recovery.RestoreRefused as exc:
            self.message("That backup wasn't restored", str(exc))
            self.start_again()
            return
        log.info("Restored %s; the old database is in %s", backup, aside)
        self.start_again()

    def start_fresh(self) -> None:
        import recovery
        from app.core import config

        if not self.window.create_confirmation_dialog(
                "Set the damaged database aside?", f"{APP_NAME} will start with no records, as if newly installed. The damaged "
                                                   f"file isn't deleted: it moves to a folder in {self.data}, and repair tools or "
                                                   "a backup may still recover it."):
            return
        if not self.stopped_for_recovery():
            return
        try:
            recovery.set_aside(config.db_path())
        except OSError as exc:
            log.exception("Couldn't set the database aside")
            self.message("The database couldn't be moved", f"{exc}. Nothing has been changed.")
        self.start_again()

    def go_home(self) -> None:
        """Back to the app from another site (the tray or menu bar, and pages the app writes itself)."""
        self.show()
        if self.mac:
            self.mac.go_home_soon()
        elif self.win:
            self.win.go_home()

    def toggle_lan(self) -> None:
        """From the menu. Without a password, Settings → Security explains the choice instead of just opening up."""
        if self.joined:
            return
        if self.starting:
            return self.show()
        if not self.lan and not self.server.status().get("auth_required"):
            self.open_page("/#/settings/security")      # the page's CSP blocks evaluate_js
            return
        self.set_lan(not self.lan)
        if self.window and self.server.running and not self.quitting:
            try:
                self.window.load_url(self.window.get_current_url() or self.server.url)
            except Exception:  # noqa: BLE001
                self.window.load_url(self.server.url)

    def set_lan(self, on: bool) -> None:
        """Listen on the home network and Tailscale as well as this computer (restarts the server)."""
        with self.switching:        # the menu and Settings could both ask at once
            if on != self.lan:
                said_so = self.wait_for_server_to_finish()
                if self.quitting:
                    return None
                self.switch_network_access(on)
                if self.finishing:
                    self.finishing = self.starting = False
                if said_so and self.server.running:     # otherwise the window is saying why not
                    self.window.load_url(self.server.url)
        self.refresh_menu()
        # syntropyhealth.local is announced a few seconds after the server starts; show it in the menu then.
        threading.Timer(6, self.refresh_menu).start()

    def wait_for_server_to_finish(self) -> bool:
        """A long job can outlast Server.stop's wait. By then the server has closed its sockets and can't be called back, so
        nothing answers until it ends, however long that takes, and only then can another start. Returns whether the window
        was told, and so needs sending back."""
        if self.server.stop():
            return False
        log.warning("The server is still finishing what it was doing; waiting for it")
        self.finishing = True
        self.starting = not self.browser_mode       # (there, show() and open_browser() would hand over to each other)
        self.refresh_menu()
        said_so = bool(self.window and not self.browser_mode and not self.away)
        if said_so:         # another site, such as a portal's sign-in, carries on undisturbed
            self.show_own_page(own_pages.working_page(
                "Still finishing a job…", f"{APP_NAME} changes who can connect as soon as the job it's working on is done. This "
                f"usually takes a few minutes at most. If it doesn't come back, quit {APP_NAME} and open it again."))
            self.show()
        while self.server.running and not self.quitting:
            self.server.thread.join(1)
        return said_so

    def switch_network_access(self, on: bool) -> None:
        word = "on" if on else "off"
        try:
            self.server.restart("0.0.0.0" if on else "127.0.0.1")
        except (RuntimeError, OSError):
            log.exception("Couldn't switch network access %s; staying as it was", word)
        else:
            self.prefs.set("lan", on)
            log.info("Network access %s", word)
            return
        if self.server.running:
            return
        try:
            self.server.start("127.0.0.1" if on else "0.0.0.0", self.server.port)
        except (RuntimeError, OSError) as exc:
            log.exception("The server couldn't start again")
            self.show_problem(*describe_failure(exc))

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
        if self.starting:
            return self.show()
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
        if not open_at_login() and runs_from_temporary_place():
            return self.message("Move Syntropy Health to Applications first",
                                f"{APP_NAME} is running from its disk image or Downloads, so opening it at login "
                                f"wouldn't find it later. Drag it into Applications, open it from there, then turn on "
                                f"Open at Login.")
        set_open_at_login(not open_at_login())
        self.refresh_menu()

    def uninstall(self, erase: bool) -> None:
        """Mac, from the app menu once the person has confirmed (see MacWindow.confirm_uninstall): stop opening at
        login, move the app to the Trash and quit, and with erase delete everything it kept on this Mac first."""
        log.info("Uninstalling (and deleting the data in %s: %s)", self.data, erase)
        set_open_at_login(False)
        bundle = app_bundle()
        if bundle:
            remove_server_service(bundle)
        self.quitting = True
        if not self.joined:
            self.server.stop()
        from app.core import keystore

        if erase:
            if self.mac and bundle:
                self.mac.forget_website_data(LOGIN_LABEL)
            keystore.forget()       # the key that opened it, wherever it was kept
            shutil.rmtree(self.data, ignore_errors=True)
        else:
            shutil.rmtree(self.data / "updates", ignore_errors=True)      # downloaded installers
        if bundle and not move_to_trash(bundle):
            self.message(f"Drag {APP_NAME} to the Trash to finish",
                         f"{APP_NAME} couldn't move itself to the Trash. It will quit now: drag it from {bundle.parent} "
                         f"to the Trash.")
        self.quit()

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
        if self.mac:
            self.mac.in_dock(False)       # it runs on in the menu bar alone
        if self.tray:
            self.tray.hidden_hint()
        return False

    # ----------------------------------------------------------- run
    def run(self) -> None:
        import webview

        # Exports and backups (Settings → Your data) are downloads: without this pywebview cancels them on Windows,
        # and on a Mac the window navigated to the file instead (a CSV filled the window, with no way back).
        # With it, each asks where to save, as a browser would.
        webview.settings["ALLOW_DOWNLOADS"] = True
        sites.guard_external_links()

        instance = SingleInstance(self.data, self.show)
        if instance.signal_existing():
            return
        instance.listen()

        mac = sys.platform == "darwin"
        if not mac and self.joined:          # joining is offered on a Mac; elsewhere this computer is the server
            self.prefs.set("role", None)
        # The window first, showing that it's starting: the server may take minutes (see own_pages.starting_page).
        first_url, first_page = None, None
        if self.joined:
            first_url = self.joined if reachable(self.joined) else "about:blank"
        else:
            self.starting = True
            first_page = own_pages.starting_page()

        # On a Mac the window stays hidden until the page has drawn (mac_window shows it), so it never flashes blank.
        self.window = webview.create_window(APP_NAME, first_url, html=first_page, width=1280, height=860, min_size=(900, 600),
                                            hidden=self.background or mac, text_select=True,
                                            background_color="#0b0b0e" if dark_mode() else "#f6f7f9")
        self.window.events.closing += self.on_closing

        actions = self.menu_actions()
        from tray import make_tray
        self.tray = make_tray(actions, self.menu_state, self.on_app_quit)
        if mac:
            from mac_window import MacWindow
            heal_open_at_login()
            self.mac = MacWindow(self.window, background=self.background, prefs=self.prefs, tray_actions=actions,
                                 dock_menu_state=self.menu_state, on_request=self.on_request,
                                 home_url=lambda: self.home_url, on_uninstall=self.uninstall, location=self.location,
                                 on_load_failed=self.on_load_failed,
                                 on_away_changed=lambda: threading.Thread(target=self.refresh_menu, daemon=True).start())
        elif sys.platform == "win32":
            self.guard_windows_window()

        def started() -> None:
            self.tray.start()
            if self.mac:
                self.mac.install()
            if not self.joined:
                self.bring_up_own_server()
            else:
                import updater
                updater.review_last_attempt(self.data / "updates")
                updater.started_fine(self.data / "updates")
                if first_url == "about:blank":      # the server's computer is off or away
                    self.open_joined()

        webview.start(started, private_mode=False, storage_path=str(self.data / "webview"), user_agent=user_agent(),
                      **window_icon())
        self.server.stop()

    def menu_actions(self) -> dict[str, Callable[[], None]]:
        return {"show": self.show, "browser": self.open_browser, "lan": self.toggle_lan, "login": self.toggle_login,
                "quit": self.quit, "switch": self.switch_server, "updates": self.check_for_updates, "home": self.go_home}

    def guard_windows_window(self) -> None:
        from windows_window import WindowsWindow
        self.win = WindowsWindow(self.window, self.location, lambda: self.home_url, self.on_request, self.refresh_menu,
                                 self.on_view_failed)
        try:
            self.win.guard()
        except Exception:  # noqa: BLE001 - sites.guard_external_links still keeps links to the web and mail
            log.exception("Couldn't set up the window's rules for other sites")

    def on_view_failed(self, reason: str) -> None:
        """WebView2 is installed but wouldn't start (a damaged runtime, say), which leaves an empty window: the browser instead."""
        log.error("The window's web view didn't start: %s", reason)
        alert(f"{APP_NAME}'s window couldn't open", f"Microsoft Edge WebView2, which draws the window, didn't start. "
              f"{APP_NAME} will open in your web browser instead. To get the window back, install WebView2 again from "
              f"{WEBVIEW2_DOWNLOAD}, then quit {APP_NAME} from its notification-area icon and open it again.")
        while self.starting:
            time.sleep(0.2)
        self.browser_mode = True
        self.hidden = True          # no window to interrupt, as in run_in_browser
        webbrowser.open(self.home_url)

    def run_in_browser(self) -> None:
        """Windows without the WebView2 runtime: the server and tray icon run as usual, and the app opens in the
        default browser instead of its own window."""
        instance = SingleInstance(self.data, self.open_browser)
        if instance.signal_existing():
            return
        instance.listen()
        self.prefs.set("role", None)
        import updater
        updater.review_last_attempt(self.data / "updates")
        self.start_own_server()
        problem = self.server.problem()
        if not problem:
            updater.started_fine(self.data / "updates")
        elif problem[0] in updater.GO_BACK_KINDS and updater.started_badly(self.data / "updates", problem[1]):
            self.server.stop()
            return
        if problem:     # the browser shows the same explanation, from the server
            threading.Thread(target=alert, daemon=True, args=(f"{APP_NAME} can't start", problem[1])).start()
        from tray import make_tray
        self.tray = make_tray(self.menu_actions(), self.menu_state, self.on_app_quit)
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


def when_working(fn: Optional[Callable[[], None]]) -> None:
    from app.services import updates
    updates.set_on_working(fn)


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


def alert(title: str, text: str, offer: Optional[str] = None) -> bool:
    """A message box with no window of the app's own, for when the window itself can't open. ``offer`` adds a choice
    alongside OK; the answer is whether it was taken."""
    log.info("%s: %s", title, text)
    if sys.platform == "win32":
        import ctypes
        if offer:
            answer = ctypes.windll.user32.MessageBoxW(None, f"{text}\n\n{offer}?", title, 0x04 | 0x30 | 0x10000)   # MB_YESNO | MB_ICONWARNING
            return answer == 6      # IDYES
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40 | 0x10000)      # MB_ICONINFORMATION | MB_SETFOREGROUND
        return False
    if sys.platform == "darwin":
        return _mac_alert(title, text, offer)
    return False


def _mac_alert(title: str, text: str, offer: Optional[str]) -> bool:
    if threading.current_thread() is threading.main_thread():
        try:
            import AppKit
            app = AppKit.NSApplication.sharedApplication()
            app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
            app.activateIgnoringOtherApps_(True)
            box = AppKit.NSAlert.alloc().init()
            box.setAlertStyle_(AppKit.NSAlertStyleCritical)
            box.setMessageText_(title)
            box.setInformativeText_(text)
            box.addButtonWithTitle_("OK")
            if offer:
                box.addButtonWithTitle_(offer)
            return box.runModal() == AppKit.NSAlertSecondButtonReturn
        except ImportError:
            pass
    # AppKit only draws on the main thread, or isn't there: the system's own message box asks for nothing of this process.
    script = 'on run argv\ndisplay alert (item 1 of argv) message (item 2 of argv) as critical buttons (rest of rest of argv) default button 1\nreturn button returned of result\nend run'
    buttons = ["OK"] + ([offer] if offer else [])
    try:
        out = subprocess.run(["/usr/bin/osascript", "-e", script, title, text, *buttons], capture_output=True, text=True, check=False)
    except OSError:
        return False
    return bool(offer) and out.stdout.strip() == offer


def reveal_folder(folder: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(folder))       # type: ignore[attr-defined]  # a local folder this app chose, never a page's address
    elif sys.platform == "darwin":
        subprocess.run(["/usr/bin/open", str(folder)], check=False)


ATS_REFUSED = -1022     # App Transport Security: macOS refuses plain http to this name


def reachable(url: str) -> bool:
    import httpx   # its own certificate authorities: the packaged Mac app has none for urllib, so https:// failed

    try:
        r = httpx.get(f"{url.rstrip('/')}/api/wearables/pair", timeout=4.0, follow_redirects=True)
        return r.json().get("service") == "syntropy-health"
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


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
        startup_failed(exc)
        sys.exit(1)     # not an exception: PyInstaller would show its traceback in a second box


def startup_failed(exc: Exception) -> None:
    """Before there's a window: the data folder can't be used, or the window itself couldn't open."""
    from app.core import config

    _kind, text = describe_failure(exc)
    folder = None
    try:
        folder = config.data_dir()
    except OSError:
        pass
    if alert(f"{APP_NAME} couldn't start", f"{text}\n\n{where_details_are()}", "Show Data Folder" if folder else None) and folder:
        reveal_folder(folder)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(1, str(Path(__file__).resolve().parent.parent))
    main()
