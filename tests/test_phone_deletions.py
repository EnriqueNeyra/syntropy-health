"""What a phone says was deleted is read one item at a time, like its samples: a batch that's refused is sent again on
every sync, so one malformed deletion would otherwise stop everything after it from ever arriving."""

from __future__ import annotations

from app.store import biometrics
from tests.test_accounts import _phone

SAMPLE = {"metric_type": "heart_rate", "value": 60, "unit": "count/min",
          "start_date": "2026-01-02T08:00:00Z", "end_date": "2026-01-02T08:00:00Z"}


def test_a_malformed_deletion_is_left_out_and_the_rest_of_the_batch_arrives(client):
    paired, anon, token, _ = _phone(client)
    me = paired["profile_id"]
    first = {"device_id": "ios-1", "device_name": "iPhone", "samples": [{**SAMPLE, "id": "gone"},
                                                                      {**SAMPLE, "id": "kept", "value": 70, "start_date": "2026-01-02T09:00:00Z",
                                                                       "end_date": "2026-01-02T09:00:00Z"}]}
    assert anon.post("/api/ingest/wearables", json=first, headers=token).json()["inserted"] == 2
    batch = {"device_id": "ios-1", "device_name": "iPhone",
             "samples": [{**SAMPLE, "id": "new", "start_date": "2026-01-03T08:00:00Z", "end_date": "2026-01-03T08:00:00Z"}],
             "deleted": [{"id": "gone"}, {"kind": "sample"}, {"id": "x", "kind": "photo"}, {"id": "y" * 201}, "gone", None, 7,
                         {"id": ["a list"]}]}
    r = anon.post("/api/ingest/wearables", json=batch, headers=token)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["inserted"] == 1 and body["deleted"] == 1 and body["deleted_rejected"] == 7
    assert body["rejected_fields"]["deleted"] == {"id": 3, "kind": 1, "item": 3}      # which field, never what it held
    assert {s["id"] for s in biometrics.recent_samples(me)} == {"kept", "new"}


def test_a_batch_of_good_deletions_says_nothing_was_left_out(client):
    paired, anon, token, _ = _phone(client)
    r = anon.post("/api/ingest/wearables", json={"device_id": "ios-1", "device_name": "iPhone",
                                                 "deleted": [{"id": "a"}, {"id": "b", "kind": "workout"}]}, headers=token)
    assert r.status_code == 200, r.text
    assert "deleted_rejected" not in r.json() and "rejected_fields" not in r.json()
