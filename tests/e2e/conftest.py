"""Browser tests: the real app, a real server and the Chrome on this computer. Skipped where Playwright isn't installed
(``pip install -e ".[checks]"``) or there's no Chrome; CI runs them in its own job."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "correct horse battery"
CSRF = {"X-Requested-With": "syntropy"}
WIDTHS = {"desktop": (1280, 860), "phone": (390, 800)}


# The server runs in its own process with its own data; these guard what runs in this one, where Playwright's loop would
# look like a request's.
@pytest.fixture(autouse=True)
def isolated_data() -> None:
    return None


@pytest.fixture(autouse=True)
def writes_stay_off_the_event_loop() -> None:
    return None


class Server:
    def __init__(self, data_dir: Path, env: dict[str, str] | None = None) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.log = (data_dir / "server.log").open("w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", str(ROOT), "--port", str(self.port), "--log-level", "warning"],
            cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT,
            env={**os.environ, "SYNTROPY_DATA_DIR": str(data_dir), "SYNTROPY_SCHEDULER": "false", "SYNTROPY_MDNS": "0",
                 "SYNTROPY_ALLOW_NO_PASSWORD": "1", **(env or {})})
        for _ in range(150):
            try:
                if httpx.get(f"{self.url}/api/status", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"The server didn't start: {(data_dir / 'server.log').read_text()[-2000:]}")

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.url, headers=CSRF, timeout=30)


def seed(server: Server, *, simulated: bool = True) -> httpx.Client:
    """A person with a little of everything, so the pages have something to draw."""
    c = server.client()
    r = c.post("/api/setup", json={"password": PASSWORD, "profile_name": "Alex", "accept_terms": True, "timezone": "America/New_York"})
    assert r.status_code == 200, r.text
    assert c.post("/api/onboarding/done").status_code == 200
    if simulated:
        assert c.put("/api/settings/developer", json={"enabled": True}).status_code == 200
        assert c.post("/api/connections/wearable", json={"provider": "oura", "mode": "simulated"}).status_code == 200
        for _ in range(150):
            if c.get("/api/biometrics/metrics").json().get("metrics"):
                break
            time.sleep(0.2)
        assert c.put("/api/settings/developer", json={"enabled": False}).status_code == 200
    r = c.post("/api/labs", json={"collected": "2026-01-15", "results": [{"test": "Glucose", "value": 92, "unit": "mg/dL", "ref_low": 70, "ref_high": 99}]})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as p:
        try:
            chrome = p.chromium.launch(channel="chrome", headless=True)
        except sync_api.Error as exc:
            pytest.skip(f"No Chrome here: {exc}")
        yield chrome
        chrome.close()


@pytest.fixture(scope="module")
def server(tmp_path_factory) -> Iterator[Server]:
    s = Server(tmp_path_factory.mktemp("e2e"))
    seed(s).close()
    yield s
    s.stop()


@pytest.fixture
def fresh_server(tmp_path) -> Iterator[Server]:
    s = Server(tmp_path)
    yield s
    s.stop()


class Session:
    """A browser page that keeps what went wrong: uncaught errors, console errors, and anything but this server."""

    def __init__(self, browser: Any, server: Server, width: str = "desktop", dark: bool = False, sign_in: bool = True, **context: Any) -> None:
        w, h = WIDTHS[width]
        self.server = server
        self.errors: list[str] = []
        self.ctx = browser.new_context(viewport={"width": w, "height": h}, color_scheme="dark" if dark else "light",
                                       service_workers="block", **context)
        self.ctx.route(lambda url: not url.startswith(server.url),
                       lambda route: (self.errors.append(f"left the server: {route.request.url}"), route.abort()))
        self.page = self.ctx.new_page()
        self.page.on("pageerror", lambda e: self.errors.append(f"page error: {e}"))
        self.page.on("console", lambda m: self.errors.append(f"console: {m.text}") if m.type == "error" else None)
        self.page.set_default_timeout(10000)
        if sign_in:
            r = self.ctx.request.post(f"{server.url}/api/auth/login", data={"password": PASSWORD}, headers=CSRF)
            assert r.ok, r.text()

    def open(self, hash_: str = "#/overview") -> Any:
        self.page.goto(f"{self.server.url}/{hash_}")
        self.settle()
        return self.page

    def settle(self) -> None:
        self.page.wait_for_load_state("networkidle")

    def close(self) -> None:
        self.ctx.close()


@pytest.fixture
def session(browser, server) -> Iterator[Session]:
    s = Session(browser, server)
    yield s
    s.close()
