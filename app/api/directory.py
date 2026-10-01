"""Institution directory search."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app import directory
from app.core import auth, settings

router = APIRouter(prefix="/api/directory", tags=["directory"], dependencies=[Depends(auth.require_user)])


def _decorate(inst: dict) -> dict:
    cfg = settings.platform_config(inst["platform"]) if inst["platform"] in settings.EHR_PLATFORMS else None
    preset = directory.PLATFORMS.get(inst["platform"], {})
    mode = cfg["mode"] if cfg else "production"
    return {**inst, "platform_label": preset.get("label", inst["platform"]), "portal": inst.get("portal") or preset.get("portal"),
            "mode": mode, "production_available": bool(inst.get("fhir_base_url")),
            "sandbox_hint": preset.get("sandbox_hint") if mode == "sandbox" else None}


@router.get("")
def search(q: Optional[str] = None, platform: Optional[str] = None, limit: int = Query(25, ge=1, le=200)) -> dict:
    res = directory.search(q, platform, limit)
    return {"total": res["total"], "results": [_decorate(i) for i in res["results"]], "platforms": directory.stats()}


@router.get("/featured")
def featured() -> dict:
    return {"results": [_decorate(i) for i in directory.featured()], "total": sum(directory.stats().values())}
