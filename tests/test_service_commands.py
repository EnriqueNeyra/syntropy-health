"""`syntropy-health service`: what each computer's service manager is asked to do, and what a re-install keeps. launchd,
systemd and Windows' Task Scheduler are stood in for; CI's install jobs run them for real."""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import plistlib
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from app import cli
from app.core import config
from app.services import updates

TASK = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


@pytest.fixture
def commands(monkeypatch):
    """Records what would be run; each answer is a successful one unless a test says otherwise."""
    ran: list[list[str]] = []
    answers: dict[str, SimpleNamespace] = {}

    def run(cmd, check=True, env=None):
        ran.append([str(c) for c in cmd])
        for key, answer in answers.items():
            if key in " ".join(str(c) for c in cmd):
                return answer
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(cli, "_run", run)
    ran_answers = SimpleNamespace(ran=ran, answers=answers)
    return ran_answers


@pytest.fixture
def linux(monkeypatch, tmp_path):
    """A Linux computer with systemd, whose system and user units live under tmp_path, as a person who isn't root."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "systemctl" else None)
    monkeypatch.setattr(cli, "SYSTEM_UNIT", tmp_path / "etc" / "syntropy-health.service")
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)
    (tmp_path / "etc").mkdir()
    return tmp_path


def _root(monkeypatch):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0, raising=False)


# ---------------------------------------------------------------------------
# Windows: a task that keeps running
# ---------------------------------------------------------------------------

def _task(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def test_the_windows_task_runs_for_as_long_as_the_computer_does(monkeypatch):
    monkeypatch.setenv("USERDOMAIN", "HOME")
    monkeypatch.setenv("USERNAME", "sam & co")
    task = _task(cli._windows_task_xml(["C:\\Program Files\\S&H\\pythonw.exe", "-m", "app", "serve", "--port", "8010"]))
    settings = task.find(f"{TASK}Settings")
    assert settings.findtext(f"{TASK}ExecutionTimeLimit") == "PT0S"          # the default stops it after 72 hours
    assert settings.findtext(f"{TASK}DisallowStartIfOnBatteries") == "false"
    assert settings.findtext(f"{TASK}StopIfGoingOnBatteries") == "false"
    restart = settings.find(f"{TASK}RestartOnFailure")
    assert restart.findtext(f"{TASK}Interval") == "PT1M" and int(restart.findtext(f"{TASK}Count")) > 0
    action = task.find(f"{TASK}Actions/{TASK}Exec")
    assert action.findtext(f"{TASK}Command") == "C:\\Program Files\\S&H\\pythonw.exe"
    assert action.findtext(f"{TASK}Arguments") == "-m app serve --port 8010"
    assert task.find(f"{TASK}Triggers/{TASK}LogonTrigger/{TASK}UserId").text == "HOME\\sam & co"
    assert task.find(f"{TASK}Principals/{TASK}Principal/{TASK}RunLevel").text == "LeastPrivilege"


def test_the_windows_task_is_made_from_that_definition(monkeypatch, commands, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cli, "_installed_address", lambda: (None, None))
    seen = {}

    def run(cmd, check=True, env=None):
        commands.ran.append(cmd)
        if "/XML" in cmd:
            seen["xml"] = Path(cmd[cmd.index("/XML") + 1]).read_bytes()
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(cli, "_run", run)
    cli.service_install(None, None, None)
    assert seen["xml"].decode("utf-16").lstrip().startswith('<?xml version="1.0" encoding="UTF-16"?>')
    assert ["schtasks", "/Run", "/TN", cli.WINDOWS_TASK] in commands.ran
    assert "sign in to Windows" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Installing again keeps the address it listens on
# ---------------------------------------------------------------------------

def test_reinstalling_the_linux_service_keeps_its_port_and_address(linux, monkeypatch, commands, capsys):
    _root(monkeypatch)
    cli.service_install("127.0.0.1", 9000, "syntropy")
    assert "--host 127.0.0.1 --port 9000" in cli.SYSTEM_UNIT.read_text()
    cli.service_install(None, None, "syntropy")
    assert "--host 127.0.0.1 --port 9000" in cli.SYSTEM_UNIT.read_text()
    assert capsys.readouterr().out.rstrip().endswith("http://localhost:9000")
    cli.service_install(None, 9100, "syntropy")      # what's asked for changes only that
    assert "--host 127.0.0.1 --port 9100" in cli.SYSTEM_UNIT.read_text()


def test_a_first_install_listens_where_it_always_did(linux, monkeypatch, commands):
    _root(monkeypatch)
    cli.service_install(None, None, "syntropy")
    assert f"--host 0.0.0.0 --port {cli.DEFAULT_PORT}" in cli.SYSTEM_UNIT.read_text()


def test_reinstalling_the_mac_service_keeps_its_port_and_address(monkeypatch, tmp_path, commands):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(cli, "_launchd_path", lambda: tmp_path / "agent.plist")
    cli.service_install("127.0.0.1", 9000, None)
    cli.service_install(None, None, None)
    args = plistlib.loads((tmp_path / "agent.plist").read_bytes())["ProgramArguments"]
    assert args[-4:] == ["--host", "127.0.0.1", "--port", "9000"]


# What earlier versions left behind: a plist launchctl refused, written before it did, from a folder with an ampersand.
MALFORMED_PLIST = b"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>io.syntropyhealth.server</string>
  <key>ProgramArguments</key><array><string>python</string><string>--host</string><string>0.0.0.0</string></array>
  <key>EnvironmentVariables</key><dict><key>SYNTROPY_DATA_DIR</key><string>/Users/me/A&B</string></dict>
</dict></plist>
"""


@pytest.mark.parametrize("left_behind", [MALFORMED_PLIST, b"", b"not a plist", plistlib.dumps(["a list"]),
                                         plistlib.dumps({"ProgramArguments": [1, 2]})])
def test_a_broken_plist_counts_as_no_address(monkeypatch, tmp_path, left_behind):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli, "_launchd_path", lambda: tmp_path / "agent.plist")
    (tmp_path / "agent.plist").write_bytes(left_behind)
    assert cli._installed_address() == (None, None)


def test_installing_again_replaces_the_plist_an_ampersand_broke(monkeypatch, tmp_path, commands):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(cli, "_launchd_path", lambda: tmp_path / "agent.plist")
    (tmp_path / "agent.plist").write_bytes(MALFORMED_PLIST)
    cli.service_install(None, None, None)
    args = plistlib.loads((tmp_path / "agent.plist").read_bytes())["ProgramArguments"]
    assert args[-4:] == ["--host", "0.0.0.0", "--port", str(cli.DEFAULT_PORT)]
    assert ["launchctl", "bootstrap", "gui/501", str(tmp_path / "agent.plist")] in commands.ran


def test_updating_gets_past_a_plist_an_ampersand_broke(monkeypatch, tmp_path, isolated_data):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli, "_launchd_path", lambda: tmp_path / "agent.plist")
    (tmp_path / "agent.plist").write_bytes(MALFORMED_PLIST)
    assert cli._last_port() == cli.DEFAULT_PORT


def test_reinstalling_the_windows_task_keeps_its_port_and_address(monkeypatch, commands):
    monkeypatch.setattr(sys, "platform", "win32")
    xml = cli._windows_task_xml(["pythonw.exe", "-m", "app", "serve", "--host", "127.0.0.1", "--port", "9000"])
    commands.answers["/Query"] = SimpleNamespace(returncode=0, stdout=xml, stderr="")
    assert cli._installed_address() == ("127.0.0.1", 9000)


def test_the_windows_startup_entry_is_read_when_there_is_no_task(monkeypatch, commands):
    monkeypatch.setattr(sys, "platform", "win32")
    commands.answers["/Query"] = SimpleNamespace(returncode=1, stdout="", stderr="no such task")
    monkeypatch.setattr(cli, "_windows_run_entry", lambda command: '"C:\\a b\\pythonw.exe" -m app serve --host 0.0.0.0 --port 8010')
    assert cli._installed_address() == ("0.0.0.0", 8010)


def test_nothing_installed_has_no_address(linux):
    assert cli._installed_address() == (None, None)


def test_the_update_waits_on_the_port_the_service_uses_even_when_it_is_stopped(linux, isolated_data):
    cli.SYSTEM_UNIT.write_text("[Service]\nExecStart=/opt/python -m app serve --host 0.0.0.0 --port 9123\n")
    assert cli._last_port() == 9123


# ---------------------------------------------------------------------------
# Stopping on a Mac
# ---------------------------------------------------------------------------

def test_stopping_on_a_mac_unloads_the_job_so_launchd_does_not_bring_it_back(monkeypatch, tmp_path, commands):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cli.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(cli, "_launchd_path", lambda: tmp_path / "agent.plist")
    cli.service_control("stop")
    assert commands.ran == [["launchctl", "bootout", f"gui/501/{cli.LAUNCHD_LABEL}"]]
    commands.ran.clear()
    cli.service_control("start")
    assert commands.ran == [["launchctl", "bootstrap", "gui/501", str(tmp_path / "agent.plist")],
                            ["launchctl", "kickstart", f"gui/501/{cli.LAUNCHD_LABEL}"]]
    commands.ran.clear()
    cli.service_control("restart")
    assert commands.ran[-1] == ["launchctl", "kickstart", "-k", f"gui/501/{cli.LAUNCHD_LABEL}"]


@pytest.mark.skipif(sys.platform == "win32", reason="signals")
def test_a_terminated_server_exits_cleanly_and_leaves_no_pid_file(isolated_data, monkeypatch):
    """launchd and systemd restart a server that died from a signal; uvicorn passes SIGTERM on once it has shut down."""
    def uvicorn_run(*_a, **_k):
        assert cli._pid_file().exists()
        signal.raise_signal(signal.SIGTERM)
    monkeypatch.setattr("uvicorn.run", uvicorn_run)
    before = signal.getsignal(signal.SIGTERM)
    try:
        with pytest.raises(SystemExit) as caught:
            cli.serve("127.0.0.1", 8123)
    finally:
        signal.signal(signal.SIGTERM, before)
    assert caught.value.code == 0
    assert not cli._pid_file().exists()


def test_the_pid_file_goes_when_the_server_stops_by_itself(isolated_data, monkeypatch):
    monkeypatch.setattr("uvicorn.run", lambda *_a, **_k: None)
    before = signal.getsignal(signal.SIGTERM)
    try:
        cli.serve("127.0.0.1", 8123)
    finally:
        signal.signal(signal.SIGTERM, before)
    assert not cli._pid_file().exists()


# ---------------------------------------------------------------------------
# Status, and a service for another account
# ---------------------------------------------------------------------------

def test_status_sees_the_system_service_from_any_account(linux, commands):
    cli.SYSTEM_UNIT.write_text("[Service]\n")
    commands.answers["is-active"] = SimpleNamespace(returncode=0, stdout="active\n", stderr="")
    assert cli._service_state() == "active"
    assert commands.ran == [["systemctl", "is-active", cli.SERVICE]]


def test_someone_with_their_own_service_still_gets_theirs(linux, commands):
    cli.SYSTEM_UNIT.write_text("[Service]\n")
    unit = linux / "home" / ".config" / "systemd" / "user" / f"{cli.SERVICE}.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n")
    commands.answers["is-active"] = SimpleNamespace(returncode=3, stdout="inactive\n", stderr="")
    assert cli._service_state() == "inactive"
    assert commands.ran == [["systemctl", "--user", "is-active", cli.SERVICE]]


def test_no_service_anywhere(linux, commands):
    assert cli._service_state() == "not installed"


def test_replacing_the_system_service_takes_root(linux):
    cli.SYSTEM_UNIT.write_text("[Service]\n")
    with pytest.raises(SystemExit) as caught:
        cli.service_install(None, None, None)
    assert "sudo" in str(caught.value.code)


def _account(monkeypatch, home: Path, uid: int = 1234):
    pwd = pytest.importorskip("pwd")
    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(pw_dir=str(home), pw_uid=uid, pw_gid=uid))


def test_a_service_for_another_account_uses_that_accounts_data_folder(linux, monkeypatch, commands):
    """Not root's home, which the account can't read."""
    _root(monkeypatch)
    monkeypatch.delenv("SYNTROPY_DATA_DIR")
    monkeypatch.setattr(config, "is_source_checkout", lambda: False)
    home = linux / "alice"
    home.mkdir()
    _account(monkeypatch, home, uid=home.stat().st_uid)
    chowned = []
    monkeypatch.setattr(cli.os, "chown", lambda p, uid, gid: chowned.append(str(p)), raising=False)
    cli.service_install(None, None, "alice")
    data = home / ".local" / "share" / "syntropy-health"
    assert f'Environment="SYNTROPY_DATA_DIR={data}"' in cli.SYSTEM_UNIT.read_text()
    assert data.is_dir()
    assert {str(data), str(home / ".local"), str(home / ".local" / "share")} <= set(chowned)
    assert "root" not in str(data).split(os.sep)


def test_an_account_without_a_home_of_its_own_uses_the_shared_folder(linux, monkeypatch):
    _root(monkeypatch)
    monkeypatch.delenv("SYNTROPY_DATA_DIR")
    _account(monkeypatch, linux / "nowhere")
    assert cli._service_data_dir("syntropy") == Path("/var/lib/syntropy-health")


def test_a_data_folder_someone_chose_is_kept(linux, monkeypatch, isolated_data):
    _root(monkeypatch)
    assert cli._service_data_dir("alice") == isolated_data


def test_a_service_for_an_account_that_does_not_exist_says_so(linux, monkeypatch):
    pwd = pytest.importorskip("pwd")
    _root(monkeypatch)
    monkeypatch.delenv("SYNTROPY_DATA_DIR")

    def missing(_name):
        raise KeyError(_name)
    monkeypatch.setattr(pwd, "getpwnam", missing)
    with pytest.raises(SystemExit) as caught:
        cli.service_install(None, None, "nobody-here")
    assert "nobody-here" in str(caught.value.code)


# ---------------------------------------------------------------------------
# The service definitions
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="a folder name can't hold < there, and launchd is the Mac's")
def test_the_plist_survives_a_data_folder_with_an_ampersand(monkeypatch, tmp_path):
    folder = tmp_path / "A&B <mine>"
    monkeypatch.setenv("SYNTROPY_DATA_DIR", str(folder))
    plist = plistlib.loads(cli._launchd_plist("0.0.0.0", 8000).encode())
    assert plist["EnvironmentVariables"]["SYNTROPY_DATA_DIR"] == str(folder)
    assert plist["KeepAlive"] == {"SuccessfulExit": False} and plist["RunAtLoad"] is True
    assert plist["Label"] == cli.LAUNCHD_LABEL
    if sys.platform == "darwin":
        path = tmp_path / "check.plist"
        path.write_text(cli._launchd_plist("0.0.0.0", 8000))
        assert subprocess.run(["plutil", "-lint", str(path)], capture_output=True).returncode == 0


def test_the_server_keeps_its_own_log_under_launchd(isolated_data):
    plist = plistlib.loads(cli._launchd_plist("0.0.0.0", 8000).encode())
    assert plist["EnvironmentVariables"]["SYNTROPY_LOG_TO_FILE"] == "1"
    assert Path(plist["StandardOutPath"]).name != "server.log"     # launchd would grow that one for ever


def test_the_log_file_is_rotated(isolated_data, monkeypatch):
    monkeypatch.setattr(cli, "LOG_BYTES", 1000)
    log = cli._RotatingLog(isolated_data / "server.log")
    for i in range(400):
        log.write(f"line {i} of the log 100%\n")
    log.close()
    logs = sorted(p.name for p in isolated_data.glob("server.log*"))
    assert logs[0] == "server.log" and len(logs) == 1 + cli.LOG_KEEP
    assert (isolated_data / "server.log").stat().st_size <= 1000
    assert "line 399 of the log 100%\n" in (isolated_data / "server.log").read_text()


class _NoRoom(io.StringIO):
    def write(self, _text):
        raise OSError(errno.ENOSPC, "No space left on device")


@pytest.fixture
def log(isolated_data):
    opened = cli._RotatingLog(isolated_data / "server.log")
    yield opened
    opened.close()


# Under serve() sys.stderr is the log itself, so anything sent there about a failed write fails again, without end. Here
# it's captured, so that being sent there at all is what fails: the real recursion is in test_a_full_disk_does_not_stop_the_server.
def test_a_log_that_cannot_be_written_stays_quiet_about_it(log, monkeypatch, capsys):
    log.write("opens the file\n")
    log._handler.stream.close()
    monkeypatch.setattr(log._handler, "stream", _NoRoom())
    assert log.write("a request\n") == len("a request\n")
    assert capsys.readouterr().err == ""


def test_the_log_carries_on_once_there_is_room_again(log, isolated_data, capsys):
    log.write("before\n")
    log._handler.stream.close()      # how a flush that fails while rotating leaves it
    log.write("while it was full\n")
    log.write("after\n")
    written = (isolated_data / "server.log").read_text()
    assert "before\n" in written and "after\n" in written and capsys.readouterr().err == ""


def test_a_log_that_cannot_be_opened_does_not_stop_the_server(isolated_data, capsys):
    (isolated_data / "server.log").mkdir()
    opened = cli._RotatingLog(isolated_data / "server.log")
    assert opened.write("a request\n") == len("a request\n")
    opened.close()
    assert capsys.readouterr().err == ""


def test_text_that_cannot_be_encoded_is_still_logged(log, isolated_data, capsys):
    log.write("a file named \udc80\n")
    assert "a file named \\udc80\n" in (isolated_data / "server.log").read_text() and capsys.readouterr().err == ""


@pytest.mark.skipif(sys.platform == "win32", reason="a limit on file size stands in for a full disk")
def test_a_full_disk_does_not_stop_the_server(isolated_data):
    """The real thing, with the operating system refusing the writes."""
    program = f"""
import logging, resource, sys
from pathlib import Path
from app import cli
real = sys.stdout
resource.setrlimit(resource.RLIMIT_FSIZE, (2000, 2000))
sys.stdout = sys.stderr = cli._RotatingLog(Path({str(isolated_data / "server.log")!r}))
logging.basicConfig()
for i in range(2000):
    print("a line of the log, over and over")
    logging.getLogger("uvicorn.access").warning("a request")
real.write("still here")
"""
    done = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=60,
                          cwd=Path(cli.__file__).parent.parent)
    assert (done.returncode, done.stdout) == (0, "still here"), done.stderr
    assert (isolated_data / "server.log").stat().st_size <= 2000


def test_a_full_disk_does_not_stop_the_server_starting(isolated_data, monkeypatch, capsys):
    monkeypatch.setattr("uvicorn.run", lambda *_a, **_k: print("from uvicorn"))
    before = signal.getsignal(signal.SIGTERM)

    def no_room(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(Path, "write_text", no_room)
    try:
        cli.serve("127.0.0.1", 8123)
    finally:
        signal.signal(signal.SIGTERM, before)
    assert "from uvicorn" in capsys.readouterr().out


def test_serve_with_no_console_writes_to_the_rotating_log(isolated_data, monkeypatch):
    monkeypatch.setattr("uvicorn.run", lambda *_a, **_k: print("from uvicorn"))
    monkeypatch.setattr(sys, "stdout", None)
    stderr = sys.stderr
    before = signal.getsignal(signal.SIGTERM)
    try:
        cli.serve("127.0.0.1", 8123)
        sys.stdout.flush()
    finally:
        sys.stderr = stderr
        signal.signal(signal.SIGTERM, before)
    text = (isolated_data / "server.log").read_text()
    assert "from uvicorn" in text and f"Syntropy Health {config.APP_VERSION}" in text


def test_the_system_service_gives_the_server_only_what_it_needs(isolated_data):
    unit = cli._systemd_unit("0.0.0.0", 8000, "syntropy")
    for line in ("ProtectSystem=strict", f"ReadWritePaths={isolated_data}", "PrivateTmp=true", "NoNewPrivileges=true",
                 "CapabilityBoundingSet=", "ProtectKernelTunables=true", "ProtectKernelModules=true", "ProtectKernelLogs=true",
                 "ProtectControlGroups=true", "RestrictNamespaces=true", "LockPersonality=true", "RestrictSUIDSGID=true",
                 "PrivateDevices=true"):
        assert line in unit.splitlines()
    families = next(line for line in unit.splitlines() if line.startswith("RestrictAddressFamilies="))
    assert {"AF_UNIX", "AF_INET", "AF_INET6", "AF_NETLINK"} == set(families.split("=")[1].split())


@pytest.mark.skipif(sys.platform == "win32", reason="Linux paths, which Windows turns into its own")
def test_home_folders_are_hidden_unless_the_server_lives_in_one(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNTROPY_DATA_DIR", "/var/lib/syntropy-health")
    monkeypatch.setattr(cli, "_program", lambda: ["/opt/syntropy-health/venv/bin/python", "-m", "app"])
    monkeypatch.setattr(config.Path, "mkdir", lambda *_a, **_k: None)
    assert "ProtectHome=true" in cli._systemd_unit("0.0.0.0", 8000, "syntropy").splitlines()
    monkeypatch.setenv("SYNTROPY_DATA_DIR", "/home/alice/.local/share/syntropy-health")
    unit = cli._systemd_unit("0.0.0.0", 8000, "alice").splitlines()
    assert "ProtectHome=read-only" in unit and "ReadWritePaths=/home/alice/.local/share/syntropy-health" in unit
    monkeypatch.setenv("SYNTROPY_DATA_DIR", "/var/lib/syntropy-health")
    monkeypatch.setattr(cli, "_program", lambda: ["/home/alice/venv/bin/python", "-m", "app"])
    assert "ProtectHome=read-only" in cli._systemd_unit("0.0.0.0", 8000, "alice").splitlines()


def test_a_data_folder_with_a_space_is_quoted_for_systemd(monkeypatch, tmp_path):
    folder = tmp_path / "my data"
    monkeypatch.setenv("SYNTROPY_DATA_DIR", str(folder))
    assert f'ReadWritePaths="{folder}"' in cli._systemd_unit("0.0.0.0", 8000, "syntropy").splitlines()


# ---------------------------------------------------------------------------
# Windows: the PID in a stale file may be anyone's by now
# ---------------------------------------------------------------------------

def _pid_file_says(pid: int) -> None:
    cli._pid_file().write_text(f"{pid} 8010")


def test_windows_stop_leaves_a_process_that_is_not_the_server(monkeypatch, commands, isolated_data):
    monkeypatch.setattr(cli, "_windows_command_line", lambda pid: '"C:\\Games\\game.exe" --full-screen')
    _pid_file_says(4242)
    cli._windows_stop()
    assert commands.ran == []
    assert not cli._pid_file().exists()


def test_windows_stop_leaves_a_process_it_cannot_identify(monkeypatch, commands, isolated_data):
    monkeypatch.setattr(cli, "_windows_command_line", lambda pid: "")
    _pid_file_says(4242)
    cli._windows_stop()
    assert commands.ran == []


def test_windows_stop_ends_the_server(monkeypatch, commands, isolated_data):
    monkeypatch.setattr(cli, "_windows_command_line", lambda pid: '"C:\\venv\\pythonw.exe" -m app serve --host 0.0.0.0 --port 8010')
    _pid_file_says(4242)
    cli._windows_stop()
    assert commands.ran == [["taskkill", "/F", "/PID", "4242"]]


def test_the_windows_command_line_comes_from_the_system(monkeypatch, commands):
    commands.answers["powershell"] = SimpleNamespace(returncode=0, stdout='"C:\\venv\\python.exe" -m app serve\n', stderr="")
    assert "serve" in cli._windows_command_line(4242)
    assert "ProcessId=4242" in " ".join(commands.ran[0])


def test_info_does_not_report_a_stale_port_on_windows(monkeypatch, isolated_data):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cli, "_windows_command_line", lambda pid: "notepad.exe")
    _pid_file_says(4242)
    assert cli._running_port() is None


# ---------------------------------------------------------------------------
# update: the install itself
# ---------------------------------------------------------------------------

@pytest.fixture
def installing(monkeypatch, tmp_path):
    prefix = tmp_path / "venv"
    prefix.mkdir()
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "executable", str(prefix / "bin" / "python"))
    ran: list[list[str]] = []

    def run(cmd, check=False, **_k):
        ran.append([str(c) for c in cmd])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    return SimpleNamespace(prefix=prefix, ran=ran, root=tmp_path)


def test_update_finds_the_uv_the_installer_recorded(installing):
    uv = installing.root / "uv"
    uv.write_text("#!/bin/sh\n")
    uv.chmod(0o755)
    (installing.prefix / "syntropy-uv.txt").write_text(f"{uv}\n")
    cli._pip_install("syntropy-health @ https://example.test/w.whl#sha256=abc", installing.root / "locked.txt")
    assert installing.ran == [[str(uv), "pip", "install", "--python", sys.executable, "--upgrade", "-c",
                               str(installing.root / "locked.txt"), "syntropy-health @ https://example.test/w.whl#sha256=abc"]]


def _wheel(folder: Path, name: str, version: str, needs: tuple[str, ...] = ()) -> Path:
    path = folder / f"{name}-{version}-py3-none-any.whl"
    info = f"{name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" + "".join(f"Requires-Dist: {n}\n" for n in needs)
    with zipfile.ZipFile(path, "w") as whl:
        whl.writestr(f"{name}.py", f"VERSION = {version!r}\n")
        whl.writestr(f"{info}/METADATA", metadata)
        whl.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        whl.writestr(f"{info}/RECORD", "")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def venv_with_pip(tmp_path_factory) -> Path:
    pytest.importorskip("ensurepip")
    folder = tmp_path_factory.mktemp("pip-install") / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(folder)], check=True, capture_output=True)
    return folder / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


@pytest.fixture
def pip_install(venv_with_pip, monkeypatch, tmp_path):
    """Real pip with no uv around, as for someone who ran `pip install`, finding packages in a folder instead of PyPI."""
    packages = tmp_path / "packages"
    packages.mkdir()
    monkeypatch.setattr(sys, "executable", str(venv_with_pip))
    monkeypatch.setattr(cli, "_uv", lambda: None)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(packages))
    monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "1")

    def installed() -> dict[str, str]:
        shown = subprocess.run([str(venv_with_pip), "-m", "pip", "list", "--format=json"], check=True, capture_output=True, text=True)
        return {p["name"]: p["version"] for p in json.loads(shown.stdout)}
    yield SimpleNamespace(packages=packages, installed=installed)
    subprocess.run([str(venv_with_pip), "-m", "pip", "uninstall", "-y", "syntropy_health", "lockeddep"], check=True, capture_output=True)


def _release(pip_install, version: str, dep_version: str, dep_digest: Optional[str] = None) -> tuple[str, Path]:
    """A release as desktop.yml publishes one: its wheel, which asks loosely, and every dependency locked with hashes."""
    dep = _wheel(pip_install.packages, "lockeddep", dep_version)
    wheel = _wheel(pip_install.packages.parent, "syntropy_health", version, ("lockeddep>=0.1",))
    lock = pip_install.packages.parent / f"lock-{version}" / updates.REQUIREMENTS_ASSET
    lock.parent.mkdir()
    lock.write_text(f"lockeddep=={dep_version} \\\n    --hash=sha256:{dep_digest or _sha256(dep)}\n")
    return f"syntropy-health @ {wheel.as_uri()}#sha256={_sha256(wheel)}", lock


def test_pip_installs_a_release_with_its_locked_dependencies(pip_install):
    cli._pip_install(*_release(pip_install, "99.0.0", "1.5"))
    assert pip_install.installed()["syntropy_health"] == "99.0.0" and pip_install.installed()["lockeddep"] == "1.5"
    # Going back uses the same call, and takes the older dependency with it.
    cli._pip_install(*_release(pip_install, "98.0.0", "1.4"))
    assert pip_install.installed()["syntropy_health"] == "98.0.0" and pip_install.installed()["lockeddep"] == "1.4"


def test_pip_refuses_a_dependency_that_does_not_match_the_list(pip_install):
    source, lock = _release(pip_install, "99.0.0", "1.5", dep_digest="0" * 64)
    with pytest.raises(cli.InstallProblem):
        cli._pip_install(source, lock)
    assert "syntropy_health" not in pip_install.installed()


def test_pip_refuses_a_wheel_that_does_not_match_its_checksum(pip_install):
    source, lock = _release(pip_install, "99.0.0", "1.5")
    with pytest.raises(cli.InstallProblem):
        cli._pip_install(f"{source.split('#')[0]}#sha256={'0' * 64}", lock)
    assert "syntropy_health" not in pip_install.installed() and "lockeddep" not in pip_install.installed()


def test_pip_still_installs_a_release_from_before_lock_files(pip_install):
    wheel = _wheel(pip_install.packages.parent, "syntropy_health", "97.0.0")
    cli._pip_install(f"syntropy-health @ {wheel.as_uri()}#sha256={_sha256(wheel)}")
    assert pip_install.installed()["syntropy_health"] == "97.0.0"


def test_update_without_uv_or_pip_says_what_to_do(installing, monkeypatch):
    def no_pip(cmd, check=False, **_k):
        raise subprocess.CalledProcessError(1, cmd)
    monkeypatch.setattr(cli.subprocess, "run", no_pip)
    with pytest.raises(cli.InstallProblem) as caught:
        cli._pip_install("anything")
    assert "uv" in str(caught.value) and "Traceback" not in str(caught.value)


def test_a_failed_install_leaves_the_service_alone_and_says_so(monkeypatch, tmp_path):
    actions: list[str] = []
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(config, "is_source_checkout", lambda: False)
    monkeypatch.setattr(updates, "fetch_latest", lambda: {"version": "99.0.0", "assets": {
        "syntropy_health-99.0.0-py3-none-any.whl": "https://example.test/new.whl"}})
    monkeypatch.setattr(updates, "fetch_checksums", lambda version: {"syntropy_health-99.0.0-py3-none-any.whl": "a" * 64})
    monkeypatch.setattr(cli, "service_control", actions.append)

    def fails(_source, _constraints=None):
        raise cli.InstallProblem("uv stopped with an error.")
    monkeypatch.setattr(cli, "_pip_install", fails)
    with pytest.raises(SystemExit) as caught:
        cli.update()
    assert actions == [] and "still in place" in str(caught.value.code) and "uv stopped" in str(caught.value.code)


def test_everyone_can_read_what_a_linux_update_installed(installing, monkeypatch):
    """Root may have a strict umask; the service runs as another account."""
    monkeypatch.setattr(updates, "LINUX_VENV", installing.prefix)
    (installing.prefix / "syntropy-uv.txt").write_text(f"{sys.executable}\n")
    monkeypatch.setattr(cli.Path, "is_file", lambda self: True)
    cli._pip_install("x")
    assert installing.ran[-1] == ["chmod", "-R", "a+rX", str(installing.prefix.parent)]


def test_other_installs_are_left_as_they_are(installing, monkeypatch):
    monkeypatch.setattr(updates, "LINUX_VENV", installing.root / "elsewhere")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    cli._pip_install("x")
    assert all(cmd[0] != "chmod" for cmd in installing.ran)


def test_inside_docker_update_gives_the_docker_command(monkeypatch):
    monkeypatch.setenv("SYNTROPY_INSTALL", "docker")
    monkeypatch.setattr(config, "is_source_checkout", lambda: False)
    with pytest.raises(SystemExit) as caught:
        cli.update()
    assert "docker compose pull" in str(caught.value.code)


# ---------------------------------------------------------------------------
# update: what it downloads is what the release says it published
# ---------------------------------------------------------------------------

SUMS = f"{'a' * 64}  syntropy_health-99.0.0-py3-none-any.whl\n{'b' * 64}  SHA256SUMS.notes\n"


def test_checksums_are_read_from_the_releases_file(monkeypatch):
    urls = []

    def fetch(url):
        urls.append(url)
        return SUMS.encode()
    monkeypatch.setattr(updates, "_download", fetch)
    sums = updates.fetch_checksums("99.0.0")
    assert sums["syntropy_health-99.0.0-py3-none-any.whl"] == "a" * 64
    assert urls == [f"https://github.com/{updates.REPO}/releases/download/v99.0.0/SHA256SUMS"]


def test_a_release_without_checksums_is_not_installed(monkeypatch):
    def fetch(url):
        raise updates.InstallFailed("not there")
    monkeypatch.setattr(updates, "_download", fetch)
    with pytest.raises(updates.InstallFailed):
        updates.fetch_checksums("99.0.0")


def test_the_wheel_is_installed_only_with_its_checksum():
    release = {"version": "99.0.0", "assets": {"syntropy_health-99.0.0-py3-none-any.whl": "https://example.test/w.whl"}}
    assert updates.package_source(release, {"syntropy_health-99.0.0-py3-none-any.whl": "a" * 64}) == \
        f"syntropy-health @ https://example.test/w.whl#sha256={'a' * 64}"
    with pytest.raises(updates.InstallFailed):
        updates.package_source(release, {})


def test_going_back_uses_the_old_releases_checksum():
    name = "syntropy_health-1.2.3-py3-none-any.whl"
    assert updates.package_source_for("1.2.3", {name: "c" * 64}).endswith(f"/v1.2.3/{name}#sha256={'c' * 64}")


def test_the_locked_dependency_list_is_checked_before_use(monkeypatch, tmp_path):
    good = b"pillow==13.0.0\n"
    import hashlib
    sums = {updates.REQUIREMENTS_ASSET: hashlib.sha256(good).hexdigest()}
    monkeypatch.setattr(updates, "_download", lambda url: good)
    path = updates.fetch_requirements("99.0.0", sums, tmp_path)
    assert path.read_bytes() == good
    monkeypatch.setattr(updates, "_download", lambda url: b"pillow==6.6.6\n")
    with pytest.raises(updates.InstallFailed):
        updates.fetch_requirements("99.0.0", sums, tmp_path)


def test_a_release_from_before_lock_files_installs_without_one(tmp_path):
    assert updates.fetch_requirements("1.6.0", {"syntropy_health-1.6.0-py3-none-any.whl": "a" * 64}, tmp_path) is None
