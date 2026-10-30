"""The Mac and Windows app's start-up (desktop/syntropy_desktop.py), as far as it can be checked off those systems."""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import logging.handlers
import os
import socket
import sqlite3
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

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


def _packaged_mac(monkeypatch, tmp_path, exe):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(syntropy_desktop.sys, "platform", "darwin")
    monkeypatch.setattr(syntropy_desktop.sys, "frozen", True, raising=False)
    monkeypatch.setattr(syntropy_desktop.sys, "executable", exe)


def test_mac_login_item_is_valid_and_follows_the_app(monkeypatch, tmp_path):
    import plistlib
    # A folder with "&" in its name used to make the login item's XML invalid, so it never opened.
    _packaged_mac(monkeypatch, tmp_path, "/Users/a/R&D/Syntropy Health.app/Contents/MacOS/Syntropy Health")
    syntropy_desktop.set_open_at_login(True)
    agent = plistlib.loads(syntropy_desktop._launch_agent().read_bytes())
    assert agent["ProgramArguments"] == [syntropy_desktop.sys.executable, "--background"]
    assert agent["AssociatedBundleIdentifiers"] == ["io.syntropyhealth.desktop"]

    # Moved into Applications: the login item opens the new place.
    moved = "/Applications/Syntropy Health.app/Contents/MacOS/Syntropy Health"
    monkeypatch.setattr(syntropy_desktop.sys, "executable", moved)
    syntropy_desktop.heal_open_at_login()
    assert syntropy_desktop._launch_agent_program() == moved

    # Opened from the disk image (or a temporary copy macOS made): left pointing at the installed one.
    for temporary in ("/Volumes/Syntropy Health/Syntropy Health.app/Contents/MacOS/Syntropy Health",
                      "/private/var/folders/x/AppTranslocation/1/d/Syntropy Health.app/Contents/MacOS/Syntropy Health"):
        monkeypatch.setattr(syntropy_desktop.sys, "executable", temporary)
        assert syntropy_desktop.runs_from_temporary_place()
        syntropy_desktop.heal_open_at_login()
        assert syntropy_desktop._launch_agent_program() == moved

    syntropy_desktop.set_open_at_login(False)
    assert not syntropy_desktop.open_at_login()


@pytest.mark.skipif(sys.platform == "win32", reason="a Mac LaunchAgent: launchctl, and a home folder from HOME")
def test_uninstalling_removes_only_this_apps_server_service(monkeypatch, tmp_path):
    import plistlib
    monkeypatch.setenv("HOME", str(tmp_path))
    ran = []
    monkeypatch.setattr(syntropy_desktop.subprocess, "run", lambda cmd, **_: ran.append(cmd))
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    plist = agents / "io.syntropyhealth.server.plist"
    bundle = Path("/Applications/Syntropy Health.app")

    plist.write_bytes(plistlib.dumps({"ProgramArguments": [str(Path.home() / ".local/bin/syntropy-health"), "serve"]}))
    syntropy_desktop.remove_server_service(bundle)          # installed with install.sh --server: not the app's
    assert plist.exists() and not ran

    plist.write_bytes(plistlib.dumps({"ProgramArguments": [str(bundle / "Contents/MacOS/Syntropy Health"), "serve"]}))
    syntropy_desktop.remove_server_service(bundle)
    assert not plist.exists() and ran[0][:2] == ["launchctl", "bootout"]


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

def _listener(family: int, address: str, port: int = 0) -> socket.socket:
    s = socket.socket(family, socket.SOCK_STREAM)
    s.bind((address, port))
    s.listen()
    return s


def test_another_program_on_ipv6_localhost_counts_as_taken():
    if not socket.has_ipv6:
        pytest.skip("no IPv6")
    try:
        taken = _listener(socket.AF_INET6, "::1")
    except OSError:
        pytest.skip("no IPv6 loopback")
    with taken:
        # The window opens "localhost", which tries ::1 first, so that program would appear as the app.
        assert not syntropy_desktop.port_free("0.0.0.0", taken.getsockname()[1])


def test_a_port_that_is_busy_for_a_moment_is_waited_for(monkeypatch):
    with _listener(socket.AF_INET, "127.0.0.1") as held:
        port = held.getsockname()[1]
    answers = iter([False, False, True])
    monkeypatch.setattr(syntropy_desktop, "port_free", lambda _h, _p: next(answers))
    monkeypatch.setattr(syntropy_desktop.time, "sleep", lambda _s: None)
    assert syntropy_desktop.choose_port(port) == port


def test_a_busy_saved_port_stays_saved_and_is_tried_again_next_time(monkeypatch, tmp_path):
    from app.services import network
    monkeypatch.setenv("SYNTROPY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(syntropy_desktop, "HOME_PORT_WAIT", 0)
    monkeypatch.setattr(syntropy_desktop, "update_installer", lambda fn: None)
    monkeypatch.setattr(syntropy_desktop, "network_controller", lambda fn: None)
    monkeypatch.setattr(syntropy_desktop, "program_on_port", lambda p: "some-dev-server")
    with _listener(socket.AF_INET, "127.0.0.1") as held:
        home = held.getsockname()[1]
        (tmp_path / "desktop.json").write_text(json.dumps({"port": home}))
        app = syntropy_desktop.DesktopApp(background=True)
        monkeypatch.setattr(app.server, "start", lambda host, port: setattr(app.server, "port", port))
        app.start_own_server()
        assert app.server.port != home
        assert json.loads((tmp_path / "desktop.json").read_text())["port"] == home      # what the phone was paired with
        assert app.menu_state()["port_note"] == f"Using port {app.server.port}: {home} is busy"
        problem = network.problems()[0]
        assert str(home) in problem["text"] and "some-dev-server" in problem["text"]
    app.start_own_server()
    assert app.server.port == home and app.menu_state()["port_note"] is None and network.problems() == []


def test_first_port_is_saved(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNTROPY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(syntropy_desktop, "update_installer", lambda fn: None)
    monkeypatch.setattr(syntropy_desktop, "network_controller", lambda fn: None)
    app = syntropy_desktop.DesktopApp(background=True)
    monkeypatch.setattr(app.server, "start", lambda host, port: setattr(app.server, "port", port))
    app.start_own_server()
    assert json.loads((tmp_path / "desktop.json").read_text())["port"] == app.server.port


# ---------------------------------------------------------------------------
# The server's life
# ---------------------------------------------------------------------------

@pytest.fixture
def real_server(monkeypatch, isolated_data):
    """The real Server class around a small app with a slow streaming answer, and a lifespan that says what it does."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse, StreamingResponse
    from starlette.routing import Route

    import app.main as server_main

    events: list[str] = []

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        events.append("start")
        yield
        events.append("stop")

    async def slow(_request):
        import asyncio

        async def body():
            while True:
                yield b"more"
                await asyncio.sleep(0.2)

        return StreamingResponse(body())

    async def hello(_request):
        return PlainTextResponse("hello")

    monkeypatch.setattr(server_main, "app", Starlette(routes=[Route("/slow", slow), Route("/hello", hello)], lifespan=lifespan))
    server = syntropy_desktop.Server()
    yield server, events
    server.stop()


def _free_port() -> int:
    with _listener(socket.AF_INET, "127.0.0.1") as s:
        return s.getsockname()[1]


def _get(host: str, port: int, path: str = "/hello") -> bytes:
    with socket.create_connection((host, port), timeout=3) as s:
        s.sendall(f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())
        answer = b""
        while chunk := s.recv(4096):
            answer += chunk
        return answer


def test_the_server_answers_on_both_loopback_addresses(real_server):
    server, _ = real_server
    server.start("127.0.0.1", _free_port())
    assert b"hello" in _get("127.0.0.1", server.port)
    if socket.has_ipv6:
        try:
            assert b"hello" in _get("::1", server.port)
        except OSError:
            pytest.skip("no IPv6 loopback")
    assert not syntropy_desktop.port_free("0.0.0.0", server.port)


def test_a_busy_port_is_an_error_in_words(real_server):
    server, _ = real_server
    with _listener(socket.AF_INET, "127.0.0.1") as held:
        with pytest.raises(OSError) as caught:
            server.start("127.0.0.1", held.getsockname()[1])
    assert syntropy_desktop.describe_failure(caught.value)[1]


def test_restarting_during_a_long_answer_finishes_the_old_server_first(real_server):
    # It used to give up after 15 seconds with the old server running, whose shutdown then stopped the new one's
    # scheduler, announcing and update checks for good.
    server, events = real_server
    server.start("127.0.0.1", _free_port())
    held = socket.create_connection(("127.0.0.1", server.port), timeout=3)
    held.sendall(b"GET /slow HTTP/1.0\r\nHost: localhost\r\n\r\n")
    held.recv(100)
    began = time.monotonic()
    server.restart("127.0.0.1")
    assert time.monotonic() - began < 12
    assert events == ["start", "stop", "start"]
    assert b"hello" in _get("127.0.0.1", server.port)
    held.close()


def test_a_server_that_wont_finish_isnt_started_beside(monkeypatch):
    server = syntropy_desktop.Server()
    stuck = threading.Event()
    server.thread = threading.Thread(target=stuck.wait, daemon=True)
    server.thread.start()
    monkeypatch.setattr(syntropy_desktop, "STOP_WAIT", 0.01)
    started = []
    monkeypatch.setattr(server, "start", lambda *a: started.append(a))
    with pytest.raises(RuntimeError):
        server.restart("127.0.0.1")
    assert not started
    stuck.set()


def test_problem_reports_what_startup_found(monkeypatch):
    import app.main as server_main
    monkeypatch.setattr(server_main.app.state, "problem", "It's damaged", raising=False)
    monkeypatch.setattr(server_main.app.state, "problem_kind", "damaged", raising=False)
    assert syntropy_desktop.Server().problem() == ("damaged", "It's damaged")
    monkeypatch.setattr(server_main.app.state, "problem", None, raising=False)
    assert syntropy_desktop.Server().problem() is None


# ---------------------------------------------------------------------------
# Saying what's wrong
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc,words", [
    (OSError(errno.ENOSPC, "No space left", "/d"), "disk is full"),
    (PermissionError(errno.EACCES, "Permission denied", "/d"), "isn't allowed to write"),
    (OSError(errno.EROFS, "Read-only", "/d"), "read-only"),
    (FileExistsError(errno.EEXIST, "File exists", "/d"), "isn't one"),
    (NotADirectoryError(errno.ENOTDIR, "Not a directory", "/d"), "isn't one"),
    (OSError(errno.ENOENT, "No such file", "/Volumes/gone"), "connect the drive"),
    (OSError(errno.EADDRINUSE, "No free port"), "taken by other programs"),
    (RuntimeError("The server stopped while it was starting."), "stopped while"),
])
def test_start_failures_say_what_to_do(exc, words):
    assert words in syntropy_desktop.describe_failure(exc)[1]


def test_damage_found_at_start_is_named(isolated_data):
    kind, text = syntropy_desktop.describe_failure(sqlite3.connect(":memory:").execute("SELECT 1") and _not_a_database(isolated_data))
    assert kind == "damaged" and "damaged" in text


def _not_a_database(folder: Path) -> sqlite3.DatabaseError:
    bad = folder / "bad.db"
    bad.write_bytes(os.urandom(5000))
    try:
        sqlite3.connect(bad).execute("SELECT * FROM sqlite_master")
    except sqlite3.DatabaseError as exc:
        return exc
    raise AssertionError


def test_a_file_where_the_data_folder_should_be_is_explained(monkeypatch, tmp_path, capsys):
    # Used to fail in setup_logging, before anything could say why, with no message at all on a Mac.
    afile = tmp_path / "afile"
    afile.write_text("x")
    monkeypatch.setenv("SYNTROPY_DATA_DIR", str(afile))
    shown = []
    monkeypatch.setattr(syntropy_desktop, "alert", lambda title, text, offer=None: shown.append((title, text, offer)) or False)
    monkeypatch.setattr(syntropy_desktop.sys, "argv", ["app"])
    before = logging.getLogger().handlers[:]
    try:
        with pytest.raises(OSError):
            syntropy_desktop.main()
    finally:
        logging.getLogger().handlers[:] = before
    assert "isn't one" in shown[0][1] and "no details could be saved" in shown[0][1] and shown[0][2] is None


def test_macs_get_a_message_too(monkeypatch):
    monkeypatch.setattr(syntropy_desktop.sys, "platform", "darwin")
    seen = []
    monkeypatch.setattr(syntropy_desktop, "_mac_alert", lambda *a: seen.append(a) or True)
    assert syntropy_desktop.alert("Title", "Text", "Show Data Folder") is True
    assert seen == [("Title", "Text", "Show Data Folder")]


def test_mac_message_from_another_thread_uses_the_systems_box(monkeypatch):
    monkeypatch.setattr(syntropy_desktop.sys, "platform", "darwin")
    ran = []

    class Done:
        stdout = "Show Data Folder\n"

    monkeypatch.setattr(syntropy_desktop.subprocess, "run", lambda cmd, **_: ran.append(cmd) or Done())
    box = {}
    thread = threading.Thread(target=lambda: box.update(answer=syntropy_desktop._mac_alert("T", "It's \"bad\"", "Show Data Folder")))
    thread.start()
    thread.join()
    assert box["answer"] is True
    assert ran[0][0] == "/usr/bin/osascript" and ran[0][3:] == ["T", "It's \"bad\"", "OK", "Show Data Folder"]


@pytest.mark.parametrize("answer,taken", [(6, True), (7, False)])
def test_windows_message_offers_the_folder(monkeypatch, answer, taken):
    import types
    calls = []
    fake = types.SimpleNamespace(windll=types.SimpleNamespace(user32=types.SimpleNamespace(
        MessageBoxW=lambda *a: calls.append(a) or answer)))
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    monkeypatch.setattr(syntropy_desktop.sys, "platform", "win32")
    assert syntropy_desktop.alert("T", "Text", "Show Data Folder") is taken
    assert calls[0][1].endswith("Show Data Folder?")


def test_output_after_the_log_rolls_over_still_reaches_the_log(tmp_path, monkeypatch):
    # sys.stderr used to be the log file's first stream, closed at the first rollover: print() then raised, and
    # tracebacks from threads were lost.
    monkeypatch.setenv("SYNTROPY_DATA_DIR", str(tmp_path))
    out, err, handlers = sys.stdout, sys.stderr, logging.getLogger().handlers[:]
    level = logging.getLogger().level
    try:
        syntropy_desktop.setup_logging()
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        print("before")
        handler.doRollover()
        print("after the rollover")
        sys.stderr.write("Traceback: boom\n")
        handler.flush()
        text = (tmp_path / "desktop.log").read_text()
        assert "after the rollover" in text and "Traceback: boom" in text
        handler.close()
    finally:
        sys.stdout, sys.stderr = out, err
        logging.getLogger().handlers[:] = handlers
        logging.getLogger().setLevel(level)


# ---------------------------------------------------------------------------
# Damaged small files
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content", ["null", "[]", "3", "{bad", ""])
def test_damaged_preferences_start_empty(tmp_path, content):
    (tmp_path / "desktop.json").write_text(content)
    prefs = syntropy_desktop.Prefs(tmp_path)
    assert prefs.get("port") is None and prefs.port() is None
    prefs.set("port", 8000)
    assert json.loads((tmp_path / "desktop.json").read_text()) == {"port": 8000}


@pytest.mark.parametrize("port", ["abc", 0, 70000, -1, True, 3.5, None, [8000]])
def test_a_nonsense_saved_port_is_ignored(tmp_path, port):
    (tmp_path / "desktop.json").write_text(json.dumps({"port": port}))
    assert syntropy_desktop.Prefs(tmp_path).port() is None


@pytest.mark.parametrize("zoom", ["big", [], None, 99, True])
def test_a_nonsense_zoom_is_ignored(tmp_path, zoom):
    (tmp_path / "desktop.json").write_text(json.dumps({"zoom": zoom}))
    assert syntropy_desktop.Prefs(tmp_path).number("zoom", 1.0, 0.75, 1.5) == 1.0


def test_preferences_survive_a_failed_write(tmp_path, monkeypatch):
    prefs = syntropy_desktop.Prefs(tmp_path)
    prefs.set("port", 8000)
    monkeypatch.setattr(syntropy_desktop.os, "replace", lambda *_: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full")))
    prefs.set("port", 9000)     # logged, not raised
    monkeypatch.undo()
    assert json.loads((tmp_path / "desktop.json").read_text()) == {"port": 8000}
    assert [p.name for p in tmp_path.iterdir()] == ["desktop.json"]


@pytest.mark.parametrize("content", ["null", "[]", "3", "{bad", '{"port": "x", "token": "y"}', '{"port": 99999, "token": "y"}', '{"token": "y"}'])
def test_a_damaged_instance_file_just_means_no_other_copy(tmp_path, content):
    (tmp_path / "desktop.instance").write_text(content)
    assert syntropy_desktop.SingleInstance(tmp_path, lambda: None).signal_existing() is False


def test_external_drive_is_no_temporary_place(monkeypatch, tmp_path):
    _packaged_mac(monkeypatch, tmp_path, "/Volumes/Data/Applications/Syntropy Health.app/Contents/MacOS/Syntropy Health")
    monkeypatch.setattr(syntropy_desktop, "_read_only_disk", lambda path: False)
    assert not syntropy_desktop.runs_from_temporary_place()
    monkeypatch.setattr(syntropy_desktop, "_read_only_disk", lambda path: True)       # its own disk image
    assert syntropy_desktop.runs_from_temporary_place()


# ---------------------------------------------------------------------------
# Getting past a damaged database
# ---------------------------------------------------------------------------

@pytest.fixture
def recovery():
    import recovery as module
    return module


def _database(path: Path, value: str = "good") -> None:
    from app.core import db
    db.ensure_migrated(path)
    db.reset_migration_cache()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS marker (v TEXT)")
    conn.execute("INSERT INTO marker VALUES (?)", (value,))
    conn.commit()
    conn.close()


def _marker(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT v FROM marker").fetchone()[0]
    finally:
        conn.close()


def test_restoring_a_database_keeps_what_it_replaces(recovery, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    damaged = os.urandom(5000)
    (data / "syntropy.db").write_bytes(damaged)
    (data / "syntropy.db-wal").write_bytes(b"stale log")
    (data / "secret.key").write_bytes(b"k" * 32)
    backup = tmp_path / "backup.db"
    _database(backup)
    aside = recovery.restore(backup, data / "syntropy.db")
    assert _marker(data / "syntropy.db") == "good"
    assert (aside / "syntropy.db").read_bytes() == damaged and (aside / "syntropy.db-wal").read_bytes() == b"stale log"
    assert not (data / "syntropy.db-wal").exists()      # a stale log replayed over a restored database corrupts it
    assert (data / "secret.key").read_bytes() == b"k" * 32
    assert not list(data.glob("*.restoring*"))


def test_restoring_a_zip_brings_its_key_and_uploads(recovery, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "syntropy.db").write_bytes(os.urandom(5000))
    (data / "secret.key").write_bytes(b"old" * 10)
    (data / "uploads").mkdir()
    (data / "uploads" / "kept.pdf").write_bytes(b"newer")
    good = tmp_path / "good.db"
    _database(good)
    backup = tmp_path / "backup.zip"
    with zipfile.ZipFile(backup, "w") as z:
        z.write(good, "syntropy.db")
        z.writestr("secret.key", b"new" * 10)
        z.writestr("uploads/report.pdf", b"%PDF")
        z.writestr("uploads/kept.pdf", b"older")
        z.writestr("uploads/../../outside.txt", b"nope")
        z.writestr("elsewhere.txt", b"nope")
    aside = recovery.restore(backup, data / "syntropy.db")
    assert _marker(data / "syntropy.db") == "good"
    assert (data / "secret.key").read_bytes() == b"new" * 10 and (aside / "secret.key").read_bytes() == b"old" * 10
    assert (data / "uploads" / "report.pdf").read_bytes() == b"%PDF"
    assert (data / "uploads" / "kept.pdf").read_bytes() == b"newer"
    assert not (tmp_path / "outside.txt").exists() and not (data / "elsewhere.txt").exists()


@pytest.mark.parametrize("make,words", [
    (lambda p: p.write_bytes(os.urandom(5000)), "isn't a Syntropy Health backup"),
    (lambda p: sqlite3.connect(p).executescript("CREATE TABLE other (x);"), "isn't a Syntropy Health backup"),
    (lambda p: zipfile.ZipFile(p, "w").writestr("notes.txt", "x"), "doesn't hold"),
])
def test_a_file_that_isnt_a_backup_changes_nothing(recovery, tmp_path, make, words):
    data = tmp_path / "data"
    data.mkdir()
    damaged = os.urandom(5000)
    (data / "syntropy.db").write_bytes(damaged)
    chosen = tmp_path / "chosen"
    make(chosen)
    with pytest.raises(recovery.RestoreRefused, match=words):
        recovery.restore(chosen, data / "syntropy.db")
    assert (data / "syntropy.db").read_bytes() == damaged
    assert [p.name for p in data.iterdir()] == ["syntropy.db"]


def test_a_backup_from_a_newer_version_is_refused(recovery, tmp_path):
    from app.core import db
    data = tmp_path / "data"
    data.mkdir()
    (data / "syntropy.db").write_bytes(b"x")
    backup = tmp_path / "newer.db"
    _database(backup)
    conn = sqlite3.connect(backup)
    conn.execute(f"PRAGMA user_version = {db.newest_schema() + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(recovery.RestoreRefused, match="newer version"):
        recovery.restore(backup, data / "syntropy.db")
    assert (data / "syntropy.db").read_bytes() == b"x"


def test_setting_aside_moves_and_never_deletes(recovery, tmp_path):
    (tmp_path / "syntropy.db").write_bytes(b"damaged")
    (tmp_path / "syntropy.db-wal").write_bytes(b"log")
    folder = recovery.set_aside(tmp_path / "syntropy.db")
    assert (folder / "syntropy.db").read_bytes() == b"damaged" and (folder / "syntropy.db-wal").read_bytes() == b"log"
    assert not (tmp_path / "syntropy.db").exists()


def test_probe_answers_a_mistyped_address_with_advice(raw_client):
    # http://a:99999 and http://[bad used to answer 500, on the Mac's first-launch screen.
    headers = {"X-Requested-With": "syntropy"}
    for url in ("http://a:99999", "http://[bad", "file:///etc/passwd", "   "):
        r = raw_client.post("/api/desktop/probe", json={"url": url}, headers=headers)
        assert r.status_code == 400 and "Enter an address" in r.json()["detail"], url


# ---------------------------------------------------------------------------
# What pages and failures can ask of the app
# ---------------------------------------------------------------------------

class FakeWindow:
    def __init__(self):
        self.loaded, self.pages = [], []

    def load_url(self, url):
        self.loaded.append(url)

    def load_html(self, page):
        self.pages.append(page)

    def show(self):
        pass

    def restore(self):
        pass


@pytest.fixture
def desktop(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNTROPY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(syntropy_desktop, "update_installer", lambda fn: None)
    monkeypatch.setattr(syntropy_desktop, "network_controller", lambda fn: None)
    app = syntropy_desktop.DesktopApp(background=False)
    app.window = FakeWindow()
    return app


@pytest.mark.parametrize("kind", ["problem-quit", "problem-restore", "problem-fresh", "problem-folder", "problem-retry", "go-home"])
def test_recovery_is_only_taken_from_the_apps_own_page(desktop, monkeypatch, kind):
    asked = []
    monkeypatch.setattr(desktop, "on_own_page_request", asked.append)
    desktop.on_request({"type": kind, "_origin": f"http://localhost:{desktop.server.port}"})
    assert asked == []
    desktop.on_request({"type": kind, "_own_page": True})
    assert asked == [kind]


def test_a_site_that_wont_open_says_so_with_a_way_back(desktop):
    desktop.on_load_failed("https://mychart.example.org/login", -1001, "The request timed out.")
    assert "mychart.example.org" in desktop.window.pages[0] and "timed out" in desktop.window.pages[0]
    assert "send('go-home')" in desktop.window.pages[0]
    assert desktop.location.own_page is True


def test_a_page_trying_an_apps_scheme_is_left_alone(desktop):
    # Some portals try their phone app's own address and carry on when it fails.
    desktop.on_load_failed("epicmychart://login", -1002, "unsupported URL")
    desktop.on_load_failed("", -1002, "unsupported URL")
    assert desktop.window.pages == []


def test_the_apps_own_server_failing_a_load_doesnt_replace_the_page(desktop):
    desktop.on_load_failed(f"{desktop.server.url}/x", -1004, "Could not connect")
    assert desktop.window.pages == []


def test_a_joined_server_that_macos_wont_open_is_explained(desktop):
    # Plain http to a name that isn't local: Settings there lists it, and the window was left blank.
    desktop.prefs.set("role", "join")
    desktop.prefs.set("server", "http://server.example.org:8000")
    desktop.on_load_failed("http://server.example.org:8000/", syntropy_desktop.ATS_REFUSED, "App Transport Security policy")
    assert "secure connection" in desktop.window.pages[0] and "send('switch')" in desktop.window.pages[0]


def test_while_starting_menu_items_wait(desktop, monkeypatch):
    opened = []
    monkeypatch.setattr(syntropy_desktop.webbrowser, "open", opened.append)
    desktop.starting = True
    desktop.open_browser()
    desktop.open_page("/#/settings")
    desktop.toggle_lan()
    desktop.check_for_updates()
    assert opened == [] and desktop.window.loaded == []


def test_a_problem_shows_the_page_even_when_opened_at_login(desktop, monkeypatch):
    desktop.hidden = True
    monkeypatch.setattr(desktop.server, "start", lambda host, port: None)
    monkeypatch.setattr(desktop.server, "problem", lambda: ("too_new", "This data was last used by a newer version."))
    desktop.bring_up_own_server()
    assert desktop.hidden is False and desktop.starting is False
    assert "newer version" in desktop.window.pages[0] and "problem-latest" in desktop.window.pages[0]
    assert desktop.window.loaded == []


def test_a_start_that_fails_outright_shows_the_page(desktop, monkeypatch):
    def fail(host, port):
        raise OSError(errno.ENOSPC, "No space left on device", "/data")

    monkeypatch.setattr(desktop.server, "start", fail)
    desktop.bring_up_own_server()
    assert "disk is full" in desktop.window.pages[0] and not desktop.starting
    assert desktop.window.html == desktop.window.pages[0]       # or a slow WebView2 would show the starting page for good


def test_a_good_start_opens_the_app(desktop, monkeypatch):
    monkeypatch.setattr(desktop.server, "start", lambda host, port: setattr(desktop.server, "port", port))
    monkeypatch.setattr(desktop.server, "problem", lambda: None)
    desktop.bring_up_own_server()
    assert desktop.window.loaded == [desktop.server.url] and desktop.window.pages == []


def test_a_new_version_that_cant_upgrade_goes_back(desktop, monkeypatch, tmp_path):
    import updater
    folder = tmp_path / "updates"
    folder.mkdir()
    (folder / updater.ATTEMPT).write_text(json.dumps({"version": "0.0.1", "from": "0.0.0", "at": time.time()}))
    monkeypatch.setattr(desktop.server, "start", lambda host, port: None)
    monkeypatch.setattr(desktop.server, "problem", lambda: ("upgrade_failed", "It couldn't upgrade."))
    quit_called = []
    monkeypatch.setattr(desktop, "quit", lambda: quit_called.append(True))
    desktop.bring_up_own_server()
    assert quit_called and (folder / updater.BAD_MARK).read_text() == "It couldn't upgrade."
    assert desktop.window.pages == []


def test_a_damaged_database_stays_with_the_new_version(desktop, monkeypatch, tmp_path):
    # The old version couldn't read it either: going back wouldn't help, and the person needs the page.
    import updater
    folder = tmp_path / "updates"
    folder.mkdir()
    (folder / updater.ATTEMPT).write_text(json.dumps({"version": "0.0.1", "from": "0.0.0", "at": time.time()}))
    monkeypatch.setattr(desktop.server, "start", lambda host, port: None)
    monkeypatch.setattr(desktop.server, "problem", lambda: ("damaged", "It's damaged."))
    desktop.bring_up_own_server()
    assert "damaged" in desktop.window.pages[0] and not (folder / updater.BAD_MARK).exists()


def test_back_item_appears_in_the_menu_on_another_site():
    import tray
    assert "home" in {a for a, _t, _c in tray._labels({"lan": False, "login": False, "away": True})}
    assert "home" not in {a for a, _t, _c in tray._labels({"lan": False, "login": False, "away": False})}
    assert "home" in {a for a, _t, _c in tray._labels({"joined": "Mac mini", "away": True})}


def test_a_moved_port_shows_in_the_menu():
    import tray
    lines = [t for _a, t, _c in tray._labels({"lan": False, "login": False, "port_note": "Using port 8001: 8000 is busy"})]
    assert any("8000 is busy" in t for t in lines)


def test_macs_may_open_a_household_server_by_its_tailscale_name():
    # Settings lists http://<name>.ts.net:<port>, and App Transport Security refused it: a blank window, after this Mac's
    # own server had been stopped. Tailscale encrypts the link itself.
    spec = (Path(__file__).resolve().parent.parent / "desktop" / "syntropy-health.spec").read_text()
    assert '"ts.net": {"NSIncludesSubdomains": True, "NSExceptionAllowsInsecureHTTPLoads": True}' in spec
    assert '"NSAllowsLocalNetworking": True' in spec
