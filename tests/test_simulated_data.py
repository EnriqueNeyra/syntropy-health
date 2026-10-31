"""Simulated data is for testing the app: never offered otherwise, and never shown as if it were measured."""
from __future__ import annotations

from datetime import date, timedelta

from app.core.db import db
from app.store import biometrics


def _daily(conn, profile: str, metric: str, day: str, source: str, value: float) -> None:
    conn.execute("INSERT INTO biometric_daily (profile_id, metric_type, day, source_name, value, sample_count) VALUES (?, ?, ?, ?, ?, 1)",
                 (profile, metric, day, source, value))


def _profile(client) -> str:
    return client.get("/api/profiles").json()["profiles"][0]["id"]


def test_simulated_data_is_refused_outside_developer_mode(client):
    assert client.put("/api/settings/developer", json={"enabled": False}).status_code == 200
    r = client.post("/api/connections/wearable", json={"provider": "oura", "mode": "simulated"})
    assert r.status_code == 400
    assert "developer mode" in r.json()["detail"]
    assert client.get("/api/connections").json()["connections"] == []


def test_status_says_whether_developer_mode_is_on(client):
    assert client.get("/api/status").json()["developer_mode"] is True
    client.put("/api/settings/developer", json={"enabled": False})
    assert client.get("/api/status").json()["developer_mode"] is False


def test_simulated_samples_dont_fill_gaps_beside_a_real_source(client):
    profile = _profile(client)
    today = date.today()
    days = [(today - timedelta(days=i)).isoformat() for i in range(6)]
    with db() as conn:
        for d in days:
            _daily(conn, profile, "resting_heart_rate", d, "Oura Ring (simulated)", 50)
        _daily(conn, profile, "resting_heart_rate", days[3], "Apple Watch", 62)
        _daily(conn, profile, "heart_rate_variability", days[0], "Oura Ring (simulated)", 40)
    series = biometrics.daily_series(profile, "resting_heart_rate", days=10)
    assert [(p["day"], p["value"]) for p in series["points"]] == [(days[3], 62)]
    # Asked for by name, it's still there to look at and remove.
    assert len(biometrics.daily_series(profile, "resting_heart_rate", days=10, source="Oura Ring (simulated)")["points"]) == 6
    # No real source of this one: it stays, under the app's "Includes simulated data" note.
    assert len(biometrics.daily_series(profile, "heart_rate_variability", days=10)["points"]) == 1
    found = {m["metric"]: m for m in biometrics.available_metrics(profile)}
    assert found["resting_heart_rate"]["days"] == 1
    assert found["resting_heart_rate"]["latest_day"] == days[3]


def test_the_app_can_tell_when_a_record_holds_simulated_data(client):
    assert client.get("/api/simulated-data").json() == {"simulated": False}
    assert client.post("/api/connections/wearable", json={"provider": "oura", "mode": "simulated"}).status_code == 200
    assert client.get("/api/simulated-data").json() == {"simulated": True}
