"""The iPhone app's Syntropy tab: it signs in to the web app with its device token, never the passphrase."""

from fastapi.testclient import TestClient

from app.core import auth
from tests.conftest import BASE


def _pair(client, name="Phone"):
    code = client.post("/api/devices/pairing-code", json={}).json()["code"]
    phone = TestClient(client.app, base_url=BASE)
    paired = phone.post("/api/devices/pair", json={"code": code, "device_name": name}).json()
    return paired, {"X-Syntropy-Device-Token": paired["device_token"]}


def test_paired_app_opens_a_web_session(client):
    paired, device = _pair(client)
    browser = TestClient(client.app, base_url=BASE)
    assert browser.get("/api/profiles").status_code == 401            # the web app needs a session...
    cookie = browser.post("/api/devices/web-session", headers=device).json()["cookie"]
    assert cookie["name"] == auth.SESSION_COOKIE and cookie["max_age"] > 0
    browser.cookies.set(cookie["name"], cookie["value"])
    assert browser.get("/api/profiles").status_code == 200            # ...which the device token provides

    # Each device keeps one session: opening another ends the previous one.
    again = browser.post("/api/devices/web-session", headers=device).json()["cookie"]["value"]
    assert not auth.session_valid(cookie["value"]) and auth.session_valid(again)

    # Revoking the device signs its web view out.
    assert client.delete(f"/api/devices/{paired['device_id']}").status_code == 200
    assert not auth.session_valid(again)
    assert browser.post("/api/devices/web-session", headers=device).status_code == 401


def test_only_devices_can_open_app_sessions(client):
    # A logged-in browser can't mint extra sessions this way, and an unknown token gets nothing.
    assert client.post("/api/devices/web-session").status_code == 403
    stranger = TestClient(client.app, base_url=BASE)
    assert stranger.post("/api/devices/web-session", headers={"X-Syntropy-Device-Token": "nope"}).status_code == 401


def test_disconnecting_app_revokes_itself(client):
    paired, device = _pair(client)
    phone = TestClient(client.app, base_url=BASE)
    assert phone.post("/api/devices/self/revoke", headers=device).status_code == 200
    assert phone.post("/api/devices/web-session", headers=device).status_code == 401
    assert paired["device_id"] not in [d["id"] for d in client.get("/api/devices").json()["devices"] if not d.get("revoked_at")]
