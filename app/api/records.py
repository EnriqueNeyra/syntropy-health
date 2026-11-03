"""Clinical records: summary, lists, timeline, observation trends."""

from __future__ import annotations

import os
import time
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.api.deps import check_access, resolve_profile
from app.api.labs import _check_date, _manual_connection
from app.connectors import fhir_normalize
from app.core import auth, context, settings
from app.core.db import audit, db, new_id
from app.services import insights, lab_report
from app.store import activity, connections, record_files, record_overrides, records

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
    has: Optional[str] = None,
) -> dict:
    prof = resolve_profile(profile)
    return records.list_records(prof["id"], category, q=q, connection_id=connection, code=code, status=status,
                                since=since, until=until, dedupe=dedupe, limit=limit, offset=offset,
                                kind=kind, note_type=note_type, author=author, has=has)


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
    rec["editable"] = bool(conn and conn["provider"] == "manual")   # what the person typed in can be deleted
    rec["files"] = record_files.for_record(rec["connection_id"], rec["resource_type"], rec["resource_id"])
    report = _report_read_from(rec)
    if report:
        rec["files"] += record_files.for_record(rec["connection_id"], "DiagnosticReport", report["resource_id"])
        rec["from_report"] = {k: report[k] for k in ("id", "title", "effective_at")}
    if rec["category"] == "medications":
        rec["history"] = insights.medication_history(rec["profile_id"], rec)
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


class OverrideBody(BaseModel):
    state: Optional[str] = Field(None, max_length=40)
    note: Optional[str] = Field(None, max_length=record_overrides.MAX_NOTE)


def _record_to_change(record_id: str) -> dict:
    rec = records.get_record(record_id)
    if not rec:
        raise HTTPException(404, "Record not found")
    check_access(rec["profile_id"], "manage")
    return rec


@router.put("/records/{record_id}/override")
def set_override(record_id: str, body: OverrideBody) -> dict:
    rec = _record_to_change(record_id)
    try:
        said = record_overrides.set_override(rec["profile_id"], rec, body.state, body.note, context.actor())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    with db() as conn:
        audit(conn, "user", "record.corrected", None, rec["profile_id"])
    return said


@router.delete("/records/{record_id}/override")
def clear_override(record_id: str) -> dict:
    rec = _record_to_change(record_id)
    record_overrides.clear(rec["profile_id"], rec)
    with db() as conn:
        audit(conn, "user", "record.correction_removed", None, rec["profile_id"])
    return {"ok": True}


class SelfReported(BaseModel):
    profile_id: Optional[str] = None
    kind: str = Field(..., pattern="^(medication|supplement|allergy)$")
    name: str = Field(..., min_length=1, max_length=200)
    how: Optional[str] = Field(None, max_length=500)       # how it's taken, or the reaction
    started: Optional[str] = Field(None, max_length=10)
    note: Optional[str] = Field(None, max_length=record_overrides.MAX_NOTE)


PATIENT_REPORTED = "Patient-reported"


def _self_reported_resource(body: SelfReported) -> dict:
    name, how, note = body.name.strip(), (body.how or "").strip(), (body.note or "").strip()
    notes = {"note": [{"text": note}]} if note else {}
    if body.kind == "allergy":
        return {"resourceType": "AllergyIntolerance", "id": f"man-{new_id()}", "clinicalStatus": {"coding": [{"code": "active"}]},
                "code": {"text": name}, **notes, **({"onsetDateTime": body.started} if body.started else {}),
                **({"reaction": [{"manifestation": [{"text": how}]}]} if how else {})}
    return {"resourceType": "MedicationStatement", "id": f"man-{new_id()}", "status": "active", "reportedBoolean": True,
            "category": {"coding": [{"code": "patientspecified"}], "text": "Supplement" if body.kind == "supplement" else "Medication"},
            "medicationCodeableConcept": {"text": name}, **notes, **({"effectivePeriod": {"start": body.started}} if body.started else {}),
            **({"dosage": [{"text": how}]} if how else {})}


@router.post("/records/self-reported")
def add_self_reported(body: SelfReported) -> dict:
    """A medication, supplement or allergy the person says they have, which no source lists. Goes through the manual source and
    the normalizers, so it reaches the report, Ask and exports like the rest, with its source saying whose word it is."""
    prof = resolve_profile(body.profile_id, "manage")
    if not body.name.strip():
        raise HTTPException(400, "Enter a name.")
    if body.started:
        _check_date(body.started, "The date")
    _, recs = fhir_normalize.normalize_resources([_self_reported_resource(body)])
    conn = _manual_connection(prof["id"])
    records.upsert_records(prof["id"], conn["id"], PATIENT_REPORTED, recs)
    with db() as c:
        audit(c, "user", "record.self_reported", None, prof["id"])
    return {"connection_id": conn["id"], "record_id": records.record_id(conn["id"], recs[0]["resource_type"], recs[0]["resource_id"])}


def _report_read_from(rec: dict) -> Optional[dict]:
    """The report a result was read from, which holds the original file. One file, however many results came from it."""
    for link in (rec.get("raw") or {}).get("derivedFrom") or []:
        ref = link.get("reference") if isinstance(link, dict) else None
        if isinstance(ref, str) and ref.startswith("DiagnosticReport/"):
            found = records.get_record(records.record_id(rec["connection_id"], "DiagnosticReport", ref.split("/", 1)[1]))
            if found:
                return found
    return None


def _file_of(record_id: str, file_id: str) -> tuple[dict, dict]:
    """A file, if it belongs to this record or to the report this result was read from, and the person may see it."""
    rec = records.get_record(record_id)
    found = record_files.get(file_id)
    if not rec or not found or not found.get("data"):
        raise HTTPException(404, "Document not found")
    owner = (found["connection_id"], found["resource_type"], found["resource_id"])
    report = _report_read_from(rec) if owner != (rec["connection_id"], rec["resource_type"], rec["resource_id"]) else rec
    if not report or owner != (report["connection_id"], report["resource_type"], report["resource_id"]):
        raise HTTPException(404, "Document not found")
    check_access(rec["profile_id"], "view")
    return rec, found


@router.get("/records/{record_id}/files/{file_id}/pages")
def get_record_file_pages(record_id: str, file_id: str) -> dict:
    """A file's pages as images: the phone apps and the Mac and Windows windows can't open a PDF or a HEIC, and every one can show these."""
    _, found = _file_of(record_id, file_id)
    try:
        return {"pages": lab_report.page_previews(found["data"], found["content_type"], found.get("name") or "")}
    except lab_report.ReportError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/records/{record_id}/files/{file_id}")
def get_record_file(record_id: str, file_id: str, download: bool = False) -> Response:
    _, found = _file_of(record_id, file_id)
    shown = found["content_type"] in record_files.SHOWN_INLINE and not download
    name = "".join(c for c in (found.get("name") or "document") if c.isalnum() or c in " ._-")[:120].strip() or "document"
    # Headers are latin-1, so a name in another script goes in the form made for that, beside a plain one for anything older.
    stem, suffix = os.path.splitext(name)
    plain = (stem.encode("ascii", "ignore").decode().strip() or "document") + suffix.encode("ascii", "ignore").decode()
    return Response(found["data"], media_type=found["content_type"] if shown else "application/octet-stream", headers={
        "Content-Disposition": f"""{"inline" if shown else "attachment"}; filename="{plain}"; filename*=UTF-8''{quote(name)}""",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


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
