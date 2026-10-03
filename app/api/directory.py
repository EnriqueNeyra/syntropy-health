"""Institution directory search."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app import directory
from app.core import auth, settings

router = APIRouter(prefix="/api/directory", tags=["directory"], dependencies=[Depends(auth.require_user)])


def _platforms() -> dict[str, dict[str, Any]]:
    return {key: settings.platform_config(key) for key in settings.EHR_PLATFORMS}


def _hidden(cfgs: dict[str, dict[str, Any]]) -> frozenset[str]:
    """Test servers stay out of the directory unless developer mode is on."""
    return frozenset(k for k, c in cfgs.items() if c["developer_only"] and not c["developer_mode"])


def _decorate(inst: dict, cfgs: dict[str, dict[str, Any]]) -> dict:
    cfg = cfgs.get(inst["platform"])
    preset = directory.PLATFORMS.get(inst["platform"], {})
    mode = cfg["mode"] if cfg else "production"
    return {**inst, "platform_label": preset.get("label", inst["platform"]), "portal": inst.get("portal") or preset.get("portal"),
            "mode": mode, "available": cfg["available"] if cfg else True,
            "production_available": bool(inst.get("fhir_base_url")),
            "sign_in_problem": directory.sign_in_problem(inst) if mode == "production" else None,
            "sandbox_hint": preset.get("sandbox_hint") if mode == "sandbox" else None}


@router.get("")
def search(q: Optional[str] = None, platform: Optional[str] = None, limit: int = Query(25, ge=1, le=200)) -> dict:
    cfgs = _platforms()
    hidden = _hidden(cfgs)
    res = directory.search(q, platform, limit, available={k for k, c in cfgs.items() if c["available"]}, hidden=hidden)
    return {"total": res["total"], "results": [_decorate(i, cfgs) for i in res["results"]], "platforms": directory.stats(hidden)}


@router.get("/featured")
def featured() -> dict:
    cfgs = _platforms()
    picks = directory.featured(available={k for k, c in cfgs.items() if c["available"]})
    return {"results": [_decorate(i, cfgs) for i in picks], "total": sum(directory.stats(_hidden(cfgs)).values())}


@router.get("/{inst_id}")
def institution(inst_id: str) -> dict:
    """One health system, as search returns it (for a listing suggested in place of another)."""
    inst = directory.get_institution(inst_id)
    if not inst:
        raise HTTPException(404, "Unknown institution.")
    return _decorate(inst, _platforms())
