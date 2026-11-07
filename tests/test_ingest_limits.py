"""The ingest endpoints take large bodies, compressed ones too, but only from a paired phone or someone signed in: a
body is parsed before a route's dependencies say who sent it, so the middleware asks first."""

from __future__ import annotations

import functools
import gzip
import json
import tracemalloc

import pytest
from fastapi.testclient import TestClient

from app.core import uploads
from tests.conftest import BASE, CSRF
from tests.test_accounts import _phone



@functools.cache
def _bomb() -> bytes:
    """A few dozen kilobytes that inflate to 64 MB of JSON: over a gigabyte once parsed into Python objects."""
    n = (uploads.MAX_INFLATED - 20) // 3
    return gzip.compress(b'{"samples":[' + b"{}," * (n - 1) + b"{}]}", 9)


class Counting:
    """Outside everything else: how much of a request's body the server pulled from the client."""

    def __init__(self, app):
        self.app, self.read = app, 0

    async def __call__(self, scope, receive, send):
        async def counted():
            message = await receive()
            self.read += len(message.get("body", b""))
            return message
        await self.app(scope, counted, send)


@pytest.fixture
def inflated(monkeypatch):
    """Each time the server inflates a body: the limit it was given."""
    calls = []
    real = uploads.inflate

    def spy(body, encoding, limit=uploads.MAX_INFLATED):
        calls.append(limit)
        return real(body, encoding, limit)
    monkeypatch.setattr(uploads, "inflate", spy)
    return calls


@pytest.mark.parametrize("credentials", [{}, {"X-Syntropy-Device-Token": "not a token"}, {"Authorization": "Bearer nope"}])
def test_a_compressed_body_from_nobody_is_neither_read_nor_inflated(client, inflated, credentials):
    counting = Counting(client.app)
    stranger = TestClient(counting, base_url=BASE, client=("192.168.1.77", 1234))
    bomb = _bomb()
    tracemalloc.start()
    try:
        r = stranger.post("/api/ingest/wearables", content=bomb,
                          headers={**credentials, "Content-Encoding": "gzip", "Content-Type": "application/json"})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert r.status_code == 401, r.text
    assert not inflated and counting.read == 0
    assert peak < 8 * uploads.MB, f"{peak / uploads.MB:.0f} MB"


@pytest.mark.parametrize("path", ["/api/ingest/wearables", "/api/ingest/healthkit-export"])
def test_a_plain_body_from_nobody_isnt_read_either(client, path):
    read = []

    def body():
        read.append(1)
        yield b'{"samples":[' + b"{}," * 100_000 + b"{}]}"

    stranger = TestClient(client.app, base_url=BASE, client=("192.168.1.77", 1234))
    r = stranger.post(path, content=body(), headers={"Content-Type": "application/json"})
    assert r.status_code == 401
    assert not read


def test_a_paired_phone_cant_send_a_body_built_to_inflate(client, inflated):
    _, anon, token, _ = _phone(client)
    r = anon.post("/api/ingest/wearables", content=_bomb(),
                  headers={**token, "Content-Encoding": "gzip", "Content-Type": "application/json"})
    assert r.status_code == 413
    # It stopped at a hundred times what was sent (or 8 MB), not at 64 MB.
    assert inflated == [max(uploads.INFLATED_ANYWAY, len(_bomb()) * uploads.MAX_RATIO)] and inflated[0] < uploads.MAX_INFLATED


def test_a_paired_phones_real_batches_still_arrive_compressed(client, inflated):
    _, anon, token, _ = _phone(client)
    samples = [{"id": f"{i:08x}-6a1e-4c55-9d1c-{i * 7919:012x}", "metric_type": "heart_rate", "value": 50 + i % 90,
                "unit": "count/min", "start_date": f"2026-01-02T{i // 3600 % 24:02d}:{i // 60 % 60:02d}:{i % 60:02d}Z",
                "end_date": f"2026-01-02T{i // 3600 % 24:02d}:{i // 60 % 60:02d}:{i % 60:02d}Z", "source_name": "Apple Watch",
                "hk_identifier": "HKQuantityTypeIdentifierHeartRate", "device_name": "Apple Watch",
                "metadata": {"HKMetadataKeyHeartRateMotionContext": i % 3, "HKMetadataKeySyncIdentifier": f"{i * 104729:032x}",
                             "HKMetadataKeySyncVersion": 1, "HKDeviceName": "Apple Watch Series 9"}}
               for i in range(20000)]
    body = json.dumps({"device_id": "ios-1", "device_name": "iPhone", "samples": samples}).encode()
    assert len(body) > uploads.INFLATED_ANYWAY
    r = anon.post("/api/ingest/wearables", content=gzip.compress(body),
                  headers={**token, "Content-Encoding": "gzip", "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 20000 and len(inflated) == 1


def test_the_web_app_signed_in_still_sends_to_ingest(client):
    sample = {"id": "s1", "metric_type": "heart_rate", "value": 60, "unit": "count/min",
              "start_date": "2026-01-02T08:00:00Z", "end_date": "2026-01-02T08:00:00Z"}
    r = client.post("/api/ingest/wearables", json={"device_id": "web", "device_name": "Browser", "samples": [sample]})
    assert r.status_code == 200, r.text
    # Without the header that stops other sites from sending it, it's refused before it's read.
    assert client.post("/api/ingest/wearables", json={"device_id": "web", "device_name": "Browser"},
                       headers={"X-Requested-With": ""}).status_code == 403


def test_the_ais_larger_limit_is_for_someone_signed_in(client):
    big = json.dumps({"messages": [{"role": "user", "content": "y" * 39000}] * 40, "stream": False}).encode()
    assert uploads.MAX_BODY < len(big) < 16 * uploads.MB
    stranger = TestClient(client.app, base_url=BASE, client=("192.168.1.77", 1234))
    r = stranger.post("/api/ai/chat", content=big, headers={**CSRF, "Content-Type": "application/json"})
    assert r.status_code == 413
    # A small one from nobody is refused by the route as before.
    assert stranger.post("/api/ai/chat", json={"messages": []}, headers=CSRF).status_code == 401
    # Signed in, as before.
    assert client.post("/api/ai/chat", content=big, headers={"Content-Type": "application/json"}).status_code != 413
