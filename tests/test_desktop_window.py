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
    return {**json.loads(out.read_text()), "exit_code": proc.returncode, "log": proc.stderr}


def story(seen) -> str:
    """For a failure: what happened when, and what the window logged."""
    return "\n".join([*(json.dumps(step) for step in seen["trace"]), seen["log"][-4000:]])


def test_starts_as_the_app_and_names_another_site(seen):
    assert seen["home"]["away"] is False and seen["home"]["back"] is False
    assert seen["away"]["away"] is True and seen["away"]["title"] == "127.0.0.1" and seen["away"]["back"] is True, story(seen)


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
    assert any("127.0.0.1:9" in failure[0] for failure in seen["failures"]), story(seen)


def test_saving_over_a_file_replaces_it_only_once_the_download_is_complete(seen):
    assert seen["saved_over"] == "NEW-BACKUP", story(seen)
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
    assert seen["plain_link"] == "NEW-BACKUP", story(seen)
    assert seen["download_without_cookie"] is False


# ---------------------------------------------------------------------------
# The sign-in sheet
# ---------------------------------------------------------------------------

def test_another_site_opens_in_a_sheet_and_the_window_stays_as_it_was(seen):
    sheet = seen["sheet"]
    assert sheet["attached"] is True
    assert sheet["open"]["name"] == "127.0.0.1" and sheet["open"]["covered"] is False
    assert sheet["main_url_while_open"].endswith("/page")
    assert sheet["window_unchanged"] is True and sheet["not_away"] is True


def test_the_site_sees_the_same_browser_in_the_sheet(seen):
    assert seen["sheet"]["user_agent_same"] is True


def test_a_page_in_the_sheet_cant_reach_the_app(seen):
    sheet = seen["sheet"]
    assert json.loads(sheet["handlers"]) == {"syntropy": "undefined", "bridge": "undefined", "browser": "undefined", "pywebview": "undefined"}
    assert "SPOOF" not in sheet["title_after_trying"]
    assert sheet["requests_after_trying"] == 0


def test_a_page_in_the_sheet_cant_open_another_program(seen):
    sheet = seen["sheet"]
    assert sheet["opened_by_script"] == []
    assert sheet["still_there"] is True
    assert sheet["no_box_for_a_scripted_refusal"] == 0


def test_mail_leaves_the_sheet_only_for_a_click(seen):
    assert seen["sheet"]["mail_without_a_click"] == []
    assert seen["sheet"]["mail_with_a_click"] == ["mailto:help@example.test"]


def test_a_new_window_stays_in_the_sheet_and_back_returns(seen):
    sheet = seen["sheet"]
    assert sheet["new_window_stays_in_sheet"] is True and sheet["new_window_didnt_leave"] == []
    assert sheet["windows_after_new_window"] is True
    assert sheet["back_button"] is True


def test_alert_confirm_and_prompt_work_and_name_the_site(seen):
    sheet = seen["sheet"]
    assert sheet["alert_says"] == ["127.0.0.1 says", "hello"]
    assert sheet["alert_answered"] == "alerted"
    assert sheet["confirm_and_prompt"] == "alerted,false,true,Alex"


def test_files_arent_downloaded_in_the_sheet(seen):
    sheet = seen["sheet"]
    assert sheet["file_save_panels"] == 0
    assert sheet["file_by_script"] == 0 and sheet["file_by_click"] == 1
    assert sheet["after_file"] is True


def test_the_bar_names_where_the_page_is_not_where_its_going(seen):
    assert seen["sheet"]["bar_while_going_elsewhere"] == "127.0.0.1"
    assert seen["sheet"]["bar_once_there"] == "localhost"


def test_an_address_that_only_looks_like_the_apps_ends_nothing(seen):
    assert seen["sheet"]["lookalike_keeps_sheet"] is True
    assert "settings" not in seen["sheet"]["lookalike_main_url"]


def test_the_servers_own_pages_show_only_when_theres_something_to_read(seen):
    assert seen["sheet"]["ask_page"]["covered"] is False and seen["sheet"]["ask_page"]["name"] == "Syntropy Health"
    assert seen["sheet"]["ask_page"]["lock"] is False
    assert seen["sheet"]["relay_page"]["covered"] is True and seen["sheet"]["relay_page"]["words"] == "Finishing…"


def test_cancel_stays_where_you_were_and_tells_the_page(seen):
    after = seen["sheet"]["after_cancel"]
    assert after["url"].endswith("/page") and after["stayed"] is True and after["closes"] == 1
    assert after["window_unchanged"] is True and after["attached"] is False and after["history"] == 0


def test_finishing_goes_to_the_address_without_a_reload(seen):
    after = seen["sheet"]["after_finish"]
    assert after["url"].endswith("/page#/sources?connected=abc") and after["stayed"] is True
    assert after["not_away"] is True and after["window_unchanged"] is True


def test_a_failed_callback_reaches_the_page_the_same_way(seen):
    after = seen["sheet"]["after_problem"]
    assert after["url"].endswith("/page#/sources?problem=xyz") and after["stayed"] is True


def test_the_window_commands_end_the_sheet(seen):
    assert seen["sheet"]["close_command"] is True
    assert seen["sheet"]["menu_over_a_dialog"].endswith("#/overview")
    assert seen["sheet"]["home_command"] is True


def test_a_finished_sheet_lets_go_of_its_web_view(seen):
    assert seen["sheet"]["let_go"] is True


def test_with_no_window_on_screen_the_away_chrome_is_still_there(seen):
    assert seen["sheet"]["no_window_on_screen"] == {"away": True, "sheet": True}


def test_a_sheet_doesnt_stop_the_app_quitting(seen):
    assert seen["exit_code"] == 0
