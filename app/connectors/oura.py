"""
Oura Ring (API v2) connector.

Live mode uses OAuth 2.0 (authorization code) — the client secret lives either on the
Syntropy relay worker or in this instance's settings. Simulated mode generates a
realistic, deterministic 90-day history offline.
"""

from __future__ import annotations

import asyncio
import math
import random
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from app.connectors import wearable_common as wc
from app.connectors.smart import SmartError, USER_AGENT
from app.core import settings

AUTH_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
API_BASE = "https://api.ouraring.com/v2/usercollection"
# Only what fetch_live reads, so a leaked token exposes as little as possible (no email or profile).
SCOPES = "daily heartrate workout spo2"
SOURCE = "Oura Ring"
DAILY_ENDPOINTS = ["daily_sleep", "sleep", "daily_readiness", "daily_activity", "daily_spo2", "workout"]


def authorization_url(state: str) -> str:
    creds = settings.wearable_credentials("oura")
    if not creds["client_id"]:
        raise SmartError("Oura is not configured. Add the Oura client ID in Settings → Wearables.", "error")
    return f"{AUTH_URL}?{urlencode({'response_type': 'code', 'client_id': creds['client_id'], 'redirect_uri': wc.redirect_uri(), 'scope': SCOPES, 'state': state})}"


async def exchange_code(code: str) -> dict[str, Any]:
    return await wc.exchange_code_local(TOKEN_URL, "oura", code, wc.redirect_uri())


async def refresh(credentials: dict[str, Any]) -> dict[str, Any]:
    return await wc.refresh_tokens(TOKEN_URL, "oura", credentials)


async def _fetch(client: httpx.AsyncClient, token: str, endpoint: str, params: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    next_token: Optional[str] = None
    for _ in range(50):
        q = dict(params)
        if next_token:
            q["next_token"] = next_token
        resp = await client.get(f"{API_BASE}/{endpoint}", params=q, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 401:
            raise SmartError("Oura rejected the access token.", "auth")
        if resp.status_code in (403, 404):
            return out          # scope not granted / endpoint unavailable for this ring
        resp.raise_for_status()
        body = resp.json()
        out.extend(body.get("data") or [])
        next_token = body.get("next_token")
        if not next_token:
            break
    return out


async def fetch_live(credentials: dict[str, Any], since: date, until: date) -> dict[str, list[dict[str, Any]]]:
    token = credentials["access_token"]
    day_params = {"start_date": since.isoformat(), "end_date": (until + timedelta(days=1)).isoformat()}
    hr_since = max(since, until - timedelta(days=7))  # intraday heart rate is voluminous; keep a week
    hr_params = {"start_datetime": f"{hr_since.isoformat()}T00:00:00+00:00",
                 "end_datetime": f"{(until + timedelta(days=1)).isoformat()}T00:00:00+00:00"}
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": USER_AGENT}) as client:
        results = await asyncio.gather(
            *[_fetch(client, token, ep, day_params) for ep in DAILY_ENDPOINTS],
            _fetch(client, token, "heartrate", hr_params),
            return_exceptions=True,
        )
    data: dict[str, list[dict[str, Any]]] = {}
    for ep, res in zip(DAILY_ENDPOINTS + ["heartrate"], results):
        if isinstance(res, SmartError):
            raise res
        data[ep] = res if isinstance(res, list) else []
    return data


def _sample(sid: str, metric: str, value: float, unit: str, start: str, end: Optional[str] = None,
            day: Optional[str] = None, hk: Optional[str] = None, meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    metadata = dict(meta or {})
    if day:
        metadata["day"] = day
    return {"id": sid, "metric_type": metric, "hk_identifier": hk or f"Oura.{metric}", "value": float(value),
            "unit": unit, "start_date": start, "end_date": end or start, "source_name": SOURCE,
            "device_name": "Oura Ring", "metadata": metadata or None}


def normalize(data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in data.get("daily_sleep", []):
        if r.get("score") is not None:
            out.append(_sample(f"oura-{r['id']}-sleep-score", "sleep_score", r["score"], "score",
                               r.get("timestamp") or f"{r['day']}T12:00:00Z", day=r["day"],
                               meta={"contributors": r.get("contributors")}))
    for r in data.get("sleep", []):
        if r.get("type") not in (None, "long_sleep", "sleep"):
            continue  # naps and rest periods are not counted as the night's sleep
        start, end = r.get("bedtime_start"), r.get("bedtime_end")
        if not start or not end:
            continue
        day = r.get("day")
        for field, metric in (("total_sleep_duration", "sleep_total_duration"), ("deep_sleep_duration", "sleep_deep"),
                              ("rem_sleep_duration", "sleep_rem"), ("light_sleep_duration", "sleep_light"),
                              ("awake_time", "sleep_awake")):
            if r.get(field) is not None:
                out.append(_sample(f"oura-{r['id']}-{metric}", metric, r[field], "s", start, end, day=day))
        if r.get("lowest_heart_rate") is not None:
            out.append(_sample(f"oura-{r['id']}-rhr", "resting_heart_rate", r["lowest_heart_rate"], "count/min",
                               start, end, day=day, hk="HKQuantityTypeIdentifierRestingHeartRate"))
        if r.get("average_hrv") is not None:
            out.append(_sample(f"oura-{r['id']}-hrv", "hrv_rmssd", r["average_hrv"], "ms", start, end, day=day))
        if r.get("average_breath") is not None:
            out.append(_sample(f"oura-{r['id']}-resp", "respiratory_rate", r["average_breath"], "count/min", start, end, day=day))
    for r in data.get("daily_readiness", []):
        ts = r.get("timestamp") or f"{r['day']}T12:00:00Z"
        if r.get("score") is not None:
            out.append(_sample(f"oura-{r['id']}-readiness", "readiness_score", r["score"], "score", ts, day=r["day"],
                               meta={"contributors": r.get("contributors")}))
        if r.get("temperature_deviation") is not None:
            out.append(_sample(f"oura-{r['id']}-temp", "body_temperature_deviation", r["temperature_deviation"], "degC", ts, day=r["day"]))
    for r in data.get("daily_activity", []):
        ts = r.get("timestamp") or f"{r['day']}T12:00:00Z"
        for field, metric, unit in (("steps", "step_count", "count"), ("active_calories", "active_energy", "kcal"),
                                    ("total_calories", "total_energy", "kcal"), ("score", "activity_score", "score")):
            if r.get(field) is not None:
                out.append(_sample(f"oura-{r['id']}-{metric}", metric, r[field], unit, ts, day=r["day"]))
    for r in data.get("daily_spo2", []):
        avg = (r.get("spo2_percentage") or {}).get("average")
        if avg:
            out.append(_sample(f"oura-{r['id']}-spo2", "oxygen_saturation", avg, "%", f"{r['day']}T06:00:00Z", day=r["day"]))
    for r in data.get("heartrate", []):
        if r.get("bpm") is not None and r.get("timestamp"):
            out.append(_sample(f"oura-hr-{r['timestamp']}", "heart_rate", r["bpm"], "count/min", r["timestamp"],
                               meta={"context": r.get("source")}))
    return out


def normalize_workouts(data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for w in data.get("workout", []):
        start, end = w.get("start_datetime"), w.get("end_datetime")
        if not w.get("id") or not start or not end:
            continue
        try:
            duration = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
        except ValueError:
            duration = None
        activity = w.get("activity") or "workout"
        meta = {k: w.get(k) for k in ("intensity", "source", "day") if w.get(k) is not None}
        out.append({"id": f"oura-wo-{w['id']}", "activity_type": activity,
                    "name": w.get("label") or activity.replace("_", " ").title(), "start": start, "end": end,
                    "duration_s": duration, "active_energy_kcal": w.get("calories"), "distance_m": w.get("distance"),
                    "source_name": SOURCE, "device_name": "Oura Ring", "metadata": meta or None})
    return out


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def simulate(days: int = 90, until: Optional[date] = None, seed: str = "oura") -> dict[str, list[dict[str, Any]]]:
    until = until or datetime.now(timezone.utc).date()
    rng = random.Random(seed)
    data: dict[str, list[dict[str, Any]]] = {k: [] for k in DAILY_ENDPOINTS + ["heartrate"]}
    for i in range(days):
        d = until - timedelta(days=days - 1 - i)
        ds = d.isoformat()
        weekly = math.sin(i / 7 * 2 * math.pi)
        fatigue = max(0.0, math.sin(i / 23 * 2 * math.pi)) * 0.6 + rng.uniform(0, 0.35)
        total_sleep = int(7.3 * 3600 + weekly * 1500 - fatigue * 1800 + rng.gauss(0, 900))
        hrv = round(52 - fatigue * 14 + rng.gauss(0, 3), 1)
        rhr = round(53 + fatigue * 6 + rng.gauss(0, 1.2))
        awake = int(max(300, rng.gauss(1500, 500)))
        deep = int(total_sleep * rng.uniform(0.15, 0.22))
        rem = int(total_sleep * rng.uniform(0.19, 0.25))
        bed = datetime(d.year, d.month, d.day, 6, 30, tzinfo=timezone.utc) - timedelta(seconds=total_sleep + awake)
        sid = f"sim-{seed}-{ds}"
        data["sleep"].append({
            "id": f"{sid}-sleep", "day": ds, "type": "long_sleep",
            "bedtime_start": bed.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "bedtime_end": (bed + timedelta(seconds=total_sleep + awake)).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "total_sleep_duration": total_sleep, "deep_sleep_duration": deep, "rem_sleep_duration": rem,
            "light_sleep_duration": total_sleep - deep - rem, "awake_time": awake,
            "lowest_heart_rate": rhr, "average_hrv": hrv, "average_breath": round(14.2 + rng.gauss(0, 0.4), 1),
        })
        sleep_score = int(min(98, max(55, 80 + (total_sleep - 7 * 3600) / 360 - fatigue * 8 + rng.gauss(0, 3))))
        data["daily_sleep"].append({"id": f"{sid}-ds", "day": ds, "score": sleep_score, "timestamp": f"{ds}T00:00:00+00:00"})
        readiness = int(min(97, max(50, 84 - fatigue * 22 + (hrv - 48) * 0.6 + rng.gauss(0, 3))))
        data["daily_readiness"].append({"id": f"{sid}-dr", "day": ds, "score": readiness,
                                        "temperature_deviation": round(rng.gauss(0, 0.18) + fatigue * 0.2, 2),
                                        "timestamp": f"{ds}T00:00:00+00:00"})
        steps = int(max(1500, 8200 + weekly * 2200 + rng.gauss(0, 1800)))
        data["daily_activity"].append({"id": f"{sid}-da", "day": ds, "steps": steps,
                                       "active_calories": int(steps * 0.045 + rng.gauss(0, 40)),
                                       "total_calories": int(2250 + steps * 0.045 + rng.gauss(0, 60)),
                                       "score": int(min(99, max(40, 70 + steps / 600 + rng.gauss(0, 4)))),
                                       "timestamp": f"{ds}T00:00:00+00:00"})
        data["daily_spo2"].append({"id": f"{sid}-spo2", "day": ds, "spo2_percentage": {"average": round(96.8 + rng.gauss(0, 0.6), 1)}})
        wr = random.Random(f"{seed}-{ds}-workout")   # per day, so a re-sync of recent days yields the same workouts
        activity, speed = wr.choice([("walking", 1.4), ("running", 2.9), ("cycling", 6.0), ("strength_training", 0.0)])
        minutes = wr.randint(25, 70)
        start = datetime(d.year, d.month, d.day, 23, 0, tzinfo=timezone.utc) - timedelta(minutes=wr.randint(0, 600))
        if wr.random() < 0.45 and start + timedelta(minutes=minutes) < datetime.now(timezone.utc):
            data["workout"].append({
                "id": f"{sid}-wo", "day": ds, "activity": activity, "intensity": wr.choice(["easy", "moderate", "hard"]),
                "calories": round(minutes * wr.uniform(5, 11), 1), "distance": round(minutes * 60 * speed) or None,
                "source": wr.choice(["autodetected", "confirmed", "manual"]), "label": None,
                "start_datetime": start.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                "end_datetime": (start + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            })
    for h in range(0, 24 * 3):   # three days of hourly heart rate for the intraday chart
        t = datetime(until.year, until.month, until.day, tzinfo=timezone.utc) - timedelta(hours=72 - h)
        base = 58 if t.hour < 7 else 72
        data["heartrate"].append({"bpm": int(base + rng.gauss(0, 5)), "source": "awake" if t.hour >= 7 else "rest",
                                  "timestamp": t.strftime("%Y-%m-%dT%H:%M:%S+00:00")})
    return data
