"""The Mac window with a real WKWebView (tests/desktop_window_harness.py): what a page from another site can do to it."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

if sys.platform != "darwin":
    pytest.skip("the Mac window", allow_module_level=True)
pytest.importorskip("webview")
pytest.importorskip("WebKit")

HARNESS = Path(__file__).with_name("desktop_window_harness.py")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def seen(tmp_path_factory) -> dict:
    out = tmp_path_factory.mktemp("window") / "result.json"
    proc = subprocess.run([sys.executable, str(HARNESS), str(out), str(_free_port()), str(_free_port())],
                          capture_output=True, text=True, timeout=120, check=False)
    assert out.exists(), f"The window's test run ended early:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(out.read_text())


def test_starts_as_the_app_and_names_another_site(seen):
    assert seen["home"]["away"] is False and seen["home"]["back"] is False
    assert seen["away"]["away"] is True and seen["away"]["title"] == "127.0.0.1" and seen["away"]["back"] is True


def test_a_page_cant_launch_anything_by_script(seen):
    assert seen["opened_by_script"] == []
    assert seen["away_after_scheme_navigation"]["url"].startswith("http://127.0.0.1")


def test_a_persons_click_opens_web_links_only(seen):
    assert seen["opened_after_click"] == ["https://example.test/clicked"]


def test_another_site_and_its_frames_cant_talk_to_the_app(seen):
    assert "SPOOF" not in seen["title_after_messages"]
    assert seen["requests_from_elsewhere"] == []
    assert seen["moved_by_page"] is False


def test_pages_without_an_origin_keep_the_other_site_chrome(seen):
    for page in ("blob", "blank"):
        assert seen[page]["away"] is True and seen[page]["back"] is True, page
        assert seen[page]["title"] != "Syntropy Health", page


def test_menu_commands_from_another_site_go_to_the_app(seen):
    assert seen["menu_from_away"]["away"] is False
    assert seen["menu_from_away"]["url"].endswith("#/overview")


def test_the_apps_own_page_is_home_and_is_listened_to(seen):
    assert seen["own_page"]["away"] is False and seen["own_page"]["back"] is False
    assert len(seen["own_page_requests"]) == 1
    assert seen["back_home"]["away"] is False
    assert seen["home_title"] == "Named by the app"
    assert seen["home_requests"] == ["join"]


def test_a_page_that_wont_load_is_reported(seen):
    assert any("127.0.0.1:9" in failure[0] for failure in seen["failures"])


def test_saving_over_a_file_replaces_it_only_once_the_download_is_complete(seen):
    assert seen["saved_over"] == "NEW-BACKUP"
    assert seen["leftovers_after_saving"] == []
    assert seen["kept_after_failure"] == "NEW-BACKUP"
    assert seen["leftovers_after_failure"] == []
    assert any(r[0] is False for r in seen["reported"])
    assert any(r[0] is True for r in seen["reported"])


def test_pressing_cancel_in_the_save_panel_says_nothing(seen):
    # WebKit reports it as a failed download, which put up a box saying the file wasn't saved.
    assert seen["panel_asked_before_cancel"] == 1
    assert seen["reported_after_cancel"] == []
    assert seen["leftovers_after_cancel"] == []


def test_a_plain_link_to_a_file_sends_the_sign_in_cookie(seen):
    assert seen["plain_link"] == "NEW-BACKUP"
    assert seen["download_without_cookie"] is False
