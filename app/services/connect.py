"""
Connection flows: starting OAuth authorizations and completing callbacks.

EHR connections resolve an institution to a FHIR base URL according to the
platform's configured mode:

* ``simulated``  → the built-in simulator for that institution (default until registered)
* ``sandbox``    → the vendor's public developer sandbox (requires a sandbox client ID)
* ``production`` → the institution's published endpoint (requires a production client ID)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app import directory
from app.connectors import google_health, oura, smart, whoop
from app.connectors import wearable_common as wc
from app.connectors.smart import SmartError
from app.core import security, settings
from app.simulator.server import fhir_base_path
from app.store import connections

log = logging.getLogger("syntropy.connect")

SIMULATED_CLIENT_ID = "syntropy-health"


def plan_ehr_connection(institution_id: Optional[str], origin: str, *, fhir_base_url: Optional[str] = None,
                        platform: Optional[str] = None, client_id: Optional[str] = None,
                        mode_override: Optional[str] = None) -> dict[str, Any]:
    """Resolves where and how an EHR connection will authenticate (no side effects)."""
    inst = directory.get_institution(institution_id) if institution_id else None
    if institution_id and not inst and not (platform and fhir_base_url):
        # A reconnect passes the platform and saved address, so an institution that left the directory still works.
        raise SmartError("Unknown institution.")
    platform = platform or (inst["platform"] if inst else "custom")

    if platform == "custom":
        if not fhir_base_url:
            raise SmartError("A FHIR base URL is required for a custom server.")
        if not client_id:
            raise SmartError("A client ID registered with that server is required.")
        return {"platform": "custom", "mode": "production", "fhir_base_url": fhir_base_url.rstrip("/"),
                "client_id": client_id, "redirect_uri": settings.get("relay.redirect_uri"),
                "display_name": fhir_base_url, "institution_id": None, "scopes": directory.USCDI_SCOPES,
                "simulated": False}

    cfg = settings.platform_config(platform)
    preset = directory.PLATFORMS[platform]
    mode = mode_override or cfg["mode"]
    name = inst["name"] if inst else preset["label"]
    dynamic = bool(preset.get("dynamic_registration"))
    if mode == "simulated":
        return {"platform": platform, "mode": "simulated", "institution_id": institution_id or platform,
                "fhir_base_url": f"{origin}{fhir_base_path(platform, institution_id or platform)}",
                "client_id": SIMULATED_CLIENT_ID, "redirect_uri": f"{origin}/callback",
                "display_name": name, "scopes": directory.USCDI_SCOPES, "simulated": True,
                "dynamic_registration": dynamic}

    cid = client_id or cfg["client_id"]
    if not cid:
        raise SmartError(f"{preset['label']} is set to {mode} mode but no client ID is configured. "
                         "Add it in Settings → Developer, or switch back to simulated mode.")
    if mode == "sandbox":
        base = preset["sandbox_base"]
        if not base:
            raise SmartError(f"{preset['label']} has no shared sandbox. {preset.get('sandbox_hint', '')}.")
        display = f"{name} ({preset['label']} sandbox)" if inst and inst.get("platform") != "smart-health-it" else name
    else:
        base = (inst or {}).get("fhir_base_url") or fhir_base_url
        if not base:
            raise SmartError(f"No production FHIR endpoint is published for {name}.")
        display = name
    return {"platform": platform, "mode": mode, "institution_id": institution_id, "fhir_base_url": base.rstrip("/"),
            "client_id": cid, "redirect_uri": settings.get("relay.redirect_uri"), "display_name": display,
            "scopes": preset["scopes"], "simulated": False, "dynamic_registration": dynamic,
            "hint": preset.get("sandbox_hint") if mode == "sandbox" else None}


async def start_ehr(profile_id: str, origin: str, *, institution_id: Optional[str] = None,
                    fhir_base_url: Optional[str] = None, platform: Optional[str] = None,
                    client_id: Optional[str] = None, reconnect_id: Optional[str] = None,
                    redirect_uri: Optional[str] = None) -> dict[str, Any]:
    if reconnect_id:
        existing = connections.get(reconnect_id)
        if not existing or existing["kind"] != "ehr":
            raise SmartError("Connection not found.")
        institution_id = existing.get("institution_id")
        platform = existing["provider"]
        profile_id = existing["profile_id"]
        fhir_base_url = existing["fhir_base_url"]
        if platform == "custom":
            client_id = client_id or ((connections.get(reconnect_id, include_credentials=True) or {}).get("credentials") or {}).get("client_id")
        plan = plan_ehr_connection(institution_id, origin, fhir_base_url=fhir_base_url, platform=platform,
                                   client_id=client_id, mode_override=existing["mode"] if existing["mode"] != "live" else None)
    else:
        plan = plan_ehr_connection(institution_id, origin, fhir_base_url=fhir_base_url, platform=platform, client_id=client_id)
    if redirect_uri and not plan["simulated"]:
        plan["redirect_uri"] = redirect_uri

    config = await smart.discover(plan["fhir_base_url"], plan["simulated"])
    verifier, challenge = security.generate_pkce_pair()
    state = smart.make_state("smart", origin)
    connections.save_pending(state, "smart", {
        **plan, "profile_id": profile_id, "code_verifier": verifier, "origin": origin,
        "token_endpoint": config["token_endpoint"], "reconnect_id": reconnect_id,
        "registration_endpoint": (smart.registration_endpoint(config, config["token_endpoint"])
                                  if plan.get("dynamic_registration") else None),
    })
    url = smart.authorization_url(
        config["authorization_endpoint"], client_id=plan["client_id"], redirect_uri=plan["redirect_uri"],
        scope=smart.fit_scopes(plan["scopes"], config.get("scopes_supported")), state=state, aud=plan["fhir_base_url"],
        code_challenge=challenge,
    )
    return {"auth_url": url, "mode": plan["mode"], "display_name": plan["display_name"], "hint": plan.get("hint")}


async def complete_ehr(pending: dict[str, Any], code: str) -> dict[str, Any]:
    creds, patient = await smart.exchange_code(
        token_endpoint=pending["token_endpoint"], code=code, redirect_uri=pending["redirect_uri"],
        client_id=pending["client_id"], code_verifier=pending["code_verifier"], simulated=pending["simulated"],
    )
    if not patient:
        raise SmartError("Authorization succeeded but the source did not identify the patient (missing launch/patient).", "auth")
    creds.update({"token_endpoint": pending["token_endpoint"], "client_id": pending["client_id"]})
    if pending.get("registration_endpoint"):
        try:
            creds["dynamic_client"] = await smart.register_dynamic_client(
                registration_endpoint=pending["registration_endpoint"], access_token=creds["access_token"],
                software_id=pending["client_id"], simulated=pending["simulated"])
        except SmartError as exc:
            # The first sync still works with the access token we have; later ones will ask to reconnect.
            log.warning("Dynamic client registration with %s failed: %s", pending["display_name"], exc)
    provider = pending["platform"]
    existing = None
    if pending.get("reconnect_id"):
        existing = connections.get(pending["reconnect_id"])
    if existing is None:
        existing = connections.find_existing(pending["profile_id"], provider, fhir_base_url=pending["fhir_base_url"],
                                             patient_ref=patient)
    if existing:
        connections.update(existing["id"], credentials=creds, status="active", patient_ref=patient,
                           scopes=creds.get("scope"), last_error=None, fhir_base_url=pending["fhir_base_url"])
        return {"connection_id": existing["id"], "created": False}
    conn = connections.create(
        pending["profile_id"], "ehr", provider, pending["display_name"], mode=pending["mode"],
        institution_id=pending.get("institution_id"), fhir_base_url=pending["fhir_base_url"], patient_ref=patient,
        scopes=creds.get("scope"), credentials=creds,
    )
    return {"connection_id": conn["id"], "created": True}


# ---------------------------------------------------------------------------
# Wearables
# ---------------------------------------------------------------------------

WEARABLE_MODULES = {"oura": oura, "whoop": whoop, "google": google_health}
WEARABLE_NAMES = {"oura": "Oura Ring", "whoop": "WHOOP", "google": "Google Health"}


def start_wearable(profile_id: str, provider: str, origin: str, mode: str = "live") -> dict[str, Any]:
    if provider not in WEARABLE_MODULES:
        raise SmartError("Unknown wearable.")
    if mode == "simulated":
        existing = next((c for c in connections.list_for_profile(profile_id, "wearable")
                         if c["provider"] == provider and c["mode"] == "simulated"), None)
        if existing:
            return {"connection_id": existing["id"], "created": False}
        conn = connections.create(profile_id, "wearable", provider, f"{WEARABLE_NAMES[provider]} (simulated)", mode="simulated")
        return {"connection_id": conn["id"], "created": True}
    state = smart.make_state(provider, origin)
    connections.save_pending(state, provider, {"profile_id": profile_id, "provider": provider, "origin": origin,
                                               "local_exchange": wc.uses_local_exchange(provider, origin)})
    return {"auth_url": WEARABLE_MODULES[provider].authorization_url(state)}


async def complete_wearable(pending: dict[str, Any], *, code: Optional[str] = None,
                            tokens: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    provider = pending["provider"]
    module = WEARABLE_MODULES[provider]
    if tokens:
        creds = wc.token_credentials(tokens)
    elif code:
        creds = await module.exchange_code(code)
    else:
        raise SmartError("Missing authorization code.", "auth")
    existing = next((c for c in connections.list_for_profile(pending["profile_id"], "wearable", include_disconnected=True)
                     if c["provider"] == provider and c["mode"] == "live"), None)
    if existing:
        connections.update(existing["id"], credentials=creds, status="active", last_error=None, scopes=creds.get("scope"))
        return {"connection_id": existing["id"], "created": False}
    conn = connections.create(pending["profile_id"], "wearable", provider, WEARABLE_NAMES[provider], mode="live",
                              credentials=creds, scopes=creds.get("scope"))
    return {"connection_id": conn["id"], "created": True}

