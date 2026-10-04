"""Profiles (the people whose health data is kept here), who can see them, and invitations to sign in."""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.api.deps import check_access, phone_origin, resolve_profile
from app.core import auth, context, settings
from app.store import accounts, profiles

router = APIRouter(prefix="/api/profiles", tags=["profiles"], dependencies=[Depends(auth.require_user)])


DATE = r"^\d{4}-\d{2}-\d{2}$"


def _real_date(value: Optional[str]) -> Optional[str]:
    """A birth date the age can be worked out from (the pattern alone lets 2026-02-31 through), or none."""
    if value:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Dates must look like 2026-01-31.") from exc
    return value or None


class ProfileIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    relationship: str = "other"
    birth_date: Optional[str] = Field(None, max_length=10)
    sex: Optional[str] = Field(None, max_length=20)

    check_birth_date = field_validator("birth_date")(_real_date)


class ProfilePatch(BaseModel):
    name: Optional[str] = Field(None, max_length=80)
    relationship: Optional[str] = Field(None, pattern=f"^({'|'.join(profiles.RELATIONSHIPS)})$")
    birth_date: Optional[str] = Field(None, max_length=10)       # null clears it
    sex: Optional[str] = Field(None, max_length=20)              # null clears it
    color: Optional[str] = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    is_default: Optional[bool] = None       # earlier versions: whom to open first (now always yourself)

    check_birth_date = field_validator("birth_date")(_real_date)

    @field_validator("name")
    @classmethod
    def _named(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("A name is needed.")
        return value.strip() if value is not None else None


def visible_profiles() -> list[dict[str, Any]]:
    """The people the signed-in account can see: itself first, with what it may do and whether each signs in."""
    principal = context.principal()
    signs_in = {a["profile_id"]: a for a in accounts.list_accounts()}
    invites = accounts.pending_invites()
    out = []
    for p in profiles.list_profiles():
        level = principal.level(p["id"]) if principal else "manage"
        if not level:
            continue
        account = signs_in.get(p["id"])
        mine = bool(principal) and p["id"] == principal.home
        out.append({**p, "access": level, "is_self": bool(principal) and account is not None and account["id"] == principal.account_id,
                    "is_default": mine, "signs_in": account is not None, "owner": bool(account and account["role"] == "owner"),
                    "invited_until": invites.get(p["id"]) if level == "manage" and not account else None})
    out.sort(key=lambda p: (not p["is_default"], not p["is_self"]))
    return out


@router.get("")
def list_profiles() -> dict:
    return {"profiles": visible_profiles()}


@router.post("")
async def create_profile(req: ProfileIn) -> dict:
    """Someone to look after (a child, a parent): whoever adds them manages them, and can invite them to sign in."""
    prof = profiles.create_profile(req.name, req.relationship, req.birth_date, req.sex)
    principal = context.principal()
    if principal and principal.account_id:
        accounts.grant(principal.account_id, prof["id"], "manage")
    return prof


@router.patch("/{profile_id}")
async def update_profile(profile_id: str, req: ProfilePatch) -> dict:
    check_access(profile_id, "manage")
    fields = req.model_dump(exclude_unset=True, exclude={"is_default"})
    # A name, relationship or color can only be changed; a birth date or sex can also be cleared (sent as null).
    fields = {k: v for k, v in fields.items() if v is not None or k in ("birth_date", "sex")}
    prof = profiles.update_profile(profile_id, **fields)
    if not prof:
        raise HTTPException(404, "Profile not found")
    return prof


@router.delete("/{profile_id}")
async def delete_profile(profile_id: str) -> dict:
    """Deletes a person and all their data. Someone who signs in: only they can (their own account), or an owner
    removing them from the server. Anyone else: whoever manages them."""
    principal = context.principal()
    if not profiles.get_profile(profile_id):
        raise HTTPException(404, "Profile not found")
    account = accounts.for_profile(profile_id)
    if account:
        if not principal or not (principal.account_id == account["id"] or principal.owner):
            raise HTTPException(404 if not principal or not principal.level(profile_id) else 403,
                                "Only they, or the server's owner, can remove someone who signs in.")
        if account["role"] == "owner" and not any(a["role"] == "owner" and a["id"] != account["id"]
                                                  for a in accounts.list_accounts()):
            raise HTTPException(400, "Make someone else an owner first: the server needs one.")
    else:
        check_access(profile_id, "manage")
    if len(profiles.list_profiles()) <= 1:
        raise HTTPException(400, "At least one profile must remain.")
    profiles.delete_profile(profile_id)
    settings.delete_prefix(f"overview.layout.{profile_id}")
    settings.delete_prefix(f"alerts.seen.{profile_id}")
    settings.delete_prefix(f"alerts.dismissed.{profile_id}")
    settings.delete_prefix(f"goals.{profile_id}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Sharing and invitations
# ---------------------------------------------------------------------------

@router.get("/{profile_id}/access")
def get_access(profile_id: str) -> dict:
    """Who else can see this person (for whoever manages them), and who could be given access."""
    check_access(profile_id, "manage")
    account = accounts.for_profile(profile_id)
    others = [{"account_id": a["id"], "name": a["name"], "color": a["color"]}
              for a in accounts.list_accounts() if a["profile_id"] != profile_id]
    return {"access": accounts.access_to(profile_id), "accounts": others, "signs_in": account is not None,
            "invited_until": accounts.pending_invites().get(profile_id)}


class AccessRequest(BaseModel):
    account_id: str
    level: Optional[str] = None       # 'view' or 'manage'; None removes access


@router.put("/{profile_id}/access")
async def set_access(profile_id: str, req: AccessRequest) -> dict:
    check_access(profile_id, "manage")
    if not accounts.get(req.account_id):
        raise HTTPException(404, "Account not found")
    if req.level is None:
        accounts.revoke_access(req.account_id, profile_id)
    else:
        try:
            accounts.grant(req.account_id, profile_id, req.level)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return get_access(profile_id)


@router.post("/{profile_id}/invite")
async def invite(profile_id: str, request: Request) -> dict:
    """A one-time code (a week) for this person to choose their own password. Once they do, their data is theirs:
    whoever manages them now stops seeing it unless they share it back."""
    check_access(profile_id, "manage")
    if not auth.auth_required():
        raise HTTPException(400, "Set a password for yourself first (Settings → Security). Without one, anyone who opens "
                                 "Syntropy Health is signed in as you, so a password of their own wouldn't protect them.")
    principal = context.principal()
    try:
        made = accounts.create_invite(profile_id, principal.account_id if principal else None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # The address other devices use (not "localhost", which on their device would mean themselves).
    return {**made, "server_url": phone_origin(request)}


@router.delete("/{profile_id}/invite")
async def cancel_invite(profile_id: str) -> dict:
    check_access(profile_id, "manage")
    accounts.cancel_invite(profile_id)
    return {"ok": True}


class OverviewWidget(BaseModel):
    """A card ("sleep", "labs"…) or a single number ("m:step_count"), in the order shown."""
    id: str = Field(..., max_length=60)
    visible: bool = True
    size: Optional[str] = Field(None, pattern="^(s|m|l|xl)$")      # a quarter, half, three quarters or all of the width
    options: dict[str, Any] = Field(default_factory=dict)


class OverviewLayout(BaseModel):
    widgets: list[OverviewWidget] = Field(..., max_length=60)


def _layout_key(profile_id: str) -> str:
    """Each account arranges its own view of a person."""
    principal = context.principal()
    return f"overview.layout.{profile_id}.{principal.account_id if principal and principal.account_id else ''}"


@router.get("/{profile_id}/overview")
def get_overview_layout(profile_id: str) -> dict:
    """The Overview's widgets for this person, in order, and which are shown. ``null`` until customized."""
    resolve_profile(profile_id, "view")
    return {"widgets": settings.get(_layout_key(profile_id))}


@router.put("/{profile_id}/overview")
async def set_overview_layout(profile_id: str, req: OverviewLayout) -> dict:
    resolve_profile(profile_id, "view")
    widgets = [{k: v for k, v in w.model_dump().items() if v is not None} for w in req.widgets]
    if len(str(widgets)) > 12000:
        raise HTTPException(400, "Layout is too large.")
    settings.set(_layout_key(profile_id), widgets)
    return {"widgets": widgets}


@router.delete("/{profile_id}/overview")
async def reset_overview_layout(profile_id: str) -> dict:
    resolve_profile(profile_id, "view")
    settings.set(_layout_key(profile_id), None)
    return {"widgets": None}


# Which alerts (behind the bell) have been seen or dismissed, kept here rather than in each browser so clearing them
# on the computer also clears them in the iPhone app. Alert ids are made by the web app.
ALERT_IDS_KEPT = 400


def _alerts_key(profile_id: str, kind: str) -> str:
    principal = context.principal()
    return f"alerts.{kind}.{profile_id}.{principal.account_id if principal and principal.account_id else ''}"


def _alert_ids(profile_id: str, kind: str) -> list[str]:
    return settings.get(_alerts_key(profile_id, kind)) or []


class AlertState(BaseModel):
    seen: list[str] = Field(default_factory=list, max_length=ALERT_IDS_KEPT)
    dismissed: list[str] = Field(default_factory=list, max_length=ALERT_IDS_KEPT)


def _alert_state(profile_id: str) -> dict:
    return {"seen": _alert_ids(profile_id, "seen"), "dismissed": _alert_ids(profile_id, "dismissed")}


@router.get("/{profile_id}/alerts")
def get_alert_state(profile_id: str) -> dict:
    resolve_profile(profile_id, "view")
    return _alert_state(profile_id)


@router.post("/{profile_id}/alerts")
async def add_alert_state(profile_id: str, req: AlertState) -> dict:
    """Adds ids to the seen and dismissed lists (nothing is ever un-seen). Only the most recent ids are kept. Each
    account has its own: seeing an alert about someone doesn't mark it seen for the others who look after them."""
    resolve_profile(profile_id, "view")
    for kind, ids in (("seen", req.seen), ("dismissed", req.dismissed)):
        ids = [i[:300] for i in ids if isinstance(i, str) and i]
        if not ids:
            continue
        new = set(ids)
        current = [i for i in _alert_ids(profile_id, kind) if i not in new]
        settings.set(_alerts_key(profile_id, kind), (current + ids)[-ALERT_IDS_KEPT:])
    return _alert_state(profile_id)
