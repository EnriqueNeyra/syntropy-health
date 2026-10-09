"""Imports, exports, profiles and settings."""

import io
import json
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
    r = client.put("/api/settings/platforms/trubridge", json={"mode": "sandbox"})
    assert r.json()["mode"] == "sandbox"
    hospital = next(i["id"] for i in client.get("/api/directory", params={"platform": "trubridge"}).json()["results"])
    fail = client.post("/api/connections/ehr", json={"institution_id": hospital})
    assert fail.status_code == 400 and "client ID" in fail.json()["detail"]
    client.put("/api/settings/platforms/epic", json={"mode": "sandbox"})
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


def test_directory_covers_oracle_ecw_and_athena_with_real_endpoints(client):
    from app import directory
    stats = directory.stats()
    assert stats["cerner"] > 1000 and stats["healow"] > 15000 and stats["athena"] == 1 and "va" not in stats
    banner = directory.get_institution("banner-health")
    assert banner["platform"] == "cerner" and banner["fhir_base_url"].startswith("https://fhir-myrecord.cerner.com/r4/")
    ecw = directory.search(platform="healow", limit=200)["results"]
    assert all(i["fhir_base_url"].startswith("https://fhir4.healow.com/fhir/r4/") for i in ecw)   # the patient host
    from scripts.refresh_directory import TEST_NAME
    assert not any(TEST_NAME.search(i["name"]) for i in directory.search(platform="healow", limit=20000)["results"])
    assert directory.get_institution("athenahealth")["fhir_base_url"].startswith("https://api.")


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

    # Two brands with one name keep their own ids, whatever order Epic lists them in.
    twins = [{"id": "epic-memorial-health", "name": "Memorial Health", "platform": "epic", "fhir_base_url": "https://oh/R4"},
             {"id": "epic-hca-south-atlantic", "name": "Memorial Health", "platform": "epic", "fhir_base_url": "https://ga/R4"}]
    brands = {"entry": [
        {"resource": {"resourceType": "Endpoint", "id": "ga", "address": "https://ga/R4"}},
        {"resource": {"resourceType": "Endpoint", "id": "oh", "address": "https://oh/R4"}},
        {"resource": {"resourceType": "Organization", "id": "b1", "name": "Memorial Health", "endpoint": [{"reference": "urn:uuid:ga"}]}},
        {"resource": {"resourceType": "Organization", "id": "b2", "name": "Memorial Health", "endpoint": [{"reference": "urn:uuid:oh"}]}},
    ]}
    out = merge(twins, epic_entries({"brands": brands, "r4": {"entry": []}}), "epic-")
    assert {e["id"]: e["fhir_base_url"] for e in out} == {"epic-memorial-health": "https://oh/R4",
                                                          "epic-hca-south-atlantic": "https://ga/R4"}

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


def test_trubridge_and_greenway_directories_follow_their_endpoint_references():
    from scripts.refresh_directory import greenway_entries, merge, readable_name, trubridge_entries
    assert readable_name("ST. MARY'S HOSPITAL OF THE PLAINS RHC") == "St. Mary's Hospital of the Plains RHC"
    assert readable_name("Abraham Lincoln Memorial") == "Abraham Lincoln Memorial"
    # TruBridge names endpoints by urn:uuid full URL, which isn't the endpoint's id.
    facilities = {"entry": [
        {"fullUrl": "urn:uuid:ep-1", "resource": {"resourceType": "Endpoint", "id": "other-id", "status": "active",
                                                  "address": "https://thrive-gw.cpsi-cloud.com/api/smart/x/id-osfac.1/fhir/r4"}},
        {"fullUrl": "urn:uuid:org-1", "resource": {"resourceType": "Organization", "active": True,
                                                   "name": "*Washington County Hospital", "endpoint": [{"reference": "urn:uuid:ep-1"}],
                                                   "address": [{"city": "WASHINGTON", "state": "KS"}]}},
        {"fullUrl": "urn:uuid:ep-2", "resource": {"resourceType": "Endpoint", "id": "ep-2", "address": "https://t/2"}},
        {"fullUrl": "urn:uuid:org-2", "resource": {"resourceType": "Organization", "name": "Grover Training Company",
                                                   "endpoint": [{"reference": "urn:uuid:ep-2"}]}},
    ]}
    [hospital] = merge([], trubridge_entries({"facilities": facilities}), "trubridge-")
    assert hospital == {"id": "trubridge-washington-county-hospital", "name": "Washington County Hospital",
                        "location": "Washington, KS", "platform": "trubridge",
                        "fhir_base_url": "https://thrive-gw.cpsi-cloud.com/api/smart/x/id-osfac.1/fhir/r4"}

    practices = {"entry": [
        {"resource": {"resourceType": "Endpoint", "id": "abc", "status": "active",
                      "address": "https://fhir-api.fhirprod.aws.greenwayhealth.com/fhir/R4/2.16.840.1"}},
        {"resource": {"resourceType": "Organization", "name": "Cardiothoracic Vascular Surgeons", "active": True,
                      "endpoint": [{"reference": "Endpoint/abc"}], "address": [{"city": "Chicago Ridge", "state": "IL"}]}},
        {"resource": {"resourceType": "Endpoint", "id": "off", "status": "off", "address": "https://g/off"}},
        {"resource": {"resourceType": "Organization", "name": "Closed Practice", "endpoint": [{"reference": "Endpoint/off"}]}},
    ]}
    [practice] = merge([], greenway_entries({"practices": practices}), "greenway-")
    assert practice["platform"] == "greenway" and practice["location"] == "Chicago Ridge, IL"
    assert practice["fhir_base_url"].endswith("/fhir/R4/2.16.840.1")


def test_modmed_nextgen_practice_fusion_and_medhost_directories():
    from scripts.refresh_directory import medhost_entries, modmed_entries, nextgen_entries, practicefusion_entries
    # ModMed lists endpoints, each naming its practice; a repeated address is one entry.
    endpoints = {"entry": [
        {"resource": {"resourceType": "Endpoint", "id": "e1", "status": "active", "name": "Coastal Dermatology",
                      "address": "https://coastal.derm.prod.fhir.ema-api.com/fhir/r4/",
                      "managingOrganization": {"reference": "Organization/2.1"}}},
        {"resource": {"resourceType": "Endpoint", "id": "e2", "status": "active", "name": "Coastal Dermatology",
                      "address": "https://COASTAL.derm.prod.fhir.ema-api.com/fhir/r4"}},
        {"resource": {"resourceType": "Organization", "id": "2.1", "name": "Coastal Dermatology",
                      "address": [{"city": "Tampa", "state": "FL"}]}},
        {"resource": {"resourceType": "Endpoint", "id": "e3", "status": "active", "name": "Client",
                      "address": "https://unset.gastro.prod.fhir.ema-api.com/fhir/r4/"}},
    ]}
    [derm] = modmed_entries({"endpoints": endpoints, "gastro": {"entry": []}})
    assert derm["platform"] == "modmed" and derm["location"] == "Tampa, FL"

    # NextGen's practices share one endpoint; placeholders and demo practices are dropped.
    national = "https://fhir.nextgen.com/nge/prod/fhir-api-r4/fhir/r4"
    practices = {"entry": [{"resource": {"resourceType": "Endpoint", "id": "base", "address": national}}] + [
        {"resource": {"resourceType": "Organization", "name": n, "endpoint": [{"reference": "Endpoint/base"}]}}
        for n in ("Valley Family Medicine", "A003", "Person", "*NextGen Family Practice", "Cajon Medical Group - TEST")]}
    assert [(e["name"], e["fhir_base_url"]) for e in nextgen_entries({"practices": practices})] == [
        ("Valley Family Medicine", national)]

    # Practice Fusion lists a provider and a patient endpoint per practice; patients need the patient one.
    pf = {"entry": [
        {"resource": {"resourceType": "Endpoint", "id": "ProviderSystemAccess-1", "name": "Provider / System Access",
                      "address": "https://api.practicefusion.com/fhir/r4/v1/1"}},
        {"resource": {"resourceType": "Endpoint", "id": "PatientAccess-1", "name": "Patient Access",
                      "address": "https://api.patientfusion.com/fhir/r4/v1/1"}},
        {"resource": {"resourceType": "Organization", "name": "Las Vegas Pediatric Urology", "endpoint": [
            {"reference": "Endpoint/ProviderSystemAccess-1"}, {"reference": "Endpoint/PatientAccess-1"}]}},
    ]}
    [practice] = practicefusion_entries({"practices": pf})
    assert practice["fhir_base_url"] == "https://api.patientfusion.com/fhir/r4/v1/1"

    [hospital] = medhost_entries({"facilities": [
        {"facilityName": "ANTELOPE MEMORIAL HOSPITAL", "npi": "1", "serviceBaseUrl": "https://fhir.yourcareuniverse.net/tenant/a"}]})
    assert hospital == {"name": "Antelope Memorial Hospital", "platform": "medhost",
                        "fhir_base_url": "https://fhir.yourcareuniverse.net/tenant/a"}


def test_unregistered_vendors_are_listed_but_not_connectable(client):
    from app import directory
    client.put("/api/settings/developer", json={"enabled": False})
    stats = directory.stats()
    assert stats["trubridge"] > 500 and stats["greenway"] > 1000 and "ihs" not in stats
    assert stats["modmed"] > 1000 and stats["nextgen"] > 3000 and stats["practicefusion"] > 3000 and stats["medhost"] > 100
    for inst_id in (directory.search(platform=p, limit=1)["results"][0]["id"]
                    for p in ("trubridge", "greenway", "modmed", "nextgen", "practicefusion", "medhost")):
        inst = client.get(f"/api/directory/{inst_id}").json()
        assert inst["mode"] == "production" and not inst["available"]
        res = client.post("/api/connections/ehr", json={"institution_id": inst_id})
        assert res.status_code == 400 and "isn't registered with" in res.json()["detail"]


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


def test_fhir_export_named_for_anyone(client):
    pid = client.get("/api/profiles").json()["profiles"][0]["id"]
    for name, expected in (("José \"Pepe\" Núñez", "syntropy-jose-pepe-nunez-fhir-"), ("李小龙", "syntropy-record-fhir-")):
        assert client.patch(f"/api/profiles/{pid}", json={"name": name}).status_code == 200
        r = client.get("/api/export/fhir", params={"profile": pid})
        assert r.status_code == 200 and f'filename="{expected}' in r.headers["content-disposition"]
