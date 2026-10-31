"""
What changed in a person's record since a date: for getting ready for a visit, without AI.

It lists what's on record, the same way every time. It doesn't choose what matters, and it doesn't suggest anything.
"""

from __future__ import annotations

from typing import Any

from app.services import insights
from app.store import activity, records

MOST = 200      # a list longer than this says how many it left out


def _light(r: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "title", "category", "effective_at", "status", "value_text", "interpretation", "ref_text", "source_name")
    return {k: r.get(k) for k in keep}


def _since(profile_id: str, category: str, since: str) -> list[dict[str, Any]]:
    return [r for r in records.list_records(profile_id, category, since=since, limit=1000)["items"] if r.get("effective_at")]


def _results(profile_id: str, since: str) -> list[dict[str, Any]]:
    """Each test with a result since the date: the latest, the last one before the date to set it against, and how many there were."""
    rows = records.list_records(profile_id, "labs", limit=records.SUMMARY_LABS)["items"]
    tests: dict[str, dict[str, Any]] = {}
    day = since[:10]
    for r in rows:
        when = str(r.get("effective_at") or "")[:10]
        if not when:
            continue
        key = records._row_key(r)
        test = tests.get(key)
        if when >= day:
            if test is None:
                tests[key] = {**_light(r), "count": 1, "before": None}
            else:
                test["count"] += 1
        elif test is not None and test["before"] is None:
            test["before"] = {"value_text": r.get("value_text"), "effective_at": r.get("effective_at"), "interpretation": r.get("interpretation")}
    return list(tests.values())


def changes_since(profile_id: str, since: str) -> dict[str, Any]:
    day = since[:10]
    timeline = insights.medication_timeline(profile_id)
    meds = records.list_records(profile_id, "medications", limit=500)["items"]
    stopped = [m for m in meds if (m.get("status") or "").lower() in records.ENDED_MED_STATUSES
               and str(m.get("effective_end") or m.get("effective_at") or "")[:10] >= day]
    visits = [e for e in _since(profile_id, "encounters", since) if (e.get("status") or "").lower() != "planned"]
    imaging = [r for r in _since(profile_id, "reports", since) if (r.get("details") or {}).get("kind") == "imaging"]
    lists = {
        "results": _results(profile_id, since),
        "medications_started": [{"id": m["record_id"], "title": m["name"], "effective_at": m["first"], "status": m["status"]}
                                for m in timeline if m["first"] >= day],
        "medications_stopped": [_light(m) for m in stopped],
        "visits": [{**_light(e), "clinician": ((e.get("details") or {}).get("clinicians") or [None])[0]} for e in visits],
        "diagnoses": [_light(c) for c in _since(profile_id, "conditions", since)],
        "notes": [_light(n) for n in _since(profile_id, "notes", since)],
        "procedures": [_light(p) for p in _since(profile_id, "procedures", since)],
        "imaging": [_light(r) for r in imaging],
        "journal": activity.logged_summary(profile_id, start=day),
    }
    return {"since": day, "counts": {k: len(v) for k, v in lists.items()}, **{k: v[:MOST] for k, v in lists.items()}}
