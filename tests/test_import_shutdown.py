"""A server that's stopping during an Apple Health import: the import ends cleanly between batches, and doesn't keep the
process running after the app has closed."""

from __future__ import annotations

import contextlib
import threading
import time

from fastapi.testclient import TestClient

from app.services import imports


def _endless_import(monkeypatch, tmp_path, client):
    from app.connectors import apple_health

    started = threading.Event()

    def items(_stream, _reader):
        while True:
            started.set()
            yield "sample", {"metric": "steps"}
            time.sleep(0.001)

    @contextlib.contextmanager
    def opener():
        yield None

    monkeypatch.setattr(apple_health, "open_export", lambda path: (opener, []))
    monkeypatch.setattr(apple_health, "zip_reader", lambda path: None)
    monkeypatch.setattr(apple_health, "iter_items", items)
    monkeypatch.setattr(imports.biometrics, "insert_samples", lambda profile, conn, batch: {"inserted": len(batch)})
    upload = tmp_path / "export.zip"
    upload.write_bytes(b"x")
    job_id = imports.start_apple_health_import(client.get("/api/profiles").json()[0]["id"], upload, "export.zip")
    assert started.wait(5)
    return job_id, upload


def test_stopping_ends_a_running_import_with_words_for_the_person(client, monkeypatch, tmp_path):
    job_id, upload = _endless_import(monkeypatch, tmp_path, client)
    began = time.monotonic()
    imports.stop_running()
    assert time.monotonic() - began < 5
    job = imports.get_job(job_id)
    assert job["status"] == "error" and job["error"] == imports.STOPPED and job["finished_at"]
    assert not upload.exists()
    assert not imports._running


def test_a_new_import_after_a_stop_runs_normally(client, monkeypatch, tmp_path):
    first, _ = _endless_import(monkeypatch, tmp_path, client)
    imports.stop_running()
    second, _ = _endless_import(monkeypatch, tmp_path, client)
    assert imports.get_job(second)["status"] == "running"
    imports.stop_running()
    assert imports.get_job(second)["status"] == "error"


def test_an_import_doesnt_hold_up_the_event_loops_shutdown(client, monkeypatch, tmp_path):
    # On the loop's default executor it did: asyncio waits for those threads at the end, with the server otherwise gone.
    _endless_import(monkeypatch, tmp_path, client)
    assert [t.name for t in imports._running] == ["import"]
    imports.stop_running()


def test_server_shutdown_stops_imports(isolated_data, monkeypatch):
    from app.main import create_app

    stopped = []
    monkeypatch.setattr(imports, "stop_running", lambda: stopped.append(True))
    with TestClient(create_app()):
        assert not stopped
    assert stopped == [True]
