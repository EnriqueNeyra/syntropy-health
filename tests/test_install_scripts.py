"""The real scripts/install.sh and Docker scripts, run with the system's commands stood in for and its paths moved under a
temporary folder. CI's install jobs run them for real on Linux and Mac."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell scripts")

TAG = "v9.9.9"
WHEEL = "syntropy_health-9.9.9-py3-none-any.whl"
LOCK = "Syntropy-Health-requirements.txt"
DMG = "Syntropy-Health-mac-arm64.dmg"


def _stub(folder: Path, name: str, body: str) -> None:
    path = folder / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _sums(release: Path) -> None:
    lines = [f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {f.name}" for f in sorted(release.iterdir()) if f.name != "SHA256SUMS"]
    (release / "SHA256SUMS").write_text("\n".join(lines) + "\n")


@pytest.fixture
def machine(tmp_path):
    """A computer of stand-ins: every command it runs is noted in `log`."""
    bin_dir, release = tmp_path / "stubs", tmp_path / "release"
    bin_dir.mkdir()
    release.mkdir()
    log = tmp_path / "log"
    log.touch()
    (release / WHEEL).write_bytes(b"the wheel")
    (release / LOCK).write_bytes(b"pillow==13.0.0\n")
    _sums(release)
    note = f'echo "$(basename "$0") $*" >> "{log}"'
    # curl serves the release folder: `-o FILE URL`, or the redirect that names the latest tag.
    _stub(bin_dir, "curl", f"""{note}
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift 2;; -w) shift 2;; -*) shift;; *) url="$1"; shift;; esac
done
case "$url" in
  */releases/latest) echo "https://github.com/x/y/releases/tag/{TAG}"; exit 0;;
  */health) [ ! -f "{tmp_path}/server-down" ]; exit $?;;
  *.sh) printf 'echo uv installer; mkdir -p "$UV_INSTALL_DIR"; ln -sf "{bin_dir}/uv.real" "$UV_INSTALL_DIR/uv"; touch "$UV_INSTALL_DIR/uvx"\\n' > "$out"; exit 0;;
esac
file="{release}/$(basename "$url")"
[ -f "$file" ] || exit 22
cp "$file" "$out\"""")
    # uv: makes a venv with a stand-in server program, which answers as the service would.
    uv = f"""{note}
case "$1 ${{2:-}}" in
  "venv "*) for last; do :; done; mkdir -p "$last/bin"; printf '#!/bin/sh\\n' > "$last/bin/python"; chmod +x "$last/bin/python"
    cat > "$last/bin/syntropy-health" <<'SERVER'
#!/bin/sh
echo "syntropy-health $*" >> "{log}"
case "$1" in
  version) echo 9.9.9;;
  service)
    port=""; shift 2; while [ $# -gt 0 ]; do case "$1" in --port) port="$2"; shift 2;; *) shift;; esac; done
    if [ -n "$port" ]; then echo "$port" > "{tmp_path}/port"; fi
    echo "Syntropy Health is running."
    echo "Open http://localhost:$(cat "{tmp_path}/port" 2>/dev/null || echo 8000)";;
esac
SERVER
    chmod +x "$last/bin/syntropy-health";;
  "tool dir") echo "{tmp_path}/tools";;
esac"""
    _stub(bin_dir, "uv.real", uv)
    _stub(bin_dir, "uname", 'case "$1" in -m) echo arm64;; *) cat "' + str(tmp_path) + '/system";; esac')
    (tmp_path / "system").write_text("Linux\n")
    account = tmp_path / "passwd"
    account.write_text("root:x:0:0::/root:/bin/sh\n")
    _stub(bin_dir, "id", f"""case "$1" in
  -u) cat "{tmp_path}/uid";;
  *) grep -q "^$1:" "{account}";;
esac""")
    (tmp_path / "uid").write_text("0\n")
    data = tmp_path / "var-lib"
    _stub(bin_dir, "useradd", f'{note}\nfor last; do :; done; echo "$last:x:999:999::{data}:/usr/sbin/nologin" >> "{account}"')
    _stub(bin_dir, "userdel", f'{note}\ngrep -v "^$1:" "{account}" > "{account}.new"; mv "{account}.new" "{account}"')
    for name in ("chown", "chgrp", "systemctl", "timedatectl", "hostname", "open", "osascript", "launchctl", "brew",
                 "runuser", "hdiutil", "spctl", "codesign", "pgrep", "sleep"):
        _stub(bin_dir, name, f'{note}\nexit 0' if name != "pgrep" else "exit 1")
    systemd_run = tmp_path / "run-systemd"
    systemd_run.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    moved = {"/opt/syntropy-health": tmp_path / "opt", "/var/lib/syntropy-health": data, "/etc/syntropy-health": tmp_path / "etc",
             "/usr/local/bin": tmp_path / "local-bin", "/etc/systemd/system": tmp_path / "units", "/run/systemd/system": systemd_run,
             "/etc/passwd": account}
    (tmp_path / "local-bin").mkdir()
    (tmp_path / "units").mkdir()
    script = (ROOT / "scripts" / "install.sh").read_text()
    for real, here in moved.items():
        script = script.replace(real, str(here))
    (tmp_path / "install.sh").write_text(script)
    env = {"PATH": f"{bin_dir}:{tmp_path / 'local-bin'}:/usr/bin:/bin", "HOME": str(home), "LC_ALL": "C", "TMPDIR": str(tmp_path)}

    def run(*args: str, **kw) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", str(tmp_path / "install.sh"), "--no-ocr", *args], env={**env, **kw.pop("env", {})},
                              capture_output=True, text=True, timeout=60)

    def has_uv() -> None:
        (tmp_path / "local-bin" / "uv").symlink_to(bin_dir / "uv.real")

    return SimpleNamespace(root=tmp_path, run=run, log=log, release=release, stubs=bin_dir, env=env, script=script, has_uv=has_uv,
                           prefix=tmp_path / "opt", data=data, etc=tmp_path / "etc", local_bin=tmp_path / "local-bin",
                           account=account, home=home)


def _log(machine) -> str:
    return machine.log.read_text()


# ---------------------------------------------------------------------------
# A download cut short
# ---------------------------------------------------------------------------

def test_a_download_cut_short_does_nothing(machine):
    """sh runs a piped script as it reads it, and sees the end of a cut download as a normal end. Every cut, line by line."""
    lines = machine.script.splitlines(keepends=True)
    assert lines[-1].strip() == "main"
    for cut in range(1, len(lines)):
        subprocess.run(["sh", "-s", "--", "--no-ocr"], input="".join(lines[:cut]), env=machine.env, capture_output=True, text=True, timeout=30)
        assert _log(machine) == "", f"Cut after line {cut}, it still ran things"
        assert not machine.prefix.exists() and not (machine.local_bin / "syntropy-health").exists()


def test_the_whole_script_through_a_pipe_does_install(machine):
    machine.has_uv()
    done = subprocess.run(["sh", "-s", "--", "--no-ocr"], input=machine.script, env=machine.env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert (machine.local_bin / "syntropy-health").exists() and "is installed" in done.stdout


# ---------------------------------------------------------------------------
# Linux: checked downloads
# ---------------------------------------------------------------------------

def test_a_release_is_checked_and_installed_with_its_exact_dependencies(machine):
    machine.has_uv()
    done = machine.run()
    assert done.returncode == 0, done.stderr
    install = next(line for line in _log(machine).splitlines() if line.startswith("uv pip install"))
    assert f"/{LOCK}" in install and "-c " in install and install.endswith(WHEEL)
    assert (machine.prefix / "venv" / "syntropy-source.txt").read_text().strip() == "latest"
    assert (machine.prefix / "venv" / "syntropy-uv.txt").read_text().strip().endswith("/uv")


@pytest.mark.parametrize("damaged", [WHEEL, LOCK])
def test_a_damaged_download_is_not_installed(machine, damaged):
    machine.has_uv()
    (machine.release / damaged).write_bytes(b"something else")
    done = machine.run()
    assert done.returncode != 0 and "doesn't match the checksum" in done.stderr
    assert "pip install" not in _log(machine) and not (machine.local_bin / "syntropy-health").exists()


def test_a_release_with_no_checksums_is_not_installed(machine):
    machine.has_uv()
    (machine.release / "SHA256SUMS").unlink()
    done = machine.run()
    assert done.returncode != 0 and "checksums" in done.stderr and "pip install" not in _log(machine)


def test_a_release_from_before_lock_files_still_installs(machine):
    machine.has_uv()
    (machine.release / LOCK).unlink()
    _sums(machine.release)
    assert machine.run().returncode == 0
    install = next(line for line in _log(machine).splitlines() if line.startswith("uv pip install"))
    assert " -c " not in install


def test_a_chosen_package_is_the_persons_own_choice(machine):
    machine.has_uv()
    assert machine.run("--source", "/some/own.whl").returncode == 0
    assert "uv pip install" in _log(machine) and "/some/own.whl" in _log(machine) and "github.com" not in _log(machine)
    assert (machine.prefix / "venv" / "syntropy-source.txt").read_text().strip() == "/some/own.whl"


def test_uv_comes_from_a_fixed_version(machine):
    done = machine.run()
    assert done.returncode == 0, done.stderr
    version = next(line.split("=")[1].split()[0] for line in machine.script.splitlines() if line.startswith("UV_VERSION="))
    assert f"https://astral.sh/uv/{version}/install.sh" in _log(machine)
    assert (machine.etc / "installed-uv").exists()


# ---------------------------------------------------------------------------
# Linux: running it again
# ---------------------------------------------------------------------------

def test_running_it_again_leaves_the_port_to_the_service(machine):
    machine.has_uv()
    first = machine.run("--port", "9000")
    assert first.returncode == 0, first.stderr
    assert ":9000 " in first.stdout
    machine.log.write_text("")
    again = machine.run()
    assert again.returncode == 0, again.stderr
    service = next(line for line in _log(machine).splitlines() if "service install" in line)
    assert "--port" not in service and "--user syntropy" in service
    assert ":9000 " in again.stdout        # and it says the one in use


def test_a_new_port_when_asked_for(machine):
    machine.has_uv()
    machine.run("--port", "9000")
    machine.log.write_text("")
    done = machine.run("--port", "9100")
    assert "--port 9100" in _log(machine) and ":9100 " in done.stdout


def test_without_systemd_it_says_the_default_port(machine):
    machine.has_uv()
    (machine.root / "run-systemd").rmdir()
    done = machine.run()
    assert done.returncode == 0, done.stderr
    assert "--port 8000" in done.stdout and ":8000 " in done.stdout


def test_a_server_that_does_not_come_back_is_mentioned(machine):
    machine.has_uv()
    assert "hasn't answered" not in machine.run().stdout
    (machine.root / "server-down").touch()
    done = machine.run()
    assert done.returncode == 0, done.stderr       # a big database can take minutes to upgrade
    assert "hasn't answered yet" in done.stdout and "journalctl -u syntropy-health" in done.stdout


def test_it_says_when_only_this_computer_can_reach_the_server(machine):
    machine.has_uv()
    assert "from any device on your network" in machine.run("--port", "9000").stdout
    unit = machine.root / "units" / "syntropy-health.service"
    unit.write_text("ExecStart=/x serve --host 127.0.0.1 --port 9000\n")
    done = machine.run()
    assert "http://localhost:9000 on this computer" in done.stdout and "from any device" not in done.stdout


def test_the_command_runs_data_commands_as_the_service_account(machine):
    machine.has_uv()
    machine.run()
    wrapper = (machine.local_bin / "syntropy-health").read_text()
    assert "serve|mcp|info|accounts|key|dev)" in wrapper and 'runuser -u "syntropy"' in wrapper
    assert "backup|restore)" in wrapper and '--service-account "syntropy"' in wrapper


# ---------------------------------------------------------------------------
# Linux: removing everything
# ---------------------------------------------------------------------------

def test_purge_removes_the_account_and_the_uv_it_added(machine):
    assert machine.run().returncode == 0
    assert (machine.local_bin / "uvx").exists() and "syntropy:" in machine.account.read_text()
    done = machine.run("--uninstall", "--purge")
    assert done.returncode == 0, done.stderr
    assert not machine.data.exists() and not machine.etc.exists() and not machine.prefix.exists()
    assert "syntropy:" not in machine.account.read_text()
    assert not (machine.local_bin / "uv").exists() and not (machine.local_bin / "uvx").exists()


def test_purge_leaves_a_uv_that_was_already_there(machine):
    machine.has_uv()
    assert machine.run().returncode == 0
    assert machine.run("--uninstall", "--purge").returncode == 0
    assert (machine.local_bin / "uv").is_symlink()


def test_purge_leaves_an_account_of_the_same_name_that_is_not_ours(machine):
    machine.has_uv()
    with machine.account.open("a") as f:
        f.write("syntropy:x:1500:1500::/home/syntropy:/bin/sh\n")
    assert machine.run().returncode == 0
    assert machine.run("--uninstall", "--purge").returncode == 0
    assert "syntropy:" in machine.account.read_text()


def test_uninstall_without_purge_keeps_the_data_and_the_account(machine):
    machine.has_uv()
    assert machine.run().returncode == 0
    assert machine.run("--uninstall").returncode == 0
    assert machine.data.exists() and "syntropy:" in machine.account.read_text() and not machine.prefix.exists()


# ---------------------------------------------------------------------------
# Mac
# ---------------------------------------------------------------------------

@pytest.fixture
def mac(machine):
    machine.root.joinpath("system").write_text("Darwin\n")
    machine.root.joinpath("uid").write_text("501\n")
    apps = machine.root / "Applications"
    apps.mkdir()
    (machine.release / DMG).write_bytes(b"the app")
    _sums(machine.release)
    # hdiutil "mounts" a new app; ditto copies it, as the real one does.
    log = machine.log
    _stub(machine.stubs, "hdiutil", f"""echo "hdiutil $*" >> "{log}"
if [ "$1" = attach ]; then
  while [ $# -gt 0 ]; do case "$1" in -mountpoint) m="$2"; shift 2;; *) shift;; esac; done
  mkdir -p "$m/Syntropy Health.app/Contents/MacOS"
  printf '#!/bin/sh\\necho 9.9.9\\n' > "$m/Syntropy Health.app/Contents/MacOS/Syntropy Health"
  chmod +x "$m/Syntropy Health.app/Contents/MacOS/Syntropy Health"
fi""")
    _stub(machine.stubs, "ditto", f'echo "ditto $*" >> "{log}"\ncp -R "$1" "$2"')
    machine.apps = apps
    machine.app = apps / "Syntropy Health.app"
    machine.mac_env = {"SYNTROPY_APP_DIR": str(apps)}
    return machine


def _old_app(mac) -> None:
    (mac.app / "old").mkdir(parents=True)


def test_the_mac_app_goes_in_once_it_has_been_checked(mac):
    _old_app(mac)
    done = mac.run(env=mac.mac_env)
    assert done.returncode == 0, done.stderr
    assert (mac.app / "Contents/MacOS/Syntropy Health").exists() and not (mac.app / "old").exists()
    assert not Path(f"{mac.app}.old").exists() and not Path(f"{mac.app}.new").exists()
    logged = _log(mac)
    assert "codesign --verify --deep --strict" in logged and 'certificate leaf[subject.OU] = "RJ2YA2S2C8"' in logged
    assert logged.index("codesign") < logged.index("ditto") and "spctl -a -t open" in logged


def test_a_damaged_mac_download_is_not_opened_or_installed(mac):
    _old_app(mac)
    (mac.release / DMG).write_bytes(b"something else")
    done = mac.run(env=mac.mac_env)
    assert done.returncode != 0 and "doesn't match the checksum" in done.stderr
    assert "hdiutil attach" not in _log(mac) and (mac.app / "old").exists()


@pytest.mark.parametrize("tool, says", [("codesign", "isn't signed by Syntropy Labs"), ("spctl", "macOS doesn't accept")])
def test_a_mac_app_we_did_not_sign_is_not_installed(mac, tool, says):
    _old_app(mac)
    _stub(mac.stubs, tool, f'echo "{tool} $*" >> "{mac.log}"\nexit 1')
    done = mac.run(env=mac.mac_env)
    assert done.returncode != 0 and says in done.stderr
    assert "ditto" not in _log(mac) and (mac.app / "old").exists()


def test_a_copy_that_fails_leaves_the_app_that_was_there(mac):
    _old_app(mac)
    _stub(mac.stubs, "ditto", 'mkdir -p "$2/half"; exit 1')
    done = mac.run(env=mac.mac_env)
    assert done.returncode != 0 and "wasn't changed" in done.stderr
    assert (mac.app / "old").exists() and not Path(f"{mac.app}.new").exists()


def test_a_dmg_someone_chose_is_their_own_choice(mac):
    own = mac.root / "own.dmg"
    own.write_bytes(b"built here")
    done = mac.run("--source", str(own), env=mac.mac_env)
    assert done.returncode == 0, done.stderr
    assert "codesign" not in _log(mac) and "spctl" not in _log(mac)


def test_uninstalling_on_a_mac_takes_out_only_the_lines_it_added(mac):
    profile = mac.home / ".zprofile"
    profile.write_text('export MINE=1\n\n# Added by the Syntropy Health installer: the syntropy-health command\n'
                       'export PATH="$HOME/.local/bin:$PATH"\n\nexport ALSO_MINE=2\n')
    assert mac.run("--uninstall", env=mac.mac_env).returncode == 0
    assert profile.read_text() == 'export MINE=1\n\nexport ALSO_MINE=2\n'


def test_uninstalling_leaves_a_path_line_someone_wrote_themselves(mac):
    profile = mac.home / ".zprofile"
    profile.write_text('export PATH="$HOME/.local/bin:$PATH"\n')
    assert mac.run("--uninstall", env=mac.mac_env).returncode == 0
    assert profile.read_text() == 'export PATH="$HOME/.local/bin:$PATH"\n'


def test_the_mac_server_install_is_checked_and_locked_too(mac):
    uv = mac.stubs / "uv"
    uv.symlink_to(mac.stubs / "uv.real")
    (mac.root / "tools" / "syntropy-health").mkdir(parents=True)
    bin_dir = mac.home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    _stub(bin_dir, "syntropy-health", f"""echo "syntropy-health $*" >> "{mac.log}"
case "$1" in version) echo 9.9.9;; service) echo "Open http://localhost:9000";; esac""")
    done = mac.run("--server", env=mac.mac_env)
    assert done.returncode == 0, done.stderr
    install = next(line for line in _log(mac).splitlines() if "tool install" in line)
    assert "--constraints" in install and LOCK in install and install.endswith(WHEEL)
    assert "--port" not in next(line for line in _log(mac).splitlines() if "service install" in line)
    assert (mac.root / "tools" / "syntropy-health" / "syntropy-uv.txt").exists()
    assert "localhost:9000" in done.stdout


# ---------------------------------------------------------------------------
# Docker's entrypoint and command
# ---------------------------------------------------------------------------

@pytest.fixture
def container(tmp_path):
    """The entrypoint with a folder whose owner and contents a test decides: stat and find answer from these files."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "log"
    log.touch()
    (tmp_path / "owner").write_text("1000\n")
    (tmp_path / "strays").write_text("")
    (tmp_path / "uid").write_text("0\n")
    _stub(bin_dir, "id", f'''case "$1 ${{2:-}}" in
  "-u ") cat "{tmp_path}/uid";;
  "-u syntropy"|"-g syntropy") echo 10001;;
esac''')
    _stub(bin_dir, "stat", f'cat "{tmp_path}/owner"')
    _stub(bin_dir, "find", f'cat "{tmp_path}/strays"')
    _stub(bin_dir, "chown", f'echo "chown $*" >> "{log}"')
    _stub(bin_dir, "setpriv", f'''echo "setpriv $* HOME=$HOME" >> "{log}"
while [ $# -gt 0 ]; do case "$1" in --*) shift;; *) break;; esac; done
"$@"''')
    for name in ("docker-entrypoint.sh", "docker-syntropy-health.sh"):
        (bin_dir / name).write_bytes((ROOT / "scripts" / name).read_bytes())
        (bin_dir / name).chmod(0o755)
    _stub(bin_dir, "python", f'echo "python $* PYTHONPATH=$PYTHONPATH" >> "{log}"')
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "SYNTROPY_DATA_DIR": str(tmp_path / "data")}

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", str(bin_dir / "docker-entrypoint.sh"), *args], env=env, capture_output=True, text=True, timeout=30)
    return SimpleNamespace(root=tmp_path, log=log, run=run, bin=bin_dir, env=env)


def test_a_folder_you_own_is_left_alone_and_the_server_runs_as_you(container):
    done = container.run("true")
    assert done.returncode == 0 and done.stderr == ""
    logged = container.log.read_text()
    assert "chown" not in logged and "--reuid=1000" in logged and f"HOME={container.root / 'data'}" in logged


def test_a_root_owned_folder_is_handed_over_all_the_way_down(container):
    """A restored backup: only the top folder used to change hands, and the server then couldn't write its database."""
    (container.root / "owner").write_text("0\n")
    done = container.run("true")
    assert done.returncode == 0
    logged = container.log.read_text()
    assert f"chown -R 10001:10001 {container.root / 'data'}" in logged and "--reuid=10001" in logged
    assert "belonged to root" in done.stderr and "sudo chown" in done.stderr


def test_stray_files_go_to_the_folders_owner(container):
    (container.root / "strays").write_text("/data/syntropy.db\n")
    done = container.run("true")
    assert done.returncode == 0
    logged = container.log.read_text()
    assert f"chown -R 1000:1000 {container.root / 'data'}" in logged and "--reuid=1000" in logged
    assert "belonged to another account" in done.stderr


def test_a_folder_that_cannot_be_handed_over_is_explained_and_the_server_still_starts(container):
    """The server's own page then says what's wrong, instead of the container restarting for ever."""
    (container.root / "owner").write_text("0\n")
    _stub(container.bin, "chown", "exit 1")
    _stub(container.bin, "setpriv", f'''echo "setpriv $*" >> "{container.log}"
while [ $# -gt 0 ]; do case "$1" in --*) shift;; *) break;; esac; done
if [ "$1" = test ]; then exit 1; fi
"$@"''')
    done = container.run("true")
    assert done.returncode == 0
    assert "can't write to" in done.stderr
    assert container.log.read_text().count("setpriv") == 2


def test_not_root_it_just_runs(container):
    (container.root / "uid").write_text("1234\n")
    done = container.run("true")
    assert done.returncode == 0 and container.log.read_text() == ""


def test_the_command_in_the_image_runs_as_the_data_folders_owner(container):
    done = subprocess.run(["sh", str(container.bin / "docker-syntropy-health.sh"), "backup", "/data/backups"],
                          env=container.env, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    logged = container.log.read_text()
    assert "--reuid=1000" in logged and "python -m app backup /data/backups PYTHONPATH=/app" in logged


def test_scripts_are_executable_in_git():
    for name in ("docker-entrypoint.sh", "docker-syntropy-health.sh"):
        assert os.access(ROOT / "scripts" / name, os.X_OK), name
