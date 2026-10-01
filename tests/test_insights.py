"""Sleep night by night, training load and splits, insights across sources, comparing measures, goals, the combined
timeline and saved Ask conversations."""

from datetime import datetime, timedelta, timezone

from app.services import insights
from app.store import activity, biometrics, goals, profiles, sleep, training


def _prof():
    return profiles.default_profile()["id"]


def _night(pid, wake, i, deep=1.0, core=5.0, rem=1.5, awake=0.5, source="Apple Watch"):
    """Stage samples for one night ending at ``wake``: deep, then core, then REM, then awake."""
    t = wake - timedelta(hours=deep + core + rem + awake)
    out = []
    for n, (metric, hours) in enumerate((("sleep_analysis_deep", deep), ("sleep_analysis_core", core),
                                         ("sleep_analysis_rem", rem), ("sleep_analysis_awake", awake))):
        end = t + timedelta(hours=hours)
        out.append({"id": f"n{i}-{n}-{source}", "metric_type": metric, "value": hours * 3600, "unit": "s",
                    "source_name": source, "start_date": t.isoformat(), "end_date": end.isoformat()})
        t = end
    biometrics.insert_samples(pid, None, out)


def test_sleep_nights_have_stages_times_and_consistency(client):
    pid = _prof()
    today = datetime.now(timezone.utc).replace(hour=7, minute=0, second=0, microsecond=0)
    for i in range(10):
        # In bed 23:00 to 07:00 (UTC, the tests' time zone), and 00:30 to 08:30 every third night.
        _night(pid, today - timedelta(days=i) + (timedelta(hours=1.5) if i % 3 == 0 else timedelta(0)), i)
    res = client.get("/api/biometrics/sleep", params={"days": 14}).json()
    nights = res["nights"]
    assert len(nights) == 10
    last = nights[-1]
    assert last["day"] == today.date().isoformat()
    assert last["asleep_h"] == 7.5 and last["stages"]["deep"] == 1.0 and last["stages"]["rem"] == 1.5
    assert last["stages"]["awake"] == 0.5 and last["in_bed_h"] == 8.0 and 0.9 < last["efficiency"] < 0.95
    assert last["segments"][0]["stage"] == "deep" and "segments" not in nights[0]   # only the latest night's hypnogram
    st = res["stats"]
    assert st["asleep_h"] == 7.5 and st["short_nights"] == 0
    assert "23:00" <= st["bedtime"] <= "23:59"          # usually 23:00, pulled later by the 00:30 nights
    assert 30 <= st["bedtime_spread_min"] <= 60
    # The same night, the same hours as the daily figure.
    daily = biometrics.daily_series(pid, "sleep_duration", days=1)["points"][-1]["value"]
    assert daily == last["asleep_h"]


def test_ring_summaries_become_nights(client):
    pid = _prof()
    day = datetime.now(timezone.utc).date().isoformat()
    wake_dt = datetime.now(timezone.utc).replace(hour=7, minute=0, second=0, microsecond=0)
    bed_dt = wake_dt - timedelta(hours=8)
    rows = [("sleep_total_duration", 7 * 3600), ("sleep_deep", 1.5 * 3600), ("sleep_rem", 1.5 * 3600),
            ("sleep_light", 4 * 3600), ("sleep_awake", 3600)]
    biometrics.insert_samples(pid, None, [
        {"id": f"oura-{m}", "metric_type": m, "value": v, "unit": "s", "source_name": "Oura Ring",
         "start_date": bed_dt.isoformat(), "end_date": wake_dt.isoformat(), "metadata": {"day": day}} for m, v in rows])
    n = sleep.nights(pid, 3)["nights"][-1]
    assert n["source"] == "Oura Ring" and n["asleep_h"] == 7.0
    assert n["stages"] == {"deep": 1.5, "core": 4.0, "rem": 1.5, "awake": 1.0, "unstaged": 0.0}
    assert n["in_bed_h"] == 8.0


def _workout(i, name, start, minutes, distance=None, avg_hr=None, hr=None, route=None):
    return {"id": f"w{i}", "name": name, "start": start.isoformat(), "end": (start + timedelta(minutes=minutes)).isoformat(),
            "duration_s": minutes * 60, "distance_m": distance, "avg_hr": avg_hr, "max_hr": (avg_hr or 0) + 20 or None,
            "source_name": "Apple Watch", "heart_rate": hr or [], "route": route or []}


def test_training_load_bests_and_splits(client):
    pid = _prof()
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    ws = [_workout(i, "Running", now - timedelta(days=7 + i * 3), 30, 5000, 140) for i in range(8)]
    ws.append(_workout(99, "Running", now - timedelta(days=1), 90, 21000, 160))     # a big week
    # A route running due north, 200 m a minute (0.0018° of latitude), for 25 minutes: 5 km.
    route = [{"t": (now - timedelta(days=2) + timedelta(minutes=m)).isoformat(), "lat": 40 + m * 0.0018, "lon": -73.0}
             for m in range(0, 26)]
    ws.append(_workout(100, "Running", now - timedelta(days=2), 25, 5000, 150, route=route))
    activity.insert_workouts(pid, None, ws)
    t = client.get("/api/biometrics/training", params={"weeks": 6, "type": "Running"}).json()
    assert len(t["weeks"]) == 6 and t["weeks"][-1]["week"] <= now.date().isoformat()
    assert t["load"]["acute"] > t["load"]["chronic"] > 0 and t["load"]["status"] in ("spike", "building")
    assert t["max_hr"]["bpm"] >= 150
    kinds = {b["kind"]: b for b in t["bests"]}
    assert kinds["distance"]["id"] == "w99" and kinds["speed"]["pace_s_per_km"] > 0
    assert t["progress"] and t["pace"] is True
    d = client.get("/api/biometrics/workouts/w100").json()
    assert d["pace_s_per_km"] == 300 and len(d["zones"]) == 5 and d["load"] > 0
    full = [s for s in d["splits"] if not s.get("partial")]
    assert len(full) == 5 and all(290 <= s["seconds"] <= 310 for s in full)   # 200 m a minute: 5:00 a km
    miles = client.get("/api/biometrics/workouts/w100", params={"split": "mi"}).json()["splits"]
    assert miles[0]["distance_m"] > 1600


def test_zone_minutes_use_per_minute_heart_rate():
    w = {"duration_s": 600, "avg_hr": 120, "heart_rate": [{"avg": 100}] * 5 + [{"avg": 175}] * 5}
    mins = training.zone_minutes(w, 190)
    assert mins[0] == 5 and mins[4] == 5
    assert training.workout_load(w, 190) == 5 * 1 + 5 * 5
    assert training.workout_load({"duration_s": 1200}, 190) == 20 * training.NO_HR_WEIGHT


def _daily(pid, metric, values, source="Apple Watch", unit="count"):
    today = datetime.now(timezone.utc).replace(hour=15, minute=0, second=0, microsecond=0)
    biometrics.insert_samples(pid, None, [
        {"id": f"{metric}-{i}", "metric_type": metric, "value": v, "unit": unit, "source_name": source,
         "start_date": (today - timedelta(days=len(values) - 1 - i)).isoformat(),
         "end_date": (today - timedelta(days=len(values) - 1 - i) + timedelta(minutes=1)).isoformat()}
        for i, v in enumerate(values) if v is not None])


def test_a_change_from_normal_is_an_insight(client):
    pid = _prof()
    # Resting heart rate around 55 for two months, then around 62 this week.
    base = [55 + (i % 3 - 1) for i in range(60)]
    _daily(pid, "resting_heart_rate", base + [62, 63, 61, 62, 64, 62, 63], unit="bpm")
    found = client.get("/api/insights").json()["insights"]
    change = next(i for i in found if i["kind"] == "change")
    assert change["data"]["metric"] == "resting_heart_rate"
    assert change["tone"] == "attention" and change["data"]["recent"] > change["data"]["usual"] + 5
    assert "up from your usual" in change["text"]


def test_compare_pairs_days_and_finds_the_link(client):
    pid = _prof()
    steps = [4000 + 500 * (i % 10) for i in range(40)]
    _daily(pid, "step_count", steps)
    _daily(pid, "active_energy", [s / 20 for s in steps], unit="kcal")
    res = client.get("/api/insights/compare", params={"a": "step_count", "b": "active_energy", "days": 30}).json()
    assert res["n"] >= 28 and res["r"] > 0.95 and res["strength"] == "strong"
    assert res["split"]["above"] > res["split"]["below"]
    lagged = client.get("/api/insights/compare", params={"a": "step_count", "b": "active_energy", "days": 30, "lag": -1}).json()
    assert lagged["lag"] == -1 and lagged["n"] >= 28 and lagged["r"] < res["r"]
    assert client.get("/api/insights/compare", params={"a": "step_count", "b": "nope"}).status_code == 400
    # Check-ins are comparable once there are a few.
    for i in range(6):
        when = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client.post("/api/journal", json={"event_type": "check_in", "category": "stateOfMind", "name": "Check-in",
                                          "start": when, "value": 0.5, "details": {"energy": 4, "stress": 2}})
    keys = {m["key"] for m in client.get("/api/insights/comparable").json()["measures"]}
    assert {"step_count", "journal:mood", "journal:energy"} <= keys
    mood = insights.series(pid, "journal:mood", 10)["values"]
    assert len(mood) == 6 and set(mood.values()) == {0.5}


def test_goals_are_kept_checked_and_shown(client):
    _daily(_prof(), "step_count", [9000, 7000, 12000, 8500, 3000, 10000, 9500, 8000])
    assert client.put("/api/goals", json={"metric": "step_count", "target": 8000}).status_code == 200
    assert client.put("/api/goals", json={"metric": "nope", "target": 1}).status_code == 400
    assert client.put("/api/goals", json={"metric": "step_count", "target": -5}).status_code == 400
    g = client.get("/api/goals").json()
    prog = g["progress"][0]
    assert g["goals"]["step_count"] == {"target": 8000.0, "direction": "min"}
    assert prog["days"] == 7 and prog["met"] == 5 and prog["streak"] == 3 and len(prog["history"]) == 7
    assert any(i["kind"] == "goal" for i in client.get("/api/insights").json()["insights"])
    assert client.put("/api/goals", json={"metric": "step_count", "target": None}).json()["goals"] == {}
    assert goals.met({"target": 60, "direction": "max"}, 58) is True


def test_markers_and_timeline_mix_records_and_life(client):
    pid = _prof()
    when = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    client.post("/api/journal", json={"event_type": "headache", "category": "symptoms", "name": "Headache", "start": when,
                                      "value": 2, "value_label": "Mild"})
    client.post("/api/journal", json={"event_type": "check_in", "category": "stateOfMind", "name": "Check-in", "start": when, "value": 0})
    activity.insert_workouts(pid, None, [_workout(1, "Cycling", datetime.now(timezone.utc) - timedelta(days=2), 40, 15000)])
    items = client.get("/api/timeline").json()["items"]
    assert [i["kind"] for i in items] == ["journal"]                         # check-ins and workouts have filters
    assert {i["kind"] for i in client.get("/api/timeline", params={"categories": "journal,checkins"}).json()["items"]} == {"journal"}
    assert len(client.get("/api/timeline", params={"categories": "journal,checkins"}).json()["items"]) == 2
    assert client.get("/api/timeline", params={"categories": "workouts"}).json()["items"][0]["title"] == "Cycling"
    assert client.get("/api/timeline", params={"categories": "labs"}).json()["items"] == []
    assert client.get("/api/insights/markers").json()["markers"] == []


def test_saved_conversations(client):
    msgs = [{"role": "user", "content": "How did I sleep?"}, {"role": "assistant", "content": "Well.", "steps": ["Sleep"], "by": "X"}]
    assert client.put("/api/ai/chats/c_0123456789ab", json={"title": "How did I sleep?", "messages": msgs}).status_code == 200
    assert client.put("/api/ai/chats/bad", json={"title": "x", "messages": msgs}).status_code == 400
    assert client.put("/api/ai/chats/c_0123456789ac", json={"title": "x", "messages": [{"role": "system", "content": "hi"}]}).status_code == 422
    listed = client.get("/api/ai/chats").json()["chats"]
    assert [c["id"] for c in listed] == ["c_0123456789ab"] and listed[0]["messages"] == 2
    got = client.get("/api/ai/chats/c_0123456789ab").json()
    assert got["messages"][1]["steps"] == ["Sleep"] and got["title"] == "How did I sleep?"
    assert client.delete("/api/ai/chats/c_0123456789ab").status_code == 200
    assert client.get("/api/ai/chats/c_0123456789ab").status_code == 404


def test_new_tools_for_ai_apps(client):
    from app import mcp_server
    _daily(_prof(), "step_count", [5000 + i * 10 for i in range(20)])
    for name in ("get_insights", "get_sleep_nights", "get_training_summary", "get_goals"):
        text, err = mcp_server.call_tool(name, {})
        assert not err, text
    text, err = mcp_server.call_tool("compare_measures", {"a": "step_count", "b": "workouts:minutes", "days": 14})
    assert not err and '"strength"' in text
