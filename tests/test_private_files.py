"""Uploads, import copies and archives being built: nothing readable on the disk, even for the moment they're there."""

from __future__ import annotations

import io
import json
import os
import random
import stat
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import config, private_files
from app.core.private_files import CHUNK, Damaged
from app.services import exports, imports
from app.store import profiles
from tests.conftest import BASE, LOCAL_CLIENT

MARKER = b"ZEBRAQUARTZ-DIAGNOSIS"


def _anywhere_on_disk(folder: Path, marker: bytes = MARKER) -> list[str]:
    return [str(f) for f in folder.rglob("*") if f.is_file() and marker in f.read_bytes()]


def test_what_goes_in_comes_back_out_in_any_order(tmp_path):
    data = random.Random(7).randbytes(CHUNK * 5 + 1234)
    file = private_files.create(tmp_path / "f")
    for start in range(0, len(data), 70_000):
        file.write(data[start:start + 70_000])
    assert file.tell() == len(data)
    file.seek(0)
    assert file.read() == data
    rng = random.Random(1)
    for _ in range(200):
        where = rng.randrange(len(data))
        size = rng.randrange(1, CHUNK * 2)
        file.seek(where)
        assert file.read(size) == data[where:where + size]
    file.seek(-10, os.SEEK_END)
    assert file.read() == data[-10:]
    assert file.read(5) == b""
    file.close()


def test_a_chunk_boundary_and_an_empty_file_are_nothing_special(tmp_path):
    for size in (0, 1, CHUNK - 1, CHUNK, CHUNK + 1, CHUNK * 2):
        data = b"x" * size
        file = private_files.create(tmp_path / f"f{size}")
        file.write(data)
        file.seek(0)
        assert file.read() == data and file.size == size
        file.close()


def test_nothing_on_the_disk_can_be_read_and_the_file_is_private(tmp_path):
    file = private_files.create(tmp_path / "f")
    file.write(MARKER * 100_000)
    file.finish()
    assert MARKER not in (tmp_path / "f").read_bytes()
    assert os.name == "nt" or stat.S_IMODE((tmp_path / "f").stat().st_mode) == 0o600
    file.close()


def test_a_changed_or_moved_chunk_does_not_pass(tmp_path):
    file = private_files.create(tmp_path / "f")
    file.write(b"a" * (CHUNK * 3))
    private_files.keep(tmp_path / "f", file)
    raw = bytearray((tmp_path / "f").read_bytes())
    swapped = raw[:CHUNK + 16], raw[CHUNK + 16:2 * (CHUNK + 16)]
    raw[:CHUNK + 16], raw[CHUNK + 16:2 * (CHUNK + 16)] = swapped[1], swapped[0]
    (tmp_path / "f").write_bytes(bytes(raw))
    with private_files.open_read(tmp_path / "f") as again:
        with pytest.raises(Damaged):
            again.read()


def test_without_this_process_its_key_is_gone(tmp_path):
    file = private_files.create(tmp_path / "f")
    file.write(MARKER)
    private_files.keep(tmp_path / "f", file)
    assert MARKER in private_files.open_read(tmp_path / "f").read()
    private_files.forget(tmp_path / "f")      # as a new process would be
    assert MARKER not in private_files.open_read(tmp_path / "f").read()


def test_written_once_only(tmp_path):
    file = private_files.create(tmp_path / "f")
    file.write(b"x")
    file.seek(0)
    with pytest.raises(OSError):
        file.write(b"y")
    file.close()


def test_a_zip_can_be_written_through_one_and_read_back(tmp_path):
    file = private_files.create(tmp_path / "f", streaming=True)
    with zipfile.ZipFile(file, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        z.writestr("a.txt", MARKER * 1000)
        with z.open("b.bin", "w", force_zip64=True) as out:
            out.write(os.urandom(CHUNK * 2))
    file.finish()
    assert MARKER not in (tmp_path / "f").read_bytes()
    with zipfile.ZipFile(file) as z:
        assert z.read("a.txt") == MARKER * 1000 and z.testzip() is None
    file.close()


def test_large_uploads_are_spooled_encrypted(client, tmp_path, monkeypatch):
    """Starlette's stand-in is what keeps a large upload while its request runs. If a new Starlette stops calling it by this
    name, this fails rather than sending uploads to the temporary folder in the clear."""
    from starlette import formparsers

    assert formparsers.SpooledTemporaryFile is private_files.Spool
    seen = []
    real = private_files.Spool.write

    def watching(self, data):
        seen.append(self)
        return real(self, data)
    monkeypatch.setattr(private_files.Spool, "write", watching)
    body = json.dumps({"resourceType": "Bundle", "type": "collection", "entry": [
        {"resource": {"resourceType": "Observation", "id": f"o{i}", "status": "final", "code": {"text": "Weight " + MARKER.decode()}}} for i in range(3000)]})
    assert len(body) > 200_000
    r = client.post("/api/imports/fhir", files={"file": ("big.json", body.encode(), "application/json")})
    assert r.status_code in (200, 400), r.text       # read and understood, whatever it made of the contents
    assert seen and all(isinstance(s, private_files.Spool) for s in seen)
    assert all(s.closed for s in seen)


def test_the_copy_an_import_reads_is_encrypted_and_goes_when_it_is_done(client, tmp_path, monkeypatch):
    xml = b'<?xml version="1.0"?><HealthData><Record type="HKQuantityTypeIdentifierStepCount" value="10" unit="count" ' \
          b'sourceName="' + MARKER + b'" startDate="2026-01-01 08:00:00 +0000" endDate="2026-01-01 08:10:00 +0000"/></HealthData>'
    during = {}
    real = imports._run_apple_health

    def looking(job_id, profile_id, path, filename, stop=None):
        during["clear"] = _anywhere_on_disk(config.data_dir() / "uploads")
        during["path"] = path
        return real(job_id, profile_id, path, filename, stop)
    monkeypatch.setattr(imports, "_run_apple_health", looking)
    r = client.post("/api/imports/apple-health", files={"file": ("export.xml", xml, "text/xml")})
    assert r.status_code == 200, r.text
    job = client.get(f"/api/imports/{r.json()['job_id']}").json()
    for _ in range(100):
        job = client.get(f"/api/imports/{r.json()['job_id']}").json()
        if job["status"] in ("completed", "error"):
            break
        import time
        time.sleep(0.05)
    assert job["status"] == "completed", job
    assert during["clear"] == [] and not Path(during["path"]).exists()
    assert str(during["path"]) not in private_files._kept


def test_the_archive_is_never_on_the_disk_in_the_clear_and_downloads_whole(client, monkeypatch):
    pid = profiles.default_profile()["id"]
    from app.store import activity, connections
    source = connections.create(pid, "import", "file", "A file", mode="live")["id"]
    activity.insert_events(pid, source, [{"type": "note", "occurred_at": "2026-01-01T00:00:00Z", "note": MARKER.decode(), "title": MARKER.decode()}])
    during = {}
    real = exports.build_archive

    def looking(*a):
        built = real(*a)
        during["clear"] = _anywhere_on_disk(built.folder)
        during["folder"] = built.folder
        return built
    monkeypatch.setattr(exports, "build_archive", looking)
    r = client.get("/api/export/archive")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert r.headers["content-length"] == str(len(r.content)) and "attachment" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert z.testzip() is None and "manifest.json" in z.namelist()
    assert during["clear"] == [] and not during["folder"].exists()


def test_what_a_crash_leaves_in_uploads_goes_at_the_next_start(client, tmp_path):
    folder = config.data_dir() / "uploads"
    folder.mkdir(exist_ok=True)
    (folder / "upload_left.zip").write_bytes(b"from a crash")
    from app.main import create_app
    with TestClient(create_app(), base_url=BASE, client=LOCAL_CLIENT):
        assert list(folder.iterdir()) == []
