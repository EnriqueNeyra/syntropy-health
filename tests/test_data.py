"""Imports, exports, profiles, settings and the legacy prototype upgrade."""

import io
import json
import sqlite3
import time
import zipfile

from tests.conftest import connect_institution

EXPORT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Alex's Apple Watch" unit="count" startDate="2026-09-20 08:00:00 -0700" endDate="2026-09-20 08:10:00 -0700" value="900"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="Alex's Apple Watch" unit="count" startDate="2026-09-20 09:00:00 -0700" endDate="2026-09-20 09:10:00 -0700" value="1100"/>
 <Record type="HKQuantityTypeIdentifierBodyMass" sourceName="Scale" unit="lb" startDate="2026-09-20 07:00:00 -0700" endDate="2026-09-20 07:00:00 -0700" value="200"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Alex's Apple Watch" startDate="2026-09-20 00:00:00 -0700" endDate="2026-09-20 02:00:00 -0700" value="HKCategoryValueSleepAnalysisAsleepCore"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning"/>
</HealthData>"""

CLINICAL = {"resourceType": "Observation", "id": "ah-obs-1", "status": "final",
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
            "code": {"coding": [{"system": "http://loinc.org", "code": "2345-7", "display": "Glucose"}]},
            "effectiveDateTime": "2025-01-02T08:00:00Z",
            "valueQuantity": {"value": 5.5, "unit": "mmol/L"}, "referenceRange": [{"low": {"value": 3.9}, "high": {"value": 5.5}}]}


def _wait(client, job_id):
    for _ in range(100):
        job = client.get(f"/api/imports/{job_id}").json()
        if job["status"] in ("completed", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError("import did not finish")


def test_apple_health_import(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("apple_health_export/export.xml", EXPORT_XML)
        zf.writestr("apple_health_export/clinical-records/Observation-1.json", json.dumps(CLINICAL))
    r = client.post("/api/imports/apple-health", files={"file": ("export.zip", buf.getvalue(), "application/zip")})
    job = _wait(client, r.json()["job_id"])
    assert job["status"] == "completed", job
    assert job["result"]["samples"] == 4
    assert job["result"]["clinical"]["records"] == 1
    glucose = client.get("/api/records", params={"code": "2345-7"}).json()["items"][0]
    assert glucose["value_norm"] == 99.1 and glucose["unit_norm"] == "mg/dL"   # mmol/L normalized
    weight = client.get("/api/biometrics/samples", params={"metric": "body_mass"}).json()["samples"][0]
    assert round(weight["value"], 1) == 90.7                                   # lb -> kg

    # Re-importing the same export adds nothing.
    buf.seek(0)
    r2 = client.post("/api/imports/apple-health", files={"file": ("export.zip", buf.getvalue(), "application/zip")})
    assert _wait(client, r2.json()["job_id"])["result"]["inserted"] == 0


def test_fhir_bundle_import_and_export_roundtrip(client):
    bundle = {"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "Patient", "id": "p1", "name": [{"given": ["Alex"], "family": "Rivera"}], "birthDate": "1981-04-12"}},
        {"resource": {"resourceType": "Condition", "id": "c1", "clinicalStatus": {"coding": [{"code": "active"}]},
                      "code": {"coding": [{"system": "http://snomed.info/sct", "code": "59621000", "display": "Essential hypertension"}]},
                      "subject": {"reference": "Patient/p1"}, "onsetDateTime": "2020-01-01"}},
    ]}
    r = client.post("/api/imports/fhir", files={"file": ("records.json", json.dumps(bundle), "application/json")})
    assert r.status_code == 200, r.text
    assert r.json()["records"] == 1
    exported = client.get("/api/export/fhir")
    assert exported.status_code == 200
    body = json.loads(exported.content)
    assert body["resourceType"] == "Bundle"
    assert any(e["resource"]["id"] == "c1" for e in body["entry"])


def test_fhir_import_rejects_garbage(client):
    r = client.post("/api/imports/fhir", files={"file": ("x.json", b"not json at all", "application/json")})
    assert r.status_code == 400


def test_csv_and_backup_exports(client):
    connect_institution(client, "epic-stanford-health-care")
    csv_text = client.get("/api/export/csv", params={"kind": "records"}).text
    assert csv_text.splitlines()[0].startswith("category,title")
    assert len(csv_text.splitlines()) > 100
    backup = client.get("/api/export/backup")
    assert backup.content[:15] == b"SQLite format 3"


def test_profiles_scope_data(client):
    kid = client.post("/api/profiles", json={"name": "Riley", "relationship": "child"}).json()
    connect_institution(client, "epic-stanford-children-s-health", username="riley.morgan", profile_id=kid["id"])
    kid_summary = client.get("/api/summary", params={"profile": kid["id"]}).json()
    assert kid_summary["identities"][0]["full_name"] == "Riley Morgan"
    me = client.get("/api/summary").json()
    assert me["identities"] == []
    assert client.delete(f"/api/profiles/{kid['id']}").status_code == 200
    assert client.get("/api/records", params={"profile": kid["id"]}).status_code == 404


def test_platform_settings_control_mode(client):
    r = client.put("/api/settings/platforms/epic", json={"mode": "sandbox"})
    assert r.json()["mode"] == "sandbox"
    fail = client.post("/api/connections/ehr", json={"institution_id": "epic-stanford-health-care"})
    assert fail.status_code == 400 and "client ID" in fail.json()["detail"]
    assert client.put("/api/settings/platforms/epic", json={"mode": "bogus"}).status_code == 400
    client.put("/api/settings/platforms/epic", json={"mode": "simulated"})
    snap = client.get("/api/settings").json()
    assert next(p for p in snap["platforms"] if p["platform"] == "epic")["mode"] == "simulated"


def test_wearable_secret_never_returned(client):
    client.put("/api/settings/wearables/oura", json={"client_id": "abc", "client_secret": "super-secret"})
    snap = client.get("/api/settings").json()
    assert snap["wearables"]["oura"] == {"label": "Oura Ring", "client_id": "abc", "client_secret_set": True}
    assert "super-secret" not in json.dumps(snap)
    # Defaults to the registered Syntropy app (secret held by the relay) when nothing is configured.
    assert snap["wearables"]["whoop"]["client_id"] == "91dbd202-060d-477e-a9b7-3aedd1def98c"
    assert snap["wearables"]["whoop"]["client_secret_set"] is False


def test_directory_search(client):
    res = client.get("/api/directory", params={"q": "kaiser"}).json()
    assert res["total"] >= 5
    assert all("Kaiser" in r["name"] for r in res["results"])
    assert res["results"][0]["mode"] == "simulated"
    featured = client.get("/api/directory/featured").json()["results"]
    assert len({f["platform"] for f in featured}) >= 4


def test_directory_finds_brands_by_network_and_town_and_keeps_old_ids(client):
    from app import directory
    names = [r["name"] for r in directory.search("stanford health care")["results"]]
    assert "Stanford" in names                                    # Epic's brand name; the network is Stanford Health Care
    assert directory.search("anchorage")["total"] >= 1           # facility towns are searchable
    merged = directory.get_institution("epic-kaiser-ncal-prod-326-cis")   # merged into Northern California
    assert merged and merged["id"] == "epic-kaiser-permanente-california-northern"
    assert directory.get_institution("austin-family-medicine")["id"] == "athenahealth"   # placeholder → national entry


def test_directory_covers_oracle_ecw_athena_and_va_with_real_endpoints(client):
    from app import directory
    stats = directory.stats()
    assert stats["cerner"] > 1000 and stats["healow"] > 15000 and stats["athena"] == 1 and stats["va"] == 1
    banner = directory.get_institution("banner-health")
    assert banner["platform"] == "cerner" and banner["fhir_base_url"].startswith("https://fhir-myrecord.cerner.com/r4/")
    ecw = directory.search(platform="healow", limit=200)["results"]
    assert all(i["fhir_base_url"].startswith("https://fhir4.healow.com/fhir/r4/") for i in ecw)   # the patient host
    from scripts.refresh_directory import TEST_NAME
    assert not any(TEST_NAME.search(i["name"]) for i in directory.search(platform="healow", limit=20000)["results"])
    for inst_id in ("athenahealth", "va-lighthouse"):
        assert directory.get_institution(inst_id)["fhir_base_url"].startswith("https://api.")


def test_directory_refresh_keeps_ids_and_prefers_brand_addresses():
    from scripts.refresh_directory import ecw_entries, epic_entries, merge
    existing = [
        {"id": "epic-kp-old", "name": "Kaiser Permanente - California - Southern", "platform": "epic",
         "fhir_base_url": "https://kp/212/api/FHIR/R4/", "featured": True},
        {"id": "epic-kp-dup", "name": "KP SoCal prod", "platform": "epic", "fhir_base_url": "https://kp/226/api/FHIR/R4/"},
        {"id": "epic-gone", "name": "Closed Clinic", "platform": "epic", "fhir_base_url": "https://gone/R4/"},
    ]
    brands = {"entry": [
        {"resource": {"resourceType": "Endpoint", "id": "e1", "address": "https://kp/226/api/FHIR/R4/"}},
        {"resource": {"resourceType": "Endpoint", "id": "e2", "address": "https://new/R4"}},
        {"resource": {"resourceType": "Organization", "id": "b1", "name": "Kaiser Permanente - Southern California",
                      "endpoint": [{"reference": "urn:uuid:e1"}]}},
        {"resource": {"resourceType": "Organization", "id": "f1", "name": "LA Medical Offices", "partOf": {"reference": "urn:uuid:b1"},
                      "address": [{"city": "Los Angeles", "state": "CA"}]}},
        {"resource": {"resourceType": "Organization", "id": "b2", "name": "New Clinic", "endpoint": [{"reference": "urn:uuid:e2"}]}},
    ]}
    legacy = {"entry": [{"resource": {"resourceType": "Endpoint", "name": "Kaiser Permanente - California - Southern",
                                      "address": "https://kp/212/api/FHIR/R4/"}}]}
    out = merge(existing, epic_entries({"brands": brands, "r4": legacy}), "epic-")
    kp = next(e for e in out if e["name"].startswith("Kaiser"))
    assert kp["id"] == "epic-kp-old" and kp["featured"] and kp["former_ids"] == ["epic-kp-dup"]
    assert kp["fhir_base_url"] == "https://kp/226/api/FHIR/R4/"   # the brand's address wins over the legacy one
    assert kp["location"] == "Los Angeles, CA" and kp["places"] == ["Los Angeles CA"]
    assert [e["id"] for e in out if e["name"] == "New Clinic"] == ["epic-new-clinic"]
    assert len(out) == 2

    practices = {"entry": [
        {"resource": {"resourceType": "Endpoint", "id": "ABCDEF", "address": "https://fhir4.eclinicalworks.com/fhir/r4/ABCDEF"}},
        {"resource": {"resourceType": "Organization", "name": "Main Street Pediatrics", "endpoint": [{"reference": "Endpoint/ABCDEF"}],
                      "address": [{"city": "MILWAUKEE", "state": "WI"}]}},
        {"resource": {"resourceType": "Endpoint", "id": "TTTTTT", "address": "https://fhir4.eclinicalworks.com/fhir/r4/TTTTTT"}},
        {"resource": {"resourceType": "Organization", "name": "Main Street Pediatrics TRAIN", "endpoint": [{"reference": "Endpoint/TTTTTT"}]}},
    ]}
    [ecw] = merge([], ecw_entries({"practices": practices}), "ecw-")
    assert ecw == {"id": "ecw-abcdef", "name": "Main Street Pediatrics", "location": "Milwaukee, WI", "platform": "healow",
                   "fhir_base_url": "https://fhir4.healow.com/fhir/r4/ABCDEF"}


def test_legacy_prototype_database_is_upgraded(isolated_data, raw_client_factory=None):
    from app.core import db as core_db
    path = isolated_data / "syntropy.db"
    core_db.reset_migration_cache()
    legacy = sqlite3.connect(path)
    legacy.executescript("""
        CREATE TABLE biometric_samples (id TEXT PRIMARY KEY, metric_type TEXT NOT NULL, hk_identifier TEXT NOT NULL,
            value REAL NOT NULL, unit TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, device_id TEXT,
            device_name TEXT, source_name TEXT, metadata_json TEXT, created_at REAL NOT NULL);
        CREATE TABLE sync_batches (batch_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, device_name TEXT NOT NULL,
            os_version TEXT, app_version TEXT, sync_trigger TEXT NOT NULL, sample_count INTEGER NOT NULL, synced_at REAL NOT NULL);
        CREATE TABLE clinical_records (id TEXT PRIMARY KEY, patient_id TEXT NOT NULL, category TEXT NOT NULL);
        CREATE TABLE oauth_sessions (provider_id TEXT PRIMARY KEY, access_token TEXT);
        CREATE VIEW v_daily AS SELECT * FROM biometric_samples;
        INSERT INTO biometric_samples VALUES ('a','hrv_sdnn','HK',55,'ms','2026-09-20T07:00:00Z','2026-09-20T07:00:00Z',NULL,NULL,'WHOOP 4.0',NULL,0);
        INSERT INTO biometric_samples VALUES ('b','step_count','HK',4000,'count','2026-09-20T12:00:00Z','2026-09-20T12:10:00Z',NULL,NULL,'Apple Watch',NULL,0);
    """)
    legacy.commit()
    legacy.close()
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.conftest import BASE, CSRF, LOCAL_CLIENT
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT) as c:
        c.post("/api/setup", json={"passphrase": None}, headers=CSRF)
        metrics = {m["metric"] for m in c.get("/api/biometrics/metrics").json()["metrics"]}
        assert {"hrv_rmssd", "step_count"} <= metrics
    check = sqlite3.connect(path)
    tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"legacy_clinical_records", "legacy_oauth_sessions", "clinical_records", "profiles"} <= tables
    assert check.execute("SELECT COUNT(*) FROM biometric_samples WHERE profile_id IS NULL").fetchone()[0] == 0


def test_fhir_export_includes_patient_and_round_trips(client):
    from tests.conftest import connect_institution
    connect_institution(client, "epic-stanford-health-care")
    bundle = client.get("/api/export/fhir").json()
    types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert types[0] == "Patient"
    other = client.post("/api/profiles", json={"name": "Copy", "relationship": "other"}).json()
    files = {"file": ("export.json", json.dumps(bundle), "application/json")}
    res = client.post("/api/imports/fhir", files=files, data={"profile_id": other["id"]}).json()
    assert res["records"] == len(types) - 1
    assert client.get("/api/summary", params={"profile": other["id"]}).json()["identities"][0]["full_name"] == "Alex Rivera"
