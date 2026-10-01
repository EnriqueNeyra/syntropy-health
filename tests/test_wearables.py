"""Wearables (simulated Oura/WHOOP), companion pairing, ingest and daily rollups."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.connectors import oura, whoop
from app.store import biometrics
from tests.conftest import BASE


def _sample(i, metric, value, source="Apple Watch", day_offset=0, unit="count"):
    t = datetime.now(timezone.utc).replace(hour=15, minute=0, second=0, microsecond=0) - timedelta(days=day_offset)
    return {"id": f"s-{metric}-{i}-{source}", "metric_type": metric, "hk_identifier": "HK", "value": value, "unit": unit,
            "start_date": t.isoformat(), "end_date": (t + timedelta(minutes=5)).isoformat(), "source_name": source}


def test_simulated_wearables_populate_daily_metrics(client):
    for provider in ("oura", "whoop"):
        r = client.post("/api/connections/wearable", json={"provider": provider, "mode": "simulated"})
        assert r.status_code == 200, r.text
    conns = client.get("/api/connections").json()["connections"]
    assert {c["provider"] for c in conns} == {"oura", "whoop"}
    assert all(c["sample_count"] > 100 for c in conns)
    metrics = {m["metric"] for m in client.get("/api/biometrics/metrics").json()["metrics"]}
    assert {"hrv_rmssd", "recovery_score", "readiness_score", "sleep_duration", "step_count"} <= metrics
    assert "hrv_sdnn" not in metrics            # ring/strap HRV is RMSSD
    series = client.get("/api/biometrics/daily", params={"metric": "sleep_duration", "days": 30}).json()
    assert len(series["points"]) >= 28
    # The Trends list shows each metric's latest value, the same one the chart ends on.
    latest = {m["metric"]: m for m in client.get("/api/biometrics/metrics", params={"latest": True}).json()["metrics"]}
    assert latest["sleep_duration"]["latest_value"] == series["points"][-1]["value"]
    assert all(m["latest_value"] is not None for m in latest.values())
    assert all(4 < p["value"] < 10 for p in series["points"])
    overview = client.get("/api/biometrics/overview").json()
    assert any(h["metric"] == "recovery_score" for h in overview["headline"])
    # Per-source totals are cached; they follow samples being added and removed.
    total = overview["total_samples"]
    assert total == sum(c["sample_count"] for c in conns) and {s["source_name"] for s in overview["sources"]}
    brief = client.get("/api/biometrics/overview", params={"brief": True}).json()
    assert brief["has_samples"] and brief["headline"] and "sources" not in brief
    whoop = next(c for c in conns if c["provider"] == "whoop")
    assert client.delete(f"/api/connections/{whoop['id']}", params={"delete_data": True}).status_code == 200
    after = client.get("/api/biometrics/overview").json()
    assert after["total_samples"] == total - whoop["sample_count"]
    assert client.post("/api/connections/wearable", json={"provider": "whoop", "mode": "simulated"}).status_code == 200
    assert client.get("/api/biometrics/overview").json()["total_samples"] == total


def test_oura_normalization_uses_rmssd_and_day():
    samples = oura.normalize(oura.simulate(days=3))
    types = {s["metric_type"] for s in samples}
    assert "hrv_rmssd" in types and "hrv_sdnn" not in types
    assert all(s["metadata"] and s["metadata"].get("day") for s in samples if s["metric_type"] == "readiness_score")


def test_whoop_energy_is_total_not_active():
    samples = whoop.normalize(whoop.simulate(days=2))
    assert any(s["metric_type"] == "total_energy" for s in samples)
    assert not any(s["metric_type"] == "active_energy" for s in samples)


def test_pairing_and_device_ingest(client):
    code = client.post("/api/devices/pairing-code", json={}).json()["code"]
    anon = TestClient(client.app, base_url=BASE)  # the phone: no dashboard session
    bad = anon.post("/api/devices/pair", json={"code": "WRONG-CODE", "device_name": "Phone"})
    assert bad.status_code == 400
    paired = anon.post("/api/devices/pair", json={"code": code.lower(), "device_name": "Alex's iPhone"}).json()
    token = paired["device_token"]
    assert anon.post("/api/devices/pair", json={"code": code, "device_name": "again"}).status_code == 400  # single use

    batch = {"device_id": "ios-1", "device_name": "Alex's iPhone", "sync_trigger": "manual",
             "samples": [_sample(1, "step_count", 1200), _sample(2, "step_count", 800),
                         _sample(3, "step_count", 5000, source="iPhone"),
                         _sample(4, "oxygen_saturation", 0.97, unit="%")]}
    assert anon.post("/api/ingest/wearables", json=batch).status_code == 401
    r = anon.post("/api/ingest/wearables", json=batch, headers={"X-Syntropy-Device-Token": token}).json()
    assert r["inserted"] == 4
    again = anon.post("/api/ingest/wearables", json=batch, headers={"Authorization": f"Bearer {token}"}).json()
    assert again["inserted"] == 0 and again["duplicates"] == 4
    summary = anon.get("/api/wearables/summary", headers={"X-Syntropy-Device-Token": token}).json()
    assert summary["data"]["total_samples"] == 4

    steps = client.get("/api/biometrics/daily", params={"metric": "step_count", "days": 3}).json()
    assert steps["points"][-1]["value"] == 2000          # iPhone steps in the Watch's interval aren't double counted
    assert steps["points"][-1]["source"] == "Combined"
    spo2 = client.get("/api/biometrics/daily", params={"metric": "oxygen_saturation", "days": 3}).json()
    assert spo2["points"][-1]["value"] == 97.0          # HealthKit fractions become percentages

    device_conn = [c for c in client.get("/api/connections").json()["connections"] if c["kind"] == "device"]
    assert device_conn and device_conn[0]["sample_count"] == 4

    dev_id = client.get("/api/devices").json()["devices"][0]["id"]
    client.delete(f"/api/devices/{dev_id}")
    assert anon.post("/api/ingest/wearables", json=batch, headers={"X-Syntropy-Device-Token": token}).status_code == 401


def test_pair_info_is_public_and_secret_free(raw_client):
    info = raw_client.get("/api/wearables/pair").json()
    assert "token" not in str(info).lower()


def test_sleep_stages_roll_up_to_wake_day(client):
    from app.store import profiles
    prof = profiles.default_profile()
    wake = datetime.now(timezone.utc).replace(hour=7, minute=0, second=0, microsecond=0)
    samples = [
        {"id": "deep", "metric_type": "sleep_analysis_deep", "value": 3600, "unit": "seconds",
         "start_date": (wake - timedelta(hours=7)).isoformat(), "end_date": (wake - timedelta(hours=6)).isoformat(), "source_name": "Apple Watch"},
        {"id": "core", "metric_type": "sleep_analysis_core", "value": 5 * 3600, "unit": "seconds",
         "start_date": (wake - timedelta(hours=6)).isoformat(), "end_date": (wake - timedelta(hours=1)).isoformat(), "source_name": "Apple Watch"},
        {"id": "awake", "metric_type": "sleep_analysis_awake", "value": 1800, "unit": "seconds",
         "start_date": (wake - timedelta(hours=1)).isoformat(), "end_date": wake.isoformat(), "source_name": "Apple Watch"},
    ]
    biometrics.insert_samples(prof["id"], None, samples)
    series = biometrics.daily_series(prof["id"], "sleep_duration", days=2)
    assert series["points"][-1]["value"] == 6.0
    assert series["points"][-1]["day"] == wake.date().isoformat()


def test_night_before_midnight_counts_toward_the_morning(client):
    """A night from 22:30 to 06:30 is one night on the wake-up day. Before, the stages before midnight were given to the
    evening's date: a one-hour "night" on days the Watch recorded no sleep, and a short one the next morning."""
    from app.store import profiles
    prof = profiles.default_profile()
    today = datetime.now(timezone.utc).date()
    woke = today - timedelta(days=1)            # nothing recorded the night before
    evening = datetime.combine(woke - timedelta(days=1), datetime.min.time(), timezone.utc)

    def stage(i, metric, start_h, end_h, source="Apple Watch"):
        return {"id": f"st{i}-{source}", "metric_type": metric, "value": (end_h - start_h) * 3600, "unit": "s",
                "source_name": source, "start_date": (evening + timedelta(hours=start_h)).isoformat(),
                "end_date": (evening + timedelta(hours=end_h)).isoformat()}
    biometrics.insert_samples(prof["id"], None, [
        stage(1, "sleep_analysis_core", 22.5, 23.5), stage(2, "sleep_analysis_deep", 23.5, 24.5),
        stage(3, "sleep_analysis_awake", 24.5, 24.75), stage(4, "sleep_analysis_rem", 24.75, 26),
        stage(5, "sleep_analysis_core", 26, 30.5),
        # The same app also wrote one "asleep" sample over the whole night: it mustn't count twice.
        stage(6, "sleep_analysis_asleep", 22.5, 30.5, source="Sleep app"),
        stage(7, "sleep_analysis_core", 22.5, 30.5, source="Sleep app"),
    ])
    for metric in ("sleep_duration", "sleep_core", "sleep_awake"):
        pts = biometrics.daily_series(prof["id"], metric, days=4)["points"]
        assert [p["day"] for p in pts] == [woke.isoformat()], metric
    by_source = {p["source"]: p["value"] for p in [
        *biometrics.daily_series(prof["id"], "sleep_duration", days=4, source="Apple Watch")["points"],
        *biometrics.daily_series(prof["id"], "sleep_duration", days=4, source="Sleep app")["points"]]}
    assert by_source == {"Apple Watch": 7.75, "Sleep app": 8.0}      # choosing a source in Trends shows that source
    assert biometrics.daily_series(prof["id"], "sleep_core", days=4)["points"][0]["value"] == 5.5

    # Stages arriving later (the evening's first) still move the whole night, not just the day they end on.
    biometrics.insert_samples(prof["id"], None, [stage(8, "sleep_analysis_core", 21.5, 22.5)])
    pts = biometrics.daily_series(prof["id"], "sleep_duration", days=4, source="Apple Watch")["points"]
    assert [(p["day"], p["value"]) for p in pts] == [(woke.isoformat(), 8.75)]


def _pending_state(client, provider):
    from urllib.parse import parse_qs, urlparse
    r = client.post("/api/connections/wearable", json={"provider": provider, "mode": "live"})
    assert r.status_code == 200, r.text
    return parse_qs(urlparse(r.json()["auth_url"]).query)["state"][0]


def test_live_wearable_relay_fragment_handoff(client):
    state = _pending_state(client, "whoop")
    page = client.get("/callback")
    assert "location.hash" in page.text              # the page that reads the fragment
    r = client.post("/api/connections/oauth/relay", json={"state": state, "access_token": "AT", "refresh_token": "RT",
                                                            "expires_in": 3600, "scope": "offline read:recovery"})
    assert r.status_code == 200, r.text
    conn = client.get(f"/api/connections/{r.json()['connection_id']}").json()
    assert conn["provider"] == "whoop" and conn["mode"] == "live" and conn["has_refresh_token"]
    # state is single use
    again = client.post("/api/connections/oauth/relay", json={"state": state, "access_token": "AT"})
    assert again.status_code == 400


def test_live_wearable_legacy_query_handoff(client):
    state = _pending_state(client, "oura")
    done = client.get("/callback", params={"relay_provider": "oura", "access_token": "AT", "refresh_token": "RT",
                                           "expires_in": 86400, "state": state}, follow_redirects=False)
    assert "connected=" in done.headers["location"]


def test_relay_rejects_unknown_state(client):
    r = client.post("/api/connections/oauth/relay", json={"state": "forged", "access_token": "AT"})
    assert r.status_code == 400


def test_simulated_wearables_import_workouts_once(client):
    ids = [client.post("/api/connections/wearable", json={"provider": p, "mode": "simulated"}).json()["connection_id"]
           for p in ("oura", "whoop")]
    res = client.get("/api/biometrics/workouts", params={"days": 120, "limit": 500}).json()
    by_source = {w["source_name"] for w in res["workouts"]}
    assert by_source == {"Oura Ring (simulated)", "WHOOP (simulated)"}   # demo data is labelled
    assert all(w["duration_s"] and w["duration_s"] > 0 for w in res["workouts"])
    assert all(w["end_date"] <= datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") for w in res["workouts"])
    assert res["summary"]["energy_kcal"] > 0
    count = res["summary"]["count"]
    for cid in ids:
        assert client.post(f"/api/connections/{cid}/sync").status_code == 200
    assert client.get("/api/biometrics/workouts", params={"days": 120, "limit": 500}).json()["summary"]["count"] == count


def test_oura_workout_normalization():
    w = oura.normalize_workouts({"workout": [{
        "id": "abc", "activity": "cycling", "calories": 412.5, "day": "2026-09-20", "distance": 18000.0,
        "start_datetime": "2026-09-20T07:00:00-07:00", "end_datetime": "2026-09-20T08:10:00-07:00",
        "intensity": "moderate", "label": None, "source": "confirmed"}]})
    assert w == [{"id": "oura-wo-abc", "activity_type": "cycling", "name": "Cycling",
                  "start": "2026-09-20T07:00:00-07:00", "end": "2026-09-20T08:10:00-07:00", "duration_s": 4200.0,
                  "active_energy_kcal": 412.5, "distance_m": 18000.0, "source_name": "Oura Ring",
                  "device_name": "Oura Ring", "metadata": {"intensity": "moderate", "source": "confirmed", "day": "2026-09-20"}}]


def test_whoop_workout_normalization():
    scored = {"id": "ecfc6a15", "start": "2026-09-20T14:00:00.000Z", "end": "2026-09-20T14:45:00.000Z",
              "sport_name": "functional-fitness", "score_state": "SCORED",
              "score": {"strain": 11.2, "average_heart_rate": 141, "max_heart_rate": 172, "kilojoule": 1569.34,
                        "percent_recorded": 100, "distance_meter": None, "altitude_gain_meter": None}}
    pending = dict(scored, id="x", score_state="PENDING_SCORE", score=None)
    [w] = whoop.normalize_workouts({"workout": [scored, pending]})
    assert w["id"] == "whoop-wo-ecfc6a15" and w["name"] == "Functional Fitness"
    assert w["duration_s"] == 2700 and w["avg_hr"] == 141 and w["max_hr"] == 172
    assert w["total_energy_kcal"] == 375.1 and "active_energy_kcal" not in w   # WHOOP reports total energy
    assert w["metadata"]["strain"] == 11.2


def test_simulated_sleep_stages_add_up():
    sleeps = oura.simulate(days=30)["sleep"]
    assert all(s["deep_sleep_duration"] + s["rem_sleep_duration"] + s["light_sleep_duration"] == s["total_sleep_duration"]
               for s in sleeps)
    assert len({s["awake_time"] for s in sleeps}) > 5


def test_steps_merge_watch_and_phone_like_apple_health(client):
    from app.store.biometrics import merge_interval_sources
    items = [("2026-09-18T10:00:00Z", "2026-09-18T10:10:00Z", 1000, "Alex's Apple Watch"),
             ("2026-09-18T10:00:00Z", "2026-09-18T10:10:00Z", 950, "Alex's iPhone"),    # same walk: dropped
             ("2026-09-18T10:05:00Z", "2026-09-18T10:15:00Z", 600, "Alex's iPhone"),    # half covered: 300
             ("2026-09-18T18:00:00Z", "2026-09-18T18:30:00Z", 2000, "Alex's iPhone")]   # watch off: kept
    assert merge_interval_sources(items) == 3300

    def s(i, src, start, end, v):
        return {"id": f"m{i}", "metric_type": "step_count", "hk_identifier": "HK", "value": v, "unit": "count",
                "start_date": start, "end_date": end, "source_name": src}
    today = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    iso = lambda m: (today + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
    profile = client.get("/api/status").json()["profiles"][0]["id"]
    biometrics.insert_samples(profile, None, [
        s(1, "Alex's Apple Watch", iso(0), iso(10), 1000), s(2, "Alex's iPhone", iso(0), iso(10), 950),
        s(3, "Alex's iPhone", iso(60), iso(90), 2000)])
    pts = client.get("/api/biometrics/daily", params={"metric": "step_count", "days": 3}).json()["points"]
    assert pts[-1]["value"] == 3000 and pts[-1]["source"] == "Combined"


def test_daily_rollups_rebuilt_once_after_upgrade(client, monkeypatch):
    from app.core import settings as app_settings
    calls = []
    monkeypatch.setattr(biometrics, "rebuild_daily", lambda pid, days=None, metrics=None: calls.append(pid))
    profile = client.get("/api/status").json()["profiles"][0]["id"]
    biometrics.insert_samples(profile, None, [{"id": "u1", "metric_type": "step_count", "value": 5, "unit": "count",
                                               "start_date": "2026-09-20T10:00:00Z", "source_name": "Watch"}])
    calls.clear()
    app_settings.set("biometrics.daily_version", 1)
    biometrics.upgrade_daily_if_needed()
    biometrics.upgrade_daily_if_needed()
    assert calls == [profile]


def _night(profile, day, source, hours, i):
    end = datetime.fromisoformat(f"{day}T06:30:00+00:00")
    return {"id": f"n{i}-{source}", "metric_type": "sleep_analysis_core", "value": hours * 3600, "unit": "s",
            "start_date": (end - timedelta(hours=hours)).isoformat(), "end_date": end.isoformat(), "source_name": source}


def test_source_order_per_metric_group(client):
    profile = client.get("/api/status").json()["profiles"][0]["id"]
    today = datetime.now(timezone.utc).date()
    d1, d2 = (today - timedelta(days=2)).isoformat(), (today - timedelta(days=1)).isoformat()
    biometrics.insert_samples(profile, None, [
        _night(profile, d1, "Alex's Apple Watch", 7.0, 1), _night(profile, d1, "Oura", 7.4, 2),       # both full nights
        _night(profile, d2, "Alex's Apple Watch", 7.2, 3), _night(profile, d2, "Oura", 3.0, 4),       # ring came off at 2 AM
        _night(profile, d2, "Oura Ring (simulated)", 9.0, 5),
        *[{"id": f"hr{i}-{s}", "metric_type": "heart_rate", "value": v, "unit": "count/min", "source_name": s,
           "start_date": f"{d1}T15:00:0{i}Z"} for i, (s, v) in enumerate([("Alex's Apple Watch", 70), ("Oura", 60)])]])
    sleep = {p["day"]: p for p in biometrics.daily_series(profile, "sleep_duration", days=5)["points"]}
    assert sleep[d1]["source"] == "Oura"                        # rings first for sleep by default
    assert sleep[d2]["source"] == "Alex's Apple Watch"          # ...unless the ring only caught part of the night
    assert biometrics.daily_series(profile, "sleep_core", days=5)["points"][-1]["source"] == "Alex's Apple Watch"
    hr = {p["day"]: p for p in biometrics.daily_series(profile, "heart_rate", days=5)["points"]}
    assert hr[d1]["source"] == "Alex's Apple Watch"             # Watch first for daytime heart rate

    r = client.put("/api/settings/source-order", json={"sleep": ["watch", "ring", "phone", "other"]})
    assert r.status_code == 200, r.text
    assert r.json()["orders"]["sleep"][0] == "watch"
    assert {p["day"]: p for p in biometrics.daily_series(profile, "sleep_duration", days=5)["points"]}[d1]["source"] == "Alex's Apple Watch"
    assert client.put("/api/settings/source-order", json={"sleep": ["watch"]}).status_code == 400


# ---------------------------------------------------------------------------
# Google Health
# ---------------------------------------------------------------------------

def test_simulated_google_health_fills_daily_metrics_and_workouts(client):
    r = client.post("/api/connections/wearable", json={"provider": "google", "mode": "simulated"})
    assert r.status_code == 200, r.text
    cid = r.json()["connection_id"]
    conn = client.get(f"/api/connections/{cid}").json()
    assert conn["display_name"] == "Google Health (simulated)" and conn["last_sync_status"] == "success", conn
    prof = client.get("/api/profiles").json()["profiles"][0]
    for metric in ("step_count", "resting_heart_rate", "hrv_rmssd", "sleep_duration", "sleep_deep", "body_mass",
                   "oxygen_saturation", "flights_climbed", "body_temperature_deviation"):
        points = biometrics.daily_series(prof["id"], metric, days=30)["points"]
        assert points, metric
    workouts = client.get("/api/biometrics/workouts", params={"days": 120, "limit": 500}).json()["workouts"]
    assert workouts and {w["source_name"] for w in workouts} == {"Google Health (simulated)"}


def test_google_health_normalization_uses_units_and_skips_naps():
    from app.connectors import google_health as gh
    data = {
        "rollup:distance": [{"civilStartTime": {"date": {"year": 2026, "month": 9, "day": 20}},
                             "distance": {"millimetersSum": "5230000"}}],
        "weight": [{"name": "users/me/dataTypes/weight/dataPoints/w1",
                    "weight": {"sampleTime": {"physicalTime": "2026-09-20T07:00:00Z"}, "weightGrams": 81250}}],
        "daily-heart-rate-variability": [{"dailyHeartRateVariability": {"date": {"year": 2026, "month": 9, "day": 20},
                                                                         "averageHeartRateVariabilityMilliseconds": 38.5}}],
        "sleep": [{"sleep": {"interval": {"startTime": "2026-09-20T13:00:00Z", "endTime": "2026-09-20T14:00:00Z"},
                             "metadata": {"nap": True}, "summary": {"minutesAsleep": "55"}}}],
    }
    by_metric = {s["metric_type"]: s for s in gh.normalize(data)}
    assert by_metric["distance_walking_running"]["value"] == 5230.0          # millimetres → metres
    assert by_metric["body_mass"]["value"] == 81.25 and by_metric["body_mass"]["id"] == "gh-weight-w1"
    assert by_metric["hrv_rmssd"]["metadata"]["day"] == "2026-09-20"        # Fitbit's daily HRV is RMSSD
    assert "sleep_total_duration" not in by_metric                          # naps aren't the night's sleep
    [w] = gh.normalize_workouts({"exercise": [{"name": "users/me/dataTypes/exercise/dataPoints/e1", "exercise": {
        "interval": {"startTime": "2026-09-20T15:00:00Z", "endTime": "2026-09-20T15:40:00Z"}, "exerciseType": "RUNNING",
        "activeDuration": "2280s", "metricsSummary": {"caloriesKcal": 410.5, "distanceMillimeters": 6100000,
                                                      "averageHeartRateBeatsPerMinute": "151"}}}]})
    assert (w["id"], w["name"], w["duration_s"], w["distance_m"], w["avg_hr"], w["total_energy_kcal"]) == \
        ("gh-wo-e1", "Running", 2280.0, 6100.0, 151.0, 410.5)


async def test_google_health_fetch_pages_filters_and_chunks(monkeypatch):
    from datetime import date
    from app.connectors import google_health as gh
    calls = []

    async def fake(client, method, url, token, **kw):
        calls.append((method, url.rsplit("/dataTypes/", 1)[1], {k: dict(v) for k, v in kw.items()}))
        if method == "POST":
            return {"rollupDataPoints": []}
        if "pageToken" not in kw["params"] and url.endswith("/weight/dataPoints"):
            return {"dataPoints": [{"weight": {}}], "nextPageToken": "p2"}
        return {"dataPoints": [{"weight": {}}]} if url.endswith("/weight/dataPoints") else None
    monkeypatch.setattr(gh, "_request", fake)
    data = await gh.fetch_live({"access_token": "AT"}, date(2026, 6, 1), date(2026, 9, 26))
    assert len(data["weight"]) == 2                                          # followed nextPageToken
    lists = {path: kw["params"]["filter"] for m, path, kw in calls if m == "GET" and "pageToken" not in kw["params"]}
    assert lists["weight/dataPoints"] == 'weight.sample_time.physical_time >= "2026-06-01T00:00:00Z"'
    assert lists["daily-heart-rate-variability/dataPoints"] == 'daily_heart_rate_variability.date >= "2026-06-01"'
    assert lists["sleep/dataPoints"] == 'sleep.interval.civil_end_time >= "2026-06-01"'
    assert lists["exercise/dataPoints"] == 'exercise.interval.civil_start_time >= "2026-06-01"'
    total_cal = [kw["json"]["range"] for m, path, kw in calls if path == "total-calories/dataPoints:dailyRollUp"]
    assert len(total_cal) == 9                                               # 14-day limit for total calories
    assert len([1 for m, path, _ in calls if path == "steps/dataPoints:dailyRollUp"]) == 2   # 90-day limit


def test_google_health_needs_an_oauth_client_then_asks_for_offline_access(client):
    from urllib.parse import parse_qs, urlparse
    r = client.post("/api/connections/wearable", json={"provider": "google", "mode": "live"})
    assert r.status_code == 400 and "Google Cloud OAuth client" in r.text
    client.put("/api/settings/wearables/google", json={"client_id": "123.apps.googleusercontent.com", "client_secret": "s"})
    q = parse_qs(urlparse(client.post("/api/connections/wearable", json={"provider": "google", "mode": "live"}).json()["auth_url"]).query)
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"] and "include_granted_scopes" not in q
    assert all(s.startswith("https://www.googleapis.com/auth/googlehealth.") and s.endswith(".readonly") for s in q["scope"][0].split())


def _paired_phone(client):
    code = client.post("/api/devices/pairing-code", json={}).json()["code"]
    anon = TestClient(client.app, base_url=BASE)
    token = anon.post("/api/devices/pair", json={"code": code, "device_name": "Alex's iPhone"}).json()["device_token"]
    return anon, {"X-Syntropy-Device-Token": token}


def test_compressed_uploads(client):
    import gzip
    import json
    import zlib

    anon, auth = _paired_phone(client)
    probe = anon.get("/api/wearables/pair").json()
    assert set(probe["upload_encodings"]) == {"gzip", "deflate"} and probe["app_shell"] == 1
    batch = {"device_id": "ios-1", "device_name": "Alex's iPhone",
             "samples": [_sample(i, "heart_rate", 60 + i, unit="count/min") for i in range(50)]}
    body = json.dumps(batch).encode()
    r = anon.post("/api/ingest/wearables", content=gzip.compress(body),
                  headers={**auth, "Content-Type": "application/json", "Content-Encoding": "gzip"})
    assert r.status_code == 200 and r.json()["inserted"] == 50, r.text
    # Raw deflate (what Apple's Compression framework writes) and zlib-wrapped deflate are both read.
    raw = zlib.compressobj(wbits=-15)
    for payload in (raw.compress(body) + raw.flush(), zlib.compress(body)):
        again = anon.post("/api/ingest/wearables", content=payload,
                          headers={**auth, "Content-Type": "application/json", "Content-Encoding": "deflate"})
        assert again.status_code == 200 and again.json()["duplicates"] == 50, again.text
    # A small body that would inflate past the limit is refused rather than expanded; garbage is a 400.
    bomb = gzip.compress(b" " * (65 * 1024 * 1024))
    assert anon.post("/api/ingest/wearables", content=bomb,
                     headers={**auth, "Content-Type": "application/json", "Content-Encoding": "gzip"}).status_code == 413
    assert anon.post("/api/ingest/wearables", content=b"not gzip",
                     headers={**auth, "Content-Type": "application/json", "Content-Encoding": "gzip"}).status_code == 400
    assert anon.post("/api/ingest/wearables", content=body,
                     headers={**auth, "Content-Type": "application/json", "Content-Encoding": "br"}).status_code == 415


def test_phone_reports_history_progress(client):
    anon, auth = _paired_phone(client)
    r = anon.post("/api/wearables/history", json={"complete": False, "sent": 120000, "types_done": 12, "types_total": 70,
                                                  "current": "Heart rate"}, headers=auth)
    assert r.status_code == 200, r.text
    source = next(c for c in client.get("/api/connections").json()["connections"] if c["kind"] == "device")
    assert source["metadata"]["history"]["complete"] is False and source["metadata"]["history"]["types_total"] == 70
    assert source["metadata"]["platform"] == "ios"          # the rest of the metadata is kept
    anon.post("/api/wearables/history", json={"complete": True, "sent": 2100000}, headers=auth)
    source = next(c for c in client.get("/api/connections").json()["connections"] if c["kind"] == "device")
    assert source["metadata"]["history"]["complete"] is True
    assert client.post("/api/wearables/history", json={"complete": True}).status_code == 400   # not from the dashboard


def test_rollups_for_changed_types_match_a_full_rebuild(client):
    profile = client.get("/api/status").json()["profiles"][0]["id"]
    # Scattered days, several types (sleep stages feed several rollups), two sources that get merged.
    samples = [_sample(i, "step_count", 100 + i, day_offset=d) for i, d in enumerate((0, 1, 5, 30, 31, 200))]
    samples += [_sample(i, "step_count", 50, source="iPhone", day_offset=d) for i, d in enumerate((1, 30))]
    samples += [_sample(i, "heart_rate", 70, day_offset=d, unit="count/min") for i, d in enumerate((2, 30))]
    samples += [{**_sample(i, "sleep_analysis_core", 3600, day_offset=d, unit="s"), "metric_type": "sleep_analysis_core"}
                for i, d in enumerate((3, 31))]
    for chunk in (samples[:5], samples[5:9], samples[9:]):
        biometrics.insert_samples(profile, None, chunk)

    def daily():
        with biometrics.read() as conn:
            return sorted(tuple(r) for r in conn.execute(
                "SELECT day, metric_type, source_name, value, sample_count FROM biometric_daily WHERE profile_id = ?",
                (profile,)))
    incremental = daily()
    biometrics.rebuild_daily(profile)
    assert incremental == daily() and len(incremental) >= 12


def test_averages_use_calendar_days_and_skip_today_in_progress(client):
    """Today's steps so far don't pull the average down, and the 7-day average covers 7 calendar days, not the last
    7 days that happen to have data."""
    from app.store import profiles
    prof = profiles.default_profile()
    samples = [_sample(1, "step_count", 200, day_offset=0),                            # today, so far
               *[_sample(10 + d, "step_count", 10_000, day_offset=d) for d in (1, 2)],
               *[_sample(20 + d, "step_count", 2_000, day_offset=d) for d in (20, 21, 22, 23, 24)]]
    biometrics.insert_samples(prof["id"], None, samples)
    series = biometrics.daily_series(prof["id"], "step_count", days=30)
    assert series["points"][-1]["partial"] and series["stats"]["mean"] == round((2 * 10_000 + 5 * 2_000) / 7, 2)
    (steps,) = biometrics.headline(prof["id"], ["step_count"])
    assert steps["avg_7d"] == 10_000 and steps["latest"]["value"] == 200


def test_overnight_wrist_temperature_counts_toward_the_morning(client):
    from app.store import profiles
    prof = profiles.default_profile()
    morning = datetime.combine(datetime.now(timezone.utc).date() - timedelta(days=1), datetime.min.time(), timezone.utc)
    biometrics.insert_samples(prof["id"], None, [{
        "id": "wt", "metric_type": "sleeping_wrist_temperature", "value": 35.9, "unit": "degC", "source_name": "Apple Watch",
        "start_date": (morning - timedelta(hours=1)).isoformat(), "end_date": (morning + timedelta(hours=7)).isoformat()}])
    pts = biometrics.daily_series(prof["id"], "sleeping_wrist_temperature", days=3)["points"]
    assert [p["day"] for p in pts] == [morning.date().isoformat()]
