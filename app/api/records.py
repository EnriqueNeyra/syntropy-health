"""Clinical records: summary, lists, timeline, observation trends."""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import check_access, resolve_profile
from app.core import auth, context, settings
from app.store import activity, connections, records

router = APIRouter(prefix="/api", tags=["records"], dependencies=[Depends(auth.require_user)])


@router.get("/summary")
def summary(profile: Optional[str] = None) -> dict:
    prof = resolve_profile(profile)
    return {"profile": prof, **records.summary(prof["id"])}


@router.get("/categories")
def categories(profile: Optional[str] = None) -> dict:
    prof = resolve_profile(profile)
    counts = records.category_counts(prof["id"])
    return {"categories": [{"key": k, "label": records.CATEGORY_LABELS[k], "count": counts.get(k, 0)}
                           for k in records.CATEGORIES]}


@router.get("/records")
def list_records(
    profile: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = Query(None, max_length=200),
    connection: Optional[str] = None,
    code: Optional[str] = None,
    status: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    dedupe: bool = True,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    kind: Optional[str] = None,
    note_type: Optional[str] = None,
    author: Optional[str] = None,
) -> dict:
    prof = resolve_profile(profile)
    return records.list_records(prof["id"], category, q=q, connection_id=connection, code=code, status=status,
                                since=since, until=until, dedupe=dedupe, limit=limit, offset=offset,
                                kind=kind, note_type=note_type, author=author)


@router.get("/record-filters")
def record_filters(profile: Optional[str] = None, category: Optional[str] = None) -> dict:
    return records.record_filters(resolve_profile(profile)["id"], category)


@router.get("/records/{record_id}")
def get_record(record_id: str) -> dict:
    rec = records.get_record(record_id)
    if not rec:
        raise HTTPException(404, "Record not found")
    check_access(rec["profile_id"])
    conn = connections.get(rec["connection_id"])
    rec["editable"] = bool(conn and conn["provider"] == "manual")   # results the person typed in can be deleted
    return {**rec, **records.visit_links(rec)}


def _seen_key(profile_id: str) -> str:
    principal = context.principal()
    return records.seen_key(profile_id, principal.account_id if principal else None)


@router.get("/new-records")
def new_records(profile: Optional[str] = None) -> dict:
    """What arrived since this account last marked this person's records as looked at. The first time, that starts now."""
    prof = resolve_profile(profile, "view")
    mark = settings.get(_seen_key(prof["id"]))
    if mark is None:
        mark = time.time()
        settings.set(_seen_key(prof["id"]), mark)
    return records.new_since(prof["id"], float(mark))


@router.post("/new-records/seen")
def mark_new_records_seen(profile: Optional[str] = None) -> dict:
    prof = resolve_profile(profile, "view")
    settings.set(_seen_key(prof["id"]), time.time())
    return {"ok": True}


# Besides record categories, the timeline can show what was logged and measured day to day. "Everything" includes the
# journal (symptoms, doses, notes; not daily check-ins) and device alerts (ECGs, notifications); workouts, being
# near daily, have their own filter.
LIFE = {"workouts", "journal", "checkins", "signals"}
MOMENT_MAX = 1000        # the most items one moment can add to a page
EVERYTHING_LIFE = {"journal", "signals"}


def _life_items(profile_id: str, kinds: set[str], before: Optional[str], q: Optional[str], limit: int,
                inclusive: bool = False) -> list[dict]:
    out: list[dict] = []
    end = before or None
    if "workouts" in kinds and not q:
        for w in activity.list_workouts(profile_id, None, limit, end=end):
            out.append({"id": w["id"], "kind": "workout", "category": "workouts", "title": w["name"], "effective_at": w["start_date"],
                        "source_name": w["source_name"], "duration_s": w["duration_s"], "distance_m": w["distance_m"]})
    groups = []
    if "journal" in kinds:
        groups.append(("journal", [c for c in activity.JOURNAL_CATEGORIES if c != "stateOfMind"]))
    if "checkins" in kinds:
        groups.append(("journal", ["stateOfMind"]))
    if "signals" in kinds:
        groups.append(("signal", list(activity.SIGNAL_CATEGORIES)))
    for kind, cats in groups:
        for cat in cats:
            for e in activity.list_events(profile_id, None, category=cat, end=end, limit=limit, search=q, view=None):
                if e["event_type"] in activity.NOT_JOURNAL_TYPES:
                    continue
                out.append({"id": e["id"], "kind": kind, "category": e["category"], "event_type": e["event_type"],
                            "title": e["name"], "effective_at": e["start_date"], "value": e["value"],
                            "value_label": e["value_label"], "note": e["note"], "manual": e["manual"],
                            "source_name": e["source_name"]})
    return [i for i in out if not before or i["effective_at"] < before or (inclusive and i["effective_at"] == before)]


@router.get("/timeline")
def timeline(profile: Optional[str] = None, before: Optional[str] = None, q: Optional[str] = None,
                   categories: Optional[str] = None, limit: int = Query(120, ge=1, le=500)) -> dict:
    """Health history newest first: records from every institution, and (``categories`` workouts, journal, checkins,
    signals, or by default journal and signals) what was logged and measured along the way."""
    prof = resolve_profile(profile)
    cats = categories.split(",") if categories else []
    life = {c for c in cats if c in LIFE} if cats else EVERYTHING_LIFE
    clinical = [c for c in cats if c not in LIFE]

    def collect(until: Optional[str], n: int, inclusive: bool = False) -> list[dict]:
        items = (records.timeline(prof["id"], categories=clinical or None, limit=n, before=until, q=q, inclusive=inclusive)
                 if clinical or not cats else [])
        if life:
            items += _life_items(prof["id"], life, until, q, n, inclusive)
        items.sort(key=lambda i: i.get("effective_at") or "", reverse=True)
        return items

    items = collect(before, limit)
    if len(items) < limit:
        undated = records.timeline_undated(prof["id"], categories=clinical or None, q=q) if clinical or not cats else []
        return {"items": items + undated, "next_before": None}
    # The next page starts strictly before this one's last moment, so everything at that moment comes on this page
    # (a lab panel's results share one): cut at the count alone, the rest of them were never shown.
    boundary = items[limit - 1].get("effective_at") or ""
    page = [i for i in items if (i.get("effective_at") or "") > boundary]
    page += [i for i in collect(boundary, MOMENT_MAX, inclusive=True) if i.get("effective_at") == boundary]
    return {"items": page, "next_before": boundary or None}


@router.get("/observations")
def observation_catalog(profile: Optional[str] = None, category: str = "labs") -> dict:
    prof = resolve_profile(profile)
    return {"items": records.observation_catalog(prof["id"], category)}


@router.get("/observations/before-after")
def before_after(start: str, code: Optional[str] = None, title: Optional[str] = None, profile: Optional[str] = None,
                 category: Optional[str] = None, window: Optional[int] = Query(None, ge=1, le=3650)) -> dict:
    """A test's values on each side of a date, such as a medication's first order on record."""
    if not code and not title:
        raise HTTPException(422, "Say which test: its code or its name.")
    prof = resolve_profile(profile)
    try:
        return records.before_and_after(prof["id"], start, code=code, title=title, category=category, window_days=window)
    except ValueError:
        raise HTTPException(422, "Say which date, as a date.") from None


@router.get("/observations/series")
def observation_series(code: Optional[str] = None, title: Optional[str] = None, profile: Optional[str] = None,
                       category: Optional[str] = None) -> dict:
    """One test's history, by its code, or by its name for a test the source sent with no code."""
    if not code and not title:
        raise HTTPException(422, "Say which test: its code or its name.")
    prof = resolve_profile(profile)
    return records.observation_series(prof["id"], code, category, title=title)
