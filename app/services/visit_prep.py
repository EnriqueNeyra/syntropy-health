"""
What changed in a person's record since a date: for getting ready for a visit, without AI.

It lists what's on record, the same way every time. It doesn't choose what matters, and it doesn't suggest anything.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.services import insights
from app.store import activity, records

MOST = 200      # a list longer than this says how many it left out
OTHER_KINDS = ("care_plans", "goals", "devices", "observations", "care_team", "coverage")


def _light(r: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "title", "category", "effective_at", "status", "value_text", "value_num", "unit", "interpretation", "ref_text", "source_name")
    return {k: r.get(k) for k in keep}


def _visit(e: dict[str, Any]) -> dict[str, Any]:
    return {**_light(e), "clinician": ((e.get("details") or {}).get("clinicians") or [None])[0]}


def _since(profile_id: str, category: str, since: str) -> list[dict[str, Any]]:
    return [r for r in records.list_records(profile_id, category, since=since, limit=1000)["items"] if r.get("effective_at")]


def _results(profile_id: str, since: str, category: str = "labs") -> list[dict[str, Any]]:
    """Each test with a result since the date: the latest, the last one before the date to set it against, and how many there were."""
    rows = records.list_records(profile_id, category, limit=records.SUMMARY_LABS)["items"]
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
            test["before"] = {"title": r.get("title"), "value_text": r.get("value_text"), "value_num": r.get("value_num"), "unit": r.get("unit"), "effective_at": r.get("effective_at"), "interpretation": r.get("interpretation")}
    return list(tests.values())


def changes_since(profile_id: str, since: str) -> dict[str, Any]:
    day = since[:10]
    timeline = insights.medication_timeline(profile_id)
    meds = records.list_records(profile_id, "medications", limit=500)["items"]
    stopped = [m for m in meds if (m.get("status") or "").lower() in records.ENDED_MED_STATUSES
               and str(m.get("effective_end") or m.get("effective_at") or "")[:10] >= day]
    # As the summary has it: a planned visit counts from today, and anything dated after the next couple of days hasn't happened.
    today, soon = date.today().isoformat(), (date.today() + timedelta(days=2)).isoformat()
    encounters = _since(profile_id, "encounters", since)
    planned = lambda e: (e.get("status") or "").lower() == "planned"  # noqa: E731
    upcoming = [e for e in encounters if str(e["effective_at"]) > soon or (planned(e) and str(e["effective_at"])[:10] >= today)]
    visits = [e for e in encounters if str(e["effective_at"]) <= soon and not planned(e)]
    reports = _since(profile_id, "reports", since)
    imaging = [r for r in reports if (r.get("details") or {}).get("kind") == "imaging"]
    procedures, unfinished = records.procedures_by_finished(_since(profile_id, "procedures", since))
    other = [{**_light(r), "category_label": records.CATEGORY_LABELS[kind]} for kind in OTHER_KINDS for r in _since(profile_id, kind, since)]
    lists = {
        "results": _results(profile_id, since),
        "medications_started": [{"id": m["record_id"], "title": m["name"], "effective_at": m["first"], "status": m["status"]}
                                for m in timeline if m["first"] >= day],
        "medications_stopped": [_light(m) for m in stopped],
        "visits": [_visit(e) for e in visits],
        "upcoming": [_visit(e) for e in sorted(upcoming, key=lambda e: e["effective_at"])],
        "allergies": [_light(a) for a in _since(profile_id, "allergies", since)],
        "vitals": _results(profile_id, since, "vitals"),
        "diagnoses": [_light(c) for c in _since(profile_id, "conditions", since)],
        "notes": [_light(n) for n in _since(profile_id, "notes", since)],
        "procedures": [_light(p) for p in procedures],
        "procedures_unfinished": [_light(p) for p in unfinished],
        "imaging": [_light(r) for r in imaging],
        "reports": [_light(r) for r in reports if (r.get("details") or {}).get("kind") != "imaging"],
        "immunizations": [_light(i) for i in _since(profile_id, "immunizations", since)],
        "other": other,
        "journal": activity.logged_summary(profile_id, start=day),
    }
    return {"since": day, "counts": {k: len(v) for k, v in lists.items()}, "undated": records.undated_counts(profile_id), **{k: v[:MOST] for k, v in lists.items()}}
