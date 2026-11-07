"""In a phone's Syntropy tab, signed in with the phone's token, what can't be undone asks for the password first."""
from __future__ import annotations

import httpx

from tests.e2e.conftest import CSRF, PASSWORD, Session, seed


def _phone_tab(browser, server, client, profile_id=None) -> Session:
    code = client.post("/api/devices/pairing-code", json={"profile_id": profile_id}).json()["code"]
    with httpx.Client(base_url=server.url) as phone:
        paired = phone.post("/api/devices/pair", json={"code": code, "device_name": "Phone"}).json()
        cookie = phone.post("/api/devices/web-session", headers={"X-Syntropy-Device-Token": paired["device_token"]}).json()["cookie"]
    s = Session(browser, server, width="phone", sign_in=False)
    s.ctx.add_cookies([{"name": cookie["name"], "value": cookie["value"], "url": server.url}])
    s.signed_in = True
    return s


def test_removing_someone_from_a_phone_asks_for_the_password(browser, fresh_server):
    c = seed(fresh_server, simulated=False)
    kid = c.post("/api/profiles", json={"name": "Robin", "relationship": "child"}).json()["id"]
    s = _phone_tab(browser, fresh_server, c)
    page = s.open("#/settings/people")
    page.click(f"[data-action=delete-profile][data-id={kid}]")
    page.fill("#ask-pass", "not my password")
    page.click(".modal [data-yes]")
    page.wait_for_selector("#toasts .toast")
    assert "incorrect" in page.locator("#toasts").inner_text()
    assert any(p["id"] == kid for p in c.get("/api/profiles").json()["profiles"])

    page.click(f"[data-action=delete-profile][data-id={kid}]")
    page.fill("#ask-pass", PASSWORD)
    page.click(".modal [data-yes]")
    page.wait_for_selector(f"[data-action=delete-profile][data-id={kid}]", state="detached")
    assert not any(p["id"] == kid for p in c.get("/api/profiles").json()["profiles"])
    assert not [e for e in s.errors if "403" not in e]
    s.close()
    c.close()


def test_deleting_data_from_a_phone_asks_for_the_password(browser, fresh_server):
    c = seed(fresh_server, simulated=False)
    s = _phone_tab(browser, fresh_server, c)
    page = s.open("#/settings/data")
    page.click("[data-action=wipe]")
    page.fill("#wipe-confirm", "DELETE")
    page.click(".modal [data-yes]")
    page.wait_for_selector("#toasts .toast")
    assert "password" in page.locator("#toasts").inner_text().lower()
    page.fill("#wipe-pass", PASSWORD)
    page.click(".modal [data-yes]")
    page.wait_for_selector(".modal", state="detached")
    assert "Deleted" in page.locator("#toasts").inner_text()
    # A browser, signed in with the password, isn't asked.
    assert c.get("/api/status", headers=CSRF).json()["account"]["confirms_with_password"] is False
    s.close()
    c.close()


def test_scheduled_backups_from_a_phone_ask_for_the_password(browser, fresh_server, tmp_path):
    c = seed(fresh_server, simulated=False)
    folder = tmp_path / "drive"
    folder.mkdir()
    s = _phone_tab(browser, fresh_server, c)
    page = s.open("#/settings/data")
    page.click("[data-action=schedule]")
    page.fill("#sc-folder", str(folder))
    page.fill("#bk-pass", "a long backup passphrase")
    page.fill("#bk-pass2", "a long backup passphrase")
    page.fill("#sc-password", PASSWORD)
    page.click(".modal [data-save]")
    page.wait_for_selector(".modal", state="detached", timeout=60000)
    assert "a backup was made" in page.locator("#toasts").inner_text()
    assert list(folder.glob("*.syntropy"))
    s.close()
    c.close()


def test_a_childs_phone_isnt_offered_the_parents_choices(browser, fresh_server):
    c = seed(fresh_server, simulated=False)
    kid = c.post("/api/profiles", json={"name": "Robin", "relationship": "child"}).json()["id"]
    s = _phone_tab(browser, fresh_server, c, kid)
    page = s.open("#/settings/general")
    assert page.locator("[data-action=accent]").count() == 0 and page.locator("[data-action=units]").count() == 0
    assert page.locator("[data-action=onboarding]").count() == 0
    assert not s.errors, s.errors
    s.close()
    c.close()
