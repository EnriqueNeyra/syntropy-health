"""
The ``syntropy-health`` command: run the server, keep it running in the background, back it up and update it.

    syntropy-health serve                 run in the foreground (http://localhost:8000)
    syntropy-health service install       start with the computer and restart if it stops
    syntropy-health service status|stop|start|uninstall
    syntropy-health backup [folder]       everything, in one encrypted file
    syntropy-health restore FILE          put a backup back (--check only tests it)
    syntropy-health update                install the latest release and restart the service
    syntropy-health info                  where the data is and the addresses to open
    syntropy-health mcp [--profile NAME]  the MCP server on stdio, for AI apps on this computer
    syntropy-health accounts              who signs in; `accounts reset-password NAME` for someone locked out;
                                          `accounts make-owner NAME` to hand the server over
    syntropy-health key new               replace a lost encryption key

A backup restores on any computer, and asks for the passphrase it was made with:

    Linux installer  sudo systemctl stop syntropy-health; sudo syntropy-health restore FILE; sudo systemctl start syntropy-health
    Docker           docker compose stop; docker compose run --rm -v "$PWD/FILE:/FILE" syntropy syntropy-health restore /FILE; docker compose start
    Mac and Windows  quit the app (or `syntropy-health service stop`), run restore, open it again; or use Settings → Your data

The same code runs from a clone (``./run.sh``), a pip or uv install, the Linux installer and the Mac and Windows apps.
"""

from __future__ import annotations

import argparse
import errno
import io
import json
import logging
import logging.handlers
import os
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from xml.parsers.expat import ExpatError
from xml.sax.saxutils import escape
from pathlib import Path
from typing import Optional

from app.core import config

SERVICE = "syntropy-health"
LAUNCHD_LABEL = "io.syntropyhealth.server"
WINDOWS_TASK = "Syntropy Health"
DEFAULT_PORT = 8000
UPDATE_WAIT_SECONDS = 600
SYSTEM_UNIT = Path("/etc/systemd/system") / f"{SERVICE}.service"
LOG_BYTES = 5_000_000
LOG_KEEP = 2
# Where `update` installs from: the latest release, unless the installer recorded another source (--source).
LATEST = "latest"


# ---------------------------------------------------------------------------
# serve / mcp / info
# ---------------------------------------------------------------------------

class _LogFile(logging.handlers.RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        """A log that can't be written, on a full disk say, must not stop the server. The usual report of the problem goes
        to sys.stderr, which is this log: it would fail too, and so on until Python gave up and the server fell over."""
        if self.stream and self.stream.closed:      # so it opens again once there's room
            self.stream = None


class _RotatingLog(io.TextIOBase):
    """What a server with no console writes to, instead of a file that grows for as long as the computer stays on."""
    encoding = "utf-8"

    def __init__(self, path: Path):
        self._handler = _LogFile(path, maxBytes=LOG_BYTES, backupCount=LOG_KEEP, encoding="utf-8", errors="backslashreplace", delay=True)
        self._handler.terminator = ""

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self._handler.handle(logging.makeLogRecord({"msg": text}))      # threads write here too, and one may be rotating
        return len(text)

    def close(self) -> None:
        self._handler.close()
        super().close()


def _leave_cleanly(_signum, _frame) -> None:
    sys.exit(0)


def serve(host: str = "0.0.0.0", port: int = DEFAULT_PORT, reload: bool = False) -> None:
    import uvicorn

    from app.services import network

    # Started without a console (pythonw, the windowed app) or by launchd, which can't limit a log: keep one here.
    if sys.stdout is None or config.env_bool("SYNTROPY_LOG_TO_FILE"):
        sys.stdout = sys.stderr = _RotatingLog(config.data_dir() / "server.log")
    # Uvicorn passes SIGTERM on once it has shut down. Dying from it would look like a crash to launchd, which restarts
    # the server, and would leave the pid file behind.
    signal.signal(signal.SIGTERM, _leave_cleanly)
    try:
        _pid_file().write_text(f"{os.getpid()} {port}")
    except OSError as exc:      # a full disk, perhaps: the records can still be read, and that's when they may be needed
        print(f"Couldn't write {_pid_file().name}, but starting anyway: {exc}")
    # So pairing can give the iPhone a reachable address, and syntropyhealth.local is announced on the network.
    os.environ["SYNTROPY_LISTEN_HOST"], os.environ["SYNTROPY_LISTEN_PORT"] = host, str(port)
    print(f"Syntropy Health {config.APP_VERSION}: data in {config.data_dir()}")
    print(f"Open http://localhost:{port}")
    if host in ("0.0.0.0", "::"):
        for addr in network.home_addresses() + network.tailscale_addresses():
            print(f"  or http://{addr}:{port} from your other devices")
    try:
        uvicorn.run("app.main:app", host=host, port=port, reload=reload, proxy_headers=True,
                    forwarded_allow_ips=config.env("FORWARDED_ALLOW_IPS") or "127.0.0.1", log_level="info",
                    timeout_graceful_shutdown=10)      # a local AI answer or a long download doesn't hold up stopping
    finally:
        _pid_file().unlink(missing_ok=True)


def info(port: Optional[int]) -> None:
    from app.services import network

    port = port or _running_port() or DEFAULT_PORT
    print(f"Syntropy Health {config.APP_VERSION}")
    print(f"Data:     {config.data_dir()}  (to back it up: syntropy-health backup. Copying the folder while it runs isn't reliable)")
    print(f"Program:  {sys.executable}")
    print(f"Open:     http://localhost:{port}")
    home, tailscale = network.home_addresses(), network.tailscale_addresses()
    if (home or tailscale) and not _answers((home + tailscale)[0], port):
        how = ("In the app, turn on Let Your iPhone and Other Devices Connect" if getattr(sys, "frozen", False)
               else "Start it with --host 0.0.0.0 to change that")
        print(f"          (only from this computer: other devices can't connect. {how})")
    else:
        for addr in home:
            print(f"          http://{addr}:{port}  (other devices on your home network)")
        for addr in tailscale:
            print(f"          http://{addr}:{port}  (your devices on Tailscale)")
    if getattr(sys, "frozen", False):
        print("Runs in:  the Syntropy Health app (its menu bar or tray icon has Open at Login)")
    elif config.env("SYNTROPY_INSTALL") == "docker":
        print("Runs in:  a Docker container")
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


def _read_passphrase_file(name: str) -> str:
    try:
        raw = sys.stdin.read() if name == "-" else Path(name).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        sys.exit(f"Couldn't read the passphrase from {name}: {exc}")
    return raw.rstrip("\r\n")


def _choose_passphrase() -> str:
    import getpass

    if not (sys.stdin and sys.stdin.isatty()):
        sys.exit("A backup is encrypted with a passphrase, and there's no one here to type one. Give it with "
                 "--passphrase-file FILE, or choose --no-encrypt to leave the backup readable by anyone who gets the file.")
    print("Choose a passphrase for this backup. Nobody can open the backup without it, and it can't be recovered.")
    passphrase = getpass.getpass("Passphrase: ")
    if getpass.getpass("Again: ") != passphrase:
        sys.exit("The passphrases don't match. Nothing was backed up.")
    return passphrase


def backup(dest: Optional[str], *, no_encrypt: bool = False, passphrase_file: Optional[str] = None) -> Path:
    """Everything in one file, taken while the server runs. Encrypted unless someone says otherwise, in so many words."""
    from app.core import db
    from app.services import backups

    if no_encrypt and passphrase_file:
        sys.exit("--no-encrypt and --passphrase-file can't be used together.")
    folder = Path(dest).expanduser() if dest else Path.cwd()
    db.open_connection(config.db_path()).close()      # before asking for a passphrase: this may be the wrong folder
    passphrase = None
    if not no_encrypt:
        passphrase = _read_passphrase_file(passphrase_file) if passphrase_file else _choose_passphrase()
    try:
        result = backups.create(folder, passphrase=passphrase, unencrypted=no_encrypt, made_by="command")
    except backups.BackupProblem as exc:
        sys.exit(str(exc))
    except PermissionError:
        sys.exit(f"This account can't write to {folder}. Choose a folder it can write to.")
    except OSError as exc:
        sys.exit(f"Couldn't write the backup to {folder}: {exc}")
    print(f"Backed up to {result['path']}")
    print(f"It holds {result['holds']}.")
    if result["key"] == "setting":
        print("The encryption key isn't in it: it comes from the SYNTROPY_SECRET_KEY setting. Keep that value somewhere safe "
              "too. Without it, nobody can sign in after a restore and saved sign-ins are lost.")
    elif result["key"] == "included":
        print("The encryption key that unlocks saved sign-ins is in it too.")
    if no_encrypt:
        print("WARNING: this backup isn't encrypted. Anyone who gets the file can read every record and use every saved sign-in. "
              "Keep it somewhere only you can reach.")
    else:
        print("Keep the passphrase somewhere safe: without it this backup can't be opened by anyone. Older backups keep "
              "anything you delete later, so remove those you no longer need.")
    return Path(result["path"])


def _hand_over(made: Path, target: Path) -> None:
    """Into its folder, which may be on another drive. There a move is a copy, so like any backup it has its real name only
    once it's all there."""
    from app.services import backups

    try:
        os.replace(made, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        partial = target.with_name(target.name + ".partial")
        try:
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as out, made.open("rb") as source:
                shutil.copyfileobj(source, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            os.replace(partial, target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    try:
        backups._sync_folder(target.parent)
    except OSError:     # not every drive can; the file is there either way
        pass


def _give_to_caller(path: Path) -> bool:
    """Theirs, so they can move it without sudo, and private to them. Some drives (exFAT, a NAS that maps root to nobody)
    don't keep this."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    try:
        path.chmod(0o600)
        os.chown(path, int(uid) if uid and gid else 0, int(gid) if uid and gid else 0)
    except OSError:
        return False
    return True


def _as_service_account(account: str, args: argparse.Namespace) -> None:
    """Run as root by the Linux installer's command. The data belongs to the service's account, and the folder or file
    named on the command line usually doesn't: that account makes the backup or reads the file in a private folder of its
    own, and root hands it over. Otherwise `sudo syntropy-health backup /some/folder` can't write there."""
    import pwd

    entry = pwd.getpwnam(account)
    child = ["runuser", "-u", account, "--", sys.argv[0]]
    given = None
    if getattr(args, "passphrase_file", None):
        given = _read_passphrase_file(args.passphrase_file)
    folder = None
    if args.command == "backup":
        folder = Path(args.folder).expanduser() if args.folder else Path.cwd()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=folder):
                pass
        except OSError as exc:
            sys.exit(f"Couldn't write to {folder}: {exc.strerror or exc}. Nothing was backed up. Choose a folder that can be written to.")
    work = None
    try:
        work = Path(tempfile.mkdtemp(dir=config.data_dir(), prefix=".backup-work-"))
        os.chown(work, entry.pw_uid, entry.pw_gid)
        if folder:
            cmd = [*child, "backup", str(work)]
            if args.no_encrypt:
                cmd.append("--no-encrypt")
            if given is not None:
                cmd += ["--passphrase-file", "-"]
            done = subprocess.run(cmd, input=given, text=True if given is not None else None)
            if done.returncode:
                sys.exit(done.returncode)
            for made in work.iterdir():
                target = folder / made.name
                try:
                    _hand_over(made, target)
                except OSError as exc:
                    kept = config.data_dir() / made.name
                    os.replace(made, kept)
                    kept.chmod(0o600)
                    sys.exit(f"The backup was made, but it couldn't be put in {folder}: {exc.strerror or exc}. It's at {kept} for now, where "
                             f"only an administrator can read it. Make room or choose another folder, then move it with: sudo mv {kept} {folder}/")
                if _give_to_caller(target):
                    print(f"Moved it to {target}, which only you can read.")
                else:
                    print(f"Moved it to {target}. That drive doesn't keep who can read a file, so it's up to you to keep the drive private.")
            return
        for given_path in (args.file, args.key_file):
            if given_path and not Path(given_path).expanduser().is_file():
                sys.exit(f"There's no file at {given_path}.")
        source = work / Path(args.file).name
        shutil.copyfile(Path(args.file).expanduser(), source)
        os.chown(source, entry.pw_uid, entry.pw_gid)
        source.chmod(0o600)
        cmd = [*child, "restore", str(source)]
        if args.key_file:
            key = work / "given.key"
            shutil.copyfile(Path(args.key_file).expanduser(), key)
            os.chown(key, entry.pw_uid, entry.pw_gid)
            key.chmod(0o600)
            cmd += ["--key-file", str(key)]
        for flag, value in (("--check", args.check), ("--key-only", args.key_only), ("--yes", args.yes),
                            ("--without-key", args.without_key)):
            if value:
                cmd.append(flag)
        if given is not None:
            cmd += ["--passphrase-file", "-"]
        done = subprocess.run(cmd, input=given, text=True if given is not None else None)
        if done.returncode:
            sys.exit(done.returncode)
    except OSError as exc:
        sys.exit(f"Syntropy Health couldn't do that, and nothing was changed: {exc}")
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)


def _stop_first() -> Optional[str]:
    """The server, if it's running, in words that say how to stop it here."""
    from app.services import backups

    port = _running_port()
    running = bool(port and _answers("127.0.0.1", port))
    other = backups.in_use_elsewhere()
    if not running and not other:
        return None
    if getattr(sys, "frozen", False):
        how = "Quit Syntropy Health from its menu bar or tray icon"
    elif config.env("SYNTROPY_INSTALL") == "docker":
        how = "Stop the container (docker compose stop), then run this with: docker compose run --rm syntropy syntropy-health restore …"
    elif sys.platform.startswith("linux") and shutil.which("systemctl"):
        how = f"Stop it with: sudo systemctl stop {SERVICE}"
    else:
        how = "Stop it first (syntropy-health service stop, or Ctrl-C where it's running)"
    if running:
        return f"Syntropy Health is running, so a backup can't be put back underneath it. {how}. Nothing was changed."
    return f"{other} {how}, if it's running. Nothing was changed."


def _print_report(report: dict) -> None:
    made = f"made {report['created_at']}" if report.get("created_at") else "from an older version, with no date"
    print(f"{report['file']}: {made}" + (f" by version {report['app_version']}" if report.get("app_version") else ""))
    print(f"It holds {report['holds']}, and checks out as undamaged.")
    if report["people"]:
        print(f"People: {', '.join(report['people'])}")
    if report["upgrade_needed"]:
        print("It's from an older version: it's upgraded when restored, as any update would.")
    print(report["key"]["message"])
    if report["replaces"]:
        print(f"It would replace what's here now: {report['replaces']['holds']}. That's kept in {db_folder_name()}.")
    print(report["sign_in"])


def db_folder_name() -> Path:
    from app.core import db
    return config.db_path().parent / db.PRE_RESTORE_DIR


def restore(file: str, *, check_only: bool = False, key_only: bool = False, yes: bool = False, without_key: bool = False,
            passphrase_file: Optional[str] = None, key_file: Optional[str] = None) -> None:
    """Tests the file first, whatever was asked for. The server has to be stopped, since it holds the database open."""
    import getpass

    from app.core import db, security
    from app.services import backups

    path = Path(file).expanduser()
    if not path.is_file():
        sys.exit(f"There's no file at {path}.")
    if not check_only and (problem := _stop_first()):
        sys.exit(problem)
    passphrase = _read_passphrase_file(passphrase_file) if passphrase_file else None
    item = None
    try:
        while item is None:
            try:
                item = backups.check(path, passphrase, Path(key_file).expanduser() if key_file else None)
            except (backups.backup_file.WrongPassphrase, backups.PassphraseNeeded) as exc:
                if passphrase_file or not (sys.stdin and sys.stdin.isatty()):
                    sys.exit(f"{exc} Give it with --passphrase-file FILE.")
                if passphrase:
                    print(exc)
                passphrase = getpass.getpass("Passphrase: ")
        report = item.report
        _print_report(report)
        if check_only:
            print("Nothing was changed. This was only a check.")
            return
        if key_only:
            staged_key = item.folder / "secret.key"
            if not staged_key.exists():
                sys.exit("This backup has no encryption key in it. Nothing was changed.")
            target = config.data_dir() / "secret.key"
            here = config.db_path()
            if not here.exists() or not security.opens(here, security.key_from_file(staged_key.read_bytes())):
                sys.exit("The key in this backup doesn't open what's saved in the database here. Nothing was changed.")
            if target.exists():
                os.replace(target, target.with_name(f"{target.name}.replaced-{time.strftime('%Y%m%d-%H%M%S')}"))
            if os.name != "nt":
                staged_key.chmod(0o600)
            db.same_owner(staged_key, target.parent)
            os.replace(staged_key, target)
            print(f"The encryption key is back at {target}. Your records weren't touched.")
            return
        if not report["key"]["ok"] and not without_key:
            sys.exit(f"{report['key']['message']}\nNothing was changed. To restore anyway, without passwords and saved sign-ins, "
                     "add --without-key.")
        if not yes:
            if not (sys.stdin and sys.stdin.isatty()):
                sys.exit("Say --yes to go ahead without being asked. Nothing was changed.")
            if input("Type RESTORE to go ahead: ").strip() != "RESTORE":
                sys.exit("Nothing was changed.")
        try:
            done = backups.apply(item, accept_key_problem=without_key)
        except PermissionError as exc:
            sys.exit(f"This account can't write to the data folder: {exc}. Nothing was changed.")
        print(f"Restored: {done['holds']}.")
        if done.get("safety_copy"):
            print(f"What was here before is kept in {done['safety_copy']}.")
        print(done["sign_in"])
        print("Sources may ask you to sign in again if their sign-ins have changed since the backup, and phones paired since then "
              "need pairing again. Start Syntropy Health when you're ready.")
    except backups.BackupProblem as exc:
        sys.exit(str(exc))
    except backups.BackupFileError as exc:
        sys.exit(f"{exc} Nothing was changed.")
    except OSError as exc:
        sys.exit(f"Couldn't restore: {exc}. Nothing was changed.")
    finally:
        if item:
            backups.discard(item.id)
        db.close_readers()


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
        if sys.platform == "win32":
            if not _is_our_server(_windows_command_line(int(pid))):
                return None
        else:
            os.kill(int(pid), 0)
        return int(port)
    except (OSError, ValueError):
        return None


def _windows_command_line(pid: int) -> str:
    """What a process was started as, or "" if there's no such process or it can't be told."""
    query = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", query], check=False).stdout.strip()


def _is_our_server(command_line: str) -> bool:
    return re.search(r"\bserve\b", command_line) is not None and re.search(r"python|syntropy", command_line, re.I) is not None


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
    if _is_our_server(_windows_command_line(pid)):      # after a restart, that number may be some other program's by now
        _run(["taskkill", "/F", "/PID", str(pid)], check=False)
    _pid_file().unlink(missing_ok=True)


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _windows_task_xml(args: list[str]) -> str:
    """The sign-in task. Made from a definition because the defaults of `schtasks /Create` stop the server after 72 hours
    and when a laptop is unplugged, with nothing to restart it until the next sign-in."""
    user = escape(f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\"))
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Starts the Syntropy Health server when you sign in.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>10</Count></RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(args[0])}</Command><Arguments>{escape(subprocess.list2cmdline(args[1:]))}</Arguments></Exec></Actions>
</Task>
"""


def _windows_create_task(args: list[str]) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        definition = Path(tmp) / "task.xml"
        definition.write_text(_windows_task_xml(args), encoding="utf-16")
        return _run(["schtasks", "/Create", "/F", "/TN", WINDOWS_TASK, "/XML", str(definition)], check=False)


def _data_dir_chosen() -> bool:
    return bool(config.env("SYNTROPY_DATA_DIR") or config.env("SYNTROPY_DB_PATH"))


def _service_data_dir(user: Optional[str]) -> Path:
    """Where the service keeps its data. Started as root for another account, not root's own folder, which that account
    can't read: that account's usual folder, or the shared one for an account with no home of its own."""
    if not user or not _is_root() or _data_dir_chosen():
        return config.data_dir()
    import pwd
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        sys.exit(f"There's no account named {user} on this computer. Create it first, or name another with --user.")
    home = Path(account.pw_dir)
    try:
        owns_home = home.stat().st_uid == account.pw_uid
    except OSError:
        owns_home = False
    return home / ".local" / "share" / SERVICE if owns_home else Path("/var/lib") / SERVICE


def _give_data_dir_to(user: str, folder: Path) -> None:
    import pwd
    account = pwd.getpwnam(user)
    missing = []
    place = folder
    while not place.exists():
        missing.append(place)
        place = place.parent
    folder.mkdir(parents=True, exist_ok=True)
    for made in [*missing, folder]:     # folders made along the way too: the account's own, once it has them
        os.chown(made, account.pw_uid, account.pw_gid)


def _systemd_hardening(data: Path) -> list[str]:
    """The server needs its data folder and the network, and nothing else on the computer. AF_NETLINK is how it finds its
    own addresses, for pairing and syntropyhealth.local. Home folders stay readable when it lives or runs from one."""
    in_home = any(str(p).startswith(("/home/", "/root/")) for p in [data, *map(Path, _program()[:1])])
    writable = f'"{data}"' if " " in str(data) else str(data)
    return ["NoNewPrivileges=true", "ProtectSystem=strict", f"ReadWritePaths={writable}", "PrivateTmp=true", "PrivateDevices=true",
            f"ProtectHome={'read-only' if in_home else 'true'}", "ProtectKernelTunables=true", "ProtectKernelModules=true",
            "ProtectKernelLogs=true", "ProtectControlGroups=true", "ProtectClock=true", "ProtectHostname=true",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK", "RestrictNamespaces=true", "RestrictRealtime=true",
            "RestrictSUIDSGID=true", "LockPersonality=true", "CapabilityBoundingSet=", "SystemCallArchitectures=native"]


def _systemd_unit(host: str, port: int, user: Optional[str]) -> str:
    data = _service_data_dir(user)
    env = {"SYNTROPY_DATA_DIR": str(data), "PYTHONUNBUFFERED": "1"}
    if config.env("TZ"):
        env["TZ"] = config.env("TZ")
    lines = ["[Unit]", "Description=Syntropy Health", "After=network-online.target", "Wants=network-online.target", "",
             "[Service]", "Type=simple",
             f"ExecStart={shlex.join([*_program(), 'serve', '--host', host, '--port', str(port)])}",
             *[f'Environment="{k}={v}"' for k, v in env.items()],
             "EnvironmentFile=-/etc/syntropy-health/env" if user else f"EnvironmentFile=-{data / '.env'}",
             "Restart=on-failure", "RestartSec=5"]
    if user:
        lines += [f"User={user}", *_systemd_hardening(data)]
    lines += ["", "[Install]", f"WantedBy={'multi-user.target' if user else 'default.target'}", ""]
    return "\n".join(lines)


def _launchd_plist(host: str, port: int) -> str:
    # The server writes its own log, server.log. launchd's files only catch what happens before that starts.
    early = str(config.data_dir() / "server-start.log")
    return plistlib.dumps({
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [*_program(), "serve", "--host", host, "--port", str(port)],
        "EnvironmentVariables": {"SYNTROPY_DATA_DIR": str(config.data_dir()), "SYNTROPY_LOG_TO_FILE": "1"},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": early,
        "StandardErrorPath": early,
    }).decode()


def _launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _is_root() -> bool:
    return os.geteuid() == 0 if hasattr(os, "geteuid") else False


def _user_unit() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{SERVICE}.service"


def _systemd_system() -> bool:
    """Root's commands go to the system service. So do anyone else's when it's the one there is (the Linux installer's)."""
    return _is_root() or (SYSTEM_UNIT.exists() and not _user_unit().exists())


def _systemctl(*args: str) -> list[str]:
    return ["systemctl", *args] if _systemd_system() else ["systemctl", "--user", *args]


def _unit_path() -> Path:
    return SYSTEM_UNIT if _systemd_system() else _user_unit()


def _installed_address() -> tuple[Optional[str], Optional[int]]:
    """The host and port the installed service was set up with, if it's installed."""
    text = ""
    try:
        if sys.platform == "darwin":
            text = " ".join(plistlib.loads(_launchd_path().read_bytes())["ProgramArguments"])
        elif sys.platform == "win32":
            query = _run(["schtasks", "/Query", "/TN", WINDOWS_TASK, "/XML"], check=False)
            text = query.stdout if query.returncode == 0 else _windows_run_entry(None) or ""
        else:
            text = _unit_path().read_text()
    except (OSError, ValueError, KeyError, TypeError, ExpatError):      # missing, or a file launchctl refused
        pass
    host, port = re.search(r"--host\s+([^\s<\"']+)", text), re.search(r"--port\s+(\d+)", text)
    return host.group(1) if host else None, int(port.group(1)) if port else None


def service_install(host: Optional[str], port: Optional[int], user: Optional[str]) -> None:
    """Installing again, as every run of the installers does, leaves the address alone unless a new one is asked for: the
    phones paired with this server have the port saved, and someone may have kept it to this computer on purpose."""
    kept_host, kept_port = _installed_address()
    host, port = host or kept_host or "0.0.0.0", port or kept_port or DEFAULT_PORT
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
        created = _windows_create_task(args)
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
        if _systemd_system() and not _is_root():
            sys.exit("The service is set up for the whole computer, so changing it takes root: "
                     "sudo syntropy-health service install")
        if _systemd_system() and not user:
            sys.exit("As root, say which account runs the server: syntropy-health service install --user syntropy")
        path = _unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if user and _systemd_system() and not _data_dir_chosen():
            _give_data_dir_to(user, _service_data_dir(user))
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
        domain = f"gui/{os.getuid()}"
        target = f"{domain}/{LAUNCHD_LABEL}"
        # Only unloading stops it for certain, with KeepAlive set. It loads again at the next login, as installed.
        if action == "stop":
            cmd = ["launchctl", "bootout", target]
        else:
            _run(["launchctl", "bootstrap", domain, str(_launchd_path())], check=False)
            cmd = ["launchctl", "kickstart", *(["-k"] if action == "restart" else []), target]
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

class InstallProblem(Exception):
    """Why the new version couldn't be installed, in words for the person."""


def update() -> None:
    from app.services import updates

    if getattr(sys, "frozen", False):
        sys.exit("The Mac and Windows apps install updates themselves: Settings → General → Updates, or Check for "
                 f"Updates in the app's menu. Or download the latest from {updates.RELEASES_URL}.")
    if config.env("SYNTROPY_INSTALL") == "docker":
        sys.exit("In Docker, update from outside the container: docker compose pull && docker compose up -d")
    if config.is_source_checkout():
        sys.exit("This is a clone of the repository: update it with git pull.")
    recorded = Path(sys.prefix) / "syntropy-source.txt"
    source = (recorded.read_text().strip() if recorded.exists() else "") or LATEST
    target = None       # a package chosen when installing: whichever version that is now
    if source == LATEST:
        try:
            release = updates.fetch_latest()
        except updates.CheckFailed as exc:
            sys.exit(f"Couldn't check for updates: {exc}")
        if release is None:
            sys.exit("No release of Syntropy Health has been published yet.")
        if not updates.is_newer(release["version"]):
            print(f"Syntropy Health {config.APP_VERSION} is the latest version.")
            return
        print(f"Updating Syntropy Health {config.APP_VERSION} to {release['version']}.")
        target = release["version"]
    previous = config.APP_VERSION
    started = time.time() - 2
    port = _last_port()
    print("Installing the latest version…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            constraints = None
            if target:
                checksums = updates.fetch_checksums(target)
                source = updates.package_source(release, checksums)
                constraints = updates.fetch_requirements(target, checksums, Path(tmp))
            _pip_install(source, constraints)
    except (updates.InstallFailed, InstallProblem) as exc:
        sys.exit(f"Couldn't install the new version, so version {previous} is still in place. {exc}")
    if _service_state() in ("not installed",):
        print("Updated. Your data is untouched; the database is upgraded when the server starts.")
        return
    service_control("restart")
    print("Waiting for the server to start on the new version…")
    problem = _wait_for_server(port, target, UPDATE_WAIT_SECONDS)
    if problem is None:
        updates.clear_failed_update()
        print("Updated, and the server is running. A copy of the database from before the upgrade is in "
              f"{config.db_path().parent / 'pre-upgrade'}.")
        return
    _update_failed(problem, target, previous, started, port)


def _uv() -> Optional[str]:
    """The uv the installer used: it isn't always on PATH (under sudo, from a timer). The venv it made has no pip."""
    try:
        recorded = (Path(sys.prefix) / "syntropy-uv.txt").read_text().strip()
    except OSError:
        recorded = ""
    return recorded if recorded and Path(recorded).is_file() else shutil.which("uv")


def _pip_install(source: str, constraints: Optional[Path] = None) -> None:
    """Installs ``source``, with exactly the dependencies in ``constraints`` when a release lists them."""
    from app.services import updates

    uv = _uv()
    try:
        if uv:
            pinned = ["-c", str(constraints)] if constraints else []
            cmd = [uv, "pip", "install", "--python", sys.executable, "--upgrade", *pinned, source]
        elif constraints:
            # pip won't take hashes as constraints: they make it want an exact version of everything, and the package only
            # asks for fastapi>=... So, as the Docker image does, one list of everything, all checked before any of it goes in.
            everything = constraints.with_name("everything.txt")
            everything.write_text(f"{constraints.read_text(encoding='utf-8')}\n{source}\n", encoding="utf-8")
            cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--require-hashes", "--no-deps", "-r", str(everything)]
        else:
            cmd = [sys.executable, "-m", "pip", "install", "--upgrade", source]
        subprocess.run(cmd, check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        missing = "" if uv else " uv wasn't found either: run the installer again, which puts it back."
        raise InstallProblem(f"Installing stopped with an error ({exc}).{missing} If this account can't change the installed "
                             "program, run the update with sudo.") from exc
    if Path(sys.prefix).resolve() == updates.LINUX_VENV.resolve():
        # Root's umask may be strict, and the service runs as another account: as the installer does.
        subprocess.run(["chmod", "-R", "a+rX", str(updates.LINUX_VENV.parent)], check=False)


def _last_port() -> int:
    """Where the service listens, whether or not it's running right now."""
    installed = _installed_address()[1]
    if installed:
        return installed
    try:
        return int(_pid_file().read_text().split()[1])
    except (OSError, ValueError, IndexError):
        return DEFAULT_PORT


def _wait_for_server(port: int, version: Optional[str], seconds: float) -> Optional[str]:
    """None once the server says it's healthy on ``version``, else why not. Upgrading a large database can take minutes,
    during which it doesn't answer at all. An answer of 503 is final: it's up, and has said it can't do its job."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + seconds
    reason = f"It didn't answer within {round(seconds / 60)} minutes."
    while time.monotonic() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                body = json.loads(r.read())
            if body.get("status") == "ok" and version in (None, body.get("version")):
                return None
            reason = f"It answered as version {body.get('version')} instead of {version}."
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read()).get("detail") or f"It answered {exc.code}."
            except ValueError:
                return f"It answered {exc.code}."
        except (OSError, ValueError):
            pass
        time.sleep(2)
    return reason


def _update_failed(problem: str, target: Optional[str], previous: str, started: float, port: int) -> None:
    """Automatic updates run overnight, with nobody watching: put back the version that worked, and the database as it
    was for it. What the new version left is kept. Only for the Linux service, which can be stopped for certain."""
    from app.core import db
    from app.services import updates

    print(f"The new version didn't start: {problem}")
    try:
        updates.note_failed_update(target, problem)
    except OSError as exc:      # a full disk, perhaps the very reason it didn't start: going back matters more
        print(f"Couldn't note that this release failed, so the nightly check may try it again: {exc}")
    if not target or not sys.platform.startswith("linux"):     # without a release to go back to, or a service to stop for certain
        copies = db.pre_upgrade_copies()
        where = f" A copy of the database from before the upgrade is at {copies[0]['path']}." if copies else ""
        sys.exit(f"Install version {previous} again from {updates.RELEASES_URL} to get back to where you were.{where}")
    print(f"Going back to version {previous}.")
    service_control("stop")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            checksums = updates.fetch_checksums(previous)
            _pip_install(updates.package_source_for(previous, checksums), updates.fetch_requirements(previous, checksums, Path(tmp)))
    except (InstallProblem, updates.InstallFailed, OSError):
        service_control("start")
        sys.exit(f"Couldn't reinstall version {previous}. Install it from {updates.RELEASES_URL}.")
    restored, stuck = None, None
    try:
        restored = db.restore_pre_upgrade(started)
    except OSError as exc:
        stuck = exc
    service_control("start")        # whatever happened: a server explaining the problem beats none at all
    again = _wait_for_server(port, previous, UPDATE_WAIT_SECONDS)
    if stuck:
        copies = db.pre_upgrade_copies()
        where = f" A copy from before the upgrade is at {copies[0]['path']}." if copies else ""
        said = f" The server says: {again}" if again else ""
        sys.exit(f"Version {previous} is back in place, but the database couldn't be put back as it was before the upgrade "
                 f"({stuck}), so that version may refuse to use it.{where} Free up some disk space and run "
                 f"`syntropy-health update` to go forward again.{said}")
    if again:
        sys.exit(f"Version {previous} is back in place, but the server didn't start either: {again}")
    note = f" The database was put back as it was before the upgrade, from {restored}." if restored else ""
    sys.exit(f"Version {previous} is running again.{note} Version {target} won't be installed automatically; "
             "`syntropy-health update` tries it again.")


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
    failed = updates.failed_update()
    if failed and failed.get("version") == release["version"]:
        print(f"Syntropy Health {release['version']} didn't start here, so it won't be tried again automatically. "
              "`syntropy-health update` tries it by hand.")
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


def new_key() -> None:
    """When the encryption key is lost for good. Only from this computer, and only by someone who says so: a new key
    can't open anything saved with the old one."""
    from app.core import db, security

    db.ensure_migrated()
    if config.env("SYNTROPY_SECRET_KEY"):
        sys.exit("The key comes from the SYNTROPY_SECRET_KEY setting: change it there.")
    print("This starts a new encryption key. Your records are safe, but passwords, sign-ins to health systems and AI keys "
          "saved with the old key stay unreadable: everyone sets a new password with `syntropy-health accounts "
          "reset-password NAME`, and sources and AI services need signing in to again.")
    print("Don't do this if you can still find the old secret.key: putting it back fixes everything.")
    try:
        answer = input("Type NEW to go ahead: ")
    except EOFError:
        sys.exit("There's no one here to type NEW, so nothing was changed. With Docker, run this with docker exec -it.")
    if answer.strip() != "NEW":
        sys.exit("Nothing was changed.")
    moved = security.start_new_key()
    print(f"Done. The old key file was kept as {moved}." if moved else "Done.")
    print("Restart Syntropy Health if it's running.")


def accounts_command(action: Optional[str], name: Optional[str]) -> None:
    """Who signs in, and a new password for someone who forgot theirs. Only from this computer: someone here can read
    the database anyway, so this adds no way in that wasn't already there."""
    import getpass

    from app.core import auth, context, db
    from app.store import accounts
    db.ensure_migrated()
    if action not in ("reset-password", "make-owner"):
        for a in accounts.list_accounts():
            state = "password set" if a["has_password"] else "no password"
            print(f"{a['name']:<24} {a['role']:<7} {state:<13}{a['id']}")
        if not accounts.any_accounts():
            print("No one signs in yet: open Syntropy Health to set it up.")
        return
    found = accounts.matching(name or "")
    if len(found) > 1:
        sys.exit(f"More than one person is named {name!r}. Use the id from `syntropy-health accounts` instead of the name.")
    if not found:
        sys.exit(f"No one named {name!r} signs in here. `syntropy-health accounts` lists who does.")
    account = found[0]
    if action == "make-owner":
        context.ACTING_FOR.set("cli")       # how the log names who did it
        accounts.set_role(account["id"], "owner")
        print(f"{account['name']} is now an owner of this server. Anyone else who was one still is: they can step down in "
              "Settings → People.")
        return
    password = getpass.getpass(f"New password for {account['name']}: ")
    if getpass.getpass("Again: ") != password:
        sys.exit("The passwords don't match.")
    try:
        auth.validate_password(password)
    except Exception as exc:  # noqa: BLE001 - HTTPException with a friendly message
        sys.exit(getattr(exc, "detail", str(exc)))
    accounts.set_password(account["id"], password)
    with db.db() as conn:
        db.audit(conn, "cli", "auth.password_reset", {"person": account["name"]}, account["profile_id"])
    print(f"{account['name']} can sign in with the new password. Their other browsers were signed out.")


# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> None:
    import sqlite3

    from app.core.db import StartupProblem

    try:
        _command(argv)
    except StartupProblem as exc:
        sys.exit(str(exc))
    except sqlite3.Error as exc:
        sys.exit(f"Syntropy Health couldn't use its database in {config.data_dir()}: {exc}")


def _command(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="syntropy-health", description="Syntropy Health: your health records, on your own computer.")
    parser.add_argument("--data-dir", help="where the data lives (default: the usual place on this computer, which "
                                           "`syntropy-health info` shows)", default=None)
    parser.add_argument("--service-account", help=argparse.SUPPRESS, default=None)     # the Linux installer's command, as root
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("serve", help="run the server in the foreground")
    p.add_argument("--host", default="0.0.0.0", help="0.0.0.0 lets other devices connect; 127.0.0.1 keeps it to this computer")
    p.add_argument("--port", type=int, default=int(config.env("PORT") or DEFAULT_PORT))
    p.add_argument("--reload", action="store_true", help=argparse.SUPPRESS)
    s = sub.add_parser("service", help="start with the computer (systemd, launchd or a Windows sign-in task)")
    s.add_argument("action", choices=["install", "uninstall", "status", "start", "stop", "restart"])
    s.add_argument("--host", help="install: 0.0.0.0 lets other devices connect, 127.0.0.1 keeps it to this computer "
                                  "(default: what's already set, or 0.0.0.0)")
    s.add_argument("--port", type=int, help=f"install: default: what's already set, or {DEFAULT_PORT}")
    s.add_argument("--user", help="Linux, as root: the account that runs the server")
    b = sub.add_parser("backup", help="everything in one encrypted file: the database, encryption key and uploads")
    b.add_argument("folder", nargs="?", help="where to put it (default: this folder). A drive of its own survives a failed disk")
    b.add_argument("--passphrase-file", help="read the passphrase from this file (- for standard input), for scripts")
    b.add_argument("--no-encrypt", action="store_true", help="a plain zip. Anyone who gets the file can read every record and "
                                                             "use every saved sign-in")
    r = sub.add_parser("restore", help="put a backup back, keeping what it replaces. Stop the server first")
    r.add_argument("file")
    r.add_argument("--check", action="store_true", help="only test the file and say what's in it; change nothing")
    r.add_argument("--passphrase-file", help="read the passphrase from this file (- for standard input), for scripts")
    r.add_argument("--key-file", help="the secret.key that goes with an older backup that holds only the database")
    r.add_argument("--key-only", action="store_true", help="take only the encryption key from the backup, for a key that was lost")
    r.add_argument("--without-key", action="store_true", help="restore even though passwords and saved sign-ins won't open")
    r.add_argument("--yes", action="store_true", help="don't ask before replacing what's here")
    u = sub.add_parser("update", help="install the latest release and restart the service")
    u.add_argument("--due", action="store_true", help=argparse.SUPPRESS)
    i = sub.add_parser("info", help="where the data is and which addresses to open")
    i.add_argument("--port", type=int, default=None, help="default: the running server's")
    m = sub.add_parser("mcp", help="the MCP server on stdio, for AI apps on this computer")
    m.add_argument("--profile", help="read only this person's records (a name or id); without it, everyone's")
    sub.add_parser("version", help="print the version")
    a = sub.add_parser("accounts", help="who signs in; reset-password NAME for someone locked out; make-owner NAME")
    a.add_argument("action", nargs="?", choices=["list", "reset-password", "make-owner"], default="list")
    a.add_argument("name", nargs="?")
    k = sub.add_parser("key", help="key new: replace a lost encryption key (saved passwords and sign-ins stay unreadable)")
    k.add_argument("action", choices=["new"])
    d = sub.add_parser("dev", help="development only: dev no-password on|off lets this instance run without a password")
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
    elif args.command in ("backup", "restore") and args.service_account and _systemd_system():
        _as_service_account(args.service_account, args)
    elif args.command == "backup":
        backup(args.folder, no_encrypt=args.no_encrypt, passphrase_file=args.passphrase_file)
    elif args.command == "restore":
        restore(args.file, check_only=args.check, key_only=args.key_only, yes=args.yes, without_key=args.without_key,
                passphrase_file=args.passphrase_file, key_file=args.key_file)
    elif args.command == "update":
        update_due() if args.due else update()
    elif args.command == "info":
        info(args.port)
    elif args.command == "mcp":
        from app import mcp_server
        mcp_server.main(args.profile)
    elif args.command == "version":
        print(config.APP_VERSION)
    elif args.command == "accounts":
        accounts_command(args.action, args.name)
    elif args.command == "key":
        new_key()
    elif args.command == "dev":
        dev_no_password(args.state)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
