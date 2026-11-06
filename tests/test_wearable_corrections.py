"""What the wearable paths do with corrections, odd data and interruptions: each test is a way real numbers used to go wrong."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from app.connectors import apple_health, google_health, oura, whoop
from app.connectors import wearable_common as wc
from app.connectors.smart import SmartError
from app.core import config
from app.core.db import db, read
from app.services import imports, sync
from app.store import activity, biometrics, connections, sample_counts, sleep, training
from tests.conftest import BASE

TODAY = datetime.now(timezone.utc).date()
YESTERDAY = TODAY - timedelta(days=1)


def _profile(client) -> str:
    return client.get("/api/status").json()["profiles"][0]["id"]


def _iso(d: date, clock: str = "12:00:00") -> str:
    return f"{d.isoformat()}T{clock}Z"


def _s(sample_id, metric="step_count", value=100.0, start=None, end=None, source="Watch", unit="count", **extra):
    start = start or _iso(YESTERDAY)
    return {"id": sample_id, "metric_type": metric, "value": value, "unit": unit, "start_date": start, "end_date": end or start,
            "source_name": source, **extra}


def _daily(profile, metric, days=10, source=None):
    return {p["day"]: p for p in biometrics.daily_series(profile, metric, days=days, source=source)["points"]}


def _set_zone(client, zone):
    assert client.put("/api/settings/general", json={"timezone": zone}).status_code == 200


def _live(monkeypatch, client, provider, *payloads, profile=None):
    """A live connection whose provider answers with each payload in turn."""
    profile = profile or _profile(client)
    conn = connections.create(profile, "wearable", provider, provider.title(), mode="live", credentials={"access_token": "AT"})
    answers = iter(payloads)
    module = sync.WEARABLE_MODULES[provider]

    async def fetch(creds, since, until):
        return next(answers)
    monkeypatch.setattr(module, "fetch_live", fetch)
    return conn


def _oura_daily(day, steps, score=None, row_id="d1"):
    return {"daily_activity": [{"id": row_id, "day": day, "steps": steps, "timestamp": f"{day}T04:00:00+00:00"}],
            "daily_sleep": [{"id": f"{row_id}s", "day": day, "score": score, "timestamp": f"{day}T04:00:00+00:00"}] if score else []}


# ---------------------------------------------------------------------------
# Corrections win (F009)
# ---------------------------------------------------------------------------

def test_a_later_sync_corrects_a_providers_daily_figures(client, monkeypatch):
    profile, day = _profile(client), YESTERDAY.isoformat()
    conn = _live(monkeypatch, client, "oura", _oura_daily(day, 2000, 70), _oura_daily(day, 11000, 82))
    first = asyncio.run(sync.sync_connection(conn["id"]))
    assert first["status"] == "success" and first["inserted"] == 2 and first["updated"] == 0
    second = asyncio.run(sync.sync_connection(conn["id"]))
    assert second["inserted"] == 0 and second["updated"] == 2
    assert _daily(profile, "step_count")[day]["value"] == 11000
    assert _daily(profile, "sleep_score")[day]["value"] == 82


def test_a_new_id_for_a_providers_one_daily_figure_replaces_the_old_one(client, monkeypatch):
    profile, day = _profile(client), YESTERDAY.isoformat()
    conn = _live(monkeypatch, client, "oura", _oura_daily(day, 2000, row_id="a"), _oura_daily(day, 5000, row_id="b"))
    asyncio.run(sync.sync_connection(conn["id"]))
    asyncio.run(sync.sync_connection(conn["id"]))
    assert _daily(profile, "step_count")[day]["value"] == 5000


def test_google_and_whoop_figures_are_corrected_too(client, monkeypatch):
    profile, day = _profile(client), YESTERDAY.isoformat()
    dd = {"year": YESTERDAY.year, "month": YESTERDAY.month, "day": YESTERDAY.day}
    steps = lambda n: {"rollup:steps": [{"civilStartTime": {"date": dd}, "steps": {"countSum": str(n)}}]}
    conn = _live(monkeypatch, client, "google", steps(3000), steps(9000))
    asyncio.run(sync.sync_connection(conn["id"]))
    asyncio.run(sync.sync_connection(conn["id"]))
    assert _daily(profile, "step_count")[day]["value"] == 9000
    cycle = lambda strain: {"cycle": [{"id": 5, "start": _iso(YESTERDAY, "02:00:00"), "score_state": "SCORED",
                                       "score": {"strain": strain, "kilojoule": strain * 1000}}]}
    conn = _live(monkeypatch, client, "whoop", cycle(3.2), cycle(14.8))
    asyncio.run(sync.sync_connection(conn["id"]))
    asyncio.run(sync.sync_connection(conn["id"]))
    assert _daily(profile, "strain_score", source="WHOOP")[day]["value"] == 14.8


def test_a_phone_resend_with_an_edited_value_corrects_and_counts_separately(client):
    profile, day = _profile(client), YESTERDAY.isoformat()
    assert biometrics.insert_samples(profile, None, [_s("x1", value=100)])["inserted"] == 1
    again = biometrics.insert_samples(profile, None, [_s("x1", value=100)])
    assert (again["inserted"], again["updated"], again["duplicates"]) == (0, 0, 1)
    edited = biometrics.insert_samples(profile, None, [_s("x1", value=250)])
    assert (edited["inserted"], edited["updated"], edited["duplicates"]) == (0, 1, 0)
    assert _daily(profile, "step_count")[day]["value"] == 250


def test_a_changed_end_moves_the_reading_off_the_day_it_was_on(client):
    profile = _profile(client)
    d1, d2 = TODAY - timedelta(days=3), TODAY - timedelta(days=2)
    biometrics.insert_samples(profile, None, [_s("slp", "sleep_analysis_core", 7 * 3600, _iso(d1, "01:00:00"), _iso(d1, "08:00:00"), unit="s")])
    assert d1.isoformat() in _daily(profile, "sleep_duration")
    biometrics.insert_samples(profile, None, [_s("slp", "sleep_analysis_core", 7 * 3600, _iso(d2, "01:00:00"), _iso(d2, "08:00:00"), unit="s")])
    days = _daily(profile, "sleep_duration")
    assert d1.isoformat() not in days and d2.isoformat() in days


def test_a_second_person_sending_the_same_id_doesnt_overwrite_the_first(client):
    alex = _profile(client)
    jordan = client.post("/api/profiles", json={"name": "Jordan", "relationship": "partner"}).json()["id"]
    biometrics.insert_samples(alex, None, [_s("same", value=100)])
    assert biometrics.insert_samples(jordan, None, [_s("same", value=999)])["inserted"] == 1
    assert _daily(alex, "step_count")[YESTERDAY.isoformat()]["value"] == 100
    assert _daily(jordan, "step_count")[YESTERDAY.isoformat()]["value"] == 999
    assert biometrics.insert_samples(jordan, None, [_s("same", value=1000)])["updated"] == 1
    assert _daily(alex, "step_count")[YESTERDAY.isoformat()]["value"] == 100


def test_workouts_and_events_are_corrected_without_losing_what_a_resend_leaves_out(client):
    profile = _profile(client)
    w = {"id": "w1", "name": "Run", "start": _iso(YESTERDAY), "end": _iso(YESTERDAY, "13:00:00"), "active_energy_kcal": 100.0,
         "heart_rate": [{"t": _iso(YESTERDAY), "min": 100, "avg": 120, "max": 130}], "source_name": "Watch"}
    assert activity.store_workouts(profile, None, [w])["inserted"] == 1
    fixed = {k: v for k, v in w.items() if k != "heart_rate"} | {"active_energy_kcal": 450.0}
    assert activity.store_workouts(profile, None, [fixed]) == {"inserted": 0, "updated": 1, "rejected": 0}
    assert activity.store_workouts(profile, None, [fixed]) == {"inserted": 0, "updated": 0, "rejected": 0}
    stored = activity.get_workout(profile, "w1")
    assert stored["active_energy_kcal"] == 450 and stored["heart_rate"]
    e = {"id": "e1", "event_type": "headache", "name": "Headache", "start": _iso(YESTERDAY), "value_label": "Mild"}
    activity.store_events(profile, None, [e])
    assert activity.store_events(profile, None, [{**e, "value_label": "Severe"}])["updated"] == 1
    assert activity.get_event(profile, "e1")["value_label"] == "Severe"


def test_simulated_days_keep_their_figures_when_synced_again_over_a_different_span():
    for module, seed in ((oura, "oura-x"), (whoop, "whoop-x"), (google_health, "google-x")):
        until = TODAY - timedelta(days=2)
        short = {s["id"]: s["value"] for s in module.normalize(module.simulate(days=5, until=until, seed=seed))}
        long = {s["id"]: s["value"] for s in module.normalize(module.simulate(days=40, until=until, seed=seed))}
        assert short and all(long[k] == v for k, v in short.items() if k in long and "gh-weight" not in k), module.__name__


# ---------------------------------------------------------------------------
# Re-sent aggregates (F010)
# ---------------------------------------------------------------------------

def _export(metric, units, *rows):
    return {"data": {"metrics": [{"name": metric, "units": units, "data": list(rows)}]}}


def test_a_growing_daily_total_replaces_rather_than_adds(client):
    day = (TODAY - timedelta(days=2)).isoformat()
    for qty in (1000, 3000, 6000, 9000):
        r = client.post("/api/ingest/healthkit-export", json=_export("step_count", "count", {"date": f"{day} 00:00:00 -0400", "qty": qty, "source": "Fit"}))
        assert r.status_code == 200
    assert _daily(_profile(client), "step_count")[day]["value"] == 9000


def test_the_automation_header_says_what_is_a_group(client):
    day = (TODAY - timedelta(days=2)).isoformat()
    grouped = {"automation-aggregation": "hours"}
    for qty in (100, 400):
        client.post("/api/ingest/healthkit-export", json=_export("step_count", "count", {"date": f"{day} 10:00:00 +0000", "qty": qty, "source": "Fit"}), headers=grouped)
    assert _daily(_profile(client), "step_count")[day]["value"] == 400
    # Raw rows are real samples: two entries at one moment with different values both count.
    raw = {"automation-aggregation": "raw"}
    other = (TODAY - timedelta(days=3)).isoformat()
    for qty in (300, 200):
        client.post("/api/ingest/healthkit-export", json=_export("dietary_energy", "kcal", {"date": f"{other} 10:00:00 +0000", "qty": qty, "source": "Food"}), headers=raw)
    assert _daily(_profile(client), "dietary_energy")[other]["value"] == 500


def test_rows_in_one_request_with_the_same_date_add_up(client):
    day = (TODAY - timedelta(days=2)).isoformat()
    rows = [{"date": f"{day} 10:00:00 +0000", "qty": q, "source": "Food"} for q in (300, 200)]
    client.post("/api/ingest/healthkit-export", json=_export("dietary_energy", "kcal", *rows))
    assert _daily(_profile(client), "dietary_energy")[day]["value"] == 500


def test_hourly_rows_arent_mistaken_for_daily_totals(client):
    day = (TODAY - timedelta(days=2)).isoformat()
    rows = [{"date": f"{day} {h:02d}:00:00 +0000", "qty": 100, "source": "Fit"} for h in range(4)]
    client.post("/api/ingest/healthkit-export", json=_export("step_count", "count", *rows))
    _set_zone(client, "UTC")
    assert _daily(_profile(client), "step_count")[day]["value"] == 400


def test_aggregated_sleep_replaces_even_when_its_times_move(client):
    profile, day = _profile(client), (TODAY - timedelta(days=2)).isoformat()
    def night(total, deep, end):
        return _export("sleep_analysis", "hr", {"date": f"{day} 00:00:00 +0000", "totalSleep": total, "deep": deep, "source": "S",
                                                "sleepStart": f"{day} 00:30:00 +0000", "sleepEnd": f"{day} {end}:00 +0000"})
    client.post("/api/ingest/healthkit-export", json=night(5.0, 1.0, "05:30"))
    client.post("/api/ingest/healthkit-export", json=night(7.5, 0, "08:00"))
    assert _daily(profile, "sleep_duration")[day]["value"] == 7.5
    assert day not in _daily(profile, "sleep_deep")          # revised down to nothing


# ---------------------------------------------------------------------------
# Units (F011) and fractions (F151)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("metric,name,units,qty,expected", [
    ("blood_glucose", "blood_glucose", "mmol/L", 5.6, 100.9), ("blood_glucose", "blood_glucose", "mmol<180.15588000005408>/L", 5.6, 100.9),
    ("body_temperature", "body_temperature", "°F", 98.6, 37.0), ("body_mass", "weight", "lbs", 180, 81.65),
    ("distance_cycling", "cycling_distance", "km", 20, 20000), ("distance_cycling", "cycling_distance", "mi", 20, 32186.88),
    ("distance_swimming", "swimming_distance", "yd", 1000, 914.4), ("height", "height", "in", 70, 177.8), ("height", "height", "ft", 5.83, 177.7),
    ("waist_circumference", "waist_circumference", "in", 34, 86.36), ("dietary_water", "dietary_water", "fl_oz_us", 64, 1892.71),
    ("dietary_energy", "dietary_energy", "kJ", 8368, 2000), ("running_speed", "running_speed", "km/hr", 12, 3.33),
    ("running_speed", "running_speed", "mi/hr", 7.5, 3.35), ("running_stride_length", "running_stride_length", "ft", 3.5, 1.07),
    ("basal_body_temperature", "basal_body_temperature", "degF", 97.7, 36.5),
])
def test_export_json_units_become_the_metrics_own(client, metric, name, units, qty, expected):
    day = (TODAY - timedelta(days=2)).isoformat()
    # Far enough into the day that no zone moves it, and raw so it's plainly one reading.
    r = client.post("/api/ingest/healthkit-export", json=_export(name, units, {"date": f"{day} 12:00:00 +0000", "qty": qty, "source": "App"}),
                    headers={"automation-aggregation": "raw"})
    assert r.status_code == 200
    assert _daily(_profile(client), metric)[day]["value"] == pytest.approx(expected, abs=0.02)


def test_a_unit_nobody_recognises_is_kept_flagged_and_counted_not_shown_as_canonical(client):
    day = (TODAY - timedelta(days=2)).isoformat()
    r = client.post("/api/ingest/healthkit-export", json=_export("weight", "stones and pebbles", {"date": f"{day} 12:00:00 +0000", "qty": 12, "source": "App"})).json()
    assert r["flagged"] == 1
    profile = _profile(client)
    assert day not in _daily(profile, "body_mass")
    assert biometrics.recent_samples(profile, "body_mass", left_out_only=True)[0]["flag"] == "unit"


def test_workout_elevation_and_distance_follow_their_units(client):
    w = {"name": "Hike", "start": _iso(YESTERDAY), "end": _iso(YESTERDAY, "13:00:00"), "elevationUp": {"qty": 1000, "units": "ft"},
         "distance": {"qty": 5, "units": "mi"}, "source": "App"}
    assert client.post("/api/ingest/healthkit-export", json={"data": {"workouts": [w]}}).status_code == 200
    stored = client.get("/api/biometrics/workouts", params={"days": 0}).json()["workouts"][0]
    assert stored["elevation_ascent_m"] == pytest.approx(304.8) and stored["distance_m"] == pytest.approx(8046.72)


def test_phone_ingest_converts_foreign_units_but_leaves_the_phones_own_labels(client):
    profile, day = _profile(client), YESTERDAY.isoformat()
    biometrics.insert_samples(profile, None, [_s("a", "body_mass", 180, unit="lb"), _s("b", "blood_glucose", 5.6, unit="mmol/L"),
                                              _s("c", "body_temperature", 98.6, unit="degF")])
    assert _daily(profile, "body_mass")[day]["value"] == pytest.approx(81.65, abs=0.01)
    assert _daily(profile, "blood_glucose")[day]["value"] == pytest.approx(100.89, abs=0.01)
    assert _daily(profile, "body_temperature")[day]["value"] == 37
    # The labels the iPhone and Android catalogs actually send, and an empty one, change nothing.
    own = [("heart_rate", "count/min", 60), ("body_temperature", "degC", 37), ("vo2_max", "mL/(kg·min)", 40), ("step_count", "count", 10),
           ("physical_effort", "kcal/(kg·hr)", 3), ("environmental_audio_exposure", "dBASPL", 60), ("height", "cm", 170), ("dietary_vitamin_a", "mcg", 70),
           ("forced_vital_capacity", "L", 4), ("insulin_delivery", "IU", 3), ("electrodermal_activity", "µS", 2), ("body_mass", "", 80),
           ("dietary_water", "mL", 250), ("apple_exercise_time", "min", 30), ("cycling_cadence", "count/min", 80), ("dietary_calcium", "mg", 20)]
    r = biometrics.insert_samples(profile, None, [_s(f"own{i}", m, v, _iso(TODAY - timedelta(days=4)), unit=u) for i, (m, u, v) in enumerate(own)])
    assert r["flagged"] == 0 and r["rejected"] == 0
    for m, u, v in own:
        assert _daily(profile, m, 10)[(TODAY - timedelta(days=4)).isoformat()]["value"] == v, m


def test_a_small_percentage_can_say_what_it_is(client):
    profile, day = _profile(client), YESTERDAY.isoformat()
    biometrics.insert_samples(profile, None, [_s("p", "walking_asymmetry", 0.9, unit="percent"), _s("f", "walking_double_support", 0.9, unit="fraction"),
                                              _s("v1", "oxygen_saturation", 0.97, unit="%")])
    assert _daily(profile, "walking_asymmetry")[day]["value"] == 0.9
    assert _daily(profile, "walking_double_support")[day]["value"] == 90
    assert _daily(profile, "oxygen_saturation")[day]["value"] == 97      # what both phones send today


def test_export_json_percentages_stay_percentages(client):
    day = (TODAY - timedelta(days=2)).isoformat()
    client.post("/api/ingest/healthkit-export", json=_export("walking_asymmetry_percentage", "%", {"date": f"{day} 12:00:00 +0000", "qty": 0.9, "source": "A"}))
    assert _daily(_profile(client), "walking_asymmetry")[day]["value"] == 0.9


# ---------------------------------------------------------------------------
# The same sample from two paths (F012), and from which it stays (F142)
# ---------------------------------------------------------------------------

def _export_xml(*records: str) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n' + "\n".join(records) + "\n</HealthData>\n").encode()


def _record(rtype, value, unit, start="2026-09-20 10:00:00 +0000", end="2026-09-20 10:05:00 +0000", source="Watch", extra=""):
    return f'<Record type="HKQuantityTypeIdentifier{rtype}" sourceName="{source}" unit="{unit}" startDate="{start}" endDate="{end}" value="{value}"{extra}/>'


def _import(client, profile, content: bytes, name="export.xml"):
    job_id = imports._new_job("apple_health", name, profile)
    path = imports.upload_dir() / f"{job_id}.xml"
    path.write_bytes(content)
    imports._run_apple_health(job_id, profile, path, name)
    return imports.get_job(job_id)


@pytest.mark.parametrize("phone_first", [True, False])
@pytest.mark.parametrize("rtype,metric,value,unit,phone", [("DistanceWalkingRunning", "distance_walking_running", 0.0123, "mi", 19.794931),
                                                           ("DistanceWalkingRunning", "distance_walking_running", 0.0198, "km", 19.794931),
                                                           ("ActiveEnergyBurned", "active_energy", 0.893, "kJ", 0.2134)])
def test_export_and_phone_copies_of_one_sample_count_once(client, phone_first, rtype, metric, value, unit, phone):
    profile = _profile(client)
    send = lambda: biometrics.insert_samples(profile, None, [_s("UUID-1", metric, phone, "2026-09-20T10:00:00Z", "2026-09-20T10:05:00Z", unit="")])
    if phone_first:
        send()
    assert _import(client, profile, _export_xml(_record(rtype, value, unit)))["status"] == "completed"
    if not phone_first:
        send()
    with read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM biometric_samples WHERE metric_type = ?", (metric,)).fetchone()[0] == 1
    assert biometrics.daily_series(profile, metric, days=9999)["points"][0]["value"] == pytest.approx(phone, rel=0.02)


def test_two_entries_from_one_path_close_in_value_both_count(client):
    profile, day = _profile(client), YESTERDAY.isoformat()
    biometrics.insert_samples(profile, None, [_s("f1", "dietary_energy", 200, unit="kcal"), _s("f2", "dietary_energy", 201, unit="kcal")])
    assert _daily(profile, "dietary_energy")[day]["value"] == 401


def test_rollups_ignore_a_near_duplicate_stored_before_the_paths_recognised_each_other(client):
    profile = _profile(client)
    with db() as conn:
        for sid, v in (("UUID-9", 19.794931), ("ah-abc", 19.7949312)):
            conn.execute("INSERT INTO biometric_samples(id, profile_id, metric_type, value, unit, start_date, end_date, source_name, created_at) "
                         "VALUES (?, ?, 'distance_walking_running', ?, 'm', ?, ?, 'Watch', 1)", (sid, profile, v, _iso(YESTERDAY, "10:00:00"), _iso(YESTERDAY, "10:05:00")))
    biometrics.rebuild_daily(profile, [YESTERDAY.isoformat()])
    assert _daily(profile, "distance_walking_running")[YESTERDAY.isoformat()]["value"] == pytest.approx(19.79, abs=0.01)


def test_deleting_an_old_import_keeps_what_the_phone_also_sent(client):
    profile = _profile(client)
    code = client.post("/api/devices/pairing-code", json={}).json()["code"]
    phone = TestClient(client.app, base_url=BASE)
    token = phone.post("/api/devices/pair", json={"code": code, "device_name": "iPhone"}).json()["device_token"]
    _import(client, profile, _export_xml(_record("StepCount", 25, "count")))
    batch = {"device_id": "p", "device_name": "iPhone", "samples": [_s("UUID-2", "step_count", 25, "2026-09-20T10:00:00Z", "2026-09-20T10:05:00Z")]}
    r = phone.post("/api/ingest/wearables", json=batch, headers={"X-Syntropy-Device-Token": token}).json()
    assert r["duplicates"] == 1
    export = next(c for c in client.get("/api/connections").json()["connections"] if c["provider"] == "apple_health_export")
    assert client.delete(f"/api/connections/{export['id']}", params={"delete_data": True}).status_code == 200
    assert biometrics.daily_series(profile, "step_count", days=9999)["points"][0]["value"] == 25


# ---------------------------------------------------------------------------
# Samples and rollups together (F023, F024, F048)
# ---------------------------------------------------------------------------

def test_a_failing_rollup_leaves_nothing_half_done_so_the_retry_works(client, monkeypatch):
    profile = _profile(client)
    with monkeypatch.context() as m:
        m.setattr(biometrics, "_rollup", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
        with pytest.raises(sqlite3.OperationalError):
            biometrics.insert_samples(profile, None, [_s("r1", value=500)])
    assert biometrics.recent_samples(profile) == []
    assert biometrics.insert_samples(profile, None, [_s("r1", value=500)])["inserted"] == 1
    assert _daily(profile, "step_count")[YESTERDAY.isoformat()]["value"] == 500


def test_a_failed_source_delete_is_put_right_by_the_retry(client):
    profile = _profile(client)
    conn = connections.create(profile, "import", "x", "X", mode="live")
    biometrics.insert_samples(profile, conn["id"], [_s("d1", value=55)])
    # Killed after its samples went, before the rollups were worked out again.
    with db() as c:
        biometrics.queue_rebuild(c, profile)
        c.execute("DELETE FROM biometric_samples WHERE connection_id = ?", (conn["id"],))
    assert _daily(profile, "step_count")     # still counting what was deleted
    assert client.delete(f"/api/connections/{conn['id']}", params={"delete_data": True}).status_code == 200
    assert not _daily(profile, "step_count")


def test_a_rebuild_cut_off_by_a_restart_finishes_at_the_next_start(client):
    profile = _profile(client)
    conn = connections.create(profile, "import", "x", "X", mode="live")
    biometrics.insert_samples(profile, conn["id"], [_s("d1", value=55)])
    with db() as c:
        biometrics.queue_rebuild(c, profile)
        c.execute("DELETE FROM biometric_samples WHERE connection_id = ?", (conn["id"],))
    biometrics.run_queued_rebuilds()
    assert not _daily(profile, "step_count")
    with read() as c:
        assert c.execute("SELECT COUNT(*) FROM daily_rebuilds").fetchone()[0] == 0


def test_a_zone_change_that_fails_partway_is_finished_by_trying_again(client, monkeypatch):
    profile = _profile(client)
    _set_zone(client, "America/New_York")
    biometrics.insert_samples(profile, None, [_s("z1", value=5000, start="2026-01-11T02:00:00Z")])     # the 10th in New York, the 11th in Tokyo
    assert "2026-01-10" in _daily_all(profile)
    with monkeypatch.context() as m:
        m.setattr(biometrics, "run_queued_rebuilds", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
        with pytest.raises(sqlite3.OperationalError):
            client.put("/api/settings/general", json={"timezone": "Asia/Tokyo"})
    assert client.put("/api/settings/general", json={"timezone": "Asia/Tokyo"}).status_code == 200
    assert "2026-01-11" in _daily_all(profile) and "2026-01-10" not in _daily_all(profile)


def _daily_all(profile, metric="step_count"):
    with read() as conn:
        return {r[0]: r[1] for r in conn.execute("SELECT day, value FROM biometric_daily WHERE profile_id = ? AND metric_type = ?", (profile, metric))}


def test_a_full_rebuild_a_month_at_a_time_matches_day_by_day(client):
    profile = _profile(client)
    start = date(2025, 11, 25)
    samples = []
    for i in range(70):
        d = start + timedelta(days=i)
        samples += [_s(f"hr{i}", "heart_rate", 60 + i % 5, _iso(d), unit="count/min"), _s(f"st{i}", "step_count", 1000 + i, _iso(d), _iso(d, "12:30:00")),
                    _s(f"st2{i}", "step_count", 900, _iso(d), _iso(d, "12:30:00"), source="iPhone"),
                    _s(f"sl{i}", "sleep_analysis_core", 7 * 3600, _iso(d - timedelta(days=1), "23:30:00"), _iso(d, "06:30:00"), unit="s"),
                    _s(f"oura{i}", "readiness_score", 80, _iso(d), source="Oura Ring", unit="score", metadata={"day": d.isoformat()})]
    biometrics.insert_samples(profile, None, samples)
    with read() as conn:
        before = sorted(tuple(r)[:7] for r in conn.execute("SELECT * FROM biometric_daily WHERE profile_id = ?", (profile,)))
    with db() as conn:
        conn.execute("INSERT INTO biometric_daily(profile_id, day, metric_type, source_name, value, sample_count) VALUES (?, 'yesterday', 'step_count', 'X', 1, 1)", (profile,))
        conn.execute("INSERT INTO biometric_daily(profile_id, day, metric_type, source_name, value, sample_count) VALUES (?, '2020-01-01', 'step_count', 'Deleted', 1, 1)", (profile,))
    biometrics.rebuild_daily(profile)
    with read() as conn:
        after = sorted(tuple(r)[:7] for r in conn.execute("SELECT * FROM biometric_daily WHERE profile_id = ?", (profile,)))
    assert after == before


def test_one_odd_item_doesnt_fail_a_batch_or_lose_its_rollups(client):
    profile, day = _profile(client), YESTERDAY.isoformat()
    bad = [{"id": "n", "metric_type": "step_count", "value": None, "start_date": _iso(YESTERDAY)}, {"id": "x" * 300, "metric_type": "step_count", "value": 1, "start_date": _iso(YESTERDAY)},
           {"metric_type": "step_count", "value": 1, "start_date": _iso(YESTERDAY)}, _s("past", value=5, start="0001-01-01T00:00:00+14:00"),
           _s("day1", value=7, metadata={"day": "yesterday"}), _s("day2", value=8, metadata={"day": ["x"]}), _s("past2", value=5, start="0001-01-01T00:00:00Z")]
    good = _s("good", value=10)
    wk_bad = [{"id": "w1", "name": None, "start": "not a time", "end": _iso(YESTERDAY)}, {"id": "w2", "name": "Run", "start": _iso(YESTERDAY), "end": _iso(YESTERDAY),
              "activity_type": "run", "route": [{"lat": 91, "lon": 0, "t": _iso(YESTERDAY)}, {"lat": 1, "lon": 1, "t": _iso(YESTERDAY)}], "heart_rate": [{"t": "x", "min": None, "avg": 1, "max": 1}]}]
    ev_bad = [{"id": "e1", "event_type": "x", "name": None, "start": _iso(YESTERDAY)}, {"id": "e2", "name": "no type", "start": _iso(YESTERDAY)}]
    r = client.post("/api/ingest/wearables", json={"device_id": "d", "device_name": "P", "samples": [*bad, good], "workouts": wk_bad, "events": ev_bad})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rejected"] == 4 and body["received"] == 8 and body["inserted"] == 4     # a date of year 1 with no zone is kept, flagged
    assert body["rejected_fields"]["samples"]["value"] == 1 and "1000" not in json.dumps(body["rejected_fields"])
    assert body["workouts_inserted"] == 1 and body["workouts_rejected"] == 1 and body["events_inserted"] == 1 and body["events_rejected"] == 1
    assert _daily(profile, "step_count")[day]["value"] == 25              # 10 + 7 + 8, with the junk day labels ignored
    assert len(activity.get_workout(profile, "w2")["route"]) == 1
    assert client.get("/api/biometrics/metrics", params={"latest": "true"}).status_code == 200
    assert client.post("/api/ingest/wearables", json={"device_id": "d"}).status_code == 422     # the envelope still has to make sense


def test_a_workout_or_event_with_no_real_date_is_counted_not_a_500(client):
    far = "0001-01-01T00:00:00+14:00"
    r = client.post("/api/ingest/wearables", json={"device_id": "d", "device_name": "P", "workouts": [{"id": "w", "start": far, "end": far}, {"id": "w2", "start": "0001-01-01T00:00:00Z"}],
                                                   "events": [{"id": "e", "event_type": "x", "start": far}]})
    assert r.status_code == 200 and r.json()["workouts_rejected"] == 2 and r.json()["events_rejected"] == 1


def test_a_junk_day_already_stored_doesnt_break_the_trends_list(client):
    profile = _profile(client)
    with db() as conn:
        conn.execute("INSERT INTO biometric_daily(profile_id, day, metric_type, source_name, value, sample_count) VALUES (?, 'yesterday', 'step_count', 'X', 1, 1)", (profile,))
        conn.execute("INSERT INTO biometric_samples(id, profile_id, metric_type, value, unit, start_date, end_date, source_name, metadata_json, created_at) "
                     "VALUES ('j', ?, 'step_count', 3, '', ?, ?, 'X', '{\"day\":\"yesterday\"}', 1)", (profile, _iso(YESTERDAY), _iso(YESTERDAY)))
    assert client.get("/api/biometrics/metrics", params={"latest": "true"}).status_code == 200
    biometrics.rebuild_daily(profile, [YESTERDAY.isoformat()])
    assert _daily(profile, "step_count")[YESTERDAY.isoformat()]["value"] == 3


def test_export_json_with_the_wrong_types_is_read_or_left_out_not_a_500(client):
    day = (TODAY - timedelta(days=2)).isoformat()
    payload = {"data": {"metrics": [{"name": "step_count", "units": "count", "data": [{"date": f"{day} 10:00:00 +0000", "qty": 5, "source": ["a"]},
                                                                                     {"date": f"{day} 11:00:00 +0000", "qty": "x", "source": "b"}]}],
                        "workouts": [{"name": {"x": 1}, "start": f"{day} 10:00:00 +0000", "source": 5}],
                        "symptoms": [{"name": ["x"], "start": f"{day} 10:00:00 +0000", "value": {"a": 1}, "source": 1}]}}
    r = client.post("/api/ingest/healthkit-export", json=payload)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# WHOOP dates (F044, F150)
# ---------------------------------------------------------------------------

def _whoop_day(d: date, cycle_id=93845, sleep_id="ecfc6a15"):
    """WHOOP's documented shapes (developer.whoop.com): the cycle starts when the sleep does, here at 22:30 the evening before."""
    bed, wake = f"{(d - timedelta(days=1)).isoformat()}T02:30:00.000Z", f"{d.isoformat()}T10:30:00.000Z"
    return {
        "cycle": [{"id": cycle_id, "start": f"{(d - timedelta(days=0)).isoformat()}T02:30:00.000Z", "end": None, "timezone_offset": "-04:00", "score_state": "SCORED",
                   "score": {"strain": 5.3, "kilojoule": 8288.3, "average_heart_rate": 68, "max_heart_rate": 141}, "step_count": 8234}],
        "sleep": [{"id": sleep_id, "cycle_id": cycle_id, "start": f"{d.isoformat()}T02:30:00.000Z", "end": wake, "timezone_offset": "-04:00", "nap": False, "score_state": "SCORED",
                   "score": {"stage_summary": {"total_in_bed_time_milli": 28800000, "total_awake_time_milli": 1800000, "total_light_sleep_time_milli": 15000000,
                                               "total_slow_wave_sleep_time_milli": 6000000, "total_rem_sleep_time_milli": 6000000},
                             "respiratory_rate": 16.1, "sleep_performance_percentage": 98}}],
        "recovery": [{"cycle_id": cycle_id, "sleep_id": sleep_id, "created_at": f"{d.isoformat()}T11:25:44.774Z", "score_state": "SCORED",
                      "score": {"recovery_score": 44, "resting_heart_rate": 64, "hrv_rmssd_milli": 31.8, "spo2_percentage": 95.7, "skin_temp_celsius": 33.7}}],
        "workout": [], "bed": bed,
    }


def test_whoop_recovery_and_strain_land_on_the_morning_of_their_sleep(client, monkeypatch):
    profile = _profile(client)
    _set_zone(client, "America/New_York")
    d = TODAY - timedelta(days=3)
    conn = _live(monkeypatch, client, "whoop", _whoop_day(d))
    asyncio.run(sync.sync_connection(conn["id"]))
    day = d.isoformat()
    for metric in ("sleep_duration", "recovery_score", "resting_heart_rate", "hrv_rmssd", "oxygen_saturation", "strain_score", "total_energy", "step_count", "skin_temperature"):
        assert list(_daily(profile, metric, 30)) == [day], metric


def test_whoop_open_cycle_counts_a_day_after_its_start_when_no_sleep_matches():
    start = "2026-10-05T22:00:00.000Z"
    samples = whoop.normalize({"cycle": [{"id": 1, "start": start, "end": None, "score_state": "SCORED", "score": {"strain": 3.0}}]})
    assert samples[0]["start_date"] == "2026-10-06T10:00:00.000Z"


def test_simulated_whoop_cycles_follow_the_documented_shape():
    data = whoop.simulate(days=5, until=TODAY - timedelta(days=1))
    for cycle, night in zip(data["cycle"], data["sleep"]):
        assert cycle["start"] == night["start"] and night["cycle_id"] == cycle["id"] and cycle["end"] > night["end"]


def test_stored_whoop_dates_are_put_right_without_asking_whoop(client):
    from app.services import wearable_fixes
    profile = _profile(client)
    bed, wake = "2026-10-06T02:30:00Z", "2026-10-06T10:30:00Z"
    with db() as conn:
        for sid, metric, s, e in (("whoop-slp-1-total", "sleep_total_duration", bed, wake), ("whoop-rec-9-recovery_score", "recovery_score", bed, bed),
                                  ("whoop-cyc-9-strain", "strain_score", bed, "2026-10-07T01:00:00Z")):
            conn.execute("INSERT INTO biometric_samples(id, profile_id, metric_type, value, unit, start_date, end_date, source_name, created_at) "
                         "VALUES (?, ?, ?, 7, '', ?, ?, 'WHOOP', 1)", (sid, profile, metric, s, e))
        wearable_fixes._fix_whoop_dates(conn, None, 100)
    with read() as conn:
        got = {r[0]: r[1] for r in conn.execute("SELECT id, start_date FROM biometric_samples")}
    assert got["whoop-rec-9-recovery_score"] == wake and got["whoop-cyc-9-strain"] == wake and got["whoop-slp-1-total"] == bed


# ---------------------------------------------------------------------------
# Time zones (F045)
# ---------------------------------------------------------------------------

def test_a_person_can_have_their_own_home_zone(client):
    alex = _profile(client)
    jordan = client.post("/api/profiles", json={"name": "Jordan", "relationship": "partner"}).json()["id"]
    _set_zone(client, "America/New_York")
    at = "2026-01-11T02:00:00Z"
    biometrics.insert_samples(alex, None, [_s("a1", value=10, start=at)])
    biometrics.insert_samples(jordan, None, [_s("j1", value=10, start=at)])
    assert "2026-01-10" in _daily_all(alex) and "2026-01-10" in _daily_all(jordan)
    r = client.patch(f"/api/profiles/{jordan}", json={"timezone": "Asia/Tokyo"})
    assert r.status_code == 200 and r.json()["timezone"] == "Asia/Tokyo"
    assert "2026-01-11" in _daily_all(jordan) and "2026-01-10" not in _daily_all(jordan)
    assert "2026-01-10" in _daily_all(alex)
    assert client.patch(f"/api/profiles/{jordan}", json={"timezone": "Mars/Olympus"}).status_code == 400
    biometrics.insert_samples(jordan, None, [_s("j2", value=5, start="2026-01-12T02:00:00Z")])
    assert "2026-01-12" in _daily_all(jordan)
    client.patch(f"/api/profiles/{jordan}", json={"timezone": None})
    assert "2026-01-10" in _daily_all(jordan)


def test_a_readings_own_offset_says_which_day_it_belongs_to(client):
    profile = _profile(client)
    _set_zone(client, "America/New_York")
    # A day in Tokyo: 09:00 and 20:00 on the 13th are both the 13th there, whatever the home zone says.
    biometrics.insert_samples(profile, None, [
        _s("t1", value=9000, start="2026-10-13T00:00:00Z", end="2026-10-13T01:00:00Z", tz_offset_min=540),
        _s("t2", value=4000, start="2026-10-13T11:00:00Z", end="2026-10-13T12:00:00Z", metadata={"HKTimeZone": "Asia/Tokyo"}),
        _s("bad", "heart_rate", 60, "2026-10-13T00:00:00Z", metadata={"TimeZone": "not a zone"}, unit="count/min"),
    ])
    assert _daily_all(profile) == {"2026-10-13": 13000}


def test_a_night_abroad_has_its_clock_times_from_there(client):
    profile = _profile(client)
    _set_zone(client, "America/New_York")
    wake_day = TODAY - timedelta(days=2)
    start = datetime(wake_day.year, wake_day.month, wake_day.day, tzinfo=timezone.utc) - timedelta(hours=10)    # 23:00 in Tokyo the night before
    end = start + timedelta(hours=8)
    stamp = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
    biometrics.insert_samples(profile, None, [_s("n1", "sleep_analysis_core", 8 * 3600, stamp(start), stamp(end), unit="s", tz_offset_min=540)])
    night = sleep.nights(profile, days=10)["nights"][-1]
    assert night["day"] == wake_day.isoformat() and "T23:00" in night["bed"] and "T07:00" in night["wake"]


def test_a_rebuild_finds_a_reading_from_the_far_side_of_the_world(client):
    profile = _profile(client)
    client.put("/api/settings/general", json={"timezone": "Pacific/Pago_Pago"})     # UTC-11
    # Local date the 13th at UTC+14 is still the 12th in UTC, a day and more from the 13th at home.
    biometrics.insert_samples(profile, None, [_s("far", value=70, start="2026-10-12T10:30:00Z", tz_offset_min=840)])
    biometrics.rebuild_daily(profile, ["2026-10-13"])
    assert _daily_all(profile) == {"2026-10-13": 70}


def test_oura_and_whoop_and_google_offsets_are_kept():
    oura_rows = oura.normalize({"sleep": [{"id": "s", "day": "2026-10-01", "type": "long_sleep", "bedtime_start": "2026-09-30T23:00:00+09:00",
                                           "bedtime_end": "2026-10-01T07:00:00+09:00", "total_sleep_duration": 25000}]})
    assert oura_rows[0]["tz_offset_min"] == 540
    assert whoop.normalize(_whoop_day(date(2026, 10, 5)))[0]["tz_offset_min"] == -240
    google = google_health.normalize({"sleep": [{"sleep": {"interval": {"startTime": "2026-10-01T00:00:00Z", "endTime": "2026-10-01T07:00:00Z",
                                                                        "endUtcOffset": "-14400s", "civilEndTime": {"date": {"year": 2026, "month": 10, "day": 1}}},
                                                           "summary": {"minutesAsleep": "400"}}}]})
    assert google[0]["tz_offset_min"] == -240


# ---------------------------------------------------------------------------
# export.xml faults (F046) and coverage (F047)
# ---------------------------------------------------------------------------

BROKEN_DTD = '<!DOCTYPE HealthData [\n<!ELEMENT HealthData (ExportDate,Me,(Record|Correlation|Workout|ActivitySummary)*)>\n<!ATTLIST VisionAttachment\n  brand CDATA #IMPLIED\n'


def test_the_ios16_export_with_a_broken_doctype_imports(client):
    profile = _profile(client)
    xml = _export_xml(_record("StepCount", 25, "count")).replace(b'<HealthData', BROKEN_DTD.encode() + b'<HealthData', 1)
    job = _import(client, profile, xml)
    assert job["status"] == "completed" and job["result"]["inserted"] == 1 and "warning" not in job["result"]


def test_a_doctype_longer_than_one_read_is_still_found():
    xml = _export_xml(_record("StepCount", 25, "count")).replace(b'<HealthData', BROKEN_DTD.encode() + b"<!-- " + b"x" * 200_000 + b" -->" + b'<HealthData', 1)
    items = list(apple_health.iter_items(apple_health.CleanStream(io.BytesIO(xml))))
    assert [k for k, _ in items] == ["sample"]


def test_a_control_character_across_a_read_boundary_is_removed():
    filler = "".join(_record("StepCount", 25, "count", start=f"2026-09-20 10:{i % 60:02d}:00 +0000", end=f"2026-09-20 10:{i % 60:02d}:30 +0000", source=f"W{i}") for i in range(400))
    xml = _export_xml(filler, _record("StepCount", 1, "count", source="Bad\x0bName"))
    assert len(list(apple_health.iter_items(apple_health.CleanStream(io.BytesIO(xml))))) == 401


def test_a_truncated_export_keeps_what_came_before_and_says_so(client):
    profile = _profile(client)
    records = [_record("StepCount", 25, "count", start=f"2026-09-20 10:{i:02d}:00 +0000", end=f"2026-09-20 10:{i:02d}:30 +0000") for i in range(20)]
    workout = '<Workout workoutActivityType="HKWorkoutActivityTypeRunning" sourceName="Watch" startDate="2026-09-20 12:00:00 +0000" endDate="2026-09-20 12:30:00 +0000"/>'
    xml = _export_xml(*records, workout, _record("StepCount", 1, "count"))[:-30]
    fhir = {"resourceType": "Observation", "id": "o1", "status": "final", "code": {"text": "Glucose"}, "effectiveDateTime": "2026-01-01", "valueQuantity": {"value": 90, "unit": "mg/dL"}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("apple_health_export/export.xml", xml)
        zf.writestr("apple_health_export/clinical-records/o1.json", json.dumps(fhir))
    job_id = imports._new_job("apple_health", "export.zip", profile)
    path = imports.upload_dir() / f"{job_id}.zip"
    path.write_bytes(buf.getvalue())
    imports._run_apple_health(job_id, profile, path, "export.zip")
    job = imports.get_job(job_id)
    assert job["status"] == "completed" and job["result"]["stopped_early"] and "line" in job["result"]["warning"]
    assert job["result"]["inserted"] == 20 and job["result"]["workouts"] == 1
    assert job["result"]["clinical"]["records"] == 1
    with read() as conn:
        runs = conn.execute("SELECT status FROM sync_runs").fetchall()
    assert "running" not in {r[0] for r in runs} and "partial" in {r[0] for r in runs}


def test_a_file_thats_not_an_export_gets_a_plain_answer_and_no_stuck_run(client):
    profile = _profile(client)
    assert "isn't an Apple Health export" in _import(client, profile, b"just some text")["error"]
    assert "incomplete" in _import(client, profile, b"PK\x03\x04 truncated zip", "export.zip")["error"]
    job = _import(client, profile, b"<HealthData><Record")
    assert job["status"] == "error" and "couldn't be read" in job["error"]
    with read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sync_runs WHERE status = 'running'").fetchone()[0] == 0


def test_every_quantity_type_the_iphone_sends_is_imported_with_a_count_of_what_wasnt(client):
    profile = _profile(client)
    job = _import(client, profile, _export_xml(
        _record("Height", 70, "in"), _record("DistanceCycling", 2, "mi"), _record("DietaryEnergyConsumed", 500, "Cal"),
        _record("AppleSleepingWristTemperature", 34, "degC"), _record("WaistCircumference", 34, "in"), _record("InsulinDelivery", 3, "IU"),
        _record("MadeUpType", 1, "count"), '<Record type="HKDataTypeSleepDurationGoal" sourceName="H" startDate="2026-09-20 10:00:00 +0000" endDate="2026-09-20 10:00:00 +0000" value="8"/>'))
    types = job["result"]["types"]
    assert len(types["imported"]) == 6 and types["skipped"]["HKQuantityTypeIdentifierMadeUpType"] == 1 and types["skipped"]["HKDataTypeSleepDurationGoal"] == 1
    assert job["result"]["inserted"] == 6
    assert biometrics.daily_series(profile, "height", days=9999)["points"][0]["value"] == pytest.approx(177.8)
    assert biometrics.daily_series(profile, "distance_cycling", days=9999)["points"][0]["value"] == pytest.approx(3218.69, abs=0.01)
    assert len(apple_health.QUANTITY_TYPES) >= 114


# ---------------------------------------------------------------------------
# Refused access (F049), page caps (F148), Google (F149, F277, F278)
# ---------------------------------------------------------------------------

_REAL_CLIENT = httpx.AsyncClient


def _transport(handler):
    def client(**kw):
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)
    return client


def test_oura_refusing_everything_is_an_error_that_doesnt_move_the_window(client, monkeypatch):
    profile = _profile(client)
    conn = connections.create(profile, "wearable", "oura", "Oura", mode="live", credentials={"access_token": "AT", "scope": oura.SCOPES})
    with monkeypatch.context() as m:
        m.setattr(oura.httpx, "AsyncClient", _transport(lambda req: httpx.Response(403, json={"detail": "no"})))
        result = asyncio.run(sync.sync_connection(conn["id"]))
    assert result["status"] == "error" and result["kind"] == "error" and "membership" in result["error"]
    stored = connections.get(conn["id"])
    assert connections.last_success_at(stored) is None and stored["last_error"]


def test_some_refused_warns_and_holds_the_window_until_they_are_allowed(client, monkeypatch):
    profile = _profile(client)
    conn = connections.create(profile, "wearable", "oura", "Oura", mode="live", credentials={"access_token": "AT", "scope": oura.SCOPES})
    seen = []

    def some_refused(req):
        seen.append(str(req.url))
        return httpx.Response(403) if "daily_spo2" in str(req.url) else httpx.Response(200, json={"data": []})
    with monkeypatch.context() as m:
        m.setattr(oura.httpx, "AsyncClient", _transport(some_refused))
        result = asyncio.run(sync.sync_connection(conn["id"]))
        assert result["status"] == "partial" and "daily spo2" in result["warnings"][0]
        floor = connections.get(conn["id"])["metadata"]["window_floor"]
        result = asyncio.run(sync.sync_connection(conn["id"]))
        assert result["since"] == floor                # not moved on by the partial success
        m.setattr(oura.httpx, "AsyncClient", _transport(lambda req: httpx.Response(200, json={"data": []})))
        assert asyncio.run(sync.sync_connection(conn["id"]))["status"] == "success"
    assert "window_floor" not in connections.get(conn["id"])["metadata"]


def test_a_data_type_declined_at_sign_in_isnt_a_warning(client, monkeypatch):
    profile = _profile(client)
    conn = connections.create(profile, "wearable", "oura", "Oura", mode="live", credentials={"access_token": "AT", "scope": "daily heartrate workout"})

    def handler(req):
        return httpx.Response(403) if "daily_spo2" in str(req.url) else httpx.Response(404) if "workout" in str(req.url) else httpx.Response(200, json={"data": []})
    with monkeypatch.context() as m:
        m.setattr(oura.httpx, "AsyncClient", _transport(handler))
        assert asyncio.run(sync.sync_connection(conn["id"]))["status"] == "success"


def test_a_whole_lapse_is_fetched_after_the_kept_runs_have_aged_out(client):
    today = date(2026, 10, 30)
    assert sync._hold_window(today, today, {"fetched_through": "2026-07-01"}) == date(2026, 6, 28)
    assert sync._hold_window(today, today, {"fetched_through": "not a date", "window_floor": 5}) == today


def test_google_says_when_its_api_is_turned_off(monkeypatch):
    disabled = httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED", "details": [{"reason": "SERVICE_DISABLED"}]}})
    with monkeypatch.context() as m:
        m.setattr(google_health.httpx, "AsyncClient", _transport(lambda req: disabled))
        with pytest.raises(SmartError, match="isn't turned on"):
            asyncio.run(google_health.fetch_live({"access_token": "AT"}, date(2026, 10, 1), date(2026, 10, 2)))


def test_google_scopes_not_granted_stay_quiet(monkeypatch):
    def handler(req):
        if "sleep" in str(req.url):
            return httpx.Response(403, json={"error": {"details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}})
        return httpx.Response(200, json={})
    with monkeypatch.context() as m:
        m.setattr(google_health.httpx, "AsyncClient", _transport(handler))
        data = asyncio.run(google_health.fetch_live({"access_token": "AT"}, date(2026, 10, 1), date(2026, 10, 2)))
    assert data["_warnings"] == []


def test_a_page_limit_is_an_error_rather_than_a_quiet_gap(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(whoop.httpx, "AsyncClient", _transport(lambda req: httpx.Response(200, json={"records": [{"id": 1}], "next_token": "more"})))
        with pytest.raises(SmartError, match="more"):
            asyncio.run(whoop.fetch_live({"access_token": "AT"}, date(2026, 10, 1), date(2026, 10, 2)))


def test_google_rollups_ask_for_a_page_within_the_documented_maximum(monkeypatch):
    bodies = []

    async def fake(client, method, url, token, **kw):
        bodies.append((url, kw.get("json")))
        return {}
    with monkeypatch.context() as m:
        m.setattr(google_health, "_request", fake)
        asyncio.run(google_health.fetch_live({"access_token": "AT"}, date(2026, 6, 1), date(2026, 9, 26)))
    sizes = {url.split("/dataTypes/")[1].split("/")[0]: b["pageSize"] for url, b in bodies if b}
    assert sizes["steps"] <= 90 and sizes["total-calories"] <= 14


def test_a_sync_asks_for_the_local_day_ahead_of_utc(client, monkeypatch):
    profile = _profile(client)
    _set_zone(client, "Pacific/Kiritimati")      # UTC+14
    asked = []

    async def fetch(creds, since, until):
        asked.append(until)
        return {}
    conn = connections.create(profile, "wearable", "google", "G", mode="live", credentials={"access_token": "AT"})
    with monkeypatch.context() as m:
        m.setattr(google_health, "fetch_live", fetch)
        asyncio.run(sync.sync_connection(conn["id"]))
    assert asked[0] == datetime.now(biometrics._tz(profile)).date()


def test_google_test_mode_sign_in_ending_is_explained(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(wc.httpx, "AsyncClient", _transport(lambda req: httpx.Response(400, json={"error": "invalid_grant"})))
        m.setattr(wc.settings, "wearable_credentials", lambda p: {"client_id": "c", "client_secret": "s"})
        with pytest.raises(SmartError, match="Testing") as e:
            asyncio.run(google_health.refresh({"refresh_token": "r"}))
    assert e.value.kind == "auth"


def _fixes_finished():
    from app.core import data_fixes
    data_fixes.stop()       # the background run for this test's database, so it can't step in partway


def test_history_is_fetched_again_a_stretch_at_a_time(client, monkeypatch):
    from app.services import wearable_fixes
    _fixes_finished()
    profile, day = _profile(client), TODAY - timedelta(days=500)
    conn = connections.create(profile, "wearable", "oura", "Oura", mode="live", credentials={"access_token": "AT"})
    biometrics.insert_samples(profile, conn["id"], [_s("oura-d1-step_count", value=2000, start=_iso(day), metadata={"day": day.isoformat()}, source="Oura Ring")])
    with db() as c:
        wearable_fixes._fix_fetch_again(c, None, 100)
    windows = []

    async def fetch(creds, since, until):
        windows.append((since, until))
        return _oura_daily(day.isoformat(), 11000) if since <= day <= until and since < TODAY - timedelta(days=30) else {}
    with monkeypatch.context() as m:
        m.setattr(oura, "fetch_live", fetch)
        for _ in range(4):
            assert asyncio.run(sync.sync_connection(conn["id"]))["status"] == "success"
    assert biometrics.daily_series(profile, "step_count", days=600, source="Oura Ring")["points"][0]["value"] == 11000
    assert all((u - s).days <= sync.REFETCH_STRETCH_DAYS for s, u in windows[1:2])
    assert "refetch_from" not in connections.get(conn["id"])["metadata"]


def test_older_history_can_be_asked_for_and_comes_a_stretch_at_a_time(client, monkeypatch):
    _fixes_finished()
    profile = _profile(client)
    conn = connections.create(profile, "wearable", "oura", "Oura", mode="live", credentials={"access_token": "AT"})
    sim = connections.create(profile, "wearable", "whoop", "WHOOP", mode="simulated")
    assert client.post(f"/api/connections/{sim['id']}/fetch-history").status_code == 400
    asked = []

    async def fetch(creds, since, until):
        asked.append((since, until))
        return {}
    with monkeypatch.context() as m:
        m.setattr(oura, "fetch_live", fetch)
        m.setattr(sync, "HISTORY_PAUSE_S", 0)
        assert client.post(f"/api/connections/{conn['id']}/fetch-history").status_code == 200
    assert "refetch_from" not in connections.get(conn["id"])["metadata"]
    older = [a for a in asked if a[1] < TODAY - timedelta(days=4)]
    assert len(older) >= 15 and all((u - s).days <= sync.REFETCH_STRETCH_DAYS for s, u in older)
    assert min(s for s, _ in asked) <= TODAY - timedelta(days=sync.HISTORY_DAYS - 1)


def test_older_history_asked_for_during_a_sync_survives_it(client, monkeypatch):
    _fixes_finished()
    profile = _profile(client)
    conn = connections.create(profile, "wearable", "oura", "Oura", mode="live", credentials={"access_token": "AT"})

    async def fetch(creds, since, until):
        await asyncio.to_thread(connections.update, conn["id"], metadata={"refetch_from": "2020-01-01"})
        return {}
    with monkeypatch.context() as m:
        m.setattr(oura, "fetch_live", fetch)
        asyncio.run(sync.sync_connection(conn["id"]))
    meta = connections.get(conn["id"])["metadata"]
    assert meta["refetch_from"] == "2020-01-01" and meta["fetched_through"]


def test_a_source_removed_during_its_sync_leaves_nothing_behind(client, monkeypatch):
    profile = _profile(client)
    conn = connections.create(profile, "wearable", "oura", "Oura", mode="live", credentials={"access_token": "AT"})

    async def fetch(creds, since, until):
        await asyncio.to_thread(connections.disconnect, conn["id"], True)
        return _oura_daily(YESTERDAY.isoformat(), 5000)
    with monkeypatch.context() as m:
        m.setattr(oura, "fetch_live", fetch)
        asyncio.run(sync.sync_connection(conn["id"]))
    assert biometrics.recent_samples(profile) == []


def test_removing_a_source_waits_for_its_sync(client, monkeypatch):
    profile = _profile(client)
    conn = connections.create(profile, "wearable", "oura", "Oura", mode="simulated")
    with monkeypatch.context() as m:
        m.setattr(sync, "is_running", lambda cid: True)
        assert client.delete(f"/api/connections/{conn['id']}").status_code == 409


# ---------------------------------------------------------------------------
# Deleted readings (F050)
# ---------------------------------------------------------------------------

def _oura_night(row_id, hours, type_="long_sleep", day=None, hr=50):
    day = day or YESTERDAY.isoformat()
    return {"id": row_id, "day": day, "type": type_, "bedtime_start": f"{day}T01:00:00+00:00", "bedtime_end": f"{day}T{1 + int(hours):02d}:00:00+00:00",
            "total_sleep_duration": int(hours * 3600), "lowest_heart_rate": hr, "average_hrv": 60, "average_breath": 14}


def test_a_sleep_oura_deleted_goes_and_a_replacement_doesnt_add_to_it(client, monkeypatch):
    profile, day = _profile(client), YESTERDAY.isoformat()
    conn = _live(monkeypatch, client, "oura", {"sleep": [_oura_night("S1", 7)]},
                 {"sleep": [_oura_night("S1", 7, "deleted"), _oura_night("S2", 6)]}, {"sleep": [_oura_night("S2", 6, "deleted")]})
    asyncio.run(sync.sync_connection(conn["id"]))
    assert _daily(profile, "sleep_duration")[day]["value"] == 7
    asyncio.run(sync.sync_connection(conn["id"]))
    assert _daily(profile, "sleep_duration")[day]["value"] == 6
    asyncio.run(sync.sync_connection(conn["id"]))
    assert day not in _daily(profile, "sleep_duration")


def test_a_reading_removed_here_doesnt_come_back(client):
    profile, day = _profile(client), YESTERDAY.isoformat()
    biometrics.insert_samples(profile, None, [_s("k1", "body_mass", 25, unit="kg"), _s("k2", "body_mass", 80, _iso(YESTERDAY, "13:00:00"), unit="kg")])
    assert _daily(profile, "body_mass")[day]["value"] == 80
    assert client.delete("/api/biometrics/samples/k2").status_code == 200
    assert _daily(profile, "body_mass")[day]["value"] == 25
    assert client.delete("/api/biometrics/samples/k2").status_code == 404
    biometrics.insert_samples(profile, None, [_s("k2", "body_mass", 80, _iso(YESTERDAY, "13:00:00"), unit="kg")])
    assert _daily(profile, "body_mass")[day]["value"] == 25


def test_a_phone_can_say_what_was_deleted_for_its_own_person_only(client):
    alex = _profile(client)
    jordan = client.post("/api/profiles", json={"name": "Jordan", "relationship": "partner"}).json()["id"]
    code = client.post("/api/devices/pairing-code", json={}).json()["code"]
    phone = TestClient(client.app, base_url=BASE)
    token = phone.post("/api/devices/pair", json={"code": code, "device_name": "Pixel"}).json()["device_token"]
    biometrics.insert_samples(jordan, None, [_s("UUID-J", value=1)])
    headers = {"X-Syntropy-Device-Token": token}
    batch = {"device_id": "d", "device_name": "Pixel", "samples": [_s("hc-r1-0", "heart_rate", 60, unit="count/min"), _s("hc-r1-1", "heart_rate", 61, _iso(YESTERDAY, "12:01:00"), unit="count/min"),
                                                                   _s("hc-r10", "heart_rate", 62, _iso(YESTERDAY, "12:02:00"), unit="count/min"), _s("UUID-A", value=3)]}
    assert phone.post("/api/ingest/wearables", json=batch, headers=headers).json()["inserted"] == 4
    r = phone.post("/api/ingest/wearables", json={"device_id": "d", "device_name": "Pixel", "deleted": [{"id": "hc-r1"}, {"id": "UUID-A"}, {"id": "UUID-J"}]}, headers=headers).json()
    assert r["deleted"] == 3
    assert {s["id"] for s in biometrics.recent_samples(alex)} == {"hc-r10"}
    assert [s["id"] for s in biometrics.recent_samples(jordan)] == ["UUID-J"]
    assert _daily(alex, "heart_rate")[YESTERDAY.isoformat()]["value"] == 62
    assert sample_counts.for_profile(alex) == 1


# ---------------------------------------------------------------------------
# What can't be true (F051)
# ---------------------------------------------------------------------------

def test_impossible_readings_are_kept_flagged_and_left_out(client):
    profile, day = _profile(client), YESTERDAY.isoformat()
    r = biometrics.insert_samples(profile, None, [
        _s("i1", "heart_rate", 5000, unit="count/min"), _s("i2", "oxygen_saturation", 120, unit="%"), _s("i3", "body_mass", -5, unit="kg"),
        _s("i4", "resting_heart_rate", 0, unit="count/min"), _s("i5", "step_count", 500, _iso(YESTERDAY, "12:00:00"), _iso(YESTERDAY, "11:00:00")),
        _s("i6", "heart_rate", 62, unit="count/min"), _s("i7", "oxygen_saturation", 0.65, _iso(YESTERDAY, "13:00:00"), unit="%"),
        _s("i8", "body_temperature_deviation", -1.2, unit="degC"), _s("i9", "step_count", 0, _iso(YESTERDAY, "14:00:00")),
        _s("future", "step_count", 1e8, "2105-01-01T00:00:00Z"), _s("old", "body_mass", 70, "1900-01-01T00:00:00Z", unit="kg"),
    ])
    assert r["inserted"] == 11 and r["flagged"] == 7 and r["rejected"] == 0
    assert _daily(profile, "heart_rate")[day]["value"] == 62
    assert _daily(profile, "oxygen_saturation")[day]["value"] == 65       # low, and exactly what a record mustn't hide
    assert _daily(profile, "body_temperature_deviation")[day]["value"] == -1.2
    assert _daily(profile, "step_count")[day]["value"] == 0
    assert "resting_heart_rate" not in {m["metric"] for m in biometrics.available_metrics(profile)}
    metrics = {m["metric"]: m for m in biometrics.available_metrics(profile)}
    assert metrics["step_count"]["latest_day"] == day
    overview = client.get("/api/biometrics/overview").json()
    assert overview["history_span"]["latest"] < "2100" and overview["history_span"]["earliest"] > "2000"
    assert {(x["metric"], x["reason"]) for x in overview["left_out"]} >= {("heart_rate", "range"), ("step_count", "date")}
    assert overview["total_samples"] == 11
    assert client.get("/api/biometrics/daily", params={"metric": "heart_rate"}).json()["left_out"] == 1
    flagged = client.get("/api/biometrics/samples", params={"left_out": "true", "metric": "heart_rate"}).json()["samples"]
    assert flagged[0]["value"] == 5000 and flagged[0]["flag"] == "range"


def test_a_clock_a_little_fast_and_a_providers_day_ahead_are_real_readings(client):
    profile = _profile(client)
    soon = datetime.now(timezone.utc) + timedelta(hours=3)
    tomorrow = (TODAY + timedelta(days=1)).isoformat()
    r = biometrics.insert_samples(profile, None, [_s("c1", value=5, start=soon.strftime("%Y-%m-%dT%H:%M:%SZ")),
                                                  _s("c2", "sleep_score", 80, f"{tomorrow}T12:00:00Z", unit="score", metadata={"day": tomorrow})])
    assert r["flagged"] == 0


def test_a_later_good_value_clears_a_flag_and_stored_junk_is_flagged_once(client):
    from app.services import wearable_fixes
    profile, day = _profile(client), YESTERDAY.isoformat()
    biometrics.insert_samples(profile, None, [_s("v", "heart_rate", 5000, unit="count/min")])
    assert biometrics.insert_samples(profile, None, [_s("v", "heart_rate", 70, unit="count/min")])["updated"] == 1
    assert _daily(profile, "heart_rate")[day]["value"] == 70
    with db() as conn:
        for sid, metric, value, unit in (("old1", "heart_rate", 9000, "count/min"), ("hx_old2", "distance_cycling", 20, "mi"), ("ah-old3", "blood_glucose", 5.6, "mmol<180.1558800000541>/L"),
                                         ("hx_old4", "body_mass", 70, "stones and pebbles")):
            conn.execute("INSERT INTO biometric_samples(id, profile_id, metric_type, value, unit, start_date, end_date, source_name, device_name, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, 'Series 9', 'Apple Watch', 1)", (sid, profile, metric, value, unit, _iso(YESTERDAY), _iso(YESTERDAY)))
        wearable_fixes._fix_samples(conn, None, 1000)
    with read() as conn:
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM biometric_samples")}
        kind = conn.execute("SELECT device_kind FROM data_sources WHERE source_name = 'Series 9'").fetchone()[0]
    assert rows["old1"]["flag"] == "range" and rows["hx_old4"]["flag"] == "unit"
    assert rows["hx_old2"]["value"] == pytest.approx(32186.88) and rows["hx_old2"]["unit"] == "m"
    assert rows["ah-old3"]["value"] == pytest.approx(100.89, abs=0.01) and rows["ah-old3"]["flag"] is None
    assert kind == "watch"


# ---------------------------------------------------------------------------
# Rebuilding at scale (F052, F103), once on upgrade
# ---------------------------------------------------------------------------

def test_full_rebuilds_hand_the_write_lock_back_between_months(client, monkeypatch):
    profile = _profile(client)
    samples = [_s(f"m{i}", value=1, start=_iso(date(2025, 1, 1) + timedelta(days=i))) for i in range(300)]
    biometrics.insert_samples(profile, None, samples)
    steps = []
    real = biometrics._rollup
    monkeypatch.setattr(biometrics, "_rollup", lambda conn, pid, days, *a, **k: (steps.append(len(days)), real(conn, pid, days, *a, **k))[1])
    with monkeypatch.context() as m:
        m.setattr("app.store.biometrics.write_in_batches", lambda step, **k: _drain(step))
        biometrics.rebuild_daily(profile)
    assert len(steps) >= 10 and max(steps) <= 31
    assert len(_daily_all(profile)) == 300


def _drain(step):
    cursor = None
    while True:
        with db() as conn:
            cursor = step(conn, cursor, 1000)
        if cursor is None:
            return


def test_the_server_starts_without_waiting_for_rollups(client):
    """They run as a data fix now, in the background. It ran once for this database when it started."""
    from app.core import data_fixes
    names = [f["name"] for f in data_fixes.status()]
    assert f"wearables-daily-rollups-v{biometrics.DAILY_VERSION}" in names
    assert names.index(f"wearables-daily-rollups-v{biometrics.DAILY_VERSION}") > names.index("wearables-samples-units-flags-sources")


def test_the_daily_upgrade_rebuilds_everyone_once(client):
    from app.services import wearable_fixes
    profile = _profile(client)
    biometrics.insert_samples(profile, None, [_s("u1", value=5)])
    with db() as conn:
        conn.execute("DELETE FROM biometric_daily")
    cursor = None
    while True:
        with db() as conn:
            cursor = wearable_fixes._fix_rollups(conn, cursor, 100)
        if cursor is None:
            break
    assert _daily(profile, "step_count")[YESTERDAY.isoformat()]["value"] == 5


def test_a_wipe_clears_everything_from_the_person(client):
    profile = _profile(client)
    biometrics.insert_samples(profile, None, [_s("w1", value=5, metadata={"device_type": "watch"})])
    biometrics.delete_sample(profile, "w1")
    biometrics.insert_samples(profile, None, [_s("w2", value=5)])
    assert client.delete(f"/api/profiles/{profile}/data").status_code == 200
    with read() as conn:
        for table in ("biometric_samples", "biometric_daily", "data_sources", "sample_tombstones", "daily_rebuilds"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_stored_repeats_of_a_growing_total_collapse_but_real_entries_stay(client):
    from app.services import wearable_fixes
    profile = _profile(client)
    _set_zone(client, "UTC")
    conn_row = connections.create(profile, "device", "apple_health", "Automation", mode="live", metadata={"platform": "healthkit-export"})
    d = _iso(TODAY - timedelta(days=2), "00:00:00")
    noon = _iso(TODAY - timedelta(days=2), "12:00:00")
    day = (TODAY - timedelta(days=2)).isoformat()
    rows = [("hx_a", "step_count", 1000, d, 1.0, None), ("hx_b", "step_count", 3000, d, 2.0, None), ("hx_c", "dietary_energy", 300, noon, 1.0, None), ("hx_d", "dietary_energy", 200, noon, 1.0, None),
            ("hx_e", "sleep_total_duration", 18000, noon, 1.0, json.dumps({"day": day})), ("hx_f", "sleep_total_duration", 27000, noon, 2.0, json.dumps({"day": day}))]
    with db() as conn:
        for sid, metric, value, start, created, meta in rows:
            conn.execute("INSERT INTO biometric_samples(id, profile_id, connection_id, metric_type, value, unit, start_date, end_date, source_name, metadata_json, created_at) "
                         "VALUES (?, ?, ?, ?, ?, '', ?, ?, 'Fit', ?, ?)", (sid, profile, conn_row["id"], metric, value, start, start, meta, created))
        wearable_fixes._fix_json_repeats(conn, None, 100)
    with read() as conn:
        assert {r[0] for r in conn.execute("SELECT id FROM biometric_samples")} == {"hx_b", "hx_c", "hx_d", "hx_f"}


# ---------------------------------------------------------------------------
# Naps (F073, F143)
# ---------------------------------------------------------------------------

def test_a_daytime_nap_doesnt_replace_the_night_or_upset_bedtime(client):
    profile = _profile(client)
    _set_zone(client, "UTC")
    samples = []
    for i in range(10):
        d = TODAY - timedelta(days=10 - i)
        samples.append(_s(f"n{i}", "sleep_analysis_core", 7.5 * 3600, _iso(d, "00:30:00"), _iso(d, "08:00:00"), unit="s"))
        if i % 2 == 0:
            samples.append(_s(f"nap{i}", "sleep_analysis_asleep", 3600, _iso(d, "15:00:00"), _iso(d, "16:00:00"), unit="s"))
    biometrics.insert_samples(profile, None, samples)
    result = sleep.nights(profile, days=14)
    assert result["stats"]["bedtime"] == "00:30" and result["stats"]["bedtime_spread_min"] == 0
    nap_day = next(n for n in result["nights"] if n["naps"])
    assert nap_day["asleep_h"] == 7.5 and nap_day["in_bed_h"] == 7.5 and nap_day["efficiency"] == 1.0
    assert nap_day["naps"][0]["asleep_h"] == 1 and "T15:00" in nap_day["naps"][0]["start"]
    day = (TODAY - timedelta(days=10)).isoformat()
    assert _daily(profile, "sleep_duration", 14)[day]["value"] == 7.5 and _daily(profile, "sleep_nap", 14)[day]["value"] == 1


def test_sleep_broken_overnight_is_one_night_not_a_nap(client):
    profile = _profile(client)
    _set_zone(client, "UTC")
    d = TODAY - timedelta(days=2)
    biometrics.insert_samples(profile, None, [_s("a", "sleep_analysis_core", 3 * 3600, _iso(d - timedelta(days=1), "23:00:00"), _iso(d, "02:00:00"), unit="s"),
                                              _s("b", "sleep_analysis_core", 2.5 * 3600, _iso(d, "05:30:00"), _iso(d, "08:00:00"), unit="s")])
    assert _daily(profile, "sleep_duration")[d.isoformat()]["value"] == 5.5
    assert d.isoformat() not in _daily(profile, "sleep_nap")


def test_oura_naps_stay_out_of_the_nights_vitals(client, monkeypatch):
    profile, day = _profile(client), YESTERDAY.isoformat()
    nap = {**_oura_night("N", 0.83, "sleep", hr=68), "average_hrv": 28, "average_breath": 16, "bedtime_start": f"{day}T14:00:00+00:00", "bedtime_end": f"{day}T14:50:00+00:00"}
    conn = _live(monkeypatch, client, "oura", {"sleep": [_oura_night("L", 7), nap]})
    asyncio.run(sync.sync_connection(conn["id"]))
    assert _daily(profile, "resting_heart_rate")[day]["value"] == 50 and _daily(profile, "hrv_rmssd")[day]["value"] == 60
    assert _daily(profile, "sleep_duration")[day]["value"] == 7 and _daily(profile, "sleep_nap")[day]["value"] == pytest.approx(0.83, abs=0.01)


def test_a_short_oura_sleep_is_the_night_on_a_day_with_no_other(client, monkeypatch):
    profile, day = _profile(client), YESTERDAY.isoformat()
    conn = _live(monkeypatch, client, "oura", {"sleep": [_oura_night("S", 2.5, "sleep")]})
    asyncio.run(sync.sync_connection(conn["id"]))
    assert _daily(profile, "sleep_duration")[day]["value"] == 2.5


def test_naps_old_syncs_stored_as_the_night_are_taken_out(client, monkeypatch):
    profile, day = _profile(client), YESTERDAY.isoformat()
    conn = connections.create(profile, "wearable", "oura", "Oura", mode="live", credentials={"access_token": "AT"})
    with db() as c:
        c.execute("INSERT INTO biometric_samples(id, profile_id, connection_id, metric_type, value, unit, start_date, end_date, source_name, metadata_json, created_at) "
                  "VALUES ('oura-N-rhr', ?, ?, 'resting_heart_rate', 68, '', ?, ?, 'Oura Ring', ?, 1)", (profile, conn["id"], _iso(YESTERDAY), _iso(YESTERDAY), json.dumps({"day": day})))
    answers = iter([{"sleep": [_oura_night("L", 7), _oura_night("N", 1, "sleep", hr=68)]}])

    async def fetch(creds, since, until):
        return next(answers)
    with monkeypatch.context() as m:
        m.setattr(oura, "fetch_live", fetch)
        asyncio.run(sync.sync_connection(conn["id"]))
    assert _daily(profile, "resting_heart_rate")[day]["value"] == 50


def test_whoop_and_google_naps_are_apart_from_the_night():
    whoop_rows = whoop.normalize({"sleep": [{"id": "n", "nap": True, "start": "2026-10-01T14:00:00.000Z", "end": "2026-10-01T15:00:00.000Z", "score_state": "SCORED",
                                             "score": {"stage_summary": {"total_in_bed_time_milli": 3600000, "total_awake_time_milli": 0}}}]})
    assert [r["metric_type"] for r in whoop_rows] == ["sleep_nap"]
    google_rows = google_health.normalize({"sleep": [{"sleep": {"interval": {"startTime": "2026-10-01T14:00:00Z", "endTime": "2026-10-01T15:00:00Z"},
                                                                "metadata": {"nap": True}, "summary": {"minutesAsleep": "50"}}}]})
    assert [r["metric_type"] for r in google_rows] == ["sleep_nap"]


# ---------------------------------------------------------------------------
# One total per source and day (F144), source kinds (F145), time weighting (F146), every metric (F147)
# ---------------------------------------------------------------------------

def test_a_daily_total_and_the_same_days_intervals_dont_add_up(client):
    day = (TODAY - timedelta(days=2)).isoformat()
    client.post("/api/ingest/healthkit-export", json=_export("step_count", "count", {"date": f"{day} 00:00:00 +0000", "qty": 8000, "source": "Fitbit"}))
    profile = _profile(client)
    _set_zone(client, "UTC")
    biometrics.insert_samples(profile, None, [_s("i", value=8000, start=f"{day}T10:00:00Z", end=f"{day}T11:00:00Z", source="Fitbit")])
    assert _daily(profile, "step_count")[day]["value"] == 8000


def test_a_nights_summary_and_stages_from_one_source_dont_add_up(client):
    profile = _profile(client)
    _set_zone(client, "UTC")
    d = TODAY - timedelta(days=2)
    biometrics.insert_samples(profile, None, [
        _s("t", "sleep_total_duration", 7 * 3600, _iso(d, "00:00:00"), _iso(d, "07:00:00"), unit="s", metadata={"day": d.isoformat()}),
        _s("c", "sleep_analysis_core", 7 * 3600, _iso(d, "00:00:00"), _iso(d, "07:00:00"), unit="s")])
    assert _daily(profile, "sleep_duration")[d.isoformat()]["value"] == 7
    assert sleep.nights(profile, days=10)["nights"][-1]["asleep_h"] == 7


def test_source_kinds_come_from_the_device_not_words_inside_names():
    assert biometrics.device_kind("Snore Monitoring") == "other"
    assert biometrics.device_kind("Spring Scale") == "other"
    assert biometrics.device_kind("Bose Headphones") == "other"
    assert biometrics.device_kind("Alex's Apple Watch") == "watch"
    assert biometrics.device_kind("Oura Ring") == "ring"
    assert biometrics.device_kind("Alex's iPhone") == "phone"
    assert biometrics.device_kind("Series 9", "Apple Watch") == "watch"
    assert biometrics.device_kind("Fitbit", "Google Pixel Watch 3", {"device_type": "watch"}) == "watch"
    assert biometrics.device_kind("MyScale", None, {"device_type": "scale"}) == "other"
    assert biometrics.device_kind("Garmin Connect") == "watch"


def test_a_snore_app_doesnt_outrank_the_watch(client):
    profile, d = _profile(client), TODAY - timedelta(days=2)
    biometrics.insert_samples(profile, None, [_s("w", "sleep_analysis_core", 8 * 3600, _iso(d, "00:00:00"), _iso(d, "08:00:00"), source="Alex's Apple Watch", unit="s"),
                                              _s("s", "sleep_analysis_core", 6.5 * 3600, _iso(d, "01:00:00"), _iso(d, "07:30:00"), source="Snore Monitoring", unit="s")])
    assert _daily(profile, "sleep_duration")[d.isoformat()]["source"] == "Alex's Apple Watch"


def test_a_watch_renamed_by_its_owner_is_still_a_watch(client):
    profile, d = _profile(client), TODAY - timedelta(days=2)
    biometrics.insert_samples(profile, None, [_s("w", "heart_rate", 60, _iso(d), source="Series 9", unit="count/min", device_name="Apple Watch"),
                                              _s("p", "heart_rate", 90, _iso(d), source="Some App", unit="count/min")])
    assert _daily(profile, "heart_rate")[d.isoformat()]["source"] == "Series 9"
    assert biometrics.source_kinds(profile)["Series 9"] == "watch"


def test_combined_ranks_as_the_best_source_merged_into_it(client):
    profile, d = _profile(client), TODAY - timedelta(days=2)
    biometrics.insert_samples(profile, None, [
        _s("r", value=1000, start=_iso(d, "10:00:00"), end=_iso(d, "10:30:00"), source="Band App", device_name="Fitbit Charge", metadata={"device_type": "band"}),
        _s("p", value=900, start=_iso(d, "10:00:00"), end=_iso(d, "10:30:00"), source="Alex's iPhone"),
        _s("o", value=5000, start=_iso(d, "12:00:00"), source="Garmin Connect", metadata={"day": d.isoformat()})])
    # Activity prefers a watch to a ring: the Garmin's own total wins over a merge of a band and a phone, which counted as a watch before.
    assert _daily(profile, "step_count")[d.isoformat()]["source"] == "Garmin Connect"


def test_a_short_run_doesnt_become_the_days_heart_rate(client):
    profile, d = _profile(client), TODAY - timedelta(days=2)
    _set_zone(client, "UTC")
    start = datetime(d.year, d.month, d.day, 8, tzinfo=timezone.utc)
    stamp = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
    samples = [_s(f"run{i}", "heart_rate", 160, stamp(start + timedelta(seconds=i)), unit="count/min") for i in range(1800)]
    samples += [_s(f"rest{h}", "heart_rate", 60, _iso(d, f"{h:02d}:00:00") if h != 8 else _iso(d, "08:45:00"), unit="count/min") for h in range(23)]
    biometrics.insert_samples(profile, None, samples)
    assert _daily(profile, "heart_rate")[d.isoformat()]["value"] == pytest.approx(74.8, abs=0.5)     # not the 159 it was


def test_every_metric_the_iphone_sends_has_a_daily_series(client):
    profile, day = _profile(client), YESTERDAY.isoformat()
    for metric, unit, value in (("insulin_delivery", "IU", 3), ("forced_vital_capacity", "L", 4), ("dietary_iron", "mg", 8), ("uv_exposure", "count", 5), ("blood_alcohol_content", "%", 0.02)):
        biometrics.insert_samples(profile, None, [_s(f"m-{metric}", metric, value, unit=unit)])
        assert _daily(profile, metric), metric
    assert biometrics.METRICS["insulin_delivery"][0] == "Insulin delivery"
    assert _daily(profile, "blood_alcohol_content")[day]["value"] == 2


# ---------------------------------------------------------------------------
# Small things (F275, F276, F279, F280, F239)
# ---------------------------------------------------------------------------

def test_an_interval_across_midnight_is_shared_between_its_days(client):
    profile = _profile(client)
    _set_zone(client, "America/New_York")
    biometrics.insert_samples(profile, None, [_s("mid", value=600, start="2026-10-14T03:30:00Z", end="2026-10-14T04:30:00Z")])   # 23:30 to 00:30 EDT
    assert _daily_all(profile) == {"2026-10-13": 300, "2026-10-14": 300}
    biometrics.insert_samples(profile, None, [_s("mid", value=800, start="2026-10-14T03:30:00Z", end="2026-10-14T04:30:00Z")])
    assert _daily_all(profile) == {"2026-10-13": 400, "2026-10-14": 400}


def test_a_workouts_odd_route_time_and_fine_grained_heart_rate(client):
    profile = _profile(client)
    w = {"id": "w", "name": "Run", "start": _iso(YESTERDAY), "end": _iso(YESTERDAY, "12:30:00"), "duration_s": 1800, "source_name": "App",
         "route": [{"t": "not a time", "lat": 1, "lon": 1}, {"t": _iso(YESTERDAY), "lat": 1, "lon": 1}, {"t": _iso(YESTERDAY, "12:10:00"), "lat": 1.01, "lon": 1}],
         "heart_rate": [{"t": (datetime.fromisoformat(f"{YESTERDAY}T12:00:00+00:00") + timedelta(seconds=5 * i)).isoformat(), "min": 150, "avg": 150, "max": 150} for i in range(360)]}
    activity.store_workouts(profile, None, [w])
    r = client.get("/api/biometrics/workouts/w")
    assert r.status_code == 200 and r.json()["splits"]
    minutes = training.zone_minutes(w, 180)
    assert sum(minutes) == pytest.approx(30, abs=0.2)
    per_minute = [{"t": (datetime.fromisoformat(f"{YESTERDAY}T12:00:00+00:00") + timedelta(minutes=i)).isoformat(), "avg": 150} for i in range(30)]
    assert sum(training.zone_minutes({"heart_rate": per_minute}, 180)) == 30
    assert sum(training.zone_minutes({"heart_rate": [{"avg": 150}, {"t": "x", "avg": 150}]}, 180)) == 2


def test_a_comma_in_a_sources_name_stays_one_source(client):
    profile = _profile(client)
    biometrics.insert_samples(profile, None, [_s("c", value=5, source="Withings, Inc")])
    assert next(m for m in biometrics.available_metrics(profile) if m["metric"] == "step_count")["sources"] == ["Withings, Inc"]


def test_pairing_guesses_from_many_addresses_hit_a_shared_limit(client, monkeypatch):
    from app.api import biometrics as api
    with monkeypatch.context() as m:
        m.setattr(api, "_all_pair_failures", [datetime.now().timestamp()] * api.PAIR_MAX_FAILURES_ALL)
        phone = TestClient(client.app, base_url=BASE)
        assert phone.post("/api/devices/pair", json={"code": "WRONG-CODE", "device_name": "P"}).status_code == 429


def test_an_interrupted_import_says_so_and_leaves_no_upload_behind(client):
    profile = _profile(client)
    job = imports._new_job("apple_health", "export.zip", profile)
    conn = connections.create(profile, "import", "apple_health_export", "Apple Health export", mode="live")
    run = connections.start_run(conn["id"], "import")
    upload = imports.upload_dir() / "upload_left.zip"
    upload.write_bytes(b"x" * 100)
    imports._jobs.clear()       # what a restart does
    imports.recover_at_start_up()
    assert not upload.exists()
    stored = imports.get_job(job)
    assert stored["status"] == "interrupted" and "Import the file again" in stored["error"]
    assert client.get("/api/imports").json()["imports"][0]["id"] == job
    with read() as c:
        assert c.execute("SELECT status FROM sync_runs WHERE id = ?", (run,)).fetchone()[0] == "error"


def test_an_upload_that_wont_fit_is_refused_in_words_and_leaves_nothing(client, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(imports, "check_room", lambda size: (_ for _ in ()).throw(imports.HTTPException(507, imports.NO_ROOM)))
        r = client.post("/api/imports/apple-health", files={"file": ("export.zip", b"PK")})
    assert r.status_code == 507 and "disk space" in r.json()["detail"]
    assert list((config.data_dir() / "uploads").iterdir()) == []


# ---------------------------------------------------------------------------
# Vendors' documented shapes (F150), and what the phones send (F147, F011)
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_every_metric_in_the_phone_catalogs_is_known_and_its_unit_passes_untouched():
    from app.store.metric_registry import QUANTITY_TYPES, REGISTRY, to_canonical
    catalogs = _fixture("phone_metric_catalogs.json")
    assert len(catalogs["ios"]) == 114 and len(catalogs["android"]) == 30
    for entry in catalogs["ios"] + catalogs["android"]:
        assert entry["metric"] in REGISTRY, entry["metric"]
        assert to_canonical(entry["metric"], 1.5, entry["unit"])[0::2] == (1.5, True), entry
    for entry in catalogs["ios"]:
        assert QUANTITY_TYPES[f"HKQuantityTypeIdentifier{entry['hk']}"] == entry["metric"]
        assert (REGISTRY[entry["metric"]].aggregation == "sum") == entry["cumulative"], entry["metric"]


def _sync_fixture(client, monkeypatch, provider, data):
    conn = connections.create(_profile(client), "wearable", provider, provider.title(), mode="live", credentials={"access_token": "AT"})
    module = sync.WEARABLE_MODULES[provider]

    async def fetch(creds, since, until):
        return json.loads(json.dumps(data))
    with monkeypatch.context() as m:
        m.setattr(module, "fetch_live", fetch)
        first = asyncio.run(sync.sync_connection(conn["id"]))
        second = asyncio.run(sync.sync_connection(conn["id"]))
    assert first["status"] == "success" and first["inserted"] > 0 and first["rejected"] == 0
    assert second["inserted"] == 0 and second["updated"] == 0 and second["workouts"] == 0 and second["workouts_updated"] == 0     # nothing moves on a repeat
    return conn


def _days(profile, metric):
    with read() as conn:
        return {r[0]: r[1] for r in conn.execute("SELECT day, value FROM biometric_daily WHERE profile_id = ? AND metric_type = ?", (profile, metric))}


def test_whoops_documented_records_end_up_on_the_right_day(client, monkeypatch):
    profile = _profile(client)
    _set_zone(client, "America/New_York")
    _sync_fixture(client, monkeypatch, "whoop", _fixture("whoop_v2_docs.json"))
    # The doc's sleep runs 21:25 to 05:25 by its own -05:00 offset, so the night and everything scored from it is the morning of the 24th.
    for metric in ("sleep_duration", "recovery_score", "hrv_rmssd", "strain_score", "total_energy", "step_count", "heart_rate"):
        assert list(_days(profile, metric)) == ["2022-04-24"], metric
    assert _days(profile, "total_energy")["2022-04-24"] == pytest.approx(1980.9, abs=0.1)
    w = client.get("/api/biometrics/workouts", params={"days": 0}).json()["workouts"][0]
    assert w["name"] == "Running" and w["tz_offset_min"] == -300


def test_ouras_documented_records_keep_the_nap_apart(client, monkeypatch):
    profile = _profile(client)
    _set_zone(client, "Asia/Tokyo")     # the provider's day and the night's own offset win, wherever home is
    _sync_fixture(client, monkeypatch, "oura", _fixture("oura_v2_docs.json"))
    assert _days(profile, "sleep_duration") == {"2026-10-05": pytest.approx(7.33, abs=0.01)}
    assert _days(profile, "sleep_nap") == {"2026-10-05": 0.75}
    assert _days(profile, "resting_heart_rate") == {"2026-10-05": 50}
    assert _days(profile, "step_count") == {"2026-10-05": 9321} and _days(profile, "sleep_score") == {"2026-10-05": 77}
    night = sleep.nights(profile, days=30, end=date(2026, 10, 6))["nights"][0]
    assert "T22:40" in night["bed"] and "T06:50" in night["wake"]


def test_googles_documented_records_keep_the_nap_apart(client, monkeypatch):
    profile = _profile(client)
    _set_zone(client, "UTC")
    _sync_fixture(client, monkeypatch, "google", _fixture("google_health_v4_docs.json"))
    assert _days(profile, "sleep_duration") == {"2026-10-05": pytest.approx(7.33, abs=0.01)}
    assert _days(profile, "sleep_nap") == {"2026-10-05": 0.75}
    assert _days(profile, "step_count") == {"2026-10-05": 9321} and _days(profile, "distance_walking_running") == {"2026-10-05": 7084}
    assert _days(profile, "body_mass") == {"2026-10-05": 78.2}
    assert biometrics.source_kinds(profile)["Google Health"] == "ring"
    night = sleep.nights(profile, days=30, end=date(2026, 10, 6))["nights"][0]
    assert "T22:40" in night["bed"]


def test_a_long_route_is_thinned_when_an_import_stores_it_too(client):
    profile = _profile(client)
    route = [{"t": _iso(YESTERDAY), "lat": 1.0, "lon": 1.0} for _ in range(activity.SERIES_KEPT + 500)]
    activity.store_workouts(profile, None, [{"id": "long", "name": "Hike", "start": _iso(YESTERDAY), "end": _iso(YESTERDAY, "13:00:00"), "route": route}])
    assert len(activity.get_workout(profile, "long")["route"]) == activity.SERIES_KEPT


def test_a_disk_nearly_full_is_said_before_it_stops_anything(client, monkeypatch):
    from collections import namedtuple
    from app.core import maintenance
    usage = namedtuple("usage", "total used free")
    with monkeypatch.context() as m:
        m.setattr(maintenance.shutil, "disk_usage", lambda path: usage(10, 9, 500 * 1024 * 1024))
        assert any(p["id"] == "disk-low" for p in maintenance.problems())
    assert not any(p["id"] == "disk-low" for p in maintenance.problems())


# ---------------------------------------------------------------------------
# Midnight rows, split nights and later sessions, stored repeats, overflow, offsets, removal
# ---------------------------------------------------------------------------

def _new_york(d: date, clock: str) -> str:
    return datetime.fromisoformat(f"{d.isoformat()}T{clock}:00").replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _a_new_york_night_with_no_clock_change(days_ago: int = 3) -> date:
    """A recent day whose New York clocks don't change the day before, on it or the day after. The nights below are written
    in New York clock times, and on the night the clocks go back, 00:10 to 06:30 is seven hours twenty of sleep, not six twenty."""
    def offset(day):
        return datetime.combine(day, time(12), ZoneInfo("America/New_York")).utcoffset()
    d = TODAY - timedelta(days=days_ago)
    while offset(d - timedelta(days=2)) != offset(d + timedelta(days=1)):
        d -= timedelta(days=1)
    return d


def test_a_midnight_row_sent_alone_doesnt_hide_the_hours_sent_after_it(client):
    _set_zone(client, "America/New_York")
    day = (TODAY - timedelta(days=2)).isoformat()
    for clock, qty in (("00:00", 100), ("01:00", 200), ("02:00", 300)):
        assert client.post("/api/ingest/healthkit-export", json=_export("step_count", "count", {"date": f"{day} {clock}:00 -0400", "qty": qty, "source": "Fit"})).status_code == 200
    assert _daily(_profile(client), "step_count")[day]["value"] == 600


def test_a_midnight_row_counts_with_hours_that_came_before_it_too(client):
    _set_zone(client, "America/New_York")
    day = (TODAY - timedelta(days=2)).isoformat()
    for clock, qty in (("05:00", 200), ("00:00", 100)):
        client.post("/api/ingest/healthkit-export", json=_export("step_count", "count", {"date": f"{day} {clock}:00 -0400", "qty": qty, "source": "Fit"}))
    assert _daily(_profile(client), "step_count")[day]["value"] == 300


def test_the_automation_saying_days_makes_a_midnight_row_the_whole_days_whatever_else_came(client):
    _set_zone(client, "UTC")
    day = (TODAY - timedelta(days=2)).isoformat()
    client.post("/api/ingest/healthkit-export", json=_export("step_count", "count", {"date": f"{day} 00:00:00 +0000", "qty": 8000, "source": "Fit"}),
                headers={"automation-aggregation": "days"})
    client.post("/api/ingest/healthkit-export", json=_export("step_count", "count", {"date": f"{day} 09:00:00 +0000", "qty": 500, "source": "Fit"}))
    assert _daily(_profile(client), "step_count")[day]["value"] == 8000


def _core(sample_id, start, end):
    return _s(sample_id, "sleep_analysis_core", (biometrics.parse_ts(end) - biometrics.parse_ts(start)).total_seconds(), start, end, unit="s")


def test_a_stretch_of_sleep_arriving_later_moves_the_night_off_the_evening(client):
    profile = _profile(client)
    _set_zone(client, "America/New_York")
    d = _a_new_york_night_with_no_clock_change()
    eve = d - timedelta(days=1)
    biometrics.insert_samples(profile, None, [_core("eve", _new_york(eve, "22:30"), _new_york(eve, "23:50"))])
    assert _daily(profile, "sleep_duration")[eve.isoformat()]["value"] == 1.33
    biometrics.insert_samples(profile, None, [_core("morn", _new_york(d, "00:10"), _new_york(d, "06:30"))])
    assert {k: v["value"] for k, v in _daily(profile, "sleep_duration").items()} == {d.isoformat(): 7.67}
    # And back: without the later part, the evening is a night of its own again.
    assert biometrics.delete_sample(profile, "morn")
    assert {k: v["value"] for k, v in _daily(profile, "sleep_duration").items()} == {eve.isoformat(): 1.33}


def test_correcting_the_later_stretch_of_a_night_gives_the_evening_back(client):
    profile = _profile(client)
    _set_zone(client, "America/New_York")
    d = _a_new_york_night_with_no_clock_change()
    eve = d - timedelta(days=1)
    biometrics.insert_samples(profile, None, [_core("eve", _new_york(eve, "22:30"), _new_york(eve, "23:50")), _core("morn", _new_york(d, "00:10"), _new_york(d, "06:30"))])
    # The same id, corrected: it was the next night's sleep.
    biometrics.insert_samples(profile, None, [_core("morn", _new_york(d + timedelta(days=1), "00:10"), _new_york(d + timedelta(days=1), "06:50"))])
    shown = {k: v["value"] for k, v in _daily(profile, "sleep_duration").items()}
    assert shown[eve.isoformat()] == 1.33 and d.isoformat() not in shown
    biometrics.rebuild_daily(profile)
    assert shown == {k: v["value"] for k, v in _daily(profile, "sleep_duration").items()}


def test_an_export_files_batches_splitting_a_night_dont_leave_a_false_one(client, monkeypatch):
    profile = _profile(client)
    _set_zone(client, "America/New_York")
    monkeypatch.setattr(imports, "BATCH", 1)
    d = TODAY - timedelta(days=3)
    eve = d - timedelta(days=1)
    record = ('<Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Watch" value="HKCategoryValueSleepAnalysisAsleepCore" '
              'startDate="{} -0400" endDate="{} -0400"/>')
    job = _import(client, profile, _export_xml(record.format(f"{eve} 22:30:00", f"{eve} 23:50:00"), record.format(f"{d} 00:10:00", f"{d} 06:30:00")))
    assert job["status"] != "error", job
    assert {k: v["value"] for k, v in _daily(profile, "sleep_duration").items()} == {d.isoformat(): 7.67}


@pytest.mark.parametrize("platform", ["ios", None])
def test_stored_repeats_collapse_on_any_connection_that_route_wrote(client, platform):
    from app.services import wearable_fixes
    profile = _profile(client)
    _set_zone(client, "UTC")
    connection = connections.create(profile, "device", "apple_health", "Phone", mode="live", metadata={"platform": platform})["id"] if platform else None
    d = _iso(TODAY - timedelta(days=2), "00:00:00")
    with db() as conn:
        for sid, value, created in (("hx_0", 1000, 1.0), ("hx_1", 3000, 2.0), ("hx_2", 9000, 3.0)):
            conn.execute("INSERT INTO biometric_samples(id, profile_id, connection_id, metric_type, value, unit, start_date, end_date, source_name, created_at) "
                         "VALUES (?, ?, ?, 'step_count', ?, '', ?, ?, 'Fit', ?)", (sid, profile, connection, value, d, d, created))
        wearable_fixes._fix_json_repeats(conn, None, 100)
    with read() as conn:
        assert {r[0] for r in conn.execute("SELECT id FROM biometric_samples")} == {"hx_2"}


@pytest.mark.parametrize("row", [
    {"name": "step_count", "units": "count", "data": [{"date": "0001-01-01 00:00:00 +1400", "qty": 5, "source": "Fit"}]},
    {"name": "step_count", "units": "count", "data": [{"date": "9999-12-31 23:59:59 -1400", "qty": 5, "source": "Fit"}]},
    {"name": "blood_pressure", "data": [{"date": "0001-01-01 00:00:00 +1400", "systolic": 120, "diastolic": 80, "source": "Cuff"}]},
    {"name": "mindful_minutes", "data": [{"date": "2026-01-01 10:00:00 +0000", "qty": 1e30, "source": "Calm"}]},
    {"name": "mindful_minutes", "data": [{"date": "9999-12-31 23:59:59 +0000", "qty": 90, "source": "Calm"}]},
])
def test_dates_and_lengths_that_overflow_go_alone_in_the_export_route(client, row):
    day = (TODAY - timedelta(days=2)).isoformat()
    good = {"name": "dietary_water", "units": "mL", "data": [{"date": f"{day} 10:00:00 +0000", "qty": 250, "source": "Log"}]}
    r = client.post("/api/ingest/healthkit-export", json={"data": {"metrics": [row, good]}})
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 1
    _set_zone(client, "UTC")
    assert _daily(_profile(client), "dietary_water")[day]["value"] == 250


def test_a_workout_with_a_length_that_overflows_is_kept_without_it(client):
    day = (TODAY - timedelta(days=2)).isoformat()
    r = client.post("/api/ingest/healthkit-export", json={"data": {"workouts": [{"name": "Run", "start": f"{day} 10:00:00 +0000", "duration": 1e30, "source": "Watch"}]}})
    assert r.status_code == 200, r.text
    assert r.json()["workouts_inserted"] == 1
    workout = activity.list_workouts(_profile(client), None)[0]
    assert workout["duration_s"] is None and workout["end_date"] == workout["start_date"]
    assert client.get("/api/biometrics/training").status_code == 200


def test_an_impossible_length_on_a_workout_doesnt_break_the_training_page(client):
    batch = {"device_id": "d", "device_name": "Phone", "profile_id": _profile(client),
             "workouts": [{"id": f"w{i}", "name": "Run", "start": _iso(TODAY - timedelta(days=2 + i)), "duration_s": 1.7e308, "source_name": "Watch"} for i in range(3)]}
    assert client.post("/api/ingest/wearables", json=batch).json()["workouts_inserted"] == 3
    assert client.get("/api/biometrics/training").status_code == 200
    assert activity.list_workouts(_profile(client), None)[0]["duration_s"] is None


def test_an_out_of_range_offset_on_a_workout_or_event_isnt_kept(client):
    start = _iso(TODAY - timedelta(days=2))
    batch = {"device_id": "d", "device_name": "Phone", "profile_id": _profile(client),
             "workouts": [{"id": "w1", "name": "Run", "start": start, "duration_s": 600, "tz_offset_min": 100000}],
             "events": [{"id": "e1", "event_type": "headache", "start": start, "tz_offset_min": -100000}]}
    r = client.post("/api/ingest/wearables", json=batch).json()
    assert r["workouts_inserted"] == 1 and r["events_inserted"] == 1
    with read() as conn:
        assert conn.execute("SELECT tz_offset_min FROM workouts WHERE id = 'w1'").fetchone()[0] is None
        assert conn.execute("SELECT tz_offset_min FROM health_events WHERE id = 'e1'").fetchone()[0] is None
    assert client.get("/api/biometrics/training").status_code == 200


def test_an_offset_stored_out_of_range_doesnt_break_the_training_page(client):
    activity.store_workouts(_profile(client), None, [{"id": "w1", "name": "Run", "start": _iso(TODAY - timedelta(days=2)), "duration_s": 600}])
    with db() as conn:
        conn.execute("UPDATE workouts SET tz_offset_min = 100000")
    assert client.get("/api/biometrics/training").status_code == 200


def _night(profile, d):
    return next(n for n in sleep.nights(profile, days=10)["nights"] if n["day"] == d.isoformat())


def test_a_doze_in_the_evening_isnt_part_of_last_nights_sleep(client):
    profile = _profile(client)
    _set_zone(client, "America/New_York")
    d = _a_new_york_night_with_no_clock_change()
    biometrics.insert_samples(profile, None, [_core("night", _new_york(d, "00:30"), _new_york(d, "08:00")), _core("doze", _new_york(d, "20:15"), _new_york(d, "21:00"))])
    night = _night(profile, d)
    assert night["asleep_h"] == 7.5 and night["in_bed_h"] == 7.5 and night["efficiency"] == 1.0 and "T08:00" in night["wake"]
    assert [n["asleep_h"] for n in night["naps"]] == [0.75] and "T20:15" in night["naps"][0]["start"]
    assert _daily(profile, "sleep_nap")[d.isoformat()]["value"] == 0.75


def test_a_long_sleep_in_the_day_isnt_part_of_last_nights_sleep(client):
    profile = _profile(client)
    _set_zone(client, "UTC")
    d = TODAY - timedelta(days=3)
    biometrics.insert_samples(profile, None, [_core("night", _iso(d, "00:30:00"), _iso(d, "08:00:00")), _core("day", _iso(d, "12:00:00"), _iso(d, "16:00:00"))])
    night = _night(profile, d)
    assert night["asleep_h"] == 7.5 and "T08:00" in night["wake"] and night["efficiency"] == 1.0
    assert [n["asleep_h"] for n in night["naps"]] == [4]


def test_going_back_to_sleep_after_hours_awake_at_night_is_still_the_night(client):
    profile = _profile(client)
    _set_zone(client, "UTC")
    d = TODAY - timedelta(days=3)
    biometrics.insert_samples(profile, None, [_core("a", _iso(d - timedelta(days=1), "23:00:00"), _iso(d, "03:00:00")), _core("b", _iso(d, "06:15:00"), _iso(d, "09:30:00"))])
    night = _night(profile, d)
    assert night["asleep_h"] == 7.25 and "T09:30" in night["wake"] and night["naps"] == []


def test_a_stretch_of_only_awake_time_in_the_day_isnt_a_nap(client):
    profile = _profile(client)
    _set_zone(client, "UTC")
    d = TODAY - timedelta(days=3)
    biometrics.insert_samples(profile, None, [_core("night", _iso(d, "00:30:00"), _iso(d, "08:00:00")),
                                              _s("woke", "sleep_analysis_awake", 3600, _iso(d, "15:00:00"), _iso(d, "16:00:00"), unit="s")])
    assert _night(profile, d)["naps"] == []


def test_readings_left_out_of_a_measure_with_nothing_to_chart_can_still_be_found(client):
    profile = _profile(client)
    biometrics.insert_samples(profile, None, [_s("w", "body_mass", -5, unit="kg"),
                                              _s("n", "sleep_total_duration", 900000, _iso(TODAY - timedelta(days=2)), _iso(YESTERDAY), unit="s", metadata={"day": YESTERDAY.isoformat()})])
    body = client.get("/api/biometrics/metrics", params={"latest": "true"}).json()
    assert body["metrics"] == []
    assert {(x["metric"], x["label"], x["count"]) for x in body["left_out"]} == {("body_mass", "Weight", 1), ("sleep_total_duration", "Sleep", 1)}
    listed = client.get("/api/biometrics/samples", params={"left_out": "true"}).json()["samples"]
    assert {s["id"]: s["label"] for s in listed} == {"w": "Weight", "n": "Sleep"}


def test_sleeps_readings_are_the_ones_it_is_worked_out_from(client):
    profile = _profile(client)
    d = TODAY - timedelta(days=3)
    biometrics.insert_samples(profile, None, [
        _core("a", _iso(d, "01:00:00"), _iso(d, "02:00:00")),
        _s("b", "sleep_analysis_deep", 3600, _iso(d, "02:00:00"), _iso(d, "03:00:00"), unit="s"),
        _s("bad", "sleep_total_duration", 900000, _iso(d, "01:00:00"), _iso(d, "03:00:00"), unit="s", metadata={"day": d.isoformat()})])
    listed = client.get("/api/biometrics/samples", params={"metric": "sleep_duration"}).json()["samples"]
    assert {s["id"] for s in listed} == {"a", "b", "bad"}
    assert {s["id"] for s in client.get("/api/biometrics/samples", params={"metric": "sleep_deep"}).json()["samples"]} == {"b"}
    assert biometrics.daily_series(profile, "sleep_duration", days=10)["left_out"] == 1
    assert [s["id"] for s in client.get("/api/biometrics/samples", params={"metric": "sleep_duration", "left_out": "true"}).json()["samples"]] == ["bad"]


def test_a_wrong_reading_in_the_possible_range_can_be_found_by_date_and_removed(client):
    profile = _profile(client)
    _set_zone(client, "America/New_York")
    old = TODAY - timedelta(days=5)
    biometrics.insert_samples(profile, None, [_s("kid", "body_mass", 40, _new_york(old, "23:30"), unit="kg"),
                                              *[_s(f"me{i}", "body_mass", 80, _iso(TODAY - timedelta(days=i), "08:00:00"), unit="kg") for i in range(1, 4)]])
    listed = client.get("/api/biometrics/samples", params={"metric": "body_mass", "until": old.isoformat(), "limit": 1}).json()["samples"]
    assert [s["id"] for s in listed] == ["kid"]
    assert client.get("/api/biometrics/samples", params={"metric": "body_mass", "until": "soon"}).status_code == 400
    assert client.delete(f"/api/biometrics/samples/{listed[0]['id']}").status_code == 200
    assert old.isoformat() not in _daily(profile, "body_mass")


def _two_people_one_id(client):
    alex = _profile(client)
    jordan = client.post("/api/profiles", json={"name": "Jordan"}).json()["id"]
    biometrics.insert_samples(alex, None, [_s("UUID1")])
    biometrics.insert_samples(jordan, None, [_s("UUID1")])
    return alex, jordan


def test_a_reading_removed_under_its_id_for_a_second_person_stays_removed(client):
    alex, jordan = _two_people_one_id(client)
    stored = biometrics.recent_samples(jordan)[0]["id"]
    assert stored == f"UUID1@{jordan}"
    assert biometrics.delete_sample(jordan, stored)
    assert _daily(jordan, "step_count") == {}
    assert biometrics.insert_samples(jordan, None, [_s("UUID1")])["inserted"] == 0
    assert _daily(jordan, "step_count") == {}
    assert _daily(alex, "step_count")[YESTERDAY.isoformat()]["value"] == 100


def test_a_reading_removed_under_its_plain_id_for_a_second_person_stays_removed(client):
    _, jordan = _two_people_one_id(client)
    assert biometrics.delete_sample(jordan, "UUID1")
    assert biometrics.insert_samples(jordan, None, [_s("UUID1")])["inserted"] == 0
    assert _daily(jordan, "step_count") == {}


def test_a_reading_is_listed_in_the_unit_its_daily_figure_uses(client):
    profile = _profile(client)
    d = TODAY - timedelta(days=3)
    biometrics.insert_samples(profile, None, [
        _s("steps", value=120, unit="count"), _core("slept", _iso(d, "01:00:00"), _iso(d, "02:30:00")),
        {**_s("odd", "body_mass", 70, unit="stone-ish"), "strict_units": True}])
    listed = {s["id"]: s for s in client.get("/api/biometrics/samples").json()["samples"]}
    assert (listed["steps"]["shown_value"], listed["steps"]["shown_unit"], listed["steps"]["label"]) == (120, "steps", "Steps")
    assert (listed["slept"]["shown_value"], listed["slept"]["shown_unit"]) == (1.5, "h")
    # Never converted, so it isn't called kilograms.
    assert listed["odd"]["flag"] == "unit" and (listed["odd"]["shown_value"], listed["odd"]["shown_unit"]) == (70, "stone-ish")
