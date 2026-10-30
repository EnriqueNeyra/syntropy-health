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
            "needs_release_build": bool(cfg and cfg["needs_release_build"]),
            "production_available": bool(inst.get("fhir_base_url")), "one_time": bool(preset.get("one_time")),
            "sign_in_problem": directory.sign_in_problem(inst) if mode == "production" else None,
            "sandbox_hint": preset.get("sandbox_hint") if mode == "sandbox" else None}


@router.get("")
def search(q: Optional[str] = Query(None, max_length=200), platform: Optional[str] = None, limit: int = Query(25, ge=1, le=200),
           state: Optional[str] = Query(None, max_length=2), offset: int = Query(0, ge=0, le=100000)) -> dict:
    cfgs = _platforms()
    hidden = _hidden(cfgs)
    res = directory.search(q, platform, limit, available={k for k, c in cfgs.items() if c["available"]}, hidden=hidden,
                           state=state, offset=offset)
    out = {"total": res["total"], "results": [_decorate(i, cfgs) for i in res["results"]], "platforms": directory.stats(hidden),
           "states": res["states"], "problem": directory.problem_for_people()}
    if res.get("corrected"):
        out["corrected"] = res["corrected"]
    return out


@router.get("/featured")
def featured() -> dict:
    cfgs = _platforms()
    picks = directory.featured(available={k for k, c in cfgs.items() if c["available"]})
    directory.warm()        # whoever opened the list is about to type
    stats = directory.stats(_hidden(cfgs))
    return {"results": [_decorate(i, cfgs) for i in picks], "total": sum(stats.values()),
            "connectable": sum(n for p, n in stats.items() if cfgs[p]["available"]) if cfgs else 0,
            "athena": _decorate(athena, cfgs) if (athena := directory.get_institution("athenahealth")) else None,
            "problem": directory.problem_for_people()}


@router.get("/{inst_id}")
def institution(inst_id: str) -> dict:
    """One health system, as search returns it (for a listing suggested in place of another)."""
    inst = directory.get_institution(inst_id)
    if not inst:
        raise HTTPException(404, "Unknown institution.")
    return _decorate(inst, _platforms())
