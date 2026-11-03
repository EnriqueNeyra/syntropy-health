"""
Connection flows: starting OAuth authorizations and completing callbacks.

EHR connections resolve an institution to a FHIR base URL according to the
platform's configured mode:

* ``production`` → the institution's published endpoint (requires a production client ID); always, unless
  developer mode is on, which uses each platform's own mode:
* ``simulated``  → the built-in simulator for that institution
* ``sandbox``    → the vendor's public developer sandbox (requires a sandbox client ID)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from app import directory
from app.connectors import fhir_normalize, google_health, oura, smart, whoop
from app.connectors import wearable_common as wc
from app.connectors.smart import SmartError
from app.core import context, security, settings
from app.simulator.server import fhir_base_path
from app.store import connections, records

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
    if platform != "custom" and platform not in directory.PLATFORMS:
        raise SmartError("Unknown health record system.")

    if platform == "custom":
        if not fhir_base_url:
            raise SmartError("A FHIR base URL is required for a custom server.")
        if not client_id:
            raise SmartError("A client ID registered with that server is required.")
        return {"platform": "custom", "mode": "production", "fhir_base_url": fhir_base_url.rstrip("/"),
                "client_id": client_id, "redirect_uri": settings.get("relay.redirect_uri"),
                "display_name": fhir_base_url, "institution_id": None, "scopes": directory.USCDI_SCOPES,
                "simulated": False}

    cfg = settings.platform_config(platform, mode_override)
    preset = directory.PLATFORMS[platform]
    mode = cfg["mode"]
    name = inst["name"] if inst else preset["label"]
    dynamic = bool(preset.get("dynamic_registration"))
    if mode == "simulated":
        return {"platform": platform, "mode": "simulated", "institution_id": institution_id or platform,
                "fhir_base_url": f"{origin}{fhir_base_path(platform, institution_id or platform)}",
                "client_id": SIMULATED_CLIENT_ID, "redirect_uri": f"{origin}/callback",
                "display_name": name, "scopes": directory.USCDI_SCOPES, "simulated": True,
                "dynamic_registration": dynamic}

    if cfg["developer_only"] and not cfg["developer_mode"] and not mode_override:
        raise SmartError(f"{name} is a test server, offered in developer mode only.")
    cid = client_id or cfg["client_id"]
    if not cid and cfg["needs_release_build"]:
        raise SmartError(f"This copy of Syntropy Health was built from the source code, so it can't use Syntropy Labs' "
                         f"registration with {preset['label']}. Install Syntropy Health from health.syntropylabs.io/setup, "
                         f"or add your own {preset['label']} client ID in Settings → Developer.")
    if not cid and mode == "production":
        raise SmartError(f"Syntropy Health isn't registered with {preset['label']} yet, so {name} can't be connected. "
                         "You can import a record file downloaded from its patient portal instead.")
    if not cid:
        raise SmartError(f"{preset['label']} is set to {mode} mode but no {mode} client ID is configured. "
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


def acting_as_owner() -> bool:
    """Outside a request (the command line) there's nobody less trusted than the person running it."""
    principal = context.principal()
    return principal is None or bool(principal.owner)


async def check_custom_server(url: str) -> None:
    """Who may point this server at what. The owner, whose server it is, can use their own network, so a local test server
    keeps working. Anyone else gets https to the public internet (and smart.GUARDED holds every later request to that)."""
    full_address = "Enter the FHIR server's full address, starting with https://."
    try:
        parts = urlsplit(url)
        host, _ = parts.hostname, parts.port
        httpx.URL(url)
    except (ValueError, httpx.InvalidURL) as exc:
        raise SmartError(full_address) from exc
    if parts.scheme not in ("http", "https") or not host or any(c.isspace() for c in host):
        raise SmartError(full_address)
    try:
        if parts.scheme == "https":
            if not acting_as_owner() and not await smart.is_public_host(host):
                raise SmartError("That FHIR server's address is on a private network, which only this server's owner can connect to.")
            return
        if not acting_as_owner():
            raise SmartError("FHIR servers must use https.")
        if not settings.developer_mode() and await smart.is_public_host(host):
            raise SmartError("FHIR servers on the internet must use https. Plain http is only for a server on your own network.")
    except httpx.HTTPError as exc:
        raise SmartError(f"Couldn't find a server called {host}. Check the address for a typo.", "network") from exc


async def _owner_trusts(url: str) -> bool:
    """The owner's own network, or anything while they're developing. A public server the owner added is held to the same
    rules as anyone's: it could still redirect a sync to something on this network."""
    if not acting_as_owner():
        return False
    if settings.developer_mode():
        return True
    try:
        return not await smart.is_public_host(urlsplit(url).hostname or "")
    except httpx.HTTPError:
        return False


def _allows_private(connection: dict[str, Any]) -> bool:
    return bool((connection.get("metadata") or {}).get("allow_private"))


def guard_for(connection: dict[str, Any]) -> bool:
    """A custom server the owner didn't add stays held to the public internet, at every sync. (Added before this rule,
    one on a private address stops with an explanation until the owner signs in to it again.)"""
    return connection.get("provider") == "custom" and not _allows_private(connection)


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
        plan["allow_private"] = plan["platform"] == "custom" and (_allows_private(existing) or await _owner_trusts(plan["fhir_base_url"]))
    else:
        plan = plan_ehr_connection(institution_id, origin, fhir_base_url=fhir_base_url, platform=platform, client_id=client_id)
        plan["allow_private"] = False
        if plan["platform"] == "custom":
            await check_custom_server(plan["fhir_base_url"])
            plan["allow_private"] = await _owner_trusts(plan["fhir_base_url"])
    if redirect_uri and not plan["simulated"]:
        plan["redirect_uri"] = redirect_uri

    smart.GUARDED.set(plan["platform"] == "custom" and not plan["allow_private"])
    config = await smart.discover(plan["fhir_base_url"], plan["simulated"])
    verifier, challenge = security.generate_pkce_pair()
    state = smart.make_state("smart", origin)
    await asyncio.to_thread(connections.save_pending, state, "smart", {
        **plan, "profile_id": profile_id, "code_verifier": verifier, "origin": origin,
        "token_endpoint": config["token_endpoint"], "reconnect_id": reconnect_id,
        "registration_endpoint": (smart.registration_endpoint(config, config["token_endpoint"])
                                  if plan.get("dynamic_registration") else None),
    })
    url = smart.authorization_url(
        config["authorization_endpoint"], client_id=plan["client_id"], redirect_uri=plan["redirect_uri"],
        scope=smart.fit_scopes(plan["scopes"], config.get("scopes_supported"), own_registration=plan["platform"] == "custom"),
        state=state, aud=plan["fhir_base_url"],
        code_challenge=challenge,
    )
    if not plan["simulated"]:
        url = await smart.kaiser_sign_in_url(url)
    return {"auth_url": url, "mode": plan["mode"], "display_name": plan["display_name"], "hint": plan.get("hint")}


async def _patient_seen_by(pending: dict[str, Any], creds: dict[str, Any], patient: str) -> Optional[dict[str, Any]]:
    """Who these tokens say they're for, if the source will say. Used before anything is changed, so failing is harmless."""
    fhir = smart.FhirClient(pending["fhir_base_url"], creds, patient, pending["simulated"])
    try:
        async with smart.http_client(pending["simulated"], timeout=20.0) as client:
            resource = await fhir.read(client, f"Patient/{patient}")
    except (SmartError, httpx.HTTPError):
        return None
    if not isinstance(resource, dict) or resource.get("resourceType") != "Patient":
        return None
    try:
        return fhir_normalize.normalize_patient(resource)
    except (AttributeError, TypeError, ValueError):
        return None


def _chart_numbers(patient: Any) -> dict[str, str]:
    """A person's medical record numbers, by the system that issued them. Other identifiers don't count: an insurance
    number can be shared by a family."""
    numbers: dict[str, str] = {}
    idents = patient.get("identifier") if isinstance(patient, dict) else None
    for ident in idents if isinstance(idents, list) else []:
        if not isinstance(ident, dict) or not ident.get("system") or not ident.get("value"):
            continue
        kind = ident.get("type") if isinstance(ident.get("type"), dict) else {}
        if "MR" in smart._codes(kind) or "mrn" in str(ident["system"]).lower():
            numbers[str(ident["system"])] = str(ident["value"])
    return numbers


def _same_person(stored: Optional[dict[str, Any]], seen: Optional[dict[str, Any]]) -> bool:
    """A hospital merging charts or changing systems can give one person a new id. Twins share a birth date and a surname,
    and a parent may hold both charts, so a birth date takes a chart number from the same system or, failing that, the
    whole name. Anything less gets asked about."""
    if not stored or not seen or not stored.get("birth_date") or stored["birth_date"] != seen.get("birth_date"):
        return False
    ours, theirs = _chart_numbers(stored.get("raw")), _chart_numbers(seen.get("raw"))
    if shared := ours.keys() & theirs.keys():
        return all(ours[system] == theirs[system] for system in shared)
    was, now = records._name_tokens(stored.get("full_name")), records._name_tokens(seen.get("name"))
    return len(was) > 1 and len(now) > 1 and (was <= now or now <= was)


async def _reconnect_target(pending: dict[str, Any], creds: dict[str, Any], patient: str) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """The source this sign-in renews, or, if it opened someone else's chart, what to ask the person about. A parent or
    carer picking the wrong chart at the portal must never replace one person's history with another's."""
    target = connections.get(pending["reconnect_id"]) if pending.get("reconnect_id") else None
    if not target or not target.get("patient_ref") or target["patient_ref"] == patient:
        return target, None
    seen = await _patient_seen_by(pending, creds, patient)
    stored = next((i for i in records.identities(target["profile_id"]) if i["connection_id"] == target["id"]), None)
    if _same_person({**stored, "raw": records.identity_raw(target["id"])} if stored else None, seen):
        return target, None
    return None, {
        "action": "other_patient", "creds": creds, "patient": patient, "profile_id": pending["profile_id"],
        "provider": pending["platform"], "fhir_base_url": pending["fhir_base_url"], "institution_id": pending.get("institution_id"),
        "mode": pending["mode"], "display_name": target["display_name"], "allow_private": pending.get("allow_private"),
        "name": (seen or {}).get("name"), "was": (stored or {}).get("full_name"),
    }


async def add_other_patient(choice: dict[str, Any]) -> dict[str, Any]:
    """The person looked and wants the other chart too: as its own source, or the one that person already has."""
    existing = await asyncio.to_thread(connections.find_existing, choice["profile_id"], choice["provider"],
                                       fhir_base_url=choice["fhir_base_url"], patient_ref=choice["patient"])
    creds = choice["creds"]
    if existing:
        await asyncio.to_thread(connections.update, existing["id"], credentials=creds, status="active", scopes=creds.get("scope"),
                                last_error=None, next_retry_at=None, sync_failures=0)
        return {"connection_id": existing["id"], "created": False}
    name = choice.get("name")
    conn = await asyncio.to_thread(
        connections.create, choice["profile_id"], "ehr", choice["provider"],
        f"{choice['display_name']} · {name}" if name else f"{choice['display_name']} · another chart", mode=choice["mode"],
        institution_id=choice.get("institution_id"), fhir_base_url=choice["fhir_base_url"], patient_ref=choice["patient"],
        scopes=creds.get("scope"), credentials=creds, metadata={"allow_private": True} if choice.get("allow_private") else None,
    )
    return {"connection_id": conn["id"], "created": True}


async def complete_ehr(pending: dict[str, Any], code: str) -> dict[str, Any]:
    smart.GUARDED.set(pending["platform"] == "custom" and not pending.get("allow_private"))
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
    existing, ask = await _reconnect_target(pending, creds, patient)
    if ask:
        return {"ask": ask}
    if existing is None:
        existing = connections.find_existing(pending["profile_id"], provider, fhir_base_url=pending["fhir_base_url"],
                                             patient_ref=patient)
    metadata = {"allow_private": True} if pending.get("allow_private") else None
    if existing:
        if existing.get("patient_ref") != patient:
            await asyncio.to_thread(connections.forget_query_state, existing["id"])     # saved places were in the old chart
        changes: dict[str, Any] = {"metadata": {**(existing.get("metadata") or {}), **metadata}} if metadata else {}
        await asyncio.to_thread(connections.update, existing["id"], credentials=creds, status="active", patient_ref=patient,
                                scopes=creds.get("scope"), last_error=None, fhir_base_url=pending["fhir_base_url"],
                                next_retry_at=None, sync_failures=0, **changes)
        return {"connection_id": existing["id"], "created": False}
    conn = await asyncio.to_thread(
        connections.create,
        pending["profile_id"], "ehr", provider, pending["display_name"], mode=pending["mode"],
        institution_id=pending.get("institution_id"), fhir_base_url=pending["fhir_base_url"], patient_ref=patient,
        scopes=creds.get("scope"), credentials=creds, metadata=metadata,
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
        # Made-up readings have no place in someone's record unless they're testing the app on purpose.
        if not settings.developer_mode():
            raise SmartError("Simulated data is only available in developer mode.")
        existing = next((c for c in connections.list_for_profile(profile_id, "wearable")
                         if c["provider"] == provider and c["mode"] == "simulated"), None)
        if existing:
            return {"connection_id": existing["id"], "created": False}
        conn = connections.create(profile_id, "wearable", provider, f"{WEARABLE_NAMES[provider]} (simulated)", mode="simulated")
        return {"connection_id": conn["id"], "created": True}
    state = smart.make_state(provider, origin)
    redirect = wc.redirect_uri(provider, origin)
    connections.save_pending(state, provider, {"profile_id": profile_id, "provider": provider, "origin": origin,
                                               "redirect_uri": redirect})
    return {"auth_url": WEARABLE_MODULES[provider].authorization_url(state, redirect)}


async def complete_wearable(pending: dict[str, Any], *, code: Optional[str] = None,
                            tokens: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    provider = pending["provider"]
    module = WEARABLE_MODULES[provider]
    if tokens:
        creds = wc.token_credentials(tokens)
    elif code:
        # The provider checks this is the address the sign-in used, whatever has been saved since.
        creds = await module.exchange_code(code, pending.get("redirect_uri") or wc.redirect_uri(provider, pending.get("origin", "")))
    else:
        raise SmartError("Missing authorization code.", "auth")
    existing = next((c for c in connections.list_for_profile(pending["profile_id"], "wearable", include_disconnected=True)
                     if c["provider"] == provider and c["mode"] == "live"), None)
    if existing:
        await asyncio.to_thread(connections.update, existing["id"], credentials=creds, status="active", last_error=None,
                                scopes=creds.get("scope"), next_retry_at=None, sync_failures=0)
        return {"connection_id": existing["id"], "created": False}
    conn = await asyncio.to_thread(connections.create, pending["profile_id"], "wearable", provider, WEARABLE_NAMES[provider],
                                   mode="live", credentials=creds, scopes=creds.get("scope"))
    return {"connection_id": conn["id"], "created": True}

