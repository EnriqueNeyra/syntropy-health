"""What can be sent to this server before anyone has checked who's sending it, and what files can do once they're in."""

from __future__ import annotations

import gzip
import io
import json
import sqlite3
import time
import zipfile
from pathlib import Path

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
    assert _uploads() == []


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
    out = cli.backup(str(tmp_path / "backups"))
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
