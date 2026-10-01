"""
The ``syntropy-health`` command: run the server, keep it running in the background, back it up and update it.

    syntropy-health serve                 run in the foreground (http://localhost:8000)
    syntropy-health service install       start with the computer and restart if it stops
    syntropy-health service status|stop|start|uninstall
    syntropy-health backup [folder]       database, encryption key and uploaded files, in one .zip
    syntropy-health update                install the latest release and restart the service
    syntropy-health info                  where the data is and the addresses to open
    syntropy-health mcp                   the MCP server on stdio, for AI apps on this computer
    syntropy-health accounts              who signs in; `accounts reset-password NAME` for someone locked out

The same code runs from a clone (``./run.sh``), a pip or uv install, the Linux installer and the Mac and Windows apps.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Optional

from app.core import config

SERVICE = "syntropy-health"
LAUNCHD_LABEL = "io.syntropyhealth.server"
WINDOWS_TASK = "Syntropy Health"
DEFAULT_PORT = 8000
# Where `update` installs from: the latest release, unless the installer recorded another source (--source).
LATEST = "latest"


# ---------------------------------------------------------------------------
# serve / mcp / info
# ---------------------------------------------------------------------------

def serve(host: str = "0.0.0.0", port: int = DEFAULT_PORT, reload: bool = False) -> None:
    import uvicorn

    from app.services import network

    if sys.stdout is None:      # started without a console (pythonw, the windowed app): keep a log instead
        sys.stdout = sys.stderr = open(config.data_dir() / "server.log", "a", buffering=1, encoding="utf-8")
    _pid_file().write_text(f"{os.getpid()} {port}")
    # So pairing can give the iPhone a reachable address, and syntropyhealth.local is announced on the network.
    os.environ["SYNTROPY_LISTEN_HOST"], os.environ["SYNTROPY_LISTEN_PORT"] = host, str(port)
    print(f"Syntropy Health {config.APP_VERSION}: data in {config.data_dir()}")
    print(f"Open http://localhost:{port}")
    if host in ("0.0.0.0", "::"):
        for addr in network.home_addresses() + network.tailscale_addresses():
            print(f"  or http://{addr}:{port} from your other devices")
    uvicorn.run("app.main:app", host=host, port=port, reload=reload, proxy_headers=True,
                forwarded_allow_ips=config.env("FORWARDED_ALLOW_IPS") or "127.0.0.1", log_level="info")


def info(port: Optional[int]) -> None:
    from app.services import network

    port = port or _running_port() or DEFAULT_PORT
    print(f"Syntropy Health {config.APP_VERSION}")
    print(f"Data:     {config.data_dir()}  (back up this folder, or run: syntropy-health backup)")
    print(f"Program:  {sys.executable}")
    print(f"Open:     http://localhost:{port}")
    home, tailscale = network.home_addresses(), network.tailscale_addresses()
    if (home or tailscale) and not _answers((home + tailscale)[0], port):
        print("          (only from this computer: other devices can't connect. In the app, turn on Let Your iPhone"
              " and Other Devices Connect)")
    else:
        for addr in home:
            print(f"          http://{addr}:{port}  (other devices on your home network)")
        for addr in tailscale:
            print(f"          http://{addr}:{port}  (your devices on Tailscale)")
    if getattr(sys, "frozen", False):
        print("Runs in:  the Syntropy Health app (its menu bar or tray icon has Open at Login)")
    else:
        print(f"Service:  {_service_state()}")


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------

def _answers(host: str, port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def backup(dest: Optional[str]) -> Path:
    """A consistent copy of the database (taken while the server runs), the encryption key and uploads, zipped."""
    data = config.data_dir()
    folder = Path(dest).expanduser() if dest else Path.cwd()
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"syntropy-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "syntropy.db"
        src, dst = sqlite3.connect(str(config.db_path())), sqlite3.connect(str(snapshot))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snapshot, "syntropy.db")
            if (data / "secret.key").exists():
                z.write(data / "secret.key", "secret.key")      # without it, saved sign-ins and keys can't be read
            for f in (data / "uploads").rglob("*") if (data / "uploads").exists() else []:
                if f.is_file():
                    z.write(f, str(Path("uploads") / f.relative_to(data / "uploads")))
    if os.name != "nt":
        out.chmod(0o600)
    print(f"Backed up to {out}")
    print("It holds your health records and the key that unlocks saved sign-ins: keep it somewhere private.")
    return out


# ---------------------------------------------------------------------------
# service: start with the computer
# ---------------------------------------------------------------------------

def _program() -> list[str]:
    """How to start this server: the packaged app's executable, or this Python with -m app. On Windows, the versions
    without a console window (the app's "Syntropy Health.exe", pythonw.exe), so starting at sign-in opens no window."""
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        windowed = exe.with_name("Syntropy Health.exe")
        return [str(windowed if sys.platform == "win32" and windowed.exists() else exe)]
    if sys.platform == "win32" and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    return [str(exe), "-m", "app"]


WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_RUN_VALUE = "Syntropy Health Server"     # the desktop app uses "Syntropy Health"


def _pid_file() -> Path:
    return config.data_dir() / "server.pid"


def _running_port() -> Optional[int]:
    """The port the server started by `serve` (or the service) is on, while it runs."""
    try:
        pid, port = _pid_file().read_text().split()
        if os.name != "nt":
            os.kill(int(pid), 0)
        return int(port)
    except (OSError, ValueError):
        return None


def _windows_run_entry(command: Optional[str]) -> Optional[str]:
    """Read (command=None), set, or with command="" remove the per-user Run entry that starts the server at sign-in."""
    import winreg
    access = winreg.KEY_READ | winreg.KEY_SET_VALUE
    if command:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, access) as key:
            winreg.SetValueEx(key, WINDOWS_RUN_VALUE, 0, winreg.REG_SZ, command)
        return None
    try:       # the Run key itself may not exist yet (a new account, or a sign-in task was used instead)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, access) as key:
            if command is None:
                return winreg.QueryValueEx(key, WINDOWS_RUN_VALUE)[0]
            winreg.DeleteValue(key, WINDOWS_RUN_VALUE)
    except OSError:
        pass
    return None


def _windows_start(args: "list[str] | str") -> None:
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(args, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _windows_stop() -> None:
    try:
        pid = int(_pid_file().read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return
    _run(["taskkill", "/F", "/PID", str(pid)], check=False)
    _pid_file().unlink(missing_ok=True)


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _systemd_unit(host: str, port: int, user: Optional[str]) -> str:
    env = {"SYNTROPY_DATA_DIR": str(config.data_dir()), "PYTHONUNBUFFERED": "1"}
    if config.env("TZ"):
        env["TZ"] = config.env("TZ")
    lines = ["[Unit]", "Description=Syntropy Health", "After=network-online.target", "Wants=network-online.target", "",
             "[Service]", "Type=simple",
             f"ExecStart={shlex.join([*_program(), 'serve', '--host', host, '--port', str(port)])}",
             *[f'Environment="{k}={v}"' for k, v in env.items()],
             "EnvironmentFile=-/etc/syntropy-health/env" if user else f"EnvironmentFile=-{config.data_dir() / '.env'}",
             "Restart=on-failure", "RestartSec=5"]
    if user:
        lines += [f"User={user}", "NoNewPrivileges=true", "ProtectSystem=full", "PrivateTmp=true"]
    lines += ["", "[Install]", f"WantedBy={'multi-user.target' if user else 'default.target'}", ""]
    return "\n".join(lines)


def _launchd_plist(host: str, port: int) -> str:
    args = "".join(f"<string>{a}</string>" for a in [*_program(), "serve", "--host", host, "--port", str(port)])
    log = config.data_dir() / "server.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>EnvironmentVariables</key><dict><key>SYNTROPY_DATA_DIR</key><string>{config.data_dir()}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""


def _launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _systemd_system() -> bool:
    return os.geteuid() == 0 if hasattr(os, "geteuid") else False


def _systemctl(*args: str) -> list[str]:
    return ["systemctl", *args] if _systemd_system() else ["systemctl", "--user", *args]


def _unit_path() -> Path:
    if _systemd_system():
        return Path("/etc/systemd/system") / f"{SERVICE}.service"
    return Path.home() / ".config" / "systemd" / "user" / f"{SERVICE}.service"


def service_install(host: str, port: int, user: Optional[str]) -> None:
    if sys.platform == "darwin":
        path = _launchd_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], check=False)
        path.write_text(_launchd_plist(host, port))
        _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
        print(f"Syntropy Health now starts when you log in (launchd agent {LAUNCHD_LABEL}).")
    elif sys.platform == "win32":
        args = [*_program(), "serve", "--host", host, "--port", str(port)]
        cmd = subprocess.list2cmdline(args)
        created = _run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED", "/TN", WINDOWS_TASK, "/TR", cmd],
                       check=False)
        if created.returncode == 0:
            _run(["schtasks", "/Run", "/TN", WINDOWS_TASK], check=False)
            print(f"Syntropy Health now starts when you sign in to Windows (scheduled task \"{WINDOWS_TASK}\").")
        else:
            # Standard (non-administrator) accounts can't create sign-in tasks; the per-user Run entry starts it too.
            _windows_stop()
            _windows_run_entry(cmd)
            _windows_start(args)
            print("Syntropy Health now starts when you sign in to Windows (your account's startup programs).")
    else:
        if not shutil.which("systemctl"):
            sys.exit("This system doesn't use systemd. Start `syntropy-health serve` with your init system instead.")
        if _systemd_system() and not user:
            sys.exit("As root, say which account runs the server: syntropy-health service install --user syntropy")
        path = _unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_systemd_unit(host, port, user if _systemd_system() else None))
        _run(_systemctl("daemon-reload"))
        _run(_systemctl("enable", "--now", SERVICE))
        print(f"Syntropy Health is running as the systemd service {SERVICE} and starts with the computer.")
        if not _systemd_system():
            print("To keep it running while you're logged out: sudo loginctl enable-linger $USER")
    print(f"Open http://localhost:{port}")


def service_uninstall() -> None:
    if sys.platform == "darwin":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(_launchd_path())], check=False)
        _launchd_path().unlink(missing_ok=True)
    elif sys.platform == "win32":
        _run(["schtasks", "/End", "/TN", WINDOWS_TASK], check=False)
        _run(["schtasks", "/Delete", "/F", "/TN", WINDOWS_TASK], check=False)
        _windows_run_entry("")
        _windows_stop()
    else:
        _run(_systemctl("disable", "--now", SERVICE), check=False)
        _unit_path().unlink(missing_ok=True)
        _run(_systemctl("daemon-reload"), check=False)
    print("Removed the background service. Your data is untouched in", config.data_dir())


def _service_state() -> str:
    if sys.platform == "darwin":
        if not _launchd_path().exists():
            return "not installed"
        r = _run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], check=False)
        return "running" if "state = running" in r.stdout else "installed, not running"
    if sys.platform == "win32":
        r = _run(["schtasks", "/Query", "/TN", WINDOWS_TASK], check=False)
        return "installed" if r.returncode == 0 or _windows_run_entry(None) else "not installed"
    if not shutil.which("systemctl") or not _unit_path().exists():
        return "not installed"
    return _run(_systemctl("is-active", SERVICE), check=False).stdout.strip() or "unknown"


def service_control(action: str) -> None:
    if sys.platform == "darwin":
        target = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
        cmd = {"start": ["launchctl", "kickstart", target], "stop": ["launchctl", "kill", "TERM", target],
               "restart": ["launchctl", "kickstart", "-k", target]}[action]
    elif sys.platform == "win32":
        entry = _windows_run_entry(None)
        if entry and _run(["schtasks", "/Query", "/TN", WINDOWS_TASK], check=False).returncode != 0:
            if action in ("stop", "restart"):
                _windows_stop()
            if action in ("start", "restart"):
                _windows_start(entry)
            print(f"{action}: done")
            return
        if action == "restart":
            _run(["schtasks", "/End", "/TN", WINDOWS_TASK], check=False)
            action = "start"
        cmd = ["schtasks", "/Run" if action == "start" else "/End", "/TN", WINDOWS_TASK]
    else:
        cmd = _systemctl(action, SERVICE)
    r = _run(cmd, check=False)
    print(r.stdout.strip() or r.stderr.strip() or f"{action}: done")


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

def update() -> None:
    from app.services import updates

    if getattr(sys, "frozen", False):
        sys.exit("The Mac and Windows apps install updates themselves: Settings → General → Updates, or Check for "
                 f"Updates in the app's menu. Or download the latest from {updates.RELEASES_URL}.")
    if config.is_source_checkout():
        sys.exit("This is a clone of the repository: update it with git pull.")
    recorded = Path(sys.prefix) / "syntropy-source.txt"
    source = (recorded.read_text().strip() if recorded.exists() else "") or LATEST
    if source == LATEST:
        try:
            release = updates.fetch_latest()
        except updates.CheckFailed as exc:
            sys.exit(f"Couldn't check for updates: {exc}")
        if release is None:
            print("No release has been published yet: installing the latest code instead.")
            source = updates.MAIN_SOURCE
        elif not updates.is_newer(release["version"]):
            print(f"Syntropy Health {config.APP_VERSION} is the latest version.")
            return
        else:
            print(f"Updating Syntropy Health {config.APP_VERSION} to {release['version']}.")
            source = updates.package_source(release)
    uv = shutil.which("uv")
    cmd = ([uv, "pip", "install", "--python", sys.executable, "--upgrade", source] if uv
           else [sys.executable, "-m", "pip", "install", "--upgrade", source])
    print("Installing the latest version…")
    subprocess.run(cmd, check=True)
    if _service_state() not in ("not installed",):
        service_control("restart")
    print("Updated. Your data is untouched; the database is upgraded when the server starts.")


def update_due() -> None:
    """For the Linux installer's daily timer: exits 0 when the owner turned on automatic updates in Settings and a
    newer release is out (then the timer runs `update`), 1 otherwise. Runs as the service's account, which owns the
    database."""
    from app.services import updates

    if not updates.auto_on():
        print("Automatic updates are off (Settings → General → Updates).")
        sys.exit(1)
    try:
        release = updates.fetch_latest()
    except updates.CheckFailed as exc:
        print(f"Couldn't check for updates: {exc}")
        sys.exit(1)
    if not release or not updates.is_newer(release["version"]):
        print(f"Syntropy Health {config.APP_VERSION} is the latest version.")
        sys.exit(1)
    print(f"Syntropy Health {release['version']} is available.")


def dev_no_password(state: Optional[str]) -> None:
    """For development only: let this instance run without a password (the API can't change this)."""
    from app.core import db, settings
    db.ensure_migrated()
    if state:
        settings.set("dev.allow_no_password", True if state == "on" else None)
    on = bool(settings.get("dev.allow_no_password"))
    print(f"Running without a password is {'allowed (development only)' if on else 'not allowed: a password is required'}.")
    if on:
        print("Anyone who can open Syntropy Health can read every record in it. Turn this off with: "
              "syntropy-health dev no-password off")


def accounts_command(action: Optional[str], name: Optional[str]) -> None:
    """Who signs in, and a new password for someone who forgot theirs. Only from this computer: someone here can read
    the database anyway, so this adds no way in that wasn't already there."""
    import getpass

    from app.core import auth, db
    from app.store import accounts
    db.ensure_migrated()
    if action != "reset-password":
        for a in accounts.list_accounts():
            print(f"{a['name']:<24} {a['role']:<7} {'password set' if a['has_password'] else 'no password'}")
        if not accounts.any_accounts():
            print("No one signs in yet: open Syntropy Health to set it up.")
        return
    account = accounts.find(name or "")
    if not account:
        sys.exit(f"No one named {name!r} signs in here. `syntropy-health accounts` lists who does.")
    password = getpass.getpass(f"New password for {account['name']}: ")
    if getpass.getpass("Again: ") != password:
        sys.exit("The passwords don't match.")
    try:
        auth.validate_password(password)
    except Exception as exc:  # noqa: BLE001 - HTTPException with a friendly message
        sys.exit(getattr(exc, "detail", str(exc)))
    accounts.set_password(account["id"], password)
    with db.db() as conn:
        db.audit(conn, "cli", "auth.password_reset", None, account["profile_id"])
    print(f"{account['name']} can sign in with the new password. Their other browsers were signed out.")


# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="syntropy-health", description="Syntropy Health: your health records, on your own computer.")
    parser.add_argument("--data-dir", help="where the database lives (default: %(default)s)", default=None)
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("serve", help="run the server in the foreground")
    p.add_argument("--host", default="0.0.0.0", help="0.0.0.0 lets other devices connect; 127.0.0.1 keeps it to this computer")
    p.add_argument("--port", type=int, default=int(config.env("PORT") or DEFAULT_PORT))
    p.add_argument("--reload", action="store_true", help=argparse.SUPPRESS)
    s = sub.add_parser("service", help="start with the computer (systemd, launchd or a Windows sign-in task)")
    s.add_argument("action", choices=["install", "uninstall", "status", "start", "stop", "restart"])
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--user", help="Linux, as root: the account that runs the server")
    b = sub.add_parser("backup", help="zip the database, encryption key and uploads")
    b.add_argument("folder", nargs="?")
    u = sub.add_parser("update", help="install the latest release and restart the service")
    u.add_argument("--due", action="store_true", help=argparse.SUPPRESS)
    i = sub.add_parser("info", help="where the data is and which addresses to open")
    i.add_argument("--port", type=int, default=None, help="default: the running server's")
    sub.add_parser("mcp", help="the MCP server on stdio, for AI apps on this computer")
    sub.add_parser("version")
    a = sub.add_parser("accounts", help="who signs in; reset-password NAME for someone locked out")
    a.add_argument("action", nargs="?", choices=["list", "reset-password"], default="list")
    a.add_argument("name", nargs="?")
    d = sub.add_parser("dev", help=argparse.SUPPRESS)
    d.add_argument("setting", choices=["no-password"])
    d.add_argument("state", nargs="?", choices=["on", "off"])
    args = parser.parse_args(argv)
    if args.data_dir:
        os.environ["SYNTROPY_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())

    if args.command == "serve":
        serve(args.host, args.port, args.reload)
    elif args.command == "service":
        if args.action == "install":
            service_install(args.host, args.port, args.user)
        elif args.action == "uninstall":
            service_uninstall()
        elif args.action == "status":
            print(_service_state())
        else:
            service_control(args.action)
    elif args.command == "backup":
        backup(args.folder)
    elif args.command == "update":
        update_due() if args.due else update()
    elif args.command == "info":
        info(args.port)
    elif args.command == "mcp":
        from app import mcp_server
        mcp_server.main()
    elif args.command == "version":
        print(config.APP_VERSION)
    elif args.command == "accounts":
        accounts_command(args.action, args.name)
    elif args.command == "dev":
        dev_no_password(args.state)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
