"""Regression tests for defects found in the end-to-end audit: concurrency, malformed input,
Apple Health export coverage and export safety."""

from __future__ import annotations

import io
import threading
import time
import zipfile

from fastapi.testclient import TestClient

from tests.conftest import BASE


def _device(client) -> tuple[TestClient, dict]:
    code = client.post("/api/devices/pairing-code", json={}).json()["code"]
    phone = TestClient(client.app, base_url=BASE)
    token = phone.post("/api/devices/pair", json={"code": code, "device_name": "Phone"}).json()["device_token"]
    return phone, {"X-Syntropy-Device-Token": token}


def _sample(i: int, **kw) -> dict:
    t = f"2024-05-01T08:{i // 60:02d}:{i % 60:02d}Z"
    return {"id": f"s{i}", "metric_type": "heart_rate", "value": 60, "unit": "count/min", "start_date": t, "end_date": t,
            "source_name": "Apple Watch", **kw}


def test_read_then_write_transactions_do_not_fail_under_concurrency(client):
    """A transaction that reads, then writes after another connection committed, used to fail
    instantly with "database is locked" (seen as 500s during parallel phone ingests)."""
    from app.core.db import db

    errors: list[Exception] = []
    first_read = threading.Event()

    def reader_then_writer():
        try:
            with db() as conn:
                conn.execute("SELECT COUNT(*) FROM profiles").fetchone()
                first_read.set()
                time.sleep(0.3)
                conn.execute("UPDATE profiles SET updated_at = updated_at")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def writer():
        first_read.wait(2)
        try:
            with db() as conn:
                conn.execute("UPDATE profiles SET updated_at = updated_at")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reader_then_writer), threading.Thread(target=writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_parallel_device_ingests_all_succeed(client):
    phone, headers = _device(client)
    results: list[int] = []

    def send(k: int):
        batch = {"device_id": "p", "device_name": "p", "samples": [_sample(k * 100 + i) for i in range(100)]}
        with TestClient(client.app, base_url=BASE) as c:
            results.append(c.post("/api/ingest/wearables", json=batch, headers=headers).status_code)

    threads = [threading.Thread(target=send, args=(k,)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200] * 6


def test_non_finite_values_are_rejected_not_500(client):
    phone, headers = _device(client)
    raw = (b'{"device_id":"x","device_name":"y","samples":[{"id":"n","metric_type":"heart_rate","value":NaN,'
           b'"start_date":"2024-01-01T00:00:00Z","end_date":"2024-01-01T00:00:00Z"}]}')
    r = phone.post("/api/ingest/wearables", content=raw, headers={**headers, "Content-Type": "application/json"})
    assert r.status_code == 422
    assert r.json()["detail"][0]["input"] is None  # echoed NaN is sanitized, not a JSON error


def test_one_bad_row_does_not_fail_the_batch(client):
    """Senders only advance after a success, so rejecting a whole batch would block them forever."""
    phone, headers = _device(client)
    batch = {"device_id": "x", "device_name": "y",
             "samples": [_sample(1), _sample(2, start_date="not a date", end_date="nope")],
             "workouts": [{"id": "w", "start": "garbage", "end": "garbage"}],
             "events": [{"id": "e", "event_type": "headache", "start": ""}]}
    body = phone.post("/api/ingest/wearables", json=batch, headers=headers).json()
    assert body["inserted"] == 1 and body["rejected"] == 1 and body["duplicates"] == 0
    assert body["workouts_inserted"] == 0 and body["events_inserted"] == 0


def test_healthkit_export_tolerates_malformed_sections(client):
    payloads = [
        {"data": {"metrics": "not a list", "workouts": {"x": 1}, "symptoms": [1, "two", None]}},
        {"data": {"metrics": [{"name": "step_count", "data": [{"date": "bad", "qty": 5}, {"date": 12, "qty": 1}, "row"]}]}},
        {"data": {"metrics": [{"name": "heart_rate", "data": [{"date": "2024-05-01 10:00:00 +0000", "Avg": "NaN"}]}]}},
        {"data": {"workouts": [{"name": "Run", "start": "bad"}, {"name": "Walk", "start": "2024-05-01 10:00:00 +0000",
                                                                  "end": "later", "route": "nope", "heartRateData": [5]}]}},
        {"data": {"metrics": [{"name": "sleep_analysis", "data": [{"date": "x"}, {"startDate": "2024-05-01 01:00:00 +0000",
                                                                                "endDate": "junk", "value": "Deep"}]}]}},
        {"data": "string"},
    ]
    for p in payloads:
        r = client.post("/api/ingest/healthkit-export", json=p)
        assert r.status_code == 200, (p, r.text)
    walk = client.get("/api/biometrics/workouts", params={"days": 0}).json()["workouts"]
    assert [w["name"] for w in walk] == ["Walk"]


def _export_zip() -> bytes:
    xml = """<?xml version="1.0"?><HealthData>
<Record type="HKQuantityTypeIdentifierStepCount" sourceName="Watch" unit="count" startDate="2024-03-01 08:00:00 -0800" endDate="2024-03-01 08:10:00 -0800" value="900"/>
<Record type="HKCategoryTypeIdentifierHeadache" sourceName="Health" startDate="2024-03-01 09:00:00 -0800" endDate="2024-03-01 10:00:00 -0800" value="HKCategoryValueSeverityModerate"/>
<Record type="HKCategoryTypeIdentifierMenstrualFlow" sourceName="Health" startDate="2024-03-02 00:00:00 -0800" endDate="2024-03-02 00:00:00 -0800" value="HKCategoryValueMenstrualFlowLight"/>
<Correlation type="HKCorrelationTypeIdentifierBloodPressure" startDate="2024-03-01 08:00:00 -0800" endDate="2024-03-01 08:00:00 -0800">
 <Record type="HKQuantityTypeIdentifierBloodPressureSystolic" sourceName="Cuff" unit="mmHg" startDate="2024-03-01 08:00:00 -0800" endDate="2024-03-01 08:00:00 -0800" value="121"/>
</Correlation>
<Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="31.5" durationUnit="min" sourceName="Watch" startDate="2024-03-01 18:00:00 -0800" endDate="2024-03-01 18:31:30 -0800">
 <MetadataEntry key="HKIndoorWorkout" value="0"/><MetadataEntry key="HKElevationAscended" value="4520 cm"/>
 <MetadataEntry key="HKWeatherTemperature" value="59 degF"/>
 <WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned" startDate="x" endDate="y" sum="351.2" unit="kcal"/>
 <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" startDate="x" endDate="y" sum="3.11" unit="mi"/>
 <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" startDate="x" endDate="y" average="151" minimum="98" maximum="172" unit="count/min"/>
 <WorkoutRoute sourceName="Watch" startDate="x" endDate="y"><FileReference path="/workout-routes/route_1.gpx"/></WorkoutRoute>
</Workout>
</HealthData>"""
    gpx = b"""<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
<trkpt lon="-122.40" lat="37.78"><ele>10.5</ele><time>2024-03-02T02:00:00Z</time></trkpt>
<trkpt lon="-122.41" lat="37.79"><ele>11</ele><time>2024-03-02T02:00:05Z</time></trkpt></trkseg></trk></gpx>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("apple_health_export/export.xml", xml)
        z.writestr("apple_health_export/workout-routes/route_1.gpx", gpx)
    return buf.getvalue()


def test_apple_health_export_imports_workouts_routes_and_events(client):
    r = client.post("/api/imports/apple-health", files={"file": ("export.zip", _export_zip(), "application/zip")})
    job_id = r.json()["job_id"]
    for _ in range(50):
        job = client.get(f"/api/imports/{job_id}").json()
        if job["status"] in ("completed", "error"):
            break
        time.sleep(0.1)
    assert job["status"] == "completed", job
    assert job["result"]["workouts"] == 1 and job["result"]["events"] == 2 and job["result"]["samples"] == 2

    w = client.get("/api/biometrics/workouts", params={"days": 0}).json()["workouts"][0]
    assert w["name"] == "Running" and round(w["distance_m"]) == 5005 and w["avg_hr"] == 151 and w["indoor"] is False
    detail = client.get(f"/api/biometrics/workouts/{w['id']}").json()
    assert len(detail["route"]) == 2 and detail["elevation_ascent_m"] == 45.2 and detail["temperature_c"] == 15.0
    events = {e["event_type"]: e for e in client.get("/api/biometrics/events", params={"days": 0}).json()["events"]}
    assert events["headache"]["value_label"] == "Moderate"
    assert events["menstrual_flow"]["value_label"] == "Light" and events["menstrual_flow"]["category"] == "cycle"
    bp = client.get("/api/biometrics/samples", params={"metric": "blood_pressure_systolic"}).json()["samples"]
    assert bp and bp[0]["value"] == 121  # records nested in correlations still import


def test_export_parser_memory_stays_flat():
    """Cleared elements used to stay attached to the XML root, growing memory with every record."""
    import tracemalloc

    from app.connectors import apple_health
    rec = ('<Record type="HKQuantityTypeIdentifierHeartRate" sourceName="W" unit="count/min" '
           'startDate="2024-03-01 08:00:00 -0800" endDate="2024-03-01 08:00:00 -0800" value="60"/>')
    xml = ("<HealthData>" + rec * 20000 + "</HealthData>").encode()
    tracemalloc.start()
    count = sum(1 for _ in apple_health.iter_items(io.BytesIO(xml)))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert count == 20000
    assert peak < 3_000_000, peak


def test_csv_exports_neutralize_formulas_and_cover_new_data(client):
    phone, headers = _device(client)
    phone.post("/api/ingest/wearables", headers=headers, json={
        "device_id": "x", "device_name": "y",
        "events": [{"id": "e1", "event_type": "headache", "category": "symptoms", "name": "=HYPERLINK(\"http://evil\")",
                    "start": "2024-05-01T10:00:00Z", "value_label": "@SUM(A1)"}],
        "workouts": [{"id": "w1", "name": "+Run", "start": "2024-05-01T10:00:00Z", "end": "2024-05-01T10:30:00Z", "duration_s": 1800}],
    })
    events = client.get("/api/export/csv", params={"kind": "events"}).text
    assert "'=HYPERLINK" in events and "'@SUM(A1)" in events
    workouts = client.get("/api/export/csv", params={"kind": "workouts"}).text
    assert "'+Run" in workouts and ",30.0," in workouts


def test_healthkit_export_device_is_labelled(client):
    code = client.post("/api/devices/pairing-code", json={}).json()["code"]
    client.post("/api/devices/pair", json={"code": code, "device_name": "HealthKit Export", "platform": "healthkit-export"})
    conn = [c for c in client.get("/api/connections").json()["connections"] if c["kind"] == "device"][0]
    assert conn["display_name"] == "HealthKit Export" and conn["metadata"]["platform"] == "healthkit-export"


def test_access_log_redacts_oauth_codes():
    import logging

    from app.main import _RedactSecrets
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/callback?code=abc.def&state=xyz&x=1", "1.1", 303), None)
    _RedactSecrets().filter(rec)
    assert rec.getMessage() == '1.2.3.4:5 - "GET /callback?code=REDACTED&state=REDACTED&x=1 HTTP/1.1" 303'


def test_static_scripts_revalidate(client):
    assert client.get("/static/js/app.js").headers["cache-control"] == "no-cache"
