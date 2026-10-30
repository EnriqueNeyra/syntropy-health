"""Insights across sources, day-by-day comparisons, events for charts, and goals."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.deps import resolve_profile
from app.core import auth, context, settings
from app.services import insights
from app.store import goals

router = APIRouter(tags=["insights"])
user = Depends(auth.require_user)

HIDDEN_IDS_KEPT = 400


def _hidden_key(profile_id: str) -> str:
    # Like alerts, each account has its own: hiding something about a person doesn't hide it from others looking after them.
    principal = context.principal()
    return f"insights.hidden.{profile_id}.{principal.account_id if principal and principal.account_id else ''}"


def _hidden(profile_id: str) -> list[str]:
    return settings.get(_hidden_key(profile_id)) or []


@router.get("/api/insights", dependencies=[user])
def list_insights(profile: Optional[str] = None, limit: int = Query(12, ge=1, le=40)) -> dict:
    """What's worth knowing now: changes from the person's normal, links between how they slept, trained and felt,
    labs moving out of range, goals kept."""
    prof = resolve_profile(profile)
    hidden = set(_hidden(prof["id"]))
    # Asked for in full and cut afterward, so hiding one lets the next take its place. AI apps read everything.
    found = [i for i in insights.insights(prof["id"], limit + len(hidden)) if i["id"] not in hidden]
    return {"insights": found[:limit]}


@router.get("/api/insights/urgent", dependencies=[user])
def urgent_insights(profile: Optional[str] = None) -> dict:
    """Critical lab results, readings past a published limit and the watch's own warnings, for the bell."""
    prof = resolve_profile(profile)
    return {"urgent": insights.urgent(prof["id"])}


@router.get("/api/insights/baseline", dependencies=[user])
def baseline(metric: str = Query(..., max_length=80), profile: Optional[str] = None, days: int = Query(90, ge=7, le=3650),
             source: Optional[str] = Query(None, max_length=200)) -> dict:
    """The person's usual range for a measure from one device, now and over time, and when the device changed."""
    prof = resolve_profile(profile)
    try:
        return insights.personal_range(prof["id"], metric, days, source)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class HiddenIn(BaseModel):
    ids: list[str] = Field(..., max_length=HIDDEN_IDS_KEPT)


@router.get("/api/insights/hidden", dependencies=[user])
def hidden_insights(profile: Optional[str] = None) -> dict:
    prof = resolve_profile(profile)
    return {"ids": _hidden(prof["id"])}


@router.post("/api/insights/hidden", dependencies=[user])
def hide_insights(req: HiddenIn, profile: Optional[str] = None) -> dict:
    """Keeps hidden insights hidden on every device. Only the most recent ids are kept."""
    prof = resolve_profile(profile)
    new = {i[:300] for i in req.ids if i}
    ids = [i for i in _hidden(prof["id"]) if i not in new] + sorted(new)
    settings.set(_hidden_key(prof["id"]), ids[-HIDDEN_IDS_KEPT:])
    return {"ids": ids[-HIDDEN_IDS_KEPT:]}


@router.get("/api/insights/comparable", dependencies=[user])
def comparable(profile: Optional[str] = None) -> dict:
    prof = resolve_profile(profile)
    return {"measures": insights.comparable(prof["id"])}


@router.get("/api/insights/compare", dependencies=[user])
def compare(a: str = Query(..., max_length=80), b: str = Query(..., max_length=80), profile: Optional[str] = None,
            days: int = Query(90, ge=7, le=3650), lag: int = Query(0, ge=-7, le=7)) -> dict:
    """Two measures day by day (``b`` ``lag`` days after ``a``; negative: before), with their correlation."""
    prof = resolve_profile(profile)
    try:
        return insights.compare(prof["id"], a, b, days, lag)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/insights/markers", dependencies=[user])
def markers(profile: Optional[str] = None, start: Optional[str] = Query(None, max_length=10),
            end: Optional[str] = Query(None, max_length=10)) -> dict:
    """Medications started and conditions diagnosed, to mark on charts."""
    prof = resolve_profile(profile)
    return {"markers": insights.markers(prof["id"], start, end)}


class GoalIn(BaseModel):
    metric: str = Field(..., max_length=80)
    target: Optional[float] = None            # None removes the goal
    direction: str = Field("min", pattern="^(min|max)$")


@router.get("/api/goals", dependencies=[user])
def get_goals(profile: Optional[str] = None, days: int = Query(7, ge=1, le=90)) -> dict:
    prof = resolve_profile(profile)
    return {"goals": goals.get(prof["id"]), "progress": goals.progress(prof["id"], days),
            "suggested": {k: {"target": v[0], "direction": v[1]} for k, v in goals.SUGGESTED.items()}}


@router.put("/api/goals", dependencies=[user])
def set_goal(req: GoalIn, profile: Optional[str] = None) -> dict:
    prof = resolve_profile(profile, "manage")
    try:
        saved = goals.set_goal(prof["id"], req.metric, req.target, req.direction)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"goals": saved, "progress": goals.progress(prof["id"], 7)}
