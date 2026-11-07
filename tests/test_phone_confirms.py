"""A phone's screens open with its token, not a password. Whoever holds the phone holds them, so what can't be undone,
or hands everyone's records to whoever asks, takes the password there too, counted like a sign-in. Browsers and the
desktop apps, signed in with it already, aren't asked again."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core import auth
from app.services import backup_file, backup_schedule, backups, network, updates
from app.store import accounts, biometrics, profiles
from tests.conftest import BASE, CSRF, PASSPHRASE

SECRET = "a long backup passphrase"


@pytest.fixture(autouse=True)
def quick_passphrases(monkeypatch):
    monkeypatch.setitem(backup_file._SCRYPT, "n", 2**10)


def _phone_session(client, address=("192.168.1.40", 51000)):
    """The owner's own phone, its Syntropy tab signed in with the phone's token."""
    code = client.post("/api/devices/pairing-code", json={}).json()["code"]
    phone = TestClient(client.app, base_url=BASE, client=address)
    paired = phone.post("/api/devices/pair", json={"code": code, "device_name": "Phone"}).json()
    cookie = phone.post("/api/devices/web-session", headers={"X-Syntropy-Device-Token": paired["device_token"]}).json()["cookie"]
    screens = TestClient(client.app, base_url=BASE, headers=CSRF, client=address)
    screens.cookies.set(cookie["name"], cookie["value"])
    return screens


def _member(name="Sam"):
    profile = profiles.create_profile(name)["id"]
    return profile, accounts.create(profile, "sams own password")


def test_the_phone_is_told_what_takes_its_password(client):
    assert not client.get("/api/status").json()["account"]["confirms_with_password"]
    assert _phone_session(client).get("/api/status").json()["account"]["confirms_with_password"]


def test_removing_someone_from_a_phone_takes_the_password(client):
    sam_profile, sam = _member()
    phone = _phone_session(client)
    r = phone.delete(f"/api/profiles/{sam_profile}")
    assert r.status_code == 403 and "password" in r.json()["detail"]
    assert phone.request("DELETE", f"/api/profiles/{sam_profile}", json={"password": "not it"}).status_code == 403
    assert accounts.get(sam["id"]) is not None
    assert phone.request("DELETE", f"/api/profiles/{sam_profile}", json={"password": PASSPHRASE}).status_code == 200
    assert accounts.get(sam["id"]) is None


def test_a_browser_removes_someone_as_before(client):
    sam_profile, sam = _member()
    assert client.delete(f"/api/profiles/{sam_profile}").status_code == 200
    assert accounts.get(sam["id"]) is None


def test_wiping_data_from_a_phone_takes_the_password(client):
    phone = _phone_session(client)
    me = phone.get("/api/status").json()["account"]["profile_id"]
    biometrics.insert_samples(me, None, [{"id": "s1", "metric_type": "heart_rate", "value": 60, "unit": "count/min",
                                          "start_date": "2026-01-02T08:00:00Z", "end_date": "2026-01-02T08:00:00Z"}])
    assert phone.delete(f"/api/profiles/{me}/data").status_code == 403
    assert biometrics.has_samples(me)
    assert phone.request("DELETE", f"/api/profiles/{me}/data", json={"password": PASSPHRASE}).status_code == 200
    assert not biometrics.has_samples(me)


def test_a_phone_cant_send_everyones_backups_to_a_folder_of_its_own(client, tmp_path):
    phone = _phone_session(client)
    folder = tmp_path / "theirs"
    folder.mkdir()
    plan = {"folder": str(folder), "every_hours": 24, "keep": 3, "passphrase": "phone holder passphrase"}
    r = phone.put("/api/backups/schedule", json=plan)
    assert r.status_code == 403 and "password" in r.json()["detail"]
    assert not backup_schedule.status()["enabled"]
    assert phone.post("/api/backups/schedule/run").status_code == 403
    assert not list(folder.iterdir())
    # The owner, with their password, can.
    assert phone.put("/api/backups/schedule", json={**plan, "password": PASSPHRASE}).status_code == 200
    assert phone.post("/api/backups/schedule/run", json={"password": PASSPHRASE}).status_code == 200
    assert list(folder.glob("*.syntropy"))


def test_a_browser_sets_up_scheduled_backups_as_before(client, tmp_path):
    folder = tmp_path / "drive"
    folder.mkdir()
    assert client.put("/api/backups/schedule", json={"folder": str(folder), "every_hours": 24, "keep": 3,
                                                     "passphrase": SECRET}).status_code == 200
    assert client.post("/api/backups/schedule/run").status_code == 200
    assert list(folder.glob("*.syntropy"))


def test_restoring_from_a_phone_takes_the_password(client, monkeypatch):
    restored = []
    monkeypatch.setattr(backups, "staged", lambda staged_id: object())
    monkeypatch.setattr(backups, "restore_in_server", lambda app, item, accept: restored.append(item) or
                        {"holds": "", "sign_in": "", "safety_copy": None, "accounts": []})
    phone = _phone_session(client)
    assert phone.post("/api/restore/apply", json={"id": "r1"}).status_code == 403
    assert not restored
    assert phone.post("/api/restore/apply", json={"id": "r1", "password": PASSPHRASE}).status_code == 200
    assert restored
    assert client.post("/api/restore/apply", json={"id": "r1"}).status_code == 200


def test_network_access_and_updates_from_a_phone_take_the_password(client, monkeypatch):
    switched, installed = [], []
    monkeypatch.setattr(network, "managed", lambda: True)
    monkeypatch.setattr(network, "set_on_network", switched.append)
    monkeypatch.setattr(updates, "install", lambda: installed.append(1) or {"latest": {"version": "9.9.9"}})
    phone = _phone_session(client)
    assert phone.put("/api/system/network", json={"enabled": not network.on_network()}).status_code == 403
    assert phone.post("/api/system/updates/install").status_code == 403
    assert not switched and not installed
    assert phone.put("/api/system/network", json={"enabled": not network.on_network(), "password": PASSPHRASE}).status_code == 200
    assert phone.post("/api/system/updates/install", json={"password": PASSPHRASE}).status_code == 200
    assert switched and installed
    # From a browser, as before.
    assert client.post("/api/system/updates/install").status_code == 200


def test_a_phone_cant_guess_the_password_through_the_backup(client):
    phone = _phone_session(client)
    codes = [phone.post("/api/backups", json={"passphrase": SECRET, "password": f"wrong-{i}"}).status_code
             for i in range(auth.LOGIN_MAX_FAILURES + 2)]
    assert codes[:auth.LOGIN_MAX_FAILURES] == [403] * auth.LOGIN_MAX_FAILURES
    assert set(codes[auth.LOGIN_MAX_FAILURES:]) == {429}
    # Even the right one waits now: otherwise the limit would only say which guess was right.
    assert phone.post("/api/backups", json={"passphrase": SECRET, "password": PASSPHRASE}).status_code == 429


def test_the_right_password_makes_a_backup_from_a_phone(client):
    phone = _phone_session(client)
    assert phone.post("/api/backups", json={"passphrase": SECRET}).status_code == 403
    r = phone.post("/api/backups", json={"passphrase": SECRET, "password": PASSPHRASE})
    assert r.status_code == 200, r.text


def test_where_sign_ins_go_takes_the_password_from_a_phone(client):
    """A relay of the phone holder's choosing would be handed refresh tokens, and a redirect of theirs the sign-ins."""
    phone = _phone_session(client)
    attempts = [
        ("PUT", "/api/settings/wearables/oura", {"relay_url": "https://relay.example.net"}),
        ("PUT", "/api/settings/wearables/oura", {"client_id": "theirs", "client_secret": "theirs"}),
        ("PUT", "/api/settings/platforms/epic", {"production_client_id": "theirs"}),
        ("PUT", "/api/settings/developer", {"enabled": True}),
        ("PUT", "/api/settings/general", {"redirect_uri": "https://example.net/callback"}),
    ]
    for method, path, body in attempts:
        r = phone.request(method, path, json=body)
        assert r.status_code == 403 and "password" in r.json()["detail"], (path, r.status_code, r.text)
        assert phone.request(method, path, json={**body, "password": "not it"}).status_code == 403, path
    snapshot = client.get("/api/settings").json()
    assert not snapshot["wearables"]["oura"].get("relay_url")      # still the built-in relay: nothing of theirs was saved
    assert client.put("/api/settings/developer", json={"enabled": False}).status_code == 200     # a browser, as before
    r = phone.request("PUT", "/api/settings/developer", json={"enabled": True, "password": PASSPHRASE})
    assert r.status_code == 200, r.text
    assert phone.put("/api/settings/general", json={"units": "us"}).status_code == 200      # one's own units need nothing


def test_inviting_someone_from_a_phone_takes_the_password(client):
    sam = profiles.create_profile("Sam")["id"]
    phone = _phone_session(client)
    r = phone.post(f"/api/profiles/{sam}/invite")
    assert r.status_code == 403 and "password" in r.json()["detail"]
    assert phone.post(f"/api/profiles/{sam}/invite", json={"password": PASSPHRASE}).status_code == 200
    assert client.post(f"/api/profiles/{sam}/invite").status_code == 200       # a browser, as before
