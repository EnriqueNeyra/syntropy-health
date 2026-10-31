"""What can be sent to this server before anyone has checked who's sending it, and what files can do once they're in."""

from __future__ import annotations

import gzip
import io
import json
import secrets
import sqlite3
import time
import tracemalloc
import zipfile
from pathlib import Path
from xml.etree.ElementTree import ParseError

import pytest
from fastapi.testclient import TestClient

from app.connectors import apple_health
from app.core import uploads
from tests.conftest import BASE, CSRF, LOCAL_CLIENT
from tests.test_data import EXPORT_XML

PUBLIC = ["/api/auth/login", "/api/invites/check", "/api/invites/accept", "/api/devices/pair", "/api/setup",
          "/api/connections/oauth/relay", "/api/auth/logout"]


@pytest.mark.parametrize("path", PUBLIC)
def test_public_routes_refuse_an_oversized_body_without_reading_it(raw_client, path):
    big = b'{"password": "' + b"x" * (uploads.MAX_BODY + 10) + b'"}'
    r = raw_client.post(path, content=big, headers={**CSRF, "Content-Type": "application/json"})
    assert r.status_code == 413
    assert len(r.content) < 200


def test_a_body_sent_without_its_length_is_cut_off_too(raw_client):
    sent = []

    def chunks():
        for _ in range(40):
            sent.append(1)
            yield b"x" * 64 * 1024

    r = raw_client.post("/api/auth/login", content=chunks(), headers={**CSRF, "Content-Type": "application/json"})
    assert r.status_code == 413
    assert "too large" in r.json()["detail"]


def test_everyday_requests_fit(client):
    long_chat = {"title": "t", "messages": [{"role": "user", "content": "y" * 50000}] * 40}
    r = client.post("/api/ai/chat", json={"messages": [{"role": "user", "content": "y" * 39000}] * 40, "stream": False})
    assert r.status_code != 413
    r = client.put("/api/ai/chats/c_aaaaaaaa", json=long_chat)
    assert r.status_code == 400 and "too long" in r.json()["detail"]      # its own limit, and its own words


def test_ingest_keeps_its_larger_limit(client):
    from tests.test_accounts import _phone
    paired, anon, auth, _ = _phone(client)
    samples = [{"id": f"s{i}", "metric_type": "heart_rate", "value": 60, "unit": "count/min", "start_date": "2026-01-02T08:00:00Z",
                "end_date": "2026-01-02T08:00:00Z", "source_name": "x" * 1000} for i in range(2000)]
    body = json.dumps({"device_id": "ios-1", "device_name": "iPhone", "samples": samples}).encode()
    assert len(body) > uploads.MAX_BODY
    r = anon.post("/api/ingest/wearables", content=body, headers={**auth, "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    r = anon.post("/api/ingest/wearables", content=gzip.compress(body),
                  headers={**auth, "Content-Type": "application/json", "Content-Encoding": "gzip"})
    assert r.status_code == 200, r.text
    assert anon.post("/api/ingest/wearables", content=b"x" * (uploads.MAX_INFLATED + 1), headers=auth).status_code == 413


def test_a_refused_field_isnt_sent_back(raw_client):
    secret = "my-real-password-" * 100
    r = raw_client.post("/api/auth/login", json={"password": secret}, headers=CSRF)
    assert r.status_code == 422
    assert "my-real-password" not in r.text
    assert r.json()["detail"][0]["loc"] == ["body", "password"] and r.json()["detail"][0]["msg"]


def test_nobody_signed_in_gets_a_file_read_at_all(client):
    anon = TestClient(client.app, base_url=BASE, client=LOCAL_CLIENT)
    anon.headers.update(CSRF)
    read = []

    def body():
        read.append(1)
        yield b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.zip\"\r\n\r\nzip\r\n--b--\r\n"

    for path in ("/api/imports/apple-health", "/api/imports/fhir", "/api/labs/extract"):
        r = anon.post(path, content=body(), headers={"Content-Type": "multipart/form-data; boundary=b"})
        assert r.status_code == 401, path
    assert not read


def test_a_file_with_no_room_on_the_disk_is_refused_first(client, monkeypatch):
    monkeypatch.setattr(uploads, "_no_room_for", lambda size: True)
    r = client.post("/api/imports/apple-health", files={"file": ("export.xml", b"<HealthData/>", "text/xml")})
    assert r.status_code == 507 and "disk" in r.json()["detail"]


def _bomb(path: Path, member: str, size: int = 200 * 1024 * 1024, xml: bool = True) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        if xml:
            z.writestr("apple_health_export/export.xml", EXPORT_XML)
        with z.open(member, "w", force_zip64=True) as out:
            for _ in range(size // (1024 * 1024)):
                out.write(b" " * 1024 * 1024)
    return path


def test_a_zip_bomb_among_clinical_records_is_skipped_unread(tmp_path, monkeypatch):
    path = _bomb(tmp_path / "x.zip", "apple_health_export/clinical-records/x.json")
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("apple_health_export/clinical-records/good.json", json.dumps({"resourceType": "Condition", "id": "c"}))
    export = apple_health.ExportArchive(path)
    monkeypatch.setattr(export.zip, "read", _reads_only_small(export.zip.read))
    assert [r["id"] for r in export.clinical_records()] == ["c"]
    assert export.skipped == ["x.json"]
    export.close()


def _reads_only_small(real):
    def read(info, *a):
        assert info.file_size < 1024 * 1024, "read a member it should have refused"
        return real(info, *a)
    return read


def test_clinical_records_have_a_budget_altogether(tmp_path, monkeypatch):
    monkeypatch.setattr(apple_health, "MAX_CLINICAL_TOTAL", 500)
    path = tmp_path / "x.zip"
    with zipfile.ZipFile(path, "w") as z:
        for i in range(4):
            z.writestr(f"a/clinical-records/{i}.json", json.dumps({"resourceType": "Condition", "id": str(i), "pad": "x" * 150}))
    export = apple_health.ExportArchive(path)
    assert len(export.clinical_records()) == 2 and len(export.skipped) == 2
    export.close()


def test_an_oversized_route_is_skipped(tmp_path, monkeypatch):
    path = _bomb(tmp_path / "x.zip", "apple_health_export/workout-routes/route.gpx")
    export = apple_health.ExportArchive(path)
    read = export.route_reader()
    assert read("/workout-routes/route.gpx") is None and export.skipped == ["route.gpx"]
    assert read("/workout-routes/none.gpx") is None
    export.close()


def test_an_export_xml_built_to_expand_is_refused(tmp_path):
    path = _bomb(tmp_path / "x.zip", "apple_health_export/export.xml", xml=False)
    export = apple_health.ExportArchive(path)
    with pytest.raises(ValueError):
        export.xml_opener()
    export.close()


SMALL = 1024 * 1024
_RECORD = (b'<Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Watch" unit="count/min" value="60" '
           b'startDate="2026-01-02 08:00:00 +0000" endDate="2026-01-02 08:00:00 +0000"/>')


def _items(xml: bytes) -> list:
    return list(apple_health.iter_items(io.BytesIO(xml)))


@pytest.mark.parametrize("xml", [
    b"<HealthData><Note>" + b"A" * (2 * SMALL) + b"</Note></HealthData>",                   # one text
    b'<HealthData><Note value="' + b"A" * (2 * SMALL) + b'"/></HealthData>',                # one value
    b"<HealthData>" + b"A" * (2 * SMALL) + b"</HealthData>",                                # between elements
    b"<HealthData><Note/>" + b"A" * (2 * SMALL) + b"<Note/></HealthData>",
    b"<HealthData>" + b"<a>" * 1000 + b"</a>" * 1000 + b"</HealthData>",                    # nested
    b"<HealthData><Group>" + b"<a/>" * 1100 + b"</Group></HealthData>",                     # inside one that never ends
    b"<HealthData><Workout>" + b"<a/>" * 1100 + b"</Workout></HealthData>",
    b"<HealthData><Group>" + (b'<a v="' + b"A" * 1000 + b'"/>') * 1000 + b"</Group></HealthData>",
], ids=["text", "value", "text-in-root", "text-after", "deep", "parts", "parts-in-workout", "parts-with-values"])
def test_one_enormous_part_of_export_xml_stops_the_import(monkeypatch, xml):
    monkeypatch.setattr(apple_health, "MAX_XML_ELEMENT", SMALL, raising=False)
    monkeypatch.setattr(apple_health, "MAX_XML_PARTS", 1000, raising=False)
    with pytest.raises(apple_health.TooBig):
        _items(xml)


def test_what_came_before_the_trouble_was_read(monkeypatch):
    monkeypatch.setattr(apple_health, "MAX_XML_ELEMENT", SMALL, raising=False)
    found = []
    with pytest.raises(apple_health.TooBig):
        for item in apple_health.iter_items(io.BytesIO(b"<HealthData>" + _RECORD + b"<Note>" + b"A" * (2 * SMALL) + b"</Note></HealthData>")):
            found.append(item)
    assert len(found) == 1


def test_the_limits_are_for_each_element_not_the_whole_export(monkeypatch):
    monkeypatch.setattr(apple_health, "MAX_XML_ELEMENT", SMALL, raising=False)
    monkeypatch.setattr(apple_health, "MAX_XML_PARTS", 1000, raising=False)
    entry = b'<MetadataEntry key="HKMetadataKeyHeartRateMotionContext" value="0"/>'
    record = _RECORD[:-2] + b">" + entry * 3 + b"</Record>"
    correlation = b'<Correlation type="x" startDate="2026-01-02 08:00:00 +0000" endDate="2026-01-02 08:00:00 +0000">' + record + b"</Correlation>"
    workout = (b'<Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" '
               b'startDate="2026-01-02 08:00:00 +0000" endDate="2026-01-02 08:30:00 +0000">' + entry * 900 + b"</Workout>")
    xml = b"<?xml version='1.0'?>\n<!DOCTYPE HealthData [\n<!ELEMENT HealthData (Record*)>\n]>\n<HealthData>\n <ExportDate value='x'/>\n"
    xml += (record + b"\n ") * 20000 + correlation * 500 + (workout + b"\n") * 3 + b"</HealthData>"
    assert len(xml) > 5 * SMALL
    items = _items(xml)
    assert sum(kind == "sample" for kind, _ in items) == 20500 and sum(kind == "workout" for kind, _ in items) == 3
    assert len(next(i for kind, i in items if kind == "workout")["metadata"]) == 1


def test_large_siblings_dont_pile_up_in_memory():
    """Each is allowed. They were kept until 2,000 had gone by."""
    xml = b"<HealthData>" + (b'<Note value="' + b"A" * SMALL + b'"/>') * 200 + b"</HealthData>"
    tracemalloc.start()
    try:
        assert _items(xml) == []
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 40 * SMALL, peak


def _peak_memory(work) -> tuple:
    tracemalloc.start()
    try:
        result = work()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _route_zip(path: Path, gpx: bytes) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("apple_health_export/workout-routes/route.gpx", gpx)
    return path


def test_a_route_can_be_read_once(tmp_path):
    export = apple_health.ExportArchive(_route_zip(tmp_path / "x.zip", b"<gpx/>"))
    read = export.route_reader()
    assert read("/workout-routes/route.gpx") == b"<gpx/>"
    assert read("/workout-routes/route.gpx") is None and read("workout-routes/route.gpx") is None
    export.close()


def test_a_route_stops_at_its_limit(monkeypatch):
    monkeypatch.setattr(apple_health, "MAX_ROUTE_POINTS", 1000, raising=False)
    gpx = b"<gpx><trk><trkseg>" + b'<trkpt lat="1" lon="2"/>' * 5000 + b"</trkseg></trk></gpx>"
    assert len(apple_health.parse_gpx(gpx)) == 1000
    refs = b'<WorkoutRoute><FileReference path="/a.gpx"/><FileReference path="/b.gpx"/></WorkoutRoute>'
    workout = (b'<Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" '
               b'startDate="2026-01-02 08:00:00 +0000" endDate="2026-01-02 08:30:00 +0000">' + refs + b"</Workout>")
    items = list(apple_health.iter_items(io.BytesIO(b"<HealthData>" + workout + b"</HealthData>"), lambda ref: gpx))
    assert len(items[0][1]["route"]) == 1000


@pytest.mark.parametrize("gpx", [
    b"<gpx>" + b"<a/>" * 500000 + b"</gpx>",
    b"<gpx><trk><trkseg>" + b"<a/>" * 500000 + b"</trkseg></trk></gpx>",
    b'<gpx><trk><trkseg><trkpt lat="1" lon="2">' + b"<a/>" * 500000 + b"</trkpt></trkseg></trk></gpx>",
    b'<gpx><trk><trkseg><trkpt lat="1" lon="2">' + b"<ele>1</ele>" * 200000 + b"</trkpt></trkseg></trk></gpx>",
    b"<gpx>" + b"<a>" * 500000,
], ids=["in-file", "in-segment", "in-point", "known-in-point", "deep"])
def test_whats_not_a_route_doesnt_pile_up_while_one_is_read(gpx):
    _, peak = _peak_memory(lambda: apple_health.parse_gpx(gpx))
    assert peak < 3 * len(gpx), peak


def test_a_real_route_reads_as_before():
    point = (b'<trkpt lon="-122.4" lat="37.7"><ele>12.5</ele><time>2026-01-02T08:00:00Z</time>'
             b"<extensions><speed>2.5</speed><course>10</course><hAcc>3</hAcc><vAcc>2</vAcc></extensions></trkpt>")
    gpx = b'<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><name>Route</name><trkseg>' + point * 3 + b"</trkseg></trk></gpx>"
    assert apple_health.parse_gpx(gpx) == [{"lat": 37.7, "lon": -122.4, "alt": 12.5, "t": "2026-01-02T08:00:00Z", "speed": 2.5}] * 3


def test_many_routes_arent_all_held_until_their_workouts_are_saved(client, tmp_path, monkeypatch):
    from app.services import imports
    from app.store import activity
    monkeypatch.setattr(imports, "ROUTE_POINTS_HELD", 5, raising=False)
    gpx = b'<gpx><trk><trkseg>' + b'<trkpt lat="1" lon="2"/>' * 3 + b"</trkseg></trk></gpx>"
    workouts = b"".join(
        b'<Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" '
        b'startDate="2026-01-0%d 08:00:00 +0000" endDate="2026-01-0%d 08:30:00 +0000"><WorkoutRoute>'
        b'<FileReference path="/workout-routes/%d.gpx"/></WorkoutRoute></Workout>' % (i, i, i) for i in range(1, 7))
    path = tmp_path / "x.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("apple_health_export/export.xml", b"<HealthData>" + workouts + b"</HealthData>")
        for i in range(1, 7):
            z.writestr(f"apple_health_export/workout-routes/{i}.gpx", gpx)
    sizes = []
    real = activity.insert_workouts

    def spy(profile_id, connection_id, items):
        sizes.append(len(items))
        return real(profile_id, connection_id, items)
    monkeypatch.setattr(activity, "insert_workouts", spy)
    with open(path, "rb") as fh:
        job_id = client.post("/api/imports/apple-health", files={"file": ("export.zip", fh, "application/zip")}).json()["job_id"]
    job = _wait(client, job_id)
    assert job["status"] == "completed" and job["result"]["workouts"] == 6, job
    assert max(sizes) == 2, sizes


def test_clinical_records_built_to_fill_memory_are_left_out(tmp_path, monkeypatch):
    monkeypatch.setattr(apple_health, "MAX_CLINICAL_PARTS", 1000, raising=False)
    path = tmp_path / "x.zip"
    good = {"resourceType": "Condition", "id": "1", "code": {"text": "Asthma, mild"}}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("a/clinical-records/good.json", json.dumps(good))
        z.writestr("a/clinical-records/dicts.json", b'{"resourceType":"X","a":[' + b"{}," * 2000 + b"{}]}")
        z.writestr("a/clinical-records/numbers.json", b'{"resourceType":"X","a":[' + b"1000," * 2000 + b"1]}")
        z.writestr("a/clinical-records/after.json", json.dumps({**good, "id": "2"}))
    export = apple_health.ExportArchive(path)
    assert [r["id"] for r in export.clinical_records() if r.get("id")] == ["1", "2"]
    assert export.skipped == ["dicts.json", "numbers.json"]
    export.close()


def test_a_clinical_record_nested_too_deep_is_only_that_record_lost(tmp_path):
    path = tmp_path / "x.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("a/clinical-records/deep.json", b"[" * 500000)
        z.writestr("a/clinical-records/good.json", json.dumps({"resourceType": "Condition", "id": "1"}))
    export = apple_health.ExportArchive(path)
    assert [r["id"] for r in export.clinical_records()] == ["1"]
    export.close()


def test_entities_built_to_expand_are_refused():
    laughs = b'<!DOCTYPE HealthData [<!ENTITY a "' + b"A" * 1000 + b'">' + b"".join(
        b'<!ENTITY %c "%s">' % (98 + i, b"&%c;" % (97 + i) * 10) for i in range(9)) + b"]><HealthData><Note>&j;</Note></HealthData>"
    with pytest.raises(ParseError):
        _items(laughs)


def _wait(client, job_id):
    for _ in range(200):
        job = client.get(f"/api/imports/{job_id}").json()
        if job["status"] in ("completed", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError("import didn't finish")


def _uploads():
    from app.core import config
    folder = config.data_dir() / "uploads"
    return list(folder.iterdir()) if folder.exists() else []


def _uploads_when_gone():
    """A job is marked finished just before it deletes its upload, so a look straight after can catch the file on its way out."""
    for _ in range(200):
        if not _uploads():
            break
        time.sleep(0.05)
    return _uploads()


def test_an_import_says_what_it_left_out_closes_the_file_and_deletes_it(client, monkeypatch, tmp_path):
    opened = []
    real = apple_health.ExportArchive.close

    def close(self):
        real(self)
        opened.append(self.zip.fp is None)
    monkeypatch.setattr(apple_health.ExportArchive, "close", close)
    path = _bomb(tmp_path / "x.zip", "apple_health_export/clinical-records/x.json")
    with open(path, "rb") as fh:
        job_id = client.post("/api/imports/apple-health", files={"file": ("export.zip", fh, "application/zip")}).json()["job_id"]
    job = _wait(client, job_id)
    assert job["status"] == "completed", job
    assert job["result"]["skipped_files"] == ["x.json"]
    assert opened == [True] and _uploads() == []


def test_a_bombed_upload_ends_as_an_error_with_no_file_left(client, tmp_path):
    path = _bomb(tmp_path / "x.zip", "apple_health_export/export.xml", xml=False)
    with open(path, "rb") as fh:
        job_id = client.post("/api/imports/apple-health", files={"file": ("export.zip", fh, "application/zip")}).json()["job_id"]
    job = _wait(client, job_id)
    assert job["status"] == "error" and "Nothing was imported" in job["error"]
    assert _uploads_when_gone() == []


def test_an_export_with_one_enormous_part_ends_as_an_error_in_words(client, tmp_path, monkeypatch):
    monkeypatch.setattr(apple_health, "MAX_XML_ELEMENT", SMALL, raising=False)
    path = tmp_path / "x.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z, z.open("apple_health_export/export.xml", "w") as out:
        out.write(b"<HealthData>" + _RECORD + b"<Note>")
        for _ in range(3):
            out.write(secrets.token_hex(SMALL // 2).encode())
    with open(path, "rb") as fh:
        job_id = client.post("/api/imports/apple-health", files={"file": ("export.zip", fh, "application/zip")}).json()["job_id"]
    job = _wait(client, job_id)
    assert job["status"] == "error" and "stopped there" in job["error"]
    assert _uploads_when_gone() == []


def test_a_leftover_upload_goes_at_the_next_start(isolated_data):
    from app.core import config
    from app.main import create_app
    folder = config.data_dir() / "uploads"
    folder.mkdir(parents=True)
    (folder / "upload_old.zip").write_bytes(b"a whole export")
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT):
        assert list(folder.iterdir()) == []


def test_a_command_line_backup_leaves_out_uploads_and_is_logged(client, tmp_path):
    from app import cli
    from app.core import config
    folder = config.data_dir() / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "upload_x.zip").write_bytes(b"a whole export")
    out = cli.backup(str(tmp_path / "backups"), no_encrypt=True)
    with zipfile.ZipFile(out) as z:
        assert not [n for n in z.namelist() if n.startswith("uploads")] and "syntropy.db" in z.namelist()
    assert any(e["action"] == "export.backup" and e["actor"] == "cli" for e in client.get("/api/audit").json()["events"])


def test_the_log_is_pruned_at_start_up_without_the_scheduler(isolated_data):
    from app.core import config, db
    from app.main import create_app
    db.ensure_migrated()
    with db.db() as conn:
        conn.execute("INSERT INTO audit_log(at, actor, action) VALUES (?, 'x', 'old')", (time.time() - 400 * 86400,))
    db.reset_migration_cache()
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT):
        pass
    c = sqlite3.connect(str(config.db_path()))
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'old'").fetchone()[0] == 0
    c.close()


def test_an_ordinary_file_isnt_taken_for_a_bomb():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("spaces", b" " * 2 * 1024 * 1024)
        z.writestr("tiny", b" " * 100 * 1024)
        z.writestr("xml", ("<Record type='HKQuantityTypeIdentifierStepCount' value='12' startDate='%d'/>\n" * 40000 % tuple(range(40000))).encode())
    infos = {i.filename: i for i in zipfile.ZipFile(buf).infolist()}
    assert apple_health._implausible(infos["spaces"])
    assert not apple_health._implausible(infos["tiny"]) and not apple_health._implausible(infos["xml"])
