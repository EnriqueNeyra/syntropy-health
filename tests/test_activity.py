"""Workouts, health events and HealthKit export JSON ingest.

The fixtures in tests/fixtures are written by the iPhone app's own Swift code
(SyntropyCore in the separate iOS repo, ``FixtureExport`` test), so these tests pin the wire format end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import BASE

FIXTURES = Path(__file__).parent / "fixtures"
DAYS = 3650


def _pair(client) -> tuple[TestClient, dict]:
    code = client.post("/api/devices/pairing-code", json={}).json()["code"]
    phone = TestClient(client.app, base_url=BASE)
    token = phone.post("/api/devices/pair", json={"code": code, "device_name": "Alex's iPhone"}).json()["device_token"]
    return phone, {"X-Syntropy-Device-Token": token}


def _daily(client, metric: str) -> list[dict]:
    return client.get("/api/biometrics/daily", params={"metric": metric, "days": DAYS}).json()["points"]


def test_ios_batch_with_workouts_and_events(client):
    phone, headers = _pair(client)
    batch = json.loads((FIXTURES / "ios_ingest_batch.json").read_text())
    r = phone.post("/api/ingest/wearables", json=batch, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inserted"] == len(batch["samples"])
    assert body["workouts_inserted"] == 1
    assert body["events_inserted"] == len(batch["events"]) == 5

    again = phone.post("/api/ingest/wearables", json=batch, headers=headers).json()
    assert again["inserted"] == 0 and again["workouts_inserted"] == 0 and again["events_inserted"] == 0

    # Samples: steps and sleep roll up; blood oxygen etc. keep their units.
    assert _daily(client, "step_count")[-1]["value"] == 1234
    sleep = _daily(client, "sleep_core")
    assert sleep and sleep[-1]["value"] == 7.0 and sleep[-1]["day"] == "2024-05-15"

    listing = client.get("/api/biometrics/workouts", params={"days": 0}).json()
    assert listing["summary"]["count"] == 1
    w = listing["workouts"][0]
    assert w["name"] == "Running" and w["distance_m"] == 5000 and w["has_route"] is True and w["indoor"] is False
    detail = client.get(f"/api/biometrics/workouts/{w['id']}").json()
    assert len(detail["route"]) == 2 and detail["route"][0]["lat"] == 37.3349
    assert detail["heart_rate"][0]["avg"] == 145
    assert client.get("/api/biometrics/workouts/nope").status_code == 404

    events = client.get("/api/biometrics/events", params={"days": 0}).json()
    types = {e["event_type"]: e for e in events["events"]}
    assert set(types) == {"headache", "mindful_session", "high_heart_rate_event", "ecg", "state_of_mind"}
    assert types["headache"]["value_label"] == "Moderate" and types["headache"]["category"] == "symptoms"
    assert types["ecg"]["value_label"] == "Sinus Rhythm"
    assert types["high_heart_rate_event"]["metadata"]["threshold"] == "120"
    only_symptoms = client.get("/api/biometrics/events", params={"days": 0, "category": "symptoms"}).json()["events"]
    assert [e["event_type"] for e in only_symptoms] == ["headache"]

    overview = client.get("/api/biometrics/overview").json()
    assert overview["activity"] == {"workouts": 1, "events": 5}

    # Removing the device connection with its data removes workouts and events too.
    conn_id = [c for c in client.get("/api/connections").json()["connections"] if c["kind"] == "device"][0]["id"]
    assert client.delete(f"/api/connections/{conn_id}", params={"delete_data": True}).status_code == 200
    assert client.get("/api/biometrics/overview").json()["activity"] == {"workouts": 0, "events": 0}


def test_healthkit_export_json_from_ios_app(client):
    phone, headers = _pair(client)
    payload = json.loads((FIXTURES / "ios_healthkit_export.json").read_text())
    assert phone.post("/api/ingest/healthkit-export", json=payload).status_code == 401
    r = phone.post("/api/ingest/healthkit-export", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workouts_inserted"] == 1
    assert body["events_inserted"] == 5  # symptom, HR notification, ECG, mood, mindful session
    assert _daily(client, "step_count")[-1]["value"] == 1234
    assert _daily(client, "heart_rate")[-1]["value"] == 72
    assert _daily(client, "blood_pressure_systolic")[-1]["value"] == 120
    assert _daily(client, "sleep_core")[-1]["value"] == 7.0
    events = {e["event_type"] for e in client.get("/api/biometrics/events", params={"days": 0}).json()["events"]}
    assert {"headache", "high_heart_rate_event", "ecg", "state_of_mind", "mindful_session"} <= events
    w = client.get("/api/biometrics/workouts", params={"days": 0}).json()["workouts"][0]
    assert w["distance_m"] == 5000 and w["avg_hr"] == 150

    # Re-sending is idempotent.
    again = phone.post("/api/ingest/healthkit-export", json=payload, headers=headers).json()
    assert again["inserted"] == 0 and again["workouts_inserted"] == 0 and again["events_inserted"] == 0


def test_healthkit_export_units_and_aggregated_sleep(client):
    """Payload from a HealthKit export app's REST automation, with imperial units."""
    payload = {"data": {
        "metrics": [
            {"name": "weight_body_mass", "units": "lb", "data": [{"date": "2024-06-01 07:00:00 -0700", "qty": 165.3, "source": "Scale"}]},
            {"name": "blood_oxygen_saturation", "units": "%", "data": [{"date": "2024-06-01 03:00:00 -0700", "qty": 96, "source": "Watch"}]},
            {"name": "walking_asymmetry_percentage", "units": "%", "data": [{"date": "2024-06-01 12:00:00 -0700", "qty": 0.5, "source": "iPhone"}]},
            {"name": "walking_running_distance", "units": "mi", "data": [{"date": "2024-06-01 00:00:00 -0700", "qty": 3.1, "source": "Watch"}]},
            {"name": "some_future_metric", "units": "count", "data": [{"date": "2024-06-01 00:00:00 -0700", "qty": 4}]},
            {"name": "sleep_analysis", "units": "hr", "data": [{
                "date": "2024-06-02", "totalSleep": 7.5, "asleep": 0, "core": 4, "deep": 1.5, "rem": 2, "awake": 0.4,
                "inBed": 8, "sleepStart": "2024-06-01 23:00:00 -0700", "sleepEnd": "2024-06-02 06:54:00 -0700", "source": "Watch"}]},
        ],
        "workouts": [{
            "name": "Outdoor Walk", "start": "2024-06-01 18:00:00 -0700", "end": "2024-06-01 18:45:00 -0700",
            "distance": {"qty": 2, "units": "mi"}, "activeEnergyBurned": {"qty": 836.8, "units": "kJ"},
            "heartRate": {"avg": {"qty": 110, "units": "bpm"}, "max": {"qty": 130, "units": "bpm"}, "min": {"qty": 90, "units": "bpm"}},
            "temperature": {"qty": 68, "units": "degF"}, "location": "Outdoor",
        }],
        "symptoms": [{"start": "2024-06-01 09:00:00 -0700", "end": "2024-06-01 10:00:00 -0700", "name": "Fatigue", "severity": "Mild", "source": "Health"}],
        "cycleTracking": [{"start": "2024-06-01 00:00:00 -0700", "end": "2024-06-01 00:00:00 -0700", "name": "Menstrual Flow", "value": "Light", "source": "Health"}],
    }}
    r = client.post("/api/ingest/healthkit-export", json=payload)
    assert r.status_code == 200, r.text
    assert _daily(client, "body_mass")[-1]["value"] == 74.98
    assert _daily(client, "oxygen_saturation")[-1]["value"] == 96
    assert _daily(client, "walking_asymmetry")[-1]["value"] == 0.5
    assert round(_daily(client, "distance_walking_running")[-1]["value"]) == 4989
    night = _daily(client, "sleep_duration")
    assert night[-1]["day"] == "2024-06-02" and night[-1]["value"] == 7.5
    assert _daily(client, "sleep_deep")[-1]["value"] == 1.5

    w = client.get("/api/biometrics/workouts", params={"days": 0}).json()["workouts"][0]
    assert round(w["distance_m"]) == 3219 and round(w["active_energy_kcal"]) == 200 and w["indoor"] is False
    detail = client.get(f"/api/biometrics/workouts/{w['id']}").json()
    assert detail["temperature_c"] == 20.0 and detail["duration_s"] == 2700

    events = {e["event_type"]: e for e in client.get("/api/biometrics/events", params={"days": 0}).json()["events"]}
    assert events["fatigue"]["value_label"] == "Mild" and events["fatigue"]["category"] == "symptoms"
    assert events["menstrual_flow"]["category"] == "cycle"

    samples = client.get("/api/biometrics/samples", params={"metric": "some_future_metric"}).json()["samples"]
    assert samples and samples[0]["value"] == 4  # unknown metrics are kept, just not charted


def test_healthkit_export_rejects_bad_bodies(client):
    assert client.post("/api/ingest/healthkit-export", content=b"not json",
                       headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/api/ingest/healthkit-export", json=[1, 2]).status_code == 400
    empty = client.post("/api/ingest/healthkit-export", json={"data": {}}).json()
    assert empty["received"] == 0 and empty["workouts_received"] == 0


def test_mcp_workouts_and_journal(client):
    from app import mcp_server
    phone, headers = _pair(client)
    phone.post("/api/ingest/wearables", json=json.loads((FIXTURES / "ios_ingest_batch.json").read_text()), headers=headers)

    def call(name, arguments):
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        return json.loads(resp["result"]["content"][0]["text"])

    workouts = call("list_workouts", {"days": 36500})
    assert workouts["summary"]["count"] == 1 and workouts["workouts"][0]["name"] == "Running"
    journal = call("get_health_journal", {"days": 36500, "category": "symptoms"})
    assert [e["event_type"] for e in journal["events"]] == ["headache"]


def test_journal_hides_stand_hours_but_export_keeps_them(client):
    from app.store import activity
    profile = client.get("/api/status").json()["profiles"][0]["id"]
    activity.insert_events(profile, None, [
        {"id": "e1", "event_type": "apple_stand_hour", "category": "activity", "name": "Stand Hours",
         "start": "2026-09-20T10:00:00Z", "end": "2026-09-20T11:00:00Z", "source_name": "Watch"},
        {"id": "e2", "event_type": "headache", "category": "symptoms", "name": "Headache",
         "start": "2026-09-20T12:00:00Z", "source_name": "iPhone"}])
    shown = client.get("/api/biometrics/events", params={"days": 0}).json()["events"]
    assert [e["event_type"] for e in shown] == ["headache"]
    assert len(client.get("/api/biometrics/events", params={"days": 0, "category": "activity"}).json()["events"]) == 1
    assert "apple_stand_hour" in client.get("/api/export/csv", params={"kind": "events"}).text


def test_workout_humidity_is_a_percentage(client):
    from app.store import activity
    profile = client.get("/api/status").json()["profiles"][0]["id"]
    activity.insert_workouts(profile, None, [
        {"id": "w-old-app", "name": "Running", "start": "2026-09-24T23:47:00Z", "end": "2026-09-25T00:05:00Z",
         "humidity_pct": 6500.0, "source_name": "Apple Watch"},
        {"id": "w-new-app", "name": "Walking", "start": "2026-09-23T20:50:00Z", "end": "2026-09-23T21:40:00Z",
         "humidity_pct": 58, "source_name": "Apple Watch"}])
    detail = {w: client.get(f"/api/biometrics/workouts/{w}").json() for w in ("w-old-app", "w-new-app")}
    assert detail["w-old-app"]["humidity_pct"] == 65 and detail["w-new-app"]["humidity_pct"] == 58


def test_migration_fixes_stored_workout_humidity(tmp_path):
    import sqlite3
    from app.core import config, db
    path = config.db_path()
    conn = sqlite3.connect(str(path))
    for f in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
        if int(f.name.split("_", 1)[0]) > 11:
            break
        for statement in db._split_sql(f.read_text()):
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 11")
    conn.executemany("INSERT INTO workouts(id, profile_id, name, start_date, end_date, humidity_pct, created_at) VALUES (?, 'p', 'Run', ?, ?, ?, 0)",
                     [("a", "2026-09-24T23:47:00Z", "2026-09-25T00:05:00Z", 6500.0),
                      ("b", "2026-09-20T23:42:00Z", "2026-09-21T00:06:00Z", 5799.9999999999991),
                      ("c", "2026-09-19T00:00:00Z", "2026-09-19T00:30:00Z", 71.0),
                      ("d", "2026-09-18T00:00:00Z", "2026-09-18T00:30:00Z", None)])
    conn.commit()
    conn.close()
    db.reset_migration_cache()
    db.ensure_migrated(path)
    conn = sqlite3.connect(str(path))
    rows = dict(conn.execute("SELECT id, humidity_pct FROM workouts").fetchall())
    conn.close()
    assert rows["a"] == 65 and round(rows["b"], 6) == 58 and rows["c"] == 71 and rows["d"] is None


def test_same_session_from_two_devices_is_one_workout(client):
    from app.store import activity
    profile = client.get("/api/status").json()["profiles"][0]["id"]
    activity.insert_workouts(profile, None, [
        {"id": "w-watch", "name": "Running", "start": "2026-09-20T14:00:00Z", "end": "2026-09-20T14:40:00Z", "duration_s": 2400,
         "distance_m": 6500, "active_energy_kcal": 420, "avg_hr": 150, "source_name": "Alex's Apple Watch",
         "route": [{"t": "2026-09-20T14:00:00Z", "lat": 1, "lon": 1}, {"t": "2026-09-20T14:01:00Z", "lat": 1.001, "lon": 1}]},
        {"id": "w-whoop", "name": "Running", "start": "2026-09-20T14:03:00Z", "end": "2026-09-20T14:38:00Z", "duration_s": 2100,
         "total_energy_kcal": 500, "avg_hr": 148, "source_name": "WHOOP"},
        {"id": "w-later", "name": "Walking", "start": "2026-09-20T18:00:00Z", "end": "2026-09-20T18:30:00Z", "duration_s": 1800,
         "source_name": "WHOOP"}])
    res = client.get("/api/biometrics/workouts", params={"days": 0}).json()
    assert [w["id"] for w in res["workouts"]] == ["w-later", "w-watch"]
    assert res["workouts"][1]["also_recorded_by"][0]["source_name"] == "WHOOP"
    assert res["summary"]["count"] == 2 and res["summary"]["energy_kcal"] == 420
    assert client.get("/api/export/csv", params={"kind": "workouts"}).text.count("\n") == 4   # header + all three
