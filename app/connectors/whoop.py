"""
WHOOP (Developer API v2) connector.

Recovery, cycle (strain) and sleep records are normalized into biometric samples.
WHOOP's HRV is RMSSD and a cycle's kilojoules are *total* energy expenditure, so they
map to ``hrv_rmssd`` and ``total_energy`` respectively.
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta, timezone, tzinfo
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
ENDPOINT_SCOPES = {"recovery": "read:recovery", "cycle": "read:cycles", "sleep": "read:sleep", "workout": "read:workout"}


def authorization_url(state: str) -> str:
    creds = settings.wearable_credentials("whoop")
    if not creds["client_id"]:
        raise SmartError(wc.not_configured("WHOOP"), "error")
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
        if resp.status_code == 403:
            raise wc.Refused(path.rsplit("/", 1)[-1])
        if resp.status_code == 404:
            return out
        resp.raise_for_status()
        body = resp.json()
        out.extend(body.get("records") or [])
        next_token = body.get("next_token")
        if not next_token:
            return out
    raise wc.too_many_pages("WHOOP", path.rsplit("/", 1)[-1])


async def fetch_live(credentials: dict[str, Any], since: date, until: date) -> dict[str, list[dict[str, Any]]]:
    data: dict[str, Any] = {}
    refused: list[wc.Refused] = []
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": USER_AGENT}) as client:
        for key, path in ENDPOINTS.items():
            try:
                data[key] = await _fetch(client, credentials["access_token"], path, since, until)
            except wc.Refused as exc:
                refused.append(exc)
                data[key] = []
    data["_warnings"] = wc.refusals("WHOOP", refused, len(ENDPOINTS), credentials.get("scope"), ENDPOINT_SCOPES)
    return data


def _sample(sid: str, metric: str, value: float, unit: str, start: str, end: Optional[str] = None,
            meta: Optional[dict[str, Any]] = None, offset: Optional[int] = None) -> dict[str, Any]:
    sample = {"id": sid, "metric_type": metric, "hk_identifier": f"WHOOP.{metric}", "value": float(value), "unit": unit,
              "start_date": start, "end_date": end or start, "source_name": SOURCE, "device_name": "WHOOP",
              "device_kind": "ring", "metadata": meta}
    if offset is not None:
        sample["tz_offset_min"] = offset
    return sample


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _cycle_day_anchor(cycle: dict[str, Any], woke: Optional[str]) -> str:
    """When a cycle's strain and energy count. A WHOOP cycle runs from falling asleep to falling asleep, so its start is
    usually the evening before the day it's about. It counts at the wake-up of its sleep, the same morning as its recovery;
    without one, around the middle of the cycle (one still open is taken to last a day)."""
    if woke:
        return woke
    start = _parse(cycle["start"])
    end = _parse(cycle["end"]) if cycle.get("end") else start + timedelta(hours=24)
    return _stamp(start + (end - start) / 2)


def normalize(data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    sleeps = {s.get("id"): s for s in data.get("sleep", [])}
    night_of_cycle = {s.get("cycle_id"): s for s in data.get("sleep", []) if not s.get("nap") and s.get("end")}
    offsets = {c.get("id"): wc.clock_offset_minutes(c.get("timezone_offset")) for c in data.get("cycle", [])}
    for r in data.get("recovery", []):
        score = r.get("score") or {}
        if r.get("score_state") not in (None, "SCORED"):
            continue
        # The morning of the sleep it scores, so it lands on the same day as that sleep. It's never the cycle's start, which is
        # the evening before; created_at is shortly after waking.
        night = sleeps.get(r.get("sleep_id")) or night_of_cycle.get(r.get("cycle_id")) or {}
        ts = night.get("end") or r.get("created_at")
        if not ts:
            continue
        offset = wc.clock_offset_minutes(night.get("timezone_offset")) or offsets.get(r.get("cycle_id"))
        key = f"whoop-rec-{r.get('cycle_id')}"
        for field, metric, unit in (("recovery_score", "recovery_score", "score"),
                                    ("resting_heart_rate", "resting_heart_rate", "count/min"),
                                    ("hrv_rmssd_milli", "hrv_rmssd", "ms"),
                                    ("spo2_percentage", "oxygen_saturation", "%"),
                                    ("skin_temp_celsius", "skin_temperature", "degC")):
            if score.get(field) is not None:
                out.append(_sample(f"{key}-{metric}", metric, score[field], unit, ts, offset=offset))
    for c in data.get("cycle", []):
        score = c.get("score") or {}
        if c.get("score_state") not in (None, "SCORED") or not c.get("start"):
            continue
        at = _cycle_day_anchor(c, (night_of_cycle.get(c.get("id")) or {}).get("end"))
        offset = offsets.get(c.get("id"))
        if score.get("strain") is not None:
            out.append(_sample(f"whoop-cyc-{c['id']}-strain", "strain_score", score["strain"], "score", at, offset=offset))
        if score.get("kilojoule") is not None:
            out.append(_sample(f"whoop-cyc-{c['id']}-energy", "total_energy", round(score["kilojoule"] / 4.184, 1), "kcal", at, offset=offset))
        if score.get("average_heart_rate") is not None:
            out.append(_sample(f"whoop-cyc-{c['id']}-hr", "heart_rate", score["average_heart_rate"], "count/min", at, offset=offset))
        if c.get("step_count") is not None:
            out.append(_sample(f"whoop-cyc-{c['id']}-steps", "step_count", c["step_count"], "count", at, offset=offset))
    for s in data.get("sleep", []):
        score = s.get("score") or {}
        if s.get("score_state") not in (None, "SCORED") or not s.get("start"):
            continue
        stages = score.get("stage_summary") or {}
        start, end = s["start"], s.get("end") or s["start"]
        offset = wc.clock_offset_minutes(s.get("timezone_offset"))
        in_bed = stages.get("total_in_bed_time_milli")
        awake = stages.get("total_awake_time_milli") or 0
        key = f"whoop-slp-{s['id']}"
        if s.get("nap"):
            if in_bed is not None:      # apart from the night
                out.append(_sample(f"{key}-nap", "sleep_nap", (in_bed - awake) / 1000, "s", start, end, offset=offset))
            continue
        if in_bed is not None:
            out.append(_sample(f"{key}-total", "sleep_total_duration", (in_bed - awake) / 1000, "s", start, end, offset=offset))
        for field, metric in (("total_slow_wave_sleep_time_milli", "sleep_deep"), ("total_rem_sleep_time_milli", "sleep_rem"),
                              ("total_light_sleep_time_milli", "sleep_light"), ("total_awake_time_milli", "sleep_awake")):
            if stages.get(field) is not None:
                out.append(_sample(f"{key}-{metric}", metric, stages[field] / 1000, "s", start, end, offset=offset))
        if score.get("sleep_performance_percentage") is not None:
            out.append(_sample(f"{key}-perf", "sleep_score", score["sleep_performance_percentage"], "score", end, end,
                               meta={"kind": "sleep_performance"}, offset=offset))
        if score.get("respiratory_rate") is not None:
            out.append(_sample(f"{key}-resp", "respiratory_rate", score["respiratory_rate"], "count/min", start, end, offset=offset))
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
                    "source_name": SOURCE, "device_name": "WHOOP", "metadata": meta or None,
                    **({"tz_offset_min": offset} if (offset := wc.clock_offset_minutes(w.get("timezone_offset"))) is not None else {})})
    return out


def simulate(days: int = 90, until: Optional[date] = None, seed: str = "whoop",
             tz: tzinfo = timezone.utc) -> dict[str, list[dict[str, Any]]]:
    """``days`` ending ``until``, on the clock in ``tz``: waking around 6:45, workouts later in the day. A day whose
    sleep hasn't ended yet has no cycle, recovery or sleep, as with a real strap. As WHOOP documents them, a cycle runs from
    falling asleep to next falling asleep, and is still open (no end) until then."""
    now = datetime.now(timezone.utc)
    until = until or now.astimezone(tz).date()
    out: dict[str, list[dict[str, Any]]] = {"recovery": [], "cycle": [], "sleep": [], "workout": []}

    def night(d: date) -> dict[str, Any]:
        # By the date alone, so syncing again over a different span of days gives the same figures for a day.
        rng = random.Random(f"{seed}-{d.isoformat()}")
        load = max(0.0, math.sin(d.toordinal() / 17 * 2 * math.pi)) + rng.uniform(0, 0.4)
        sleep_ms = int((7.1 + rng.gauss(0, 0.6) - load * 0.3) * 3600 * 1000)
        awake_ms = int(max(300_000, rng.gauss(1_800_000, 600_000)))
        wake = wc.simulated_wake(d, 6, 45, tz, seed)
        return {"rng": rng, "load": load, "sleep_ms": sleep_ms, "awake_ms": awake_ms, "wake": wake,
                "bed": wake - timedelta(milliseconds=sleep_ms + awake_ms)}

    for i in range(days):
        d = until - timedelta(days=days - 1 - i)
        cid = 900000 + (d - date(2020, 1, 1)).days
        tonight = night(d)
        rng, load = tonight["rng"], tonight["load"]
        sleep_ms, awake_ms, wake, bed = tonight["sleep_ms"], tonight["awake_ms"], tonight["wake"], tonight["bed"]
        strain = round(min(20.5, max(4.0, 11.5 + load * 4 + rng.gauss(0, 1.8))), 1)
        hrv = round(max(25, 68 - load * 16 + rng.gauss(0, 5)), 1)
        rhr = round(52 + load * 5 + rng.gauss(0, 1.3))
        recovery = int(min(99, max(8, 70 + (hrv - 60) * 1.4 - load * 10 + rng.gauss(0, 6))))
        next_bed = night(d + timedelta(days=1))["bed"]
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        cycle = {"id": cid, "start": bed.strftime(fmt), "end": next_bed.strftime(fmt) if next_bed <= now else None, "score_state": "SCORED",
                 "score": {"strain": strain, "kilojoule": round(8400 + strain * 310 + rng.gauss(0, 250), 1),
                           "average_heart_rate": int(66 + strain + rng.gauss(0, 2)), "max_heart_rate": int(150 + strain * 2)}}
        recovered = {"cycle_id": cid, "sleep_id": f"sim-{cid}", "created_at": wake.strftime(fmt), "score_state": "SCORED",
                     "score": {"recovery_score": recovery, "resting_heart_rate": rhr, "hrv_rmssd_milli": hrv,
                               "spo2_percentage": round(96.5 + rng.gauss(0, 0.8), 1),
                               "skin_temp_celsius": round(33.4 + rng.gauss(0, 0.3), 2)}}
        sleep = {"id": f"sim-{cid}", "cycle_id": cid, "start": bed.strftime(fmt), "end": wake.strftime(fmt), "nap": False, "score_state": "SCORED",
                 "score": {"stage_summary": {"total_in_bed_time_milli": sleep_ms + awake_ms, "total_awake_time_milli": awake_ms,
                                             "total_light_sleep_time_milli": int(sleep_ms * 0.52),
                                             "total_slow_wave_sleep_time_milli": int(sleep_ms * 0.23),
                                             "total_rem_sleep_time_milli": int(sleep_ms * 0.25)},
                           "sleep_performance_percentage": int(min(100, max(45, sleep_ms / 3600000 / 8 * 100 + rng.gauss(0, 4)))),
                           "respiratory_rate": round(15.1 + rng.gauss(0, 0.5), 1)}}
        if wake <= now:
            out["cycle"].append(cycle)
            out["recovery"].append(recovered)
            out["sleep"].append(sleep)
        wr = random.Random(f"{seed}-{d.isoformat()}-workout")   # per day, so re-syncs yield the same workouts
        sport, speed = wr.choice([("running", 2.9), ("cycling", 6.5), ("weightlifting", 0.0), ("functional-fitness", 0.0)])
        minutes = wr.randint(30, 80)
        w_start = wake + timedelta(hours=wr.uniform(2, 9))
        if wr.random() < 0.5 and w_start + timedelta(minutes=minutes) < now:
            avg_hr = int(125 + load * 12 + wr.gauss(0, 6))
            out["workout"].append({"id": f"sim-wo-{cid}", "sport_name": sport, "score_state": "SCORED",
                                   "start": w_start.strftime(fmt), "end": (w_start + timedelta(minutes=minutes)).strftime(fmt),
                                   "score": {"strain": round(min(19.5, 6 + minutes / 10 + load * 3), 1),
                                             "average_heart_rate": avg_hr, "max_heart_rate": avg_hr + wr.randint(15, 40),
                                             "kilojoule": round(minutes * wr.uniform(28, 45), 1), "percent_recorded": 100,
                                             "distance_meter": round(minutes * 60 * speed, 1) if speed else None,
                                             "altitude_gain_meter": round(wr.uniform(5, 120), 1) if speed else None}})
    return out
