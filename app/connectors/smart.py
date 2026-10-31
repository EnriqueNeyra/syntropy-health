"""
SMART on FHIR client (App Launch 2.0, standalone patient launch, public client + PKCE).

* Discovery via ``.well-known/smart-configuration`` with CapabilityStatement fallback.
* Authorization URL construction with S256 PKCE and a relay-friendly ``state``.
* Token exchange / refresh, patient context from the token response or id_token.
* A paging FHIR client that fetches the USCDI resource set and reports, per resource
  type, whether the fetch was complete — so pruning never deletes data because of a
  transient error or a page cap.

Connections in ``simulated`` mode talk to the built-in simulator in-process
(``httpx.ASGITransport``) and therefore need no network access at all.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import math
import random
import re
import socket
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlencode, urljoin, urlparse

import httpx

from app.core import config, security

log = logging.getLogger("syntropy.smart")

ROUND_PAGES = 25                # how many pages a query gets before the others have their turn
PAGE_SIZE = 100
SYNC_TIME_BUDGET = 20 * 60      # one sync's share of the source; a longer history carries on at the next one
MAX_RESOURCES_PER_SYNC = 20_000     # they're all held in memory until they're stored
OUTAGE_QUERIES = 3              # this many queries in a row failing means the server is down: stop asking
RETRY_ATTEMPTS = 3
RETRY_STATUSES = (429, 502, 503, 504)
RETRY_AFTER_CAP = 30            # the longest a sync sits and waits; beyond it, it stops and comes back then
LONGEST_PAUSE = 24 * 3600       # the longest a server can put a source off: one bad header mustn't quietly end its syncs
PLACE_TRIES = 3                 # syncs in a row a saved place can fail, with the server otherwise fine, before starting over
TOP_UP_PAGES = 5                # how much of a history too long for one sync is read at each sync, newest first
FULL_READ_EVERY = 30 * 86400    # how often such a history is read whole again, which is what brings corrections to old records
USER_AGENT = "SyntropyHealth/1.0 (+https://health.syntropylabs.io)"

_sleep = asyncio.sleep


class SmartError(Exception):
    """A recoverable SMART/FHIR failure with a user-facing message."""

    def __init__(self, message: str, kind: str = "error", retry_after: Optional[float] = None):
        super().__init__(message)
        self.kind = kind          # 'auth' (re-authorization needed), 'network' (temporary: try again soon), 'error'
        self.retry_after = retry_after      # seconds the server asked for
        self.stats: dict[str, Any] = {}     # what the sync did before this stopped it


class BlockedAddress(SmartError):
    """A FHIR server someone typed in, or one of its redirects, pointed at an address this server won't go to for them."""


# A custom FHIR server added by someone other than the server's owner: every request it leads to, each redirect included,
# has to go over https to an address on the public internet. Otherwise it could read this server's own neighbours (a
# router's admin page, a local AI server, cloud metadata) from where this server sits. Set per sync or sign-in.
GUARDED: ContextVar[bool] = ContextVar("smart_guarded", default=False)
_lookup = socket.getaddrinfo


def _public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_global


async def is_public_host(host: str) -> bool:
    """Every address the name resolves to has to be public: a name with one public and one loopback address is how DNS
    tricks get past a check like this. A name that doesn't resolve isn't reachable, which isn't this check's to say."""
    host = host.strip("[]")
    try:
        return _public_address(host)
    except ValueError:
        pass
    try:
        infos = await asyncio.to_thread(_lookup, host, None)
    except (OSError, UnicodeError) as exc:      # the second is a name DNS couldn't even carry
        raise httpx.ConnectError(f"Couldn't look up {host}") from exc
    return bool(infos) and all(_public_address(info[4][0]) for info in infos)


async def _guard_request(request: httpx.Request) -> None:
    if not GUARDED.get():
        return
    if request.url.scheme != "https":
        raise BlockedAddress("Custom FHIR servers must use https.")
    if not await is_public_host(request.url.host):
        raise BlockedAddress("That FHIR server's address is on a private network, which only this server's owner can connect to.")


def http_client(simulated: bool = False, timeout: float = 30.0) -> httpx.AsyncClient:
    headers = {"User-Agent": USER_AGENT}
    if simulated:
        from app.simulator.server import asgi_app
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app()), timeout=timeout, headers=headers)
    return httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True, event_hooks={"request": [_guard_request]})


def retry_after_seconds(resp: httpx.Response) -> Optional[float]:
    """Retry-After is either a number of seconds or a date. Anything else, like "inf", isn't a time at all."""
    value = (resp.headers.get("retry-after") or "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    return min(max(0.0, seconds), LONGEST_PAUSE) if math.isfinite(seconds) else None


def _json_object(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def temporary_refusal(resp: httpx.Response) -> bool:
    """Whether a token endpoint's "no" is about right now rather than about this sign-in. Everything else from it (an
    invalid_grant, a revoked client) means the person has to sign in again."""
    if resp.status_code in (408, 425, 429) or resp.status_code >= 500:
        return True
    return _json_object(resp).get("error") in ("temporarily_unavailable", "server_error", "slow_down")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _check_endpoints(base: str, authorize: Any, token: Any, simulated: bool) -> None:
    """The page sends the browser to the first of these and the second gets the patient's code. A server's own word for
    where they are isn't enough for a javascript: address, or for tokens going unencrypted from an https server."""
    if simulated:
        return
    for endpoint in (authorize, token):
        scheme = urlparse(str(endpoint)).scheme.lower()
        if scheme != "https" and (scheme != "http" or base.lower().startswith("https://") or GUARDED.get()):
            raise SmartError(f"{base} gave a sign-in address that isn't secure, so it can't be connected.", "error")


async def discover(fhir_base_url: str, simulated: bool = False) -> dict[str, Any]:
    base = fhir_base_url.rstrip("/")
    accept = {"Accept": "application/json, application/fhir+json"}
    async with http_client(simulated, timeout=15.0) as client:
        try:
            resp = await client.get(f"{base}/.well-known/smart-configuration", headers=accept)
            if resp.status_code == 200:
                data = _json_object(resp)
                if data.get("authorization_endpoint") and data.get("token_endpoint"):
                    _check_endpoints(base, data["authorization_endpoint"], data["token_endpoint"], simulated)
                    return {
                        "authorization_endpoint": data["authorization_endpoint"],
                        "token_endpoint": data["token_endpoint"],
                        "revocation_endpoint": data.get("revocation_endpoint"),
                        "capabilities": data.get("capabilities", []),
                        "scopes_supported": data.get("scopes_supported", []),
                        "code_challenge_methods_supported": data.get("code_challenge_methods_supported", []),
                        "source": "well-known",
                    }
        except (httpx.HTTPError, ValueError):
            pass
        try:
            resp = await client.get(f"{base}/metadata", headers=accept)
            resp.raise_for_status()
            meta = resp.json()
        except httpx.HTTPError as exc:
            raise SmartError(f"Could not reach the FHIR server at {base}.", "network") from exc
        except ValueError as exc:
            raise SmartError(f"FHIR server at {base} returned invalid metadata.", "error") from exc
    rests = meta.get("rest") if isinstance(meta, dict) else None
    for rest in rests if isinstance(rests, list) else []:
        security_ = rest.get("security") if isinstance(rest, dict) else None
        for ext in (security_.get("extension") or []) if isinstance(security_, dict) else []:
            if str(ext.get("url", "")).endswith("oauth-uris"):
                uris = {e.get("url"): e.get("valueUri") for e in ext.get("extension", [])}
                if uris.get("authorize") and uris.get("token"):
                    _check_endpoints(base, uris["authorize"], uris["token"], simulated)
                    return {"authorization_endpoint": uris["authorize"], "token_endpoint": uris["token"],
                            "capabilities": [], "scopes_supported": [], "source": "metadata"}
    raise SmartError(f"{base} does not advertise SMART on FHIR OAuth endpoints.", "error")


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

_RESOURCE_SCOPE = re.compile(r"^(patient|user)/([A-Za-z*]+)\.")


def _scope_types(scopes: Any) -> Optional[set[str]]:
    """Resource types named by SMART resource scopes (``patient/Observation.read``…); None if unrestricted or unknown."""
    types = {m.group(2) for s in (scopes.split() if isinstance(scopes, str) else scopes or ())
             if (m := _RESOURCE_SCOPE.match(s))}
    return None if not types or "*" in types else types


def fit_scopes(requested: str, scopes_supported: Optional[list[str]], own_registration: bool = False) -> str:
    """Drops resource scopes for types the server doesn't offer (Oracle has no Medication), since
    some authorization servers reject the whole request over one unknown scope. Servers that don't list resource
    scopes (Epic lists none) get the request unchanged.

    ``own_registration`` is a server the person registered their own client with. There, a type offered only in SMART 2's
    form (patient/Observation.rs) is asked for that way. Never for the platforms Syntropy is registered with: each has an
    exact list of what it granted, and asking for anything else fails after the patient has signed in."""
    offered = _scope_types(scopes_supported)
    if offered is None:
        return requested
    kept = [s for s in requested.split() if not (m := _RESOURCE_SCOPE.match(s)) or m.group(2) in offered]
    return " ".join(_smart2_form(s, scopes_supported or []) if own_registration else s for s in kept)


def _smart2_form(scope: str, offered: list[str]) -> str:
    if not _RESOURCE_SCOPE.match(scope) or scope in offered:
        return scope
    stem = scope.rsplit(".", 1)[0]
    can_read = any(o.startswith(f"{stem}.") and re.fullmatch(r"c?r?u?d?s?", o.rsplit(".", 1)[1]) and "r" in o.rsplit(".", 1)[1] for o in offered)
    return f"{stem}.rs" if can_read else scope


def make_state(kind: str, origin: str) -> str:
    """Opaque-but-routable state: the public relay reads ``d`` to find this instance, and ``k``, a release build's
    relay key, to know it serves this sign-in."""
    payload = {"p": kind, "n": security.random_token(18), "d": origin}
    if key := config.relay_key():
        payload["k"] = key
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_state(state: str) -> dict[str, Any]:
    try:
        return json.loads(base64.urlsafe_b64decode(state + "=" * (-len(state) % 4)))
    except (ValueError, TypeError):
        return {}


def authorization_url(authorization_endpoint: str, *, client_id: str, redirect_uri: str, scope: str,
                      state: str, aud: str, code_challenge: str) -> str:
    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "scope": scope,
        "state": state, "aud": aud.rstrip("/"), "code_challenge": code_challenge, "code_challenge_method": "S256",
    }
    sep = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{sep}{urlencode(params)}"


# Temporary (October 2026): Kaiser's fhir.kp.org sends every region but Washington to
# healthy.kaiserpermanente.org/<region>/Authentication/OAuth/Start, which answers 500 even without parameters, while
# the same path in lowercase reaches Kaiser's sign-in and completes. Remove once Kaiser fixes it; until then it turns
# itself off as soon as the capitalized page stops failing.
_KAISER_AUTHORIZE_HOST = "fhir.kp.org"
_KAISER_MYCHART_HOST = "healthy.kaiserpermanente.org"
_KAISER_BROKEN_PATH = re.compile(r"^/fhir[a-z]+/Authentication/OAuth/Start$")


async def kaiser_sign_in_url(url: str, transport: Optional[httpx.AsyncBaseTransport] = None) -> str:
    """Follows Kaiser's first redirect and lowercases the broken sign-in path; any other URL comes back unchanged."""
    if (urlparse(url).hostname or "").lower() != _KAISER_AUTHORIZE_HOST:
        return url
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": USER_AGENT}, transport=transport) as client:
            hop = await client.get(url)
            target = urlparse(hop.headers.get("location", ""))
            if not hop.is_redirect or (target.hostname or "").lower() != _KAISER_MYCHART_HOST \
                    or not _KAISER_BROKEN_PATH.match(target.path):
                return url
            probe = await client.get(f"https://{_KAISER_MYCHART_HOST}{target.path}")
            if probe.status_code < 500:
                return url
    except httpx.HTTPError as exc:
        log.info("Kaiser sign-in check failed (%s); using the normal address", exc)
        return url
    log.info("Kaiser's %s is failing; sending sign-in to the lowercase path", target.path)
    return target._replace(netloc=_KAISER_MYCHART_HOST, path=target.path.lower()).geturl()


def _patient_from_tokens(tokens: dict[str, Any]) -> Optional[str]:
    if tokens.get("patient"):
        return str(tokens["patient"])
    id_token = tokens.get("id_token")
    if isinstance(id_token, str) and "." in id_token:
        try:
            payload = id_token.split(".")[1] if id_token.count(".") >= 2 else id_token.split(".")[0]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if claims.get("patient"):
                return str(claims["patient"])
            fhir_user = claims.get("fhirUser") or ""
            if "Patient/" in fhir_user:
                return fhir_user.split("Patient/")[-1].split("/")[0]
        except (ValueError, TypeError, IndexError):
            return None
    return None


def _token_credentials(tokens: dict[str, Any], previous: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    creds = dict(previous or {})
    creds["access_token"] = tokens["access_token"]
    if tokens.get("refresh_token"):
        creds["refresh_token"] = tokens["refresh_token"]
    expires_in = tokens.get("expires_in")
    creds["expires_at"] = time.time() + float(expires_in) if expires_in else None
    if tokens.get("scope"):
        creds["scope"] = tokens["scope"]
    return creds


async def exchange_code(*, token_endpoint: str, code: str, redirect_uri: str, client_id: str,
                        code_verifier: str, simulated: bool = False) -> tuple[dict[str, Any], Optional[str]]:
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": client_id, "code_verifier": code_verifier}
    async with http_client(simulated) as client:
        try:
            resp = await client.post(token_endpoint, data=data, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise SmartError(f"Token endpoint unreachable: {exc}", "network") from exc
    if resp.status_code != 200:
        raise SmartError(f"Token exchange failed ({resp.status_code}): {_error_text(resp)}", "auth")
    tokens = _json_object(resp)
    if not tokens.get("access_token"):
        raise SmartError("Token response did not include an access token.", "auth")
    patient = _patient_from_tokens(tokens)
    return _token_credentials(tokens), patient


def can_refresh(credentials: dict[str, Any]) -> bool:
    return bool(credentials.get("dynamic_client") or credentials.get("refresh_token"))


async def refresh(credentials: dict[str, Any], simulated: bool = False) -> dict[str, Any]:
    if credentials.get("dynamic_client"):
        return await _refresh_with_assertion(credentials, simulated)
    if not credentials.get("refresh_token"):
        raise SmartError("Access expired and the source did not grant offline access. Reconnect to continue.", "auth")
    data = {"grant_type": "refresh_token", "refresh_token": credentials["refresh_token"],
            "client_id": credentials.get("client_id", "")}
    if credentials.get("scope"):
        data["scope"] = credentials["scope"]  # athenahealth requires it on a refresh; the others accept the granted scope
    async with http_client(simulated) as client:
        try:
            resp = await client.post(credentials["token_endpoint"], data=data, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise SmartError("Couldn't reach the source's sign-in service. Syntropy will try again.", "network") from exc
    if resp.status_code != 200:
        if temporary_refusal(resp):
            raise _token_trouble(resp)
        raise SmartError(f"Refreshing access failed ({resp.status_code}). Reconnect to continue.", "auth")
    return _renewed(resp, credentials)


# ---------------------------------------------------------------------------
# Dynamic client registration (Epic's persistent access for patient-run apps)
# ---------------------------------------------------------------------------
#
# Epic issues no refresh token to a public client. Instead, right after the first code
# exchange the app registers a key pair it generated (POST <oauth2>/register with the
# fresh access token, {"software_id": <app client id>, "jwks": ...}) and receives a
# client id for this install alone. New access tokens then come from the JWT bearer
# grant, signed with the private key, which never leaves this instance. Access lasts
# as long as the patient chose on the consent screen.

JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
ASSERTION_TTL = 300


def registration_endpoint(config: dict[str, Any], token_endpoint: str) -> str:
    """Epic doesn't advertise it; it sits next to the token endpoint (…/oauth2/register)."""
    if config.get("registration_endpoint"):
        return config["registration_endpoint"]
    base = token_endpoint.rstrip("/")
    return (base[: -len("/token")] if base.endswith("/token") else base) + "/register"


async def register_dynamic_client(*, registration_endpoint: str, access_token: str, software_id: str,
                                  simulated: bool = False) -> dict[str, Any]:
    """Registers a new key pair; returns the ``dynamic_client`` credential block to store (encrypted)."""
    private_pem, jwk = security.generate_rsa_signing_key()
    body = {"software_id": software_id, "jwks": {"keys": [jwk]}}
    async with http_client(simulated) as client:
        try:
            resp = await client.post(registration_endpoint, json=body,
                                     headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise SmartError(f"Registration endpoint unreachable: {exc}", "network") from exc
    if resp.status_code not in (200, 201):
        raise SmartError(f"Dynamic client registration failed ({resp.status_code}): {_error_text(resp)}", "error")
    try:
        client_id = resp.json().get("client_id")
    except ValueError:
        client_id = None
    if not client_id:
        raise SmartError("Dynamic client registration did not return a client ID.", "error")
    return {"client_id": str(client_id), "kid": jwk["kid"], "private_key": private_pem}


def client_assertion(dynamic: dict[str, Any], token_endpoint: str) -> str:
    now = int(time.time())
    claims = {"iss": dynamic["client_id"], "sub": dynamic["client_id"], "aud": token_endpoint,
              "jti": security.random_token(24), "iat": now, "nbf": now, "exp": now + ASSERTION_TTL}
    return security.sign_jwt_rs384(claims, dynamic["private_key"], dynamic["kid"])


async def _refresh_with_assertion(credentials: dict[str, Any], simulated: bool) -> dict[str, Any]:
    dynamic = credentials["dynamic_client"]
    data = {"grant_type": JWT_BEARER, "client_id": dynamic["client_id"],
            "assertion": client_assertion(dynamic, credentials["token_endpoint"])}
    async with http_client(simulated) as client:
        try:
            resp = await client.post(credentials["token_endpoint"], data=data, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise SmartError("Couldn't reach the source's sign-in service. Syntropy will try again.", "network") from exc
    if resp.status_code != 200:
        if temporary_refusal(resp):
            raise _token_trouble(resp)
        raise SmartError("The access you granted has ended or was revoked. Reconnect to continue.", "auth")
    return _renewed(resp, credentials)


def _token_trouble(resp: httpx.Response) -> SmartError:
    return SmartError(f"The source's sign-in service couldn't renew access just now ({resp.status_code}). Syntropy will try again.",
                      "network", retry_after=retry_after_seconds(resp))


def _renewed(resp: httpx.Response, credentials: dict[str, Any]) -> dict[str, Any]:
    tokens = _json_object(resp)
    if not tokens.get("access_token"):
        raise SmartError("The source's sign-in service sent an answer without a new access token. Syntropy will try again.", "network")
    return _token_credentials(tokens, credentials)


def _error_text(resp: httpx.Response) -> str:
    """What a vendor says went wrong, in OAuth's own fields. Anything else in the body could be from any server someone
    pointed this at, so it isn't repeated."""
    body = _json_object(resp)
    return str(body.get("error_description") or body.get("error") or "no details given")[:300]


# ---------------------------------------------------------------------------
# FHIR data access
# ---------------------------------------------------------------------------

MAX_MEDICATION_READS = 100
_FHIR_ID = re.compile(r"[A-Za-z0-9.\-]{1,64}")
MEDICATION_INTENTS = "proposal,plan,order,original-order,reflex-order,filler-order,instance-order,option"

# (resource type, list of alternative query parameter sets tried in order)
QUERY_PLAN: list[tuple[str, list[dict[str, str]]]] = [
    ("Observation", [{"category": "vital-signs"}]),
    ("Observation", [{"category": "laboratory"}]),
    ("Observation", [{"category": "social-history"}]),
    # Smoking status and screening questionnaires (Synthea-based sandboxes file smoking status here).
    ("Observation", [{"category": "survey"}]),
    ("Condition", [{}, {"category": "problem-list-item"}]),
    # athenahealth answers a search without intent 403 ("required parameter combinations [[patient,intent],[_id]]").
    ("MedicationRequest", [{"_include": "MedicationRequest:medication"}, {},
                           {"_include": "MedicationRequest:medication", "intent": MEDICATION_INTENTS}]),
    ("AllergyIntolerance", [{}, {"clinical-status": "active"}]),
    ("Immunization", [{}]),
    ("Encounter", [{}]),
    ("Procedure", [{}]),
    ("DiagnosticReport", [{}]),
    ("DocumentReference", [{}]),
    ("CareTeam", [{}, {"status": "active"}]),
    ("CarePlan", [{}, {"category": "assess-plan"}]),
    ("Goal", [{}]),
    ("Device", [{}]),
    ("Coverage", [{}, {"beneficiary": "{patient}"}]),
    # Inside the Observation scope already asked for; see OPTIONAL_QUERIES for why a refusal here doesn't matter.
    ("Observation", [{"category": "exam"}]),
    ("Observation", [{"category": "imaging"}]),
    ("Observation", [{"category": "procedure"}]),
    ("Observation", [{"category": "activity"}]),
    ("Observation", [{"category": "therapy"}]),
    ("Observation", [{"category": "sdoh"}]),
    ("Observation", [{"category": "functional-status"}]),
]

# Added after vendors registered Syntropy's API lists, and some of them may refuse these: while one has never answered,
# its refusal or failure says nothing is wrong with the source, so it isn't reported.
OPTIONAL_QUERIES = {f"Observation?category={c}" for c in ("exam", "imaging", "procedure", "activity", "therapy", "sdoh", "functional-status")}
# Types that can be asked for newest first. A server that refuses is asked again without it, and that's remembered.
NEWEST_FIRST = {"Observation", "DiagnosticReport", "DocumentReference", "Encounter", "Procedure"}
# A search on one of these only covers part of a type: a record it didn't return may simply not match it.
_NARROWING = {"category": "category", "clinical-status": "clinicalStatus", "status": "status"}

QUERY_NAMES = {
    "Patient": "Name and birth date", "Observation?category=vital-signs": "Vital signs", "Observation?category=laboratory": "Lab results",
    "Observation?category=social-history": "Social history", "Observation?category=survey": "Screening questionnaires",
    "Observation?category=exam": "Exam findings", "Observation?category=imaging": "Imaging results",
    "Observation?category=procedure": "Procedure results", "Observation?category=activity": "Activity results",
    "Observation?category=therapy": "Therapy results", "Observation?category=sdoh": "Social needs",
    "Observation?category=functional-status": "Functional status", "Condition": "Conditions", "MedicationRequest": "Medications",
    "AllergyIntolerance": "Allergies", "Immunization": "Immunizations", "Encounter": "Visits", "Procedure": "Procedures",
    "DiagnosticReport": "Diagnostic reports", "DocumentReference": "Clinical notes", "CareTeam": "Care team", "CarePlan": "Care plans",
    "Goal": "Goals", "Device": "Implants and devices", "Coverage": "Insurance",
}


def _origin(url: str) -> tuple[str, Optional[str], Optional[int]]:
    parts = urlparse(url)
    scheme = parts.scheme.lower()
    return scheme, parts.hostname, parts.port or {"https": 443, "http": 80}.get(scheme)


def _same_server(a: str, b: str) -> bool:
    """Host names ignore case: Kaiser's paging links say FHIR.KP.ORG for a server listed as fhir.kp.org."""
    try:
        return _origin(a) == _origin(b)
    except ValueError:      # a port that can't exist isn't this server
        return False


def _codes(value: Any) -> set[str]:
    codes: set[str] = set()
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, str):
            codes.add(item)
        elif isinstance(item, dict):
            codes.update(c["code"] for c in item.get("coding") or [] if isinstance(c, dict) and c.get("code"))
    return codes


def covers(resource: dict[str, Any], params: dict[str, str]) -> bool:
    """Would a search with these parameters have returned this resource, if the source still held it?"""
    return all(params[key] in _codes(resource.get(field)) for key, field in _NARROWING.items() if key in params)


@dataclass
class QueryRun:
    """One query of the plan during one sync: which alternative it's on, how far it got and how it ended."""
    rtype: str
    label: str
    alternatives: list[dict[str, str]]
    alternative: int = 0
    next_url: Optional[str] = None      # the page to carry on from
    resumed: bool = False               # this walk began in an earlier sync, so this sync didn't see all of it
    no_sort: bool = False
    status: str = "paused"
    count: int = 0
    answered: Optional[dict[str, str]] = None       # the alternative that answered
    got_records: bool = False
    failed: Optional[str] = None
    refused: Optional[str] = None
    suppressed: bool = False            # the server says it left something out
    asked: bool = False                 # it had a turn in this sync
    last_failure: Optional[str] = None  # how it ended the last time it was asked, if that was a failure
    last_asked: Optional[float] = None
    ever_answered: bool = False
    place_failures: int = 0             # syncs in a row that next_url has failed
    swept_at: Optional[float] = None    # too long for one sync: when it was last read whole, 0 until the first time
    top_up: bool = False                # read the newest pages and no more
    topped_up: bool = False             # and there was more than those

    @property
    def question(self) -> dict[str, str]:
        return self.alternatives[0]

    @property
    def complete(self) -> bool:
        return self.status in ("ok", "empty") and not self.resumed and not self.suppressed and self.answered is not None

    @property
    def failing_for_the_person(self) -> bool:
        """An extra kind of result a source has never given may simply be something it doesn't offer."""
        return self.status.startswith("error:") and (self.ever_answered or self.label not in OPTIONAL_QUERIES)


@dataclass
class FetchResult:
    patient: Optional[dict[str, Any]] = None
    resources: list[dict[str, Any]] = field(default_factory=list)
    status: dict[str, str] = field(default_factory=dict)      # "Observation?category=laboratory" -> ok|paused|forbidden|...
    counts: dict[str, int] = field(default_factory=dict)
    queries: list[QueryRun] = field(default_factory=list)
    patient_failed: bool = False        # a server that can't serve the patient isn't trusted to say what was removed
    stopped: Optional[SmartError] = None        # why the sync ended early; what came before is still good
    come_back_at: Optional[float] = None

    def prune_verdict(self, resource: dict[str, Any]) -> str:
        """What a stored resource the source didn't return this time means. 'keep': nothing complete asked about it.
        'hold': the source was asked and answered with nothing at all, which proves nothing. 'prune': it answered in
        full, with records, and this wasn't among them."""
        rtype = resource.get("resourceType")
        asked = [q for q in self.queries if q.rtype == rtype and covers(resource, q.question)]
        if self.patient_failed or self.stopped or not asked \
                or not all(q.complete and covers(resource, q.answered or {}) for q in asked):
            return "keep"
        return "prune" if all(q.status == "ok" for q in asked) else "hold"

    @property
    def more_to_fetch(self) -> bool:
        """The next sync has work to carry on with, so it shouldn't wait for the usual interval."""
        return self.patient_failed or any(q.status == "paused" or (q.next_url and q.status != "recent") or q.failing_for_the_person
                                          for q in self.queries)

    def state(self) -> dict[str, dict[str, Any]]:
        """What the next sync needs to know about each query."""
        return {q.label: {"status": q.status, "record_count": q.count, "next_url": q.next_url, "alternative": q.alternative,
                          "no_sort": q.no_sort, "place_failures": q.place_failures, "swept_at": q.swept_at,
                          "asked_at": None if q.asked else q.last_asked} for q in self.queries}


@dataclass
class _Search:
    resources: list[dict[str, Any]]
    status: str
    next_url: Optional[str] = None      # where this search can carry on: it paused, or a later page failed
    suppressed: bool = False


def _outcome_verdict(outcome: dict[str, Any]) -> tuple[bool, bool]:
    """(failed, left something out). An ordinary empty search can carry a warning (Epic's 4101), which means neither."""
    failed = suppressed = False
    issues = outcome.get("issue") or []
    if not isinstance(issues, list):
        return True, False
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if str(issue.get("severity") or "").lower() in ("error", "fatal"):
            failed = True
        if str(issue.get("code") or "").lower() in ("suppressed", "incomplete", "too-costly", "throttled"):
            suppressed = True
    return failed, suppressed


def _page_link(current: str, link: Any) -> str:
    """Where a next link goes. Raises ValueError or httpx.InvalidURL for one that can't be followed."""
    if not isinstance(link, str):
        raise ValueError("A next link has to be text.")
    url = urljoin(current, link)
    _origin(url)
    httpx.URL(url)
    return url


class FhirClient:
    def __init__(self, base_url: str, credentials: dict[str, Any], patient_id: str, simulated: bool = False,
                 on_refresh: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None):
        self.base = base_url.rstrip("/")
        self.credentials = credentials
        self.patient_id = patient_id
        self.simulated = simulated
        self.on_refresh = on_refresh    # called with every renewed set of tokens, before anything can lose them
        self.deadline: Optional[float] = None
        self.come_back_at: Optional[float] = None       # the server asked for a longer pause than a sync should sit through
        self.fetched = 0
        self._down_in_a_row = 0
        # Some portals (athenahealth) let the patient approve only some record types; don't query the rest.
        self.granted = _scope_types(credentials.get("scope"))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credentials['access_token']}",
                "Accept": "application/fhir+json, application/json"}

    def time_left(self) -> float:
        return float("inf") if self.deadline is None else self.deadline - time.monotonic()

    def _must_stop(self) -> bool:
        return self.time_left() <= 0 or self.come_back_at is not None or self.fetched >= MAX_RESOURCES_PER_SYNC \
            or self._down_in_a_row >= OUTAGE_QUERIES

    async def _get_with_retries(self, client: httpx.AsyncClient, url: str, params: Optional[dict[str, str]],
                                headers: Optional[dict[str, str]]) -> httpx.Response:
        """Reading is safe to repeat: a busy or briefly unreachable server gets a few tries, on its own terms when it
        says when. A pause longer than a sync should sit through ends this sync's work instead (see come_back_at)."""
        for attempt in range(RETRY_ATTEMPTS):
            last = attempt == RETRY_ATTEMPTS - 1
            wait = 2 ** attempt + random.random() / 2
            try:
                resp = await client.get(url, params=params, headers={**self._headers(), **(headers or {})})
            except httpx.TransportError:
                if last or self.time_left() <= wait:
                    raise
            else:
                if resp.status_code not in RETRY_STATUSES:
                    return resp
                asked = retry_after_seconds(resp)
                if asked is not None and asked > RETRY_AFTER_CAP:
                    self.come_back_at = max(self.come_back_at or 0, time.time() + asked)
                    return resp
                wait = wait if asked is None else asked
                if last or self.time_left() <= wait:
                    return resp
            await _sleep(wait)
        raise AssertionError("unreachable")

    async def _get(self, client: httpx.AsyncClient, url: str, params: Optional[dict[str, str]] = None,
                   headers: Optional[dict[str, str]] = None) -> httpx.Response:
        # A long chart can outlast several tokens (Oracle's live under ten minutes), so every expiry may renew. A token
        # refused straight after renewing stops it here: there's nothing more a renewal could do.
        resp = await self._get_with_retries(client, url, params, headers)
        if resp.status_code == 401 and can_refresh(self.credentials):
            self.credentials = await refresh(self.credentials, self.simulated)
            if self.on_refresh:
                await self.on_refresh(self.credentials)
            resp = await self._get_with_retries(client, url, params, headers)
        if resp.status_code == 401:
            raise SmartError("The source rejected our access token. Reconnect to continue.", "auth")
        return resp

    async def read(self, client: httpx.AsyncClient, path: str) -> Optional[dict[str, Any]]:
        url = path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"
        resp = await self._get(client, url)
        if resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    async def read_binary_text(self, client: Any, url: str, max_bytes: int = 512_000) -> Optional[tuple[str, bytes]]:
        target = url if url.startswith("http") else f"{self.base}/{url.lstrip('/')}"
        if not self.simulated and not _same_server(target, self.base):
            return None         # an attachment elsewhere never gets this source's access token (as with paging links)
        try:
            resp = await self._get(client, target, headers={"Accept": "application/fhir+json, text/*"})
        except BlockedAddress as exc:
            log.warning("A note attachment led somewhere it can't go: %s", exc)
            return None
        if resp.status_code != 200 or len(resp.content) > max_bytes:
            return None
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            try:
                body = resp.json()
                if isinstance(body, dict) and body.get("resourceType") == "Binary" and body.get("data"):
                    return body.get("contentType") or "text/plain", base64.b64decode(body["data"])
            except (ValueError, TypeError):
                pass
            return None
        return ctype, resp.content

    async def _search(self, client: httpx.AsyncClient, rtype: str, params: dict[str, str], *, start_url: Optional[str] = None,
                      max_pages: Optional[int] = None) -> _Search:
        """Walks a search's pages, keeping what arrived whatever happens later. No page limit of its own: the caller
        says how far this turn goes, and the time budget ends the rest."""
        query = {k: (v.replace("{patient}", self.patient_id)) for k, v in params.items()}
        if "beneficiary" not in query:
            query["patient"] = self.patient_id
        query["_count"] = str(PAGE_SIZE)
        url: Optional[str] = start_url or f"{self.base}/{rtype}"
        current: Optional[dict[str, str]] = None if start_url else query
        results: list[dict[str, Any]] = []
        walked: set[str] = set()
        suppressed = False
        pages = 0
        while url:
            place = None if current is not None else url        # the first page needs no saving: it's just a new start
            if self._must_stop() or (max_pages and pages >= max_pages):
                return _Search(results, "paused", place, suppressed)
            try:
                resp = await self._get(client, url, current)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                log.warning("FHIR %s query failed: %s", rtype, type(exc).__name__)
                return _Search(results, "error:network", place, suppressed)
            if resp.status_code == 403:
                return _Search(results, "forbidden", None, suppressed)
            if resp.status_code in (400, 404, 405, 410, 422, 501):
                return _Search(results, "unsupported", None, suppressed)
            if resp.status_code >= 400:
                return _Search(results, f"error:{resp.status_code}", place, suppressed)
            try:
                bundle = resp.json()
            except (ValueError, RecursionError):
                return _Search(results, "error:invalid-json", place, suppressed)
            kind = bundle.get("resourceType") if isinstance(bundle, dict) else None
            if kind == "OperationOutcome":
                failed, left_out = _outcome_verdict(bundle)
                if failed:
                    return _Search(results, "error:outcome", place, suppressed)
                return _Search(results, "ok" if results else "empty", None, suppressed or left_out)
            if kind != "Bundle":            # a gateway's own JSON isn't an answer, and certainly not an empty one
                return _Search(results, "error:invalid-json", place, suppressed)
            entries, links = bundle.get("entry") or [], bundle.get("link") or []
            if not isinstance(entries, list) or not isinstance(links, list):
                return _Search(results, "error:invalid-json", place, suppressed)
            failed = False
            for entry in entries:
                res = entry.get("resource") if isinstance(entry, dict) else None
                if not isinstance(res, dict) or not res.get("resourceType") or not isinstance(res["resourceType"], str):
                    continue
                if res["resourceType"] == "OperationOutcome":
                    bad, left_out = _outcome_verdict(res)
                    failed, suppressed = failed or bad, suppressed or left_out
                else:
                    results.append(res)
                    self.fetched += 1
            if failed:
                return _Search(results, "error:outcome", place, suppressed)
            nxt = next((l.get("url") for l in links if isinstance(l, dict) and l.get("relation") == "next"), None)
            if nxt:
                try:
                    nxt = _page_link(str(resp.url), nxt)
                except (ValueError, httpx.InvalidURL):
                    return _Search(results, "error:invalid-json", place, suppressed)
                # Never follow paging links to a different host with our bearer token, and never round in circles.
                if (not self.simulated and not _same_server(nxt, self.base)) or nxt in walked:
                    return _Search(results, "truncated", None, suppressed)
                walked.add(nxt)
            url, current = nxt, None
            pages += 1
        return _Search(results, "ok" if results else "empty", None, suppressed)

    async def search_all(self, client: httpx.AsyncClient, rtype: str, params: dict[str, str]) -> tuple[list[dict[str, Any]], str]:
        found = await self._search(client, rtype, params)
        return found.resources, found.status

    @staticmethod
    def _keep(resources: list[dict[str, Any]], run: QueryRun, out: FetchResult, seen: set[str]) -> None:
        for res in resources:
            key = f"{res['resourceType']}/{res.get('id')}"
            if key not in seen:
                seen.add(key)
                out.resources.append(res)
                if res["resourceType"] == run.rtype:
                    run.count += 1

    async def _advance(self, client: httpx.AsyncClient, run: QueryRun, out: FetchResult, seen: set[str]) -> bool:
        """One turn for a query: up to ROUND_PAGES more pages. True when it has nothing further to do in this sync."""
        run.asked = True
        while run.alternative < len(run.alternatives):
            params = run.alternatives[run.alternative]
            sorting = run.rtype in NEWEST_FIRST and not run.no_sort
            start = run.next_url
            looking = run.top_up and sorting and not start
            fetched = self.fetched
            found = await self._search(client, run.rtype, {**params, "_sort": "-date"} if sorting else params,
                                       start_url=start, max_pages=TOP_UP_PAGES if looking else ROUND_PAGES)
            self._keep(found.resources, run, out, seen)
            if looking:
                self.fetched = fetched      # a small look: it doesn't come out of what whole histories share
                found.next_url = None
                if found.status == "paused":
                    run.answered, run.got_records, run.topped_up = params, True, True
                    return True
            if start and found.status in ("forbidden", "unsupported"):
                run.next_url, run.resumed = None, False      # the saved place has expired: start this query over
                continue
            if start and not (found.status == "paused" and found.next_url == start):
                # A cursor can die with a 500 as easily as a 410, and being asked again won't bring it back. An outage
                # isn't its fault, though, and the place is worth keeping through one.
                stuck = found.next_url == start
                run.place_failures = run.place_failures + (0 if out.patient_failed else 1) if stuck else 0
                if run.place_failures >= PLACE_TRIES:
                    log.info("Starting %s over: its saved place keeps failing", run.label)
                    run.next_url, run.resumed, run.place_failures = None, False, 0
                    continue
            if sorting and not start and found.status not in ("ok", "paused") and not found.resources:
                # It may be the sort it refused, or quietly answered nothing to. Whatever the plain question gets is the
                # answer, and says which it was. Only that is remembered: a failure or a busy moment says nothing about sorting.
                with_sort = found.status
                found = await self._search(client, run.rtype, params, max_pages=ROUND_PAGES)
                self._keep(found.resources, run, out, seen)
                run.no_sort = (found.status in ("ok", "paused", "empty") and with_sort in ("forbidden", "unsupported")) \
                    or (found.status in ("ok", "paused") and with_sort == "empty")
            run.next_url = found.next_url
            run.suppressed = run.suppressed or found.suppressed
            if found.status == "paused":
                return False
            if found.status == "ok":
                run.answered, run.got_records = params, True
                return True
            if found.status == "empty":
                run.answered = run.answered if run.answered is not None else params
            elif found.status in ("forbidden", "unsupported"):
                run.refused = run.refused or found.status
            else:
                run.failed = run.failed or found.status
                if run.next_url:
                    return True      # the next sync carries on from this page
            run.alternative += 1
        run.alternative = 0
        return True

    @staticmethod
    def _settle(run: QueryRun, finished: bool) -> None:
        # An error stays the answer even if a narrower alternative then worked: those records are kept, but nothing may
        # be pruned against them. A later alternative's refusal doesn't undo an earlier answer.
        if run.failed:
            run.status = run.failed
        elif run.topped_up:
            run.status = "recent"
        elif not finished:
            run.status = (run.last_failure if not run.asked else None) or ("recent" if run.swept_at else "paused")
        elif run.answered is not None:
            run.status = "ok" if run.got_records else "empty"
        else:
            run.status = run.refused or "unsupported"

    @staticmethod
    def _note_length(run: QueryRun, out_of_budget: bool) -> None:
        """A history that doesn't fit in one sync would never finish if every sync began it again. Once it has been read
        whole, later syncs look at the newest pages instead, and it's read whole again now and then. A sweep over several
        syncs can't say what's gone, so these never have anything removed: keeping a record is the safe way to be wrong."""
        if run.status == "paused" and run.next_url and out_of_budget and run.swept_at is None:
            run.swept_at = 0.0
        elif run.status in ("ok", "empty") and run.answered is not None:
            if not run.resumed:
                run.swept_at = None     # it fits
            elif run.swept_at is not None:
                run.swept_at = time.time()

    async def _read_patient(self, client: httpx.AsyncClient, out: FetchResult) -> None:
        """Declining demographics, or a server having a bad moment, doesn't cost the rest of the record. A patient id
        that no longer resolves does: searches for it may well come back empty, and prove nothing."""
        if self.granted is not None and "Patient" not in self.granted:
            out.status["Patient"] = "not-granted"
            return
        try:
            resp = await self._get(client, f"{self.base}/Patient/{self.patient_id}")
        except httpx.HTTPError:
            out.status["Patient"], out.patient_failed = "error:network", True
            return
        if resp.status_code == 403:
            out.status["Patient"] = "not-granted"       # servers that don't say which scopes the patient approved
            return
        if resp.status_code == 429 or resp.status_code >= 500:
            out.status["Patient"], out.patient_failed = f"error:{resp.status_code}", True
            return
        patient = _json_object(resp) if resp.status_code == 200 else {}
        if patient.get("resourceType") != "Patient":
            raise SmartError("Could not read the patient's demographics from the source.", "error")
        out.patient = patient
        out.status["Patient"] = "ok"

    def _plan(self, saved: dict[str, dict[str, Any]], out: FetchResult) -> list[QueryRun]:
        waiting: list[QueryRun] = []
        for rtype, alternatives in QUERY_PLAN:
            shown = {k: v for k, v in alternatives[0].items() if not k.startswith("_") and "{" not in v}
            label = rtype + ("?" + "&".join(f"{k}={v}" for k, v in shown.items()) if shown else "")
            run = QueryRun(rtype, label, alternatives)
            out.queries.append(run)
            if self.granted is not None and rtype not in self.granted:
                run.status = "not-granted"
                continue
            state = saved.get(label) or {}
            run.no_sort = bool(state.get("no_sort"))
            run.ever_answered = bool(state.get("ever_answered"))
            run.last_asked = state.get("updated_at")
            run.swept_at = state.get("swept_at")
            if str(state.get("status")).startswith("error:"):
                run.last_failure = state["status"]
            url = state.get("next_url")
            if url and (self.simulated or _same_server(url, self.base)):
                run.next_url, run.resumed = url, True
                run.alternative = min(int(state.get("alternative") or 0), len(alternatives) - 1)
                run.place_failures = int(state.get("place_failures") or 0)
            elif run.swept_at and time.time() - run.swept_at < FULL_READ_EVERY and rtype in NEWEST_FIRST and not run.no_sort:
                run.top_up = True
            waiting.append(run)
        waiting.sort(key=lambda q: self._turn(q, saved.get(q.label) or {}))
        return waiting

    @staticmethod
    def _turn(run: QueryRun, state: dict[str, Any]) -> tuple[int, float]:
        """Queries the last sync didn't reach go first, so a big chart can't keep them waiting. Those that failed go last,
        the one left waiting longest first, so a run of them stopping a sync can't keep the others, or each other, away.
        Reading a long history again waits for everything else: it can use up a whole sync."""
        status = str(state.get("status"))
        if status.startswith("error:"):
            return 3, state.get("updated_at") or 0.0
        if status == "paused":
            return 0, 0.0
        return (2, 0.0) if run.swept_at and not run.top_up else (1, 0.0)

    async def _newest_page(self, client: httpx.AsyncClient, run: QueryRun, out: FetchResult, seen: set[str]) -> None:
        """Carrying on with old pages, what's new mustn't wait for them to finish."""
        params = run.alternatives[run.alternative]
        if run.rtype in NEWEST_FIRST and not run.no_sort:
            params = {**params, "_sort": "-date"}
        self._keep((await self._search(client, run.rtype, params, max_pages=1)).resources, run, out, seen)

    def _note_answer(self, run: QueryRun) -> None:
        """An extra category failing says nothing about the server: vendors may well refuse those. It doesn't count either way."""
        if run.failed and run.label in OPTIONAL_QUERIES:
            return
        down = run.failed and not run.count and run.failed != "error:invalid-json"
        self._down_in_a_row = self._down_in_a_row + 1 if down else 0

    async def fetch_all(self, saved: Optional[dict[str, dict[str, Any]]] = None) -> FetchResult:
        """Everything the plan asks for, as far as this sync's time allows. Queries take turns, so every record type
        arrives before any one of them goes deep. ``saved`` is what the last sync left (see FetchResult.state)."""
        out = FetchResult()
        self.deadline = time.monotonic() + SYNC_TIME_BUDGET
        seen: set[str] = set()
        finished: set[str] = set()
        async with http_client(self.simulated, timeout=45.0) as client:
            await self._read_patient(client, out)
            waiting = self._plan(saved or {}, out)
            turn = list(waiting)
            try:
                for run in waiting:
                    if run.resumed:
                        await self._newest_page(client, run, out, seen)
                while turn and not self._must_stop():
                    for run in list(turn):
                        if await self._advance(client, run, out, seen):
                            finished.add(run.label)
                            turn.remove(run)
                            self._note_answer(run)
                        if self._must_stop():
                            break
            except SmartError as exc:
                out.stopped = exc
        out_of_budget = self.time_left() <= 0 or self.fetched >= MAX_RESOURCES_PER_SYNC
        for run in waiting:
            self._settle(run, run.label in finished)
            self._note_length(run, out_of_budget)
        for run in out.queries:
            out.status[run.label], out.counts[run.label] = run.status, run.count
        out.come_back_at = self.come_back_at
        statuses = [q.status for q in waiting]
        if not out.stopped and not out.resources and (out.patient_failed or statuses) \
                and any(s.startswith("error:") for s in statuses + [out.status.get("Patient", "")]) \
                and all(s.startswith("error:") or s in ("paused", "recent") for s in statuses):
            out.stopped = SmartError("The source isn't answering right now. Syntropy will try again.", "network",
                                     retry_after=(self.come_back_at - time.time()) if self.come_back_at else None)
            if not out.stopped:
                await self._read_missing_medications(client, out)
        return out

    async def _read_missing_medications(self, client: httpx.AsyncClient, out: FetchResult) -> None:
        """A server may ignore _include and send a medication as a bare reference, leaving nothing to name it by. Read those few
        directly. A failure only leaves that medication unnamed; it's never part of whether a query completed."""
        if self.granted is not None and "Medication" not in self.granted:
            return
        have = {str(r.get("id")) for r in out.resources if r.get("resourceType") == "Medication"}
        wanted: list[str] = []
        for res in out.resources:
            reference = res.get("medicationReference")
            if res.get("resourceType") not in ("MedicationRequest", "MedicationStatement") or not isinstance(reference, dict) or reference.get("display"):
                continue
            ref = reference.get("reference") or ""
            # This request carries the access token, so only ever a Medication id on this server, never a URL the data supplied.
            if ref.startswith("http") and not _same_server(ref, self.base):
                continue
            kind, _, mid = ref.rpartition("/")
            if kind.rsplit("/", 1)[-1] == "Medication" and _FHIR_ID.fullmatch(mid) and mid not in have and mid not in wanted:
                wanted.append(mid)
        for mid in wanted[:MAX_MEDICATION_READS]:
            try:
                med = await self.read(client, f"Medication/{mid}")
            except (httpx.HTTPError, SmartError) as exc:
                log.warning("Couldn't read a referenced Medication: %s", type(exc).__name__)
                return
            if med and med.get("resourceType") == "Medication":
                out.resources.append(med)
