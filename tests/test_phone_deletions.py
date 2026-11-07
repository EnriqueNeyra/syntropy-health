"""What a phone says was deleted is read one item at a time, like its samples: a batch that's refused is sent again on
every sync, so one malformed deletion would otherwise stop everything after it from ever arriving."""

from __future__ import annotations

from datetime import timedelta

from app.core.db import read
from app.store import biometrics
from tests.test_accounts import _phone
from tests.test_wearable_corrections import TODAY, _profile, _set_zone

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


def _hc_stage(part: int, kind: str, start: str, end: str) -> dict:
    return {"id": f"hc-rec1-{part}", "metric_type": kind, "value": 3600, "unit": "s", "start_date": start, "end_date": end,
            "source_name": "Health Connect", "tz_offset_min": 0}


def _hc_parts(profile: str) -> dict[str, str]:
    with read() as conn:
        return dict(conn.execute("SELECT id, metric_type FROM biometric_samples WHERE profile_id = ? AND id LIKE 'hc-rec1%'",
                                 (profile,)).fetchall())


def test_a_night_android_sends_as_deleted_then_again_is_kept_and_an_edit_replaces_it(client):
    """The Android app sends a sleep session or meal as deleted (its record id) before its parts, so an edit leaves no
    old stages. The record id's tombstone mustn't stop the parts that follow."""
    profile = _profile(client)
    _set_zone(client, "UTC")
    day = (TODAY - timedelta(days=3)).isoformat()
    batch = {"device_id": "dev", "device_name": "Pixel", "profile_id": profile}
    gone = {**batch, "deleted": [{"id": "hc-rec1", "kind": "sample"}]}
    night = [_hc_stage(0, "sleep_analysis_core", f"{day}T00:00:00Z", f"{day}T01:00:00Z"),
             _hc_stage(1, "sleep_analysis_deep", f"{day}T01:00:00Z", f"{day}T02:00:00Z"),
             _hc_stage(2, "sleep_analysis_rem", f"{day}T02:00:00Z", f"{day}T03:00:00Z")]
    assert client.post("/api/ingest/wearables", json=gone).status_code == 200
    assert client.post("/api/ingest/wearables", json={**batch, "samples": night}).status_code == 200
    assert sorted(_hc_parts(profile)) == ["hc-rec1-0", "hc-rec1-1", "hc-rec1-2"]

    edited = [night[0], {**night[1], "metric_type": "sleep_analysis_core"}]     # two stages now, the second relabelled
    assert client.post("/api/ingest/wearables", json=gone).status_code == 200
    assert client.post("/api/ingest/wearables", json={**batch, "samples": edited}).status_code == 200
    assert _hc_parts(profile) == {"hc-rec1-0": "sleep_analysis_core", "hc-rec1-1": "sleep_analysis_core"}
