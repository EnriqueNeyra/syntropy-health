"""Phone sources across re-pairing, Overview layouts, display units and the password endpoints."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import BASE, CSRF, PASSPHRASE


def _pair(client, name="iPhone"):
    code = client.post("/api/devices/pairing-code", json={}).json()["code"]
    phone = TestClient(client.app, base_url=BASE)
    paired = phone.post("/api/devices/pair", json={"code": code, "device_name": name}).json()
    return phone, paired, {"X-Syntropy-Device-Token": paired["device_token"]}


def _batch(ids):
    t = datetime.now(timezone.utc).replace(hour=15, minute=0, second=0, microsecond=0)
    return {"device_id": "ios", "device_name": "iPhone", "sync_trigger": "manual",
            "samples": [{"id": f"s-{i}", "metric_type": "step_count", "hk_identifier": "HK", "value": 100, "unit": "count",
                         "start_date": (t - timedelta(hours=i)).isoformat(), "end_date": (t - timedelta(hours=i) + timedelta(minutes=5)).isoformat(),
                         "source_name": "Apple Watch"} for i in ids]}


def _phones(client):
    return [c for c in client.get("/api/connections", params={"include_disconnected": True}).json()["connections"] if c["kind"] == "device"]


def test_pairing_the_same_phone_again_reuses_its_source(client):
    phone, first, device = _pair(client)
    assert phone.post("/api/ingest/wearables", json=_batch([1, 2]), headers=device).json()["inserted"] == 2

    # Unpairing forgets the token but keeps the source and its data, shown as not paired.
    assert client.delete(f"/api/devices/{first['device_id']}").status_code == 200
    assert client.get("/api/devices").json()["devices"] == []
    [source] = _phones(client)
    assert source["status"] == "disconnected" and source["sample_count"] == 2

    # The same phone pairs again (e.g. after reinstalling the app): same source, now active, data together.
    phone, second, device = _pair(client)
    assert second["connection_id"] == first["connection_id"]
    phone.post("/api/ingest/wearables", json=_batch([2, 3]), headers=device)
    [source] = _phones(client)
    assert source["status"] == "active" and source["sample_count"] == 3
    assert [d["id"] for d in client.get("/api/devices").json()["devices"]] == [second["device_id"]]

    # A differently named phone gets its own source.
    _pair(client, "Work iPhone")
    assert len(_phones(client)) == 2


def test_removing_a_phone_source_unpairs_the_phone(client):
    phone, paired, device = _pair(client)
    phone.post("/api/ingest/wearables", json=_batch([1]), headers=device)
    assert client.delete(f"/api/connections/{paired['connection_id']}").status_code == 200   # keep data
    assert phone.post("/api/ingest/wearables", json=_batch([2]), headers=device).status_code == 401
    assert client.get("/api/devices").json()["devices"] == []

    phone, paired, device = _pair(client)                                                       # picks the source up again
    assert client.delete(f"/api/connections/{paired['connection_id']}", params={"delete_data": True}).status_code == 200
    assert _phones(client) == []
    assert client.get("/api/biometrics/overview").json()["total_samples"] == 0


def test_deleting_a_persons_data_signs_out_their_phones(client):
    phone, paired, device = _pair(client)
    cookie = TestClient(client.app, base_url=BASE).post("/api/devices/web-session", headers=device).json()["cookie"]["value"]
    from app.core import auth
    assert auth.session_valid(cookie)
    profile = client.get("/api/profiles").json()["profiles"][0]["id"]
    assert client.delete(f"/api/profiles/{profile}/data").status_code == 200
    assert not auth.session_valid(cookie)


def test_device_source_migration_folds_duplicates(client):
    """Migration 006 merges the copies earlier versions made each time a phone paired, and drops revoked tokens."""
    from app.core.db import _split_sql, db
    phone, a, device = _pair(client)
    phone.post("/api/ingest/wearables", json=_batch([1, 2]), headers=device)
    # Recreate the old behaviour: a revoked token on the old source, and a second source with the same name.
    with db() as conn:
        conn.execute("UPDATE devices SET revoked_at = 1 WHERE id = ?", (a["device_id"],))
        conn.execute("UPDATE connections SET status = 'disconnected' WHERE id = ?", (a["connection_id"],))
        conn.execute("INSERT INTO connections(id, profile_id, kind, provider, display_name, mode, status, created_at, updated_at) "
                     "SELECT 'con_new', profile_id, kind, provider, display_name, mode, 'active', created_at + 10, updated_at FROM connections WHERE id = ?",
                     (a["connection_id"],))
        conn.execute("INSERT INTO devices(id, profile_id, connection_id, name, platform, token_hash, created_at) "
                     "SELECT 'dev_new', profile_id, 'con_new', name, platform, 'hash-new', created_at + 10 FROM devices WHERE id = ?", (a["device_id"],))
        conn.execute("UPDATE biometric_samples SET connection_id = 'con_new' WHERE id = 's-2'")
        for statement in _split_sql((Path("app/core/migrations") / "006_device_sources.sql").read_text()):
            conn.execute(statement)
    from app.store import sample_counts
    sample_counts.forget()   # the app runs migrations at startup, before anything is counted
    [source] = _phones(client)
    assert source["id"] == "con_new" and source["status"] == "active" and source["sample_count"] == 2
    assert [d["id"] for d in client.get("/api/devices").json()["devices"]] == ["dev_new"]


def test_health_connect_sources_rank_like_their_devices():
    """The Android app names a sample's source by the app that wrote it to Health Connect."""
    from app.store.biometrics import source_kind
    assert source_kind("Fitbit") == "ring"             # Fitbit and Pixel Watch, as through Google Health
    assert source_kind("Samsung Health") == "watch"    # Galaxy Watch
    assert source_kind("Oura") == "ring"
    assert source_kind("Strava") == "other"


def test_overview_layout_is_saved_per_person(client):
    profile = client.get("/api/profiles").json()["profiles"][0]["id"]
    assert client.get(f"/api/profiles/{profile}/overview").json() == {"widgets": None}
    layout = [{"id": "sleep", "visible": True}, {"id": "today", "visible": True, "options": {"metrics": ["step_count"]}},
              {"id": "labs", "visible": False}]
    saved = client.put(f"/api/profiles/{profile}/overview", json={"widgets": layout}).json()["widgets"]
    assert [w["id"] for w in saved] == ["sleep", "today", "labs"] and saved[2]["visible"] is False
    assert client.get(f"/api/profiles/{profile}/overview").json()["widgets"][1]["options"] == {"metrics": ["step_count"]}
    other = client.post("/api/profiles", json={"name": "Sam"}).json()["id"]
    assert client.get(f"/api/profiles/{other}/overview").json() == {"widgets": None}
    assert client.delete(f"/api/profiles/{profile}/overview").json() == {"widgets": None}
    assert client.get("/api/profiles/nope/overview").status_code == 404
    assert client.put(f"/api/profiles/{profile}/overview", json={"widgets": [{"id": "x" * 61}]}).status_code == 422
    # Cards and single numbers each have a place and a size.
    sized = [{"id": "m:step_count", "size": "s"}, {"id": "sleep", "size": "xl"}, {"id": "labs", "visible": False}]
    assert client.put(f"/api/profiles/{profile}/overview", json={"widgets": sized}).json()["widgets"] == [
        {"id": "m:step_count", "visible": True, "size": "s", "options": {}}, {"id": "sleep", "visible": True, "size": "xl", "options": {}},
        {"id": "labs", "visible": False, "options": {}}]
    assert client.put(f"/api/profiles/{profile}/overview", json={"widgets": [{"id": "sleep", "size": "huge"}]}).status_code == 422


def test_overview_tiles_follow_the_chosen_metrics(client):
    client.post("/api/connections/wearable", json={"provider": "oura", "mode": "simulated"})
    default = client.get("/api/biometrics/overview", params={"brief": True}).json()["headline"]
    assert len(default) > 2
    chosen = client.get("/api/biometrics/overview", params={"brief": True, "tiles": "resting_heart_rate,step_count,nope"}).json()
    assert [m["metric"] for m in chosen["headline"]] == ["resting_heart_rate", "step_count"]


def test_units_preference(client):
    assert client.get("/api/status").json()["preferences"] == {"units": None, "accent": "heart"}
    assert client.put("/api/settings/general", json={"units": "us"}).json()["units"] == "us"
    assert client.get("/api/status").json()["preferences"] == {"units": "us", "accent": "heart"}
    assert client.put("/api/settings/general", json={"units": "imperial"}).status_code == 400
    assert client.put("/api/settings/general", json={"units": ""}).json()["units"] is None


def test_accent_is_shared_with_paired_devices(client):
    assert client.put("/api/preferences", json={"accent": "teal"}).json()["accent"] == "teal"
    assert client.put("/api/preferences", json={"accent": "chartreuse"}).status_code == 400
    code = client.post("/api/devices/pairing-code", json={}).json()["code"]
    token = client.post("/api/devices/pair", json={"code": code, "device_name": "Phone"}).json()["device_token"]
    client.cookies.clear()
    phone = {"X-Syntropy-Device-Token": token}
    assert client.get("/api/preferences", headers=phone).json()["accent"] == "teal"
    assert client.put("/api/preferences", json={"accent": "heart"}, headers=phone).json()["accent"] == "heart"
    assert client.get("/api/preferences").status_code == 401


def test_password_endpoints_accept_new_and_old_names(raw_client):
    from app.core import auth
    auth._failed_logins.clear()      # other tests' failed attempts count against the same test client address
    raw_client.post("/api/setup", json={"password": PASSPHRASE, "profile_name": "Alex"}, headers=CSRF)
    raw_client.post("/api/auth/logout", headers=CSRF)
    wrong = raw_client.post("/api/auth/login", json={"password": "nope"}, headers=CSRF)
    assert wrong.status_code == 401 and wrong.json()["detail"] == "Incorrect password."
    assert raw_client.post("/api/auth/login", json={"password": PASSPHRASE}, headers=CSRF).status_code == 200
    raw_client.post("/api/auth/logout", headers=CSRF)
    assert raw_client.post("/api/auth/login", json={"passphrase": PASSPHRASE}, headers=CSRF).status_code == 200   # older app
    changed = raw_client.post("/api/auth/password", json={"current": PASSPHRASE, "new": "another password"}, headers=CSRF)
    assert changed.status_code == 200
    bad = raw_client.post("/api/auth/password", json={"current": "wrong", "new": "x" * 9}, headers=CSRF)
    assert bad.json()["detail"] == "Current password is incorrect."


def test_time_zone_list_includes_utc_and_the_current_setting(client, monkeypatch):
    zones = client.get("/api/settings/timezones").json()["timezones"]
    assert zones[0] == "UTC" and zones.count("UTC") == 1
    assert "America/New_York" in zones
    # An older name set in the environment still shows as the choice in use.
    monkeypatch.setenv("TZ", "Etc/GMT+5")
    assert "Etc/GMT+5" in client.get("/api/settings/timezones").json()["timezones"]
