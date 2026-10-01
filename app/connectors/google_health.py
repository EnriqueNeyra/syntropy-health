"""
Google Health API (v4) connector — Fitbit trackers, Pixel Watch and apps that write to Google Health.

The Google Health API replaced the Fitbit Web API (turned down in September 2026) and is the cloud route to
Google's health data; Health Connect, the other Google store, lives only on an Android phone. It uses Google
OAuth 2.0 with a client secret, so like Oura and WHOOP the secret is held by the relay worker or by this
instance (your own Google Cloud OAuth client). All ``googlehealth.*`` scopes are *restricted*: a public app
needs Google's security review, while a Google Cloud project in "Testing" works for up to 100 named test users.

Daily totals (steps, distance, energy, floors) come from ``dataPoints:dailyRollUp``, which reconciles every
source Google holds without double counting. Daily summaries, measurements, sleep sessions and exercises come
from ``dataPoints`` lists with filters (``{data_type}.date``, ``sample_time.physical_time``, …). Fitbit's
daily HRV is RMSSD, and exercise calories are total energy.
"""

from __future__ import annotations

import math
import random
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from app.connectors import wearable_common as wc
from app.connectors.smart import SmartError, USER_AGENT
from app.core import settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://health.googleapis.com/v4/users/me/dataTypes"
_SCOPE = "https://www.googleapis.com/auth/googlehealth."
# Read-only, and only the bundles fetch_live reads (no profile, location or ECG).
SCOPES = " ".join(_SCOPE + s for s in ("activity_and_fitness.readonly", "health_metrics_and_measurements.readonly",
                                        "sleep.readonly"))
SOURCE = "Google Health"

# Daily rollups: data type -> (rollup value field, sum field, metric, unit, scale, max days per request)
ROLLUPS: dict[str, tuple[str, str, str, str, float, int]] = {
    "steps": ("steps", "countSum", "step_count", "count", 1, 90),
    "distance": ("distance", "millimetersSum", "distance_walking_running", "m", 0.001, 90),
    "active-energy-burned": ("activeEnergyBurned", "kcalSum", "active_energy", "kcal", 1, 90),
    "total-calories": ("totalCalories", "kcalSum", "total_energy", "kcal", 1, 14),
    "floors": ("floors", "countSum", "flights_climbed", "count", 1, 90),
}
# Listed data types: data type -> filter kind (see https://developers.google.com/health/filters)
LISTS: dict[str, str] = {
    "daily-resting-heart-rate": "daily", "daily-heart-rate-variability": "daily", "daily-oxygen-saturation": "daily",
    "daily-respiratory-rate": "daily", "daily-vo2-max": "daily", "daily-sleep-temperature-derivations": "daily",
    "weight": "sample", "body-fat": "sample", "blood-glucose": "sample", "sleep": "sleep", "exercise": "exercise",
}
PAGE_SIZE = {"sleep": 25, "exercise": 25}    # the API's maximum for sessions; other types allow 10,000
MAX_PAGES = 40


def authorization_url(state: str) -> str:
    creds = settings.wearable_credentials("google")
    if not creds["client_id"]:
        raise SmartError("Google Health isn't set up yet. Add your Google Cloud OAuth client ID and secret in "
                         "Settings → Wearables.", "error")
    return f"{AUTH_URL}?" + urlencode({
        "response_type": "code", "client_id": creds["client_id"], "redirect_uri": wc.redirect_uri(), "scope": SCOPES,
        "state": state, "access_type": "offline", "prompt": "consent",   # a refresh token on every consent
    })


async def exchange_code(code: str) -> dict[str, Any]:
    return await wc.exchange_code_local(TOKEN_URL, "google", code, wc.redirect_uri())


async def refresh(credentials: dict[str, Any]) -> dict[str, Any]:
    # Google omits refresh_token when refreshing; token_credentials keeps the one we have.
    return await wc.refresh_tokens(TOKEN_URL, "google", credentials)


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _filter(dtype: str, kind: str, since: date) -> str:
    name = dtype.replace("-", "_")
    if kind == "daily":
        return f'{name}.date >= "{since.isoformat()}"'
    if kind == "sample":
        return f'{name}.sample_time.physical_time >= "{since.isoformat()}T00:00:00Z"'
    if kind == "sleep":
        return f'sleep.interval.civil_end_time >= "{since.isoformat()}"'
    return f'{name}.interval.civil_start_time >= "{since.isoformat()}"'


async def _request(client: httpx.AsyncClient, method: str, url: str, token: str, **kwargs: Any) -> Optional[dict[str, Any]]:
    resp = await client.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
    if resp.status_code == 401:
        raise SmartError("Google rejected the access token.", "auth")
    if resp.status_code in (403, 404):
        return None          # scope not granted, or no such data for this account
    resp.raise_for_status()
    return resp.json()


async def _list(client: httpx.AsyncClient, token: str, dtype: str, kind: str, since: date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    params = {"filter": _filter(dtype, kind, since), "pageSize": str(PAGE_SIZE.get(dtype, 10000))}
    for _ in range(MAX_PAGES):
        body = await _request(client, "GET", f"{API_BASE}/{dtype}/dataPoints", token, params=params)
        if body is None:
            break
        out.extend(body.get("dataPoints") or [])
        if not body.get("nextPageToken"):
            break
        params["pageToken"] = body["nextPageToken"]
    return out


def _civil(d: date) -> dict[str, Any]:
    return {"date": {"year": d.year, "month": d.month, "day": d.day}}


async def _rollup(client: httpx.AsyncClient, token: str, dtype: str, since: date, until: date, chunk: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    start = since
    while start <= until:
        end = min(until + timedelta(days=1), start + timedelta(days=chunk))
        body = await _request(client, "POST", f"{API_BASE}/{dtype}/dataPoints:dailyRollUp", token,
                              json={"range": {"start": _civil(start), "end": _civil(end)}, "windowSizeDays": 1,
                                    "pageSize": 10000})
        if body is None:
            break
        out.extend(body.get("rollupDataPoints") or [])
        start = end
    return out


async def fetch_live(credentials: dict[str, Any], since: date, until: date) -> dict[str, list[dict[str, Any]]]:
    token = credentials["access_token"]
    data: dict[str, list[dict[str, Any]]] = {}
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": USER_AGENT}) as client:
        for dtype, spec in ROLLUPS.items():
            data[f"rollup:{dtype}"] = await _rollup(client, token, dtype, since, until, spec[5])
        for dtype, kind in LISTS.items():
            data[dtype] = await _list(client, token, dtype, kind, since)
    return data


# ---------------------------------------------------------------------------
# Normalizing
# ---------------------------------------------------------------------------

def _day(d: Optional[dict[str, Any]]) -> Optional[str]:
    try:
        return date(int(d["year"]), int(d["month"]), int(d["day"])).isoformat() if d else None
    except (KeyError, TypeError, ValueError):
        return None


def _seconds(duration: Any) -> Optional[float]:
    """Protobuf durations arrive as strings such as "3600s" or "1.5s"."""
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)s", str(duration or ""))
    return float(m.group(1)) if m else None


def _num(value: Any) -> Optional[float]:
    try:
        f = float(value)      # int64 fields are JSON strings
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _point_id(point: dict[str, Any], fallback: str) -> str:
    name = point.get("name") or ""
    return name.rsplit("/", 1)[-1] if "/dataPoints/" in name else fallback


def _device(point: dict[str, Any]) -> str:
    return ((point.get("dataSource") or {}).get("device") or {}).get("displayName") or SOURCE


def _sample(sid: str, metric: str, value: float, unit: str, start: str, end: Optional[str] = None,
            day: Optional[str] = None, device: str = SOURCE, meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    metadata = dict(meta or {})
    if day:
        metadata["day"] = day
    return {"id": sid, "metric_type": metric, "hk_identifier": f"GoogleHealth.{metric}", "value": float(value),
            "unit": unit, "start_date": start, "end_date": end or start, "source_name": SOURCE,
            "device_name": device, "metadata": metadata or None}


# Daily summaries: data type -> (payload field, value field, metric, unit)
DAILY: dict[str, tuple[str, str, str, str]] = {
    "daily-resting-heart-rate": ("dailyRestingHeartRate", "beatsPerMinute", "resting_heart_rate", "count/min"),
    "daily-heart-rate-variability": ("dailyHeartRateVariability", "averageHeartRateVariabilityMilliseconds", "hrv_rmssd", "ms"),
    "daily-oxygen-saturation": ("dailyOxygenSaturation", "averagePercentage", "oxygen_saturation", "%"),
    "daily-respiratory-rate": ("dailyRespiratoryRate", "breathsPerMinute", "respiratory_rate", "count/min"),
    "daily-vo2-max": ("dailyVo2Max", "vo2Max", "vo2_max", "mL/kg·min"),
}
# Measurements: data type -> (payload field, value field, metric, unit, scale)
SAMPLES: dict[str, tuple[str, str, str, str, float]] = {
    "weight": ("weight", "weightGrams", "body_mass", "kg", 0.001),
    "body-fat": ("bodyFat", "percentage", "body_fat_percentage", "%", 1),
    "blood-glucose": ("bloodGlucose", "bloodGlucoseMilligramsPerDeciliter", "blood_glucose", "mg/dL", 1),
}
SLEEP_STAGES = {"DEEP": "sleep_deep", "REM": "sleep_rem", "LIGHT": "sleep_light", "AWAKE": "sleep_awake"}


def normalize(data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for dtype, (field, total, metric, unit, scale, _) in ROLLUPS.items():
        for r in data.get(f"rollup:{dtype}", []):
            day = _day((r.get("civilStartTime") or {}).get("date"))
            value = _num((r.get(field) or {}).get(total))
            if day and value is not None and value > 0:
                out.append(_sample(f"gh-{dtype}-{day}", metric, round(value * scale, 3), unit, f"{day}T12:00:00Z", day=day,
                                   meta={"rollup": "daily"}))
    for dtype, (field, key, metric, unit) in DAILY.items():
        for p in data.get(dtype, []):
            body = p.get(field) or {}
            day, value = _day(body.get("date")), _num(body.get(key))
            if day and value is not None:
                out.append(_sample(f"gh-{dtype}-{day}", metric, value, unit, f"{day}T06:00:00Z", day=day, device=_device(p)))
    for p in data.get("daily-sleep-temperature-derivations", []):
        body = p.get("dailySleepTemperatureDerivations") or {}
        day, nightly, baseline = _day(body.get("date")), _num(body.get("nightlyTemperatureCelsius")), _num(body.get("baselineTemperatureCelsius"))
        if day and nightly is not None and baseline is not None:
            out.append(_sample(f"gh-temp-{day}", "body_temperature_deviation", round(nightly - baseline, 2), "degC",
                               f"{day}T06:00:00Z", day=day, device=_device(p)))
    for dtype, (field, key, metric, unit, scale) in SAMPLES.items():
        for p in data.get(dtype, []):
            body = p.get(field) or {}
            ts, value = (body.get("sampleTime") or {}).get("physicalTime"), _num(body.get(key))
            if ts and value is not None:
                out.append(_sample(f"gh-{dtype}-{_point_id(p, ts)}", metric, round(value * scale, 3), unit, ts, device=_device(p)))
    for p in data.get("sleep", []):
        body = p.get("sleep") or {}
        interval = body.get("interval") or {}
        start, end = interval.get("startTime"), interval.get("endTime")
        if not start or not end or (body.get("metadata") or {}).get("nap"):
            continue      # naps are not the night's sleep
        civil_end = ((interval.get("civilEndTime") or {}).get("date"))
        day = _day(civil_end)
        key = f"gh-sleep-{_point_id(p, start)}"
        summary = body.get("summary") or {}
        asleep = _num(summary.get("minutesAsleep"))
        if asleep is not None:
            out.append(_sample(f"{key}-total", "sleep_total_duration", asleep * 60, "s", start, end, day=day, device=_device(p)))
        for stage in summary.get("stagesSummary") or []:
            metric, minutes = SLEEP_STAGES.get(stage.get("type")), _num(stage.get("minutes"))
            if metric and minutes is not None:
                out.append(_sample(f"{key}-{metric}", metric, minutes * 60, "s", start, end, day=day, device=_device(p)))
    return out


def normalize_workouts(data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in data.get("exercise", []):
        body = p.get("exercise") or {}
        interval = body.get("interval") or {}
        start, end = interval.get("startTime"), interval.get("endTime")
        if not start or not end:
            continue
        m = body.get("metricsSummary") or {}
        kind = (body.get("exerciseType") or "EXERCISE_TYPE_UNSPECIFIED").lower()
        duration = _seconds(body.get("activeDuration"))
        if duration is None:
            try:
                duration = (datetime.fromisoformat(end.replace("Z", "+00:00"))
                            - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
            except ValueError:
                duration = None
        distance, elevation = _num(m.get("distanceMillimeters")), _num(m.get("elevationGainMillimeters"))
        steps, azm = _num(m.get("steps")), _num(m.get("activeZoneMinutes"))
        meta = {k: v for k, v in (("active_zone_minutes", azm), ("run_vo2_max", _num(m.get("runVo2Max"))),
                                  ("recording", (p.get("dataSource") or {}).get("recordingMethod"))) if v is not None}
        name = body.get("displayName") or ("Workout" if kind == "exercise_type_unspecified" else kind.replace("_", " ").title())
        out.append({"id": f"gh-wo-{_point_id(p, start)}", "activity_type": kind, "name": name, "start": start, "end": end,
                    "duration_s": duration, "total_energy_kcal": _num(m.get("caloriesKcal")),   # Fitbit reports total energy
                    "distance_m": round(distance / 1000, 1) if distance is not None else None,
                    "step_count": int(steps) if steps is not None else None,
                    "avg_hr": _num(m.get("averageHeartRateBeatsPerMinute")),
                    "elevation_ascent_m": round(elevation / 1000, 1) if elevation is not None else None,
                    "source_name": SOURCE, "device_name": _device(p), "metadata": meta or None})
    return out


# ---------------------------------------------------------------------------
# Simulated mode: the API's own payload shapes, so normalize() is exercised as in production
# ---------------------------------------------------------------------------

def simulate(days: int = 90, until: Optional[date] = None, seed: str = "google") -> dict[str, list[dict[str, Any]]]:
    until = until or datetime.now(timezone.utc).date()
    rng = random.Random(seed)
    device = {"device": {"formFactor": "WATCH", "manufacturer": "Google", "displayName": "Pixel Watch 3"}}
    out: dict[str, list[dict[str, Any]]] = {f"rollup:{t}": [] for t in ROLLUPS} | {t: [] for t in LISTS}
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    weight = 78.0
    for i in range(days):
        d = until - timedelta(days=days - 1 - i)
        dd = {"year": d.year, "month": d.month, "day": d.day}
        span = {"civilStartTime": {"date": dd}, "civilEndTime": {"date": dd}}
        steps = max(1500, int(rng.gauss(8800, 2600)))
        out["rollup:steps"].append({**span, "steps": {"countSum": str(steps)}})
        out["rollup:distance"].append({**span, "distance": {"millimetersSum": str(int(steps * 760))}})
        out["rollup:active-energy-burned"].append({**span, "activeEnergyBurned": {"kcalSum": round(steps * 0.045 + rng.gauss(120, 40), 1)}})
        out["rollup:total-calories"].append({**span, "totalCalories": {"kcalSum": round(1750 + steps * 0.05 + rng.gauss(0, 60), 1)}})
        out["rollup:floors"].append({**span, "floors": {"countSum": str(max(0, int(rng.gauss(9, 4))))}})
        out["daily-resting-heart-rate"].append({"dataSource": device, "dailyRestingHeartRate": {"date": dd, "beatsPerMinute": str(int(rng.gauss(58, 2)))}})
        out["daily-heart-rate-variability"].append({"dataSource": device, "dailyHeartRateVariability": {
            "date": dd, "averageHeartRateVariabilityMilliseconds": round(rng.gauss(42, 6), 1)}})
        out["daily-oxygen-saturation"].append({"dataSource": device, "dailyOxygenSaturation": {"date": dd, "averagePercentage": round(rng.gauss(96.4, 0.7), 1)}})
        out["daily-respiratory-rate"].append({"dataSource": device, "dailyRespiratoryRate": {"date": dd, "breathsPerMinute": round(rng.gauss(14.6, 0.5), 1)}})
        out["daily-sleep-temperature-derivations"].append({"dataSource": device, "dailySleepTemperatureDerivations": {
            "date": dd, "nightlyTemperatureCelsius": round(34.1 + rng.gauss(0, 0.25), 2), "baselineTemperatureCelsius": 34.1}})
        if i % 7 == 0:
            out["daily-vo2-max"].append({"dataSource": device, "dailyVo2Max": {"date": dd, "vo2Max": round(rng.gauss(44, 1), 1)}})
        wake = datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc)
        asleep = int(rng.gauss(430, 35))
        awake = int(max(10, rng.gauss(35, 10)))
        bed = wake - timedelta(minutes=asleep + awake)
        stages = [("DEEP", 0.18), ("REM", 0.22), ("LIGHT", 0.60)]
        out["sleep"].append({"name": f"users/me/dataTypes/sleep/dataPoints/sim-{d.isoformat()}", "dataSource": device, "sleep": {
            "interval": {"startTime": bed.strftime(fmt), "endTime": wake.strftime(fmt), "civilEndTime": {"date": dd}},
            "type": "STAGES", "metadata": {"nap": False, "processed": True},
            "summary": {"minutesAsleep": str(asleep), "minutesAwake": str(awake),
                        "stagesSummary": [{"type": t, "minutes": str(int(asleep * share))} for t, share in stages]
                                         + [{"type": "AWAKE", "minutes": str(awake)}]}}})
        if i % 3 == 0:
            weight += rng.gauss(-0.05, 0.3)
            out["weight"].append({"dataSource": {"device": {"displayName": "Withings Body+"}}, "weight": {
                "sampleTime": {"physicalTime": (wake + timedelta(minutes=20)).strftime(fmt)}, "weightGrams": round(weight * 1000)}})
        wr = random.Random(f"{seed}-{d.isoformat()}-exercise")   # per day, so re-syncs yield the same workouts
        if wr.random() < 0.45:
            start = wake + timedelta(hours=wr.uniform(2, 9))
            minutes = wr.randint(25, 70)
            if start + timedelta(minutes=minutes) < datetime.now(timezone.utc):
                kind, speed = wr.choice([("RUNNING", 2.8), ("WALKING", 1.4), ("BIKING", 6.0), ("WEIGHTLIFTING", 0.0)])
                out["exercise"].append({"name": f"users/me/dataTypes/exercise/dataPoints/sim-{d.isoformat()}", "dataSource": device,
                                        "exercise": {"interval": {"startTime": start.strftime(fmt),
                                                                  "endTime": (start + timedelta(minutes=minutes)).strftime(fmt)},
                                                     "exerciseType": kind, "activeDuration": f"{minutes * 60}s",
                                                     "metricsSummary": {"caloriesKcal": round(minutes * wr.uniform(7, 11), 1),
                                                                        "distanceMillimeters": minutes * 60 * speed * 1000 if speed else None,
                                                                        "averageHeartRateBeatsPerMinute": str(wr.randint(110, 150)),
                                                                        "activeZoneMinutes": str(wr.randint(10, minutes))}}})
    return out
