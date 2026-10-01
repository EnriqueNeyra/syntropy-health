"""
WHOOP (Developer API v2) connector.

Recovery, cycle (strain) and sleep records are normalized into biometric samples.
WHOOP's HRV is RMSSD and a cycle's kilojoules are *total* energy expenditure, so they
map to ``hrv_rmssd`` and ``total_energy`` respectively.
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from app.connectors import wearable_common as wc
from app.connectors.smart import SmartError, USER_AGENT
from app.core import settings

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE = "https://api.prod.whoop.com/developer/v2"
# Only what fetch_live reads, so a leaked token exposes as little as possible (no profile or body measurements).
SCOPES = "offline read:recovery read:cycles read:workout read:sleep"
SOURCE = "WHOOP"
ENDPOINTS = {"recovery": "recovery", "cycle": "cycle", "sleep": "activity/sleep", "workout": "activity/workout"}


def authorization_url(state: str) -> str:
    creds = settings.wearable_credentials("whoop")
    if not creds["client_id"]:
        raise SmartError("WHOOP is not configured. Add the WHOOP client ID in Settings → Wearables.", "error")
    return f"{AUTH_URL}?{urlencode({'response_type': 'code', 'client_id': creds['client_id'], 'redirect_uri': wc.redirect_uri(), 'scope': SCOPES, 'state': state})}"


async def exchange_code(code: str) -> dict[str, Any]:
    return await wc.exchange_code_local(TOKEN_URL, "whoop", code, wc.redirect_uri())


async def refresh(credentials: dict[str, Any]) -> dict[str, Any]:
    return await wc.refresh_tokens(TOKEN_URL, "whoop", credentials)


async def _fetch(client: httpx.AsyncClient, token: str, path: str, since: date, until: date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    next_token: Optional[str] = None
    params = {"start": f"{since.isoformat()}T00:00:00.000Z", "end": f"{(until + timedelta(days=1)).isoformat()}T00:00:00.000Z",
              "limit": "25"}
    for _ in range(100):
        q = dict(params)
        if next_token:
            q["nextToken"] = next_token
        resp = await client.get(f"{API_BASE}/{path}", params=q, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 401:
            raise SmartError("WHOOP rejected the access token.", "auth")
        if resp.status_code in (403, 404):
            return out
        resp.raise_for_status()
        body = resp.json()
        out.extend(body.get("records") or [])
        next_token = body.get("next_token")
        if not next_token:
            break
    return out


async def fetch_live(credentials: dict[str, Any], since: date, until: date) -> dict[str, list[dict[str, Any]]]:
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": USER_AGENT}) as client:
        return {key: await _fetch(client, credentials["access_token"], path, since, until) for key, path in ENDPOINTS.items()}


def _sample(sid: str, metric: str, value: float, unit: str, start: str, end: Optional[str] = None,
            meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {"id": sid, "metric_type": metric, "hk_identifier": f"WHOOP.{metric}", "value": float(value), "unit": unit,
            "start_date": start, "end_date": end or start, "source_name": SOURCE, "device_name": "WHOOP",
            "metadata": meta}


def normalize(data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cycles = {c.get("id"): c for c in data.get("cycle", [])}
    for r in data.get("recovery", []):
        score = r.get("score") or {}
        if r.get("score_state") not in (None, "SCORED"):
            continue
        cycle = cycles.get(r.get("cycle_id")) or {}
        ts = cycle.get("start") or r.get("created_at")
        key = f"whoop-rec-{r.get('cycle_id')}"
        for field, metric, unit in (("recovery_score", "recovery_score", "score"),
                                    ("resting_heart_rate", "resting_heart_rate", "count/min"),
                                    ("hrv_rmssd_milli", "hrv_rmssd", "ms"),
                                    ("spo2_percentage", "oxygen_saturation", "%")):
            if score.get(field) is not None:
                out.append(_sample(f"{key}-{metric}", metric, score[field], unit, ts))
    for c in data.get("cycle", []):
        score = c.get("score") or {}
        if c.get("score_state") not in (None, "SCORED") or not c.get("start"):
            continue
        end = c.get("end") or c["start"]
        if score.get("strain") is not None:
            out.append(_sample(f"whoop-cyc-{c['id']}-strain", "strain_score", score["strain"], "score", c["start"], end))
        if score.get("kilojoule") is not None:
            out.append(_sample(f"whoop-cyc-{c['id']}-energy", "total_energy", round(score["kilojoule"] / 4.184, 1), "kcal", c["start"], end))
        if score.get("average_heart_rate") is not None:
            out.append(_sample(f"whoop-cyc-{c['id']}-hr", "heart_rate", score["average_heart_rate"], "count/min", c["start"], end))
    for s in data.get("sleep", []):
        score = s.get("score") or {}
        if s.get("nap") or s.get("score_state") not in (None, "SCORED") or not s.get("start"):
            continue
        stages = score.get("stage_summary") or {}
        start, end = s["start"], s.get("end") or s["start"]
        in_bed = stages.get("total_in_bed_time_milli")
        awake = stages.get("total_awake_time_milli") or 0
        key = f"whoop-slp-{s['id']}"
        if in_bed is not None:
            out.append(_sample(f"{key}-total", "sleep_total_duration", (in_bed - awake) / 1000, "s", start, end))
        for field, metric in (("total_slow_wave_sleep_time_milli", "sleep_deep"), ("total_rem_sleep_time_milli", "sleep_rem"),
                              ("total_light_sleep_time_milli", "sleep_light"), ("total_awake_time_milli", "sleep_awake")):
            if stages.get(field) is not None:
                out.append(_sample(f"{key}-{metric}", metric, stages[field] / 1000, "s", start, end))
        if score.get("sleep_performance_percentage") is not None:
            out.append(_sample(f"{key}-perf", "sleep_score", score["sleep_performance_percentage"], "score", end, end,
                               meta={"kind": "sleep_performance"}))
        if score.get("respiratory_rate") is not None:
            out.append(_sample(f"{key}-resp", "respiratory_rate", score["respiratory_rate"], "count/min", start, end))
    return out


def normalize_workouts(data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for w in data.get("workout", []):
        start, end = w.get("start"), w.get("end")
        if not w.get("id") or not start or not end or w.get("score_state") not in (None, "SCORED"):
            continue
        score = w.get("score") or {}
        try:
            duration = (datetime.fromisoformat(end.replace("Z", "+00:00"))
                        - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            duration = None
        sport = w.get("sport_name") or "activity"
        kj = score.get("kilojoule")
        meta = {k: v for k, v in (("strain", score.get("strain")), ("percent_recorded", score.get("percent_recorded")),
                                  ("zone_durations", score.get("zone_durations"))) if v is not None}
        out.append({"id": f"whoop-wo-{w['id']}", "activity_type": sport,
                    "name": sport.replace("-", " ").replace("_", " ").title(), "start": start, "end": end,
                    "duration_s": duration, "total_energy_kcal": round(kj / 4.184, 1) if kj is not None else None,
                    "distance_m": score.get("distance_meter"), "avg_hr": score.get("average_heart_rate"),
                    "max_hr": score.get("max_heart_rate"), "elevation_ascent_m": score.get("altitude_gain_meter"),
                    "source_name": SOURCE, "device_name": "WHOOP", "metadata": meta or None})
    return out


def simulate(days: int = 90, until: Optional[date] = None, seed: str = "whoop") -> dict[str, list[dict[str, Any]]]:
    until = until or datetime.now(timezone.utc).date()
    rng = random.Random(seed)
    out: dict[str, list[dict[str, Any]]] = {"recovery": [], "cycle": [], "sleep": [], "workout": []}
    for i in range(days):
        d = until - timedelta(days=days - 1 - i)
        cid = 900000 + (d - date(2020, 1, 1)).days
        load = max(0.0, math.sin(i / 17 * 2 * math.pi)) + rng.uniform(0, 0.4)
        strain = round(min(20.5, max(4.0, 11.5 + load * 4 + rng.gauss(0, 1.8))), 1)
        hrv = round(max(25, 68 - load * 16 + rng.gauss(0, 5)), 1)
        rhr = round(52 + load * 5 + rng.gauss(0, 1.3))
        recovery = int(min(99, max(8, 70 + (hrv - 60) * 1.4 - load * 10 + rng.gauss(0, 6))))
        sleep_ms = int((7.1 + rng.gauss(0, 0.6) - load * 0.3) * 3600 * 1000)
        awake_ms = int(max(300_000, rng.gauss(1_800_000, 600_000)))
        wake = datetime(d.year, d.month, d.day, 13, 45, tzinfo=timezone.utc)
        bed = wake - timedelta(milliseconds=sleep_ms + awake_ms)
        cycle_end = wake + timedelta(hours=16)
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        out["cycle"].append({"id": cid, "start": wake.strftime(fmt), "end": cycle_end.strftime(fmt), "score_state": "SCORED",
                             "score": {"strain": strain, "kilojoule": round(8400 + strain * 310 + rng.gauss(0, 250), 1),
                                       "average_heart_rate": int(66 + strain + rng.gauss(0, 2)), "max_heart_rate": int(150 + strain * 2)}})
        out["recovery"].append({"cycle_id": cid, "sleep_id": f"sim-{cid}", "created_at": wake.strftime(fmt), "score_state": "SCORED",
                                "score": {"recovery_score": recovery, "resting_heart_rate": rhr, "hrv_rmssd_milli": hrv,
                                          "spo2_percentage": round(96.5 + rng.gauss(0, 0.8), 1),
                                          "skin_temp_celsius": round(33.4 + rng.gauss(0, 0.3), 2)}})
        out["sleep"].append({"id": f"sim-{cid}", "start": bed.strftime(fmt), "end": wake.strftime(fmt), "nap": False, "score_state": "SCORED",
                             "score": {"stage_summary": {"total_in_bed_time_milli": sleep_ms + awake_ms, "total_awake_time_milli": awake_ms,
                                                         "total_light_sleep_time_milli": int(sleep_ms * 0.52),
                                                         "total_slow_wave_sleep_time_milli": int(sleep_ms * 0.23),
                                                         "total_rem_sleep_time_milli": int(sleep_ms * 0.25)},
                                       "sleep_performance_percentage": int(min(100, max(45, sleep_ms / 3600000 / 8 * 100 + rng.gauss(0, 4)))),
                                       "respiratory_rate": round(15.1 + rng.gauss(0, 0.5), 1)}})
        wr = random.Random(f"{seed}-{d.isoformat()}-workout")   # per day, so re-syncs yield the same workouts
        sport, speed = wr.choice([("running", 2.9), ("cycling", 6.5), ("weightlifting", 0.0), ("functional-fitness", 0.0)])
        minutes = wr.randint(30, 80)
        w_start = wake + timedelta(hours=wr.uniform(2, 9))
        if wr.random() < 0.5 and w_start + timedelta(minutes=minutes) < datetime.now(timezone.utc):
            avg_hr = int(125 + load * 12 + wr.gauss(0, 6))
            out["workout"].append({"id": f"sim-wo-{cid}", "sport_name": sport, "score_state": "SCORED",
                                   "start": w_start.strftime(fmt), "end": (w_start + timedelta(minutes=minutes)).strftime(fmt),
                                   "score": {"strain": round(min(19.5, 6 + minutes / 10 + load * 3), 1),
                                             "average_heart_rate": avg_hr, "max_heart_rate": avg_hr + wr.randint(15, 40),
                                             "kilojoule": round(minutes * wr.uniform(28, 45), 1), "percent_recorded": 100,
                                             "distance_meter": round(minutes * 60 * speed, 1) if speed else None,
                                             "altitude_gain_meter": round(wr.uniform(5, 120), 1) if speed else None}})
    return out
