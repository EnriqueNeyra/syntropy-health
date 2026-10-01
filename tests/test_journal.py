"""The Journal (entries logged in the web app), time ranges for workouts and the journal, and shared alert state."""

from datetime import datetime, timedelta, timezone

from tests.conftest import BASE
from fastapi.testclient import TestClient


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pair(client):
    code = client.post("/api/devices/pairing-code", json={}).json()["code"]
    phone = TestClient(client.app, base_url=BASE)
    paired = phone.post("/api/devices/pair", json={"code": code, "device_name": "iPhone"}).json()
    return phone, {"X-Syntropy-Device-Token": paired["device_token"]}


def test_logging_editing_and_deleting_journal_entries(client):
    entry = {"event_type": "headache", "category": "symptoms", "name": "Headache", "start": _iso(1), "value": 3,
             "value_label": "Moderate", "note": "  after a long screen day  "}
    made = client.post("/api/journal", json=entry).json()
    assert made["manual"] is True and made["source_name"] == "Journal" and made["note"] == "after a long screen day"

    listed = client.get("/api/biometrics/events", params={"days": 7}).json()
    assert [e["id"] for e in listed["events"]] == [made["id"]]
    assert client.get("/api/biometrics/events", params={"days": 7, "q": "screen"}).json()["events"]
    assert not client.get("/api/biometrics/events", params={"days": 7, "q": "running"}).json()["events"]
    assert client.get("/api/biometrics/events", params={"days": 7, "source": "journal"}).json()["events"]
    assert not client.get("/api/biometrics/events", params={"days": 7, "source": "devices"}).json()["events"]

    changed = client.put(f"/api/journal/{made['id']}", json={**entry, "value": 4, "value_label": "Severe", "note": ""}).json()
    assert changed["value_label"] == "Severe" and changed["note"] is None

    assert client.post("/api/journal", json={**entry, "category": "nonsense"}).status_code == 400
    assert client.post("/api/journal", json={**entry, "event_type": "Bad Type"}).status_code == 422
    assert client.post("/api/journal", json={**entry, "end": _iso(2)}).status_code == 400

    assert client.delete(f"/api/journal/{made['id']}").status_code == 200
    assert client.get("/api/biometrics/events", params={"days": 7}).json()["events"] == []
    assert client.delete(f"/api/journal/{made['id']}").status_code == 404


def test_device_entries_cannot_be_changed_from_the_journal(client):
    phone, device = _pair(client)
    batch = {"device_id": "ios", "device_name": "iPhone", "events": [
        {"id": "ev-1", "event_type": "headache", "category": "symptoms", "name": "Headache", "start": _iso(1), "value": 2,
         "value_label": "Mild", "source_name": "Apple Watch"}]}
    assert phone.post("/api/ingest/wearables", json=batch, headers=device).json()["events_inserted"] == 1
    [e] = client.get("/api/biometrics/events", params={"days": 7}).json()["events"]
    assert e["manual"] is False
    body = {"event_type": "headache", "category": "symptoms", "name": "Headache", "start": _iso(1)}
    assert client.put("/api/journal/ev-1", json=body).status_code == 404
    assert client.delete("/api/journal/ev-1").status_code == 404


def test_workouts_and_journal_by_date_range(client):
    phone, device = _pair(client)
    workouts = [{"id": f"w-{d}", "name": "Running" if d % 2 else "Cycling", "start": _iso(d), "end": _iso(d - 0.02),
                 "duration_s": 1800, "source_name": "Apple Watch"} for d in (3, 20, 200)]
    phone.post("/api/ingest/wearables", json={"device_id": "ios", "device_name": "iPhone", "workouts": workouts}, headers=device)

    assert len(client.get("/api/biometrics/workouts", params={"days": 30}).json()["workouts"]) == 2
    assert len(client.get("/api/biometrics/workouts", params={"days": 0}).json()["workouts"]) == 3
    start = (datetime.now(timezone.utc) - timedelta(days=250)).date().isoformat()
    end = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
    ranged = client.get("/api/biometrics/workouts", params={"days": 0, "start": start, "end": end}).json()
    assert [w["id"] for w in ranged["workouts"]] == ["w-20", "w-200"] and ranged["summary"]["count"] == 2

    typed = client.get("/api/biometrics/workouts", params={"days": 0, "type": "Running"}).json()
    assert [w["name"] for w in typed["workouts"]] == ["Running"] and typed["summary"]["count"] == 1
    assert {t["name"]: t["count"] for t in typed["types"]} == {"Cycling": 2, "Running": 1}   # every kind, for the filter
    assert client.get("/api/biometrics/workouts", params={"start": "31-01-2026"}).status_code == 400

    client.post("/api/journal", json={"event_type": "note", "category": "notes", "name": "Note", "start": _iso(40), "note": "Old"})
    assert not client.get("/api/biometrics/events", params={"days": 30}).json()["events"]
    assert client.get("/api/biometrics/events", params={"days": 0, "start": start, "end": end}).json()["events"]


def test_alert_state_is_shared_between_devices(client):
    profile = client.get("/api/profiles").json()["profiles"][0]["id"]
    assert client.get(f"/api/profiles/{profile}/alerts").json() == {"seen": [], "dismissed": []}
    client.post(f"/api/profiles/{profile}/alerts", json={"seen": ["lab:1", "lab:2"]})
    state = client.post(f"/api/profiles/{profile}/alerts", json={"seen": ["lab:1"], "dismissed": ["conn:x:reauth"]}).json()
    assert state == {"seen": ["lab:2", "lab:1"], "dismissed": ["conn:x:reauth"]}
    other = TestClient(client.app, base_url=BASE, cookies=client.cookies)   # e.g. the iPhone app's web view
    assert other.get(f"/api/profiles/{profile}/alerts").json()["dismissed"] == ["conn:x:reauth"]
    assert client.get("/api/profiles/nope/alerts").status_code == 404


def test_sample_counts_follow_uploads_and_deletions(client):
    """Sources' sample counts and the total sent back to the phone come from running counts, not a full count."""
    phone, device = _pair(client)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    batch = lambda ids: {"device_id": "ios", "device_name": "iPhone", "samples": [
        {"id": f"s-{i}", "metric_type": "step_count", "value": 10, "unit": "count", "start_date": (now - timedelta(minutes=i)).isoformat(),
         "end_date": (now - timedelta(minutes=i)).isoformat(), "source_name": "Apple Watch"} for i in ids]}
    phones = lambda: [c for c in client.get("/api/connections").json()["connections"] if c["kind"] == "device"]
    assert phone.post("/api/ingest/wearables", json=batch(range(5)), headers=device).json()["server_total_samples"] == 5
    assert phones()[0]["sample_count"] == 5
    res = phone.post("/api/ingest/wearables", json=batch(range(3, 8)), headers=device).json()   # 3 new, 2 already there
    assert res["inserted"] == 3 and res["server_total_samples"] == 8 and phones()[0]["sample_count"] == 8
    client.delete(f"/api/connections/{phones()[0]['id']}", params={"delete_data": True})
    assert client.get("/api/biometrics/overview").json()["total_samples"] == 0

    from app.store import sample_counts
    phone, device = _pair(client)
    phone.post("/api/ingest/wearables", json=batch(range(4)), headers=device)
    sample_counts.forget()   # e.g. a restart: counted afresh
    assert phones()[0]["sample_count"] == 4


def test_journal_keeps_feelings_and_trends_gets_device_signals(client):
    """The Journal shows how the person feels; ECGs and watch notifications go with the measurements in Trends, and
    routine bookkeeping (stand hours, handwashing) shows in neither."""
    phone, device = _pair(client)
    def ev(i, event_type, category, name):
        return {"id": f"ev-{i}", "event_type": event_type, "category": category, "name": name, "start": _iso(1 + i / 100),
                "source_name": "Apple Watch"}
    batch = {"device_id": "ios", "device_name": "iPhone", "events": [
        ev(1, "headache", "symptoms", "Headache"), ev(2, "state_of_mind", "stateOfMind", "State of Mind"),
        ev(3, "ecg", "ecg", "ECG"), ev(4, "high_heart_rate_event", "events", "High Heart Rate Notification"),
        ev(5, "apple_stand_hour", "activity", "Stand Hours"), ev(6, "handwashing_event", "other", "Handwashing"),
        ev(7, "mindful_session", "mindfulness", "Mindful Minutes")]}
    assert phone.post("/api/ingest/wearables", json=batch, headers=device).json()["events_inserted"] == 7
    checkin = client.post("/api/journal", json={
        "event_type": "check_in", "category": "stateOfMind", "name": "Check-in", "start": _iso(0.5), "value": 0.5,
        "value_label": "Pleasant", "details": {"energy": 4, "stress": 2}}).json()
    assert checkin["metadata"] == {"energy": 4, "stress": 2}

    journal = client.get("/api/biometrics/events", params={"days": 7, "view": "journal"}).json()
    assert {e["event_type"] for e in journal["events"]} == {"check_in", "headache", "state_of_mind"}
    assert {r["event_type"] for r in journal["summary"]} == {"check_in", "headache", "state_of_mind"}
    signals = client.get("/api/biometrics/events", params={"days": 7, "view": "signals"}).json()
    assert {e["event_type"] for e in signals["events"]} == {"ecg", "high_heart_rate_event"}

    body = {"event_type": "check_in", "category": "stateOfMind", "name": "Check-in", "start": _iso(0.5), "value": 0,
            "value_label": "Neutral", "details": {"energy": 2}}
    assert client.put(f"/api/journal/{checkin['id']}", json=body).json()["metadata"] == {"energy": 2}
    assert client.put(f"/api/journal/{checkin['id']}", json={**body, "details": {"energy": 9}}).status_code == 422
