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

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

import httpx

from app.core import security

log = logging.getLogger("syntropy.smart")

MAX_PAGES = 25
PAGE_SIZE = 100
USER_AGENT = "SyntropyHealth/1.0 (+https://health.syntropylabs.io)"


class SmartError(Exception):
    """A recoverable SMART/FHIR failure with a user-facing message."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind          # 'auth' (re-authorization needed), 'network', 'error'


def http_client(simulated: bool = False, timeout: float = 30.0) -> httpx.AsyncClient:
    headers = {"User-Agent": USER_AGENT}
    if simulated:
        from app.simulator.server import asgi_app
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app()), timeout=timeout, headers=headers)
    return httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

async def discover(fhir_base_url: str, simulated: bool = False) -> dict[str, Any]:
    base = fhir_base_url.rstrip("/")
    accept = {"Accept": "application/json, application/fhir+json"}
    async with http_client(simulated, timeout=15.0) as client:
        try:
            resp = await client.get(f"{base}/.well-known/smart-configuration", headers=accept)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("authorization_endpoint") and data.get("token_endpoint"):
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
            raise SmartError(f"Could not reach FHIR server at {base}: {exc}", "network") from exc
        except ValueError as exc:
            raise SmartError(f"FHIR server at {base} returned invalid metadata.", "error") from exc
    for rest in meta.get("rest", []):
        for ext in (rest.get("security") or {}).get("extension", []):
            if ext.get("url", "").endswith("oauth-uris"):
                uris = {e.get("url"): e.get("valueUri") for e in ext.get("extension", [])}
                if uris.get("authorize") and uris.get("token"):
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


def fit_scopes(requested: str, scopes_supported: Optional[list[str]]) -> str:
    """Drops resource scopes for types the server doesn't offer (the VA has no CarePlan, Oracle no Medication), since
    some authorization servers reject the whole request over one unknown scope. Servers that don't list resource
    scopes (Epic lists none) get the request unchanged."""
    offered = _scope_types(scopes_supported)
    if offered is None:
        return requested
    return " ".join(s for s in requested.split() if not (m := _RESOURCE_SCOPE.match(s)) or m.group(2) in offered)


def make_state(kind: str, origin: str) -> str:
    """Opaque-but-routable state: the public relay reads ``d`` to find this instance."""
    payload = {"p": kind, "n": security.random_token(18), "d": origin}
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
    tokens = resp.json()
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
    async with http_client(simulated) as client:
        try:
            resp = await client.post(credentials["token_endpoint"], data=data, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise SmartError(f"Token endpoint unreachable: {exc}", "network") from exc
    if resp.status_code != 200:
        raise SmartError(f"Refreshing access failed ({resp.status_code}). Reconnect to continue.", "auth")
    return _token_credentials(resp.json(), credentials)


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
            raise SmartError(f"Token endpoint unreachable: {exc}", "network") from exc
    if resp.status_code != 200:
        raise SmartError("The access you granted has ended or was revoked. Reconnect to continue.", "auth")
    tokens = resp.json()
    if not tokens.get("access_token"):
        raise SmartError("Token response did not include an access token.", "auth")
    return _token_credentials(tokens, credentials)


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return body.get("error_description") or body.get("error") or resp.text[:300]
    except ValueError:
        return resp.text[:300]


# ---------------------------------------------------------------------------
# FHIR data access
# ---------------------------------------------------------------------------

# (resource type, list of alternative query parameter sets tried in order)
QUERY_PLAN: list[tuple[str, list[dict[str, str]]]] = [
    ("Observation", [{"category": "vital-signs"}]),
    ("Observation", [{"category": "laboratory"}]),
    ("Observation", [{"category": "social-history"}]),
    # Smoking status and screening questionnaires (Synthea-based sandboxes file smoking status here).
    ("Observation", [{"category": "survey"}]),
    ("Condition", [{}, {"category": "problem-list-item"}]),
    ("MedicationRequest", [{"_include": "MedicationRequest:medication"}, {}]),
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
]


@dataclass
class FetchResult:
    patient: Optional[dict[str, Any]] = None
    resources: list[dict[str, Any]] = field(default_factory=list)
    status: dict[str, str] = field(default_factory=dict)      # "Observation?category=laboratory" -> ok|truncated|forbidden|...
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def complete_types(self) -> set[str]:
        """Resource types whose every query completed (safe to prune against)."""
        per_type: dict[str, list[str]] = {}
        for key, st in self.status.items():
            per_type.setdefault(key.split("?")[0], []).append(st)
        return {t for t, sts in per_type.items() if all(s in ("ok", "empty") for s in sts)}


class FhirClient:
    def __init__(self, base_url: str, credentials: dict[str, Any], patient_id: str, simulated: bool = False):
        self.base = base_url.rstrip("/")
        self.credentials = credentials
        self.patient_id = patient_id
        self.simulated = simulated
        self.refreshed = False
        # Some portals (athenahealth) let the patient approve only some record types; don't query the rest.
        self.granted = _scope_types(credentials.get("scope"))

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credentials['access_token']}",
                "Accept": "application/fhir+json, application/json"}

    async def _get(self, client: httpx.AsyncClient, url: str, params: Optional[dict[str, str]] = None) -> httpx.Response:
        resp = await client.get(url, params=params, headers=self._headers())
        if resp.status_code == 401 and not self.refreshed and can_refresh(self.credentials):
            self.credentials = await refresh(self.credentials, self.simulated)
            self.refreshed = True
            resp = await client.get(url, params=params, headers=self._headers())
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

    async def read_binary_text(self, client: httpx.AsyncClient, url: str, max_bytes: int = 512_000) -> Optional[tuple[str, bytes]]:
        target = url if url.startswith("http") else f"{self.base}/{url.lstrip('/')}"
        resp = await client.get(target, headers={**self._headers(), "Accept": "application/fhir+json, text/*"})
        if resp.status_code != 200 or len(resp.content) > max_bytes:
            return None
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            try:
                body = resp.json()
            except ValueError:
                return None
            if body.get("resourceType") == "Binary" and body.get("data"):
                return body.get("contentType") or "text/plain", base64.b64decode(body["data"])
            return None
        return ctype, resp.content

    async def search_all(self, client: httpx.AsyncClient, rtype: str, params: dict[str, str]) -> tuple[list[dict[str, Any]], str]:
        query = {k: (v.replace("{patient}", self.patient_id)) for k, v in params.items()}
        if "beneficiary" not in query:
            query["patient"] = self.patient_id
        query["_count"] = str(PAGE_SIZE)
        url: Optional[str] = f"{self.base}/{rtype}"
        results: list[dict[str, Any]] = []
        pages = 0
        current: Optional[dict[str, str]] = query
        while url and pages < MAX_PAGES:
            resp = await self._get(client, url, current)
            if resp.status_code == 403:
                return results, "forbidden"
            if resp.status_code in (400, 404, 405, 422, 501):
                return results, "unsupported"
            if resp.status_code >= 400:
                return results, f"error:{resp.status_code}"
            try:
                bundle = resp.json()
            except ValueError:
                return results, "error:invalid-json"
            for entry in bundle.get("entry") or []:
                res = entry.get("resource")
                if isinstance(res, dict) and res.get("resourceType") and res["resourceType"] != "OperationOutcome":
                    results.append(res)
            url = next((l.get("url") for l in bundle.get("link") or [] if l.get("relation") == "next"), None)
            current = None
            pages += 1
            if url and not self.simulated and urlparse(url).netloc and urlparse(url).netloc != urlparse(self.base).netloc:
                # Never follow paging links to a different host with our bearer token.
                return results, "truncated"
        return results, ("truncated" if url else ("ok" if results else "empty"))

    async def fetch_all(self) -> FetchResult:
        out = FetchResult()
        async with http_client(self.simulated, timeout=45.0) as client:
            patient = await self.read(client, f"Patient/{self.patient_id}")
            if not patient or patient.get("resourceType") != "Patient":
                raise SmartError("Could not read the patient's demographics from the source.", "error")
            out.patient = patient
            seen: set[str] = set()
            for rtype, alternatives in QUERY_PLAN:
                shown = {k: v for k, v in alternatives[0].items() if not k.startswith("_") and "{" not in v}
                label = rtype + ("?" + "&".join(f"{k}={v}" for k, v in shown.items()) if shown else "")
                if self.granted is not None and rtype not in self.granted:
                    out.status[label], out.counts[label] = "not-granted", 0
                    continue
                status = "unsupported"
                for params in alternatives:
                    try:
                        resources, status = await self.search_all(client, rtype, params)
                    except SmartError:
                        raise
                    except httpx.HTTPError as exc:
                        log.warning("FHIR %s query failed: %s", rtype, exc)
                        resources, status = [], "error:network"
                    if status in ("ok", "empty", "truncated", "forbidden") and (resources or status != "unsupported"):
                        for res in resources:
                            key = f"{res['resourceType']}/{res.get('id')}"
                            if key not in seen:
                                seen.add(key)
                                out.resources.append(res)
                        if status == "empty" and params is not alternatives[-1]:
                            continue
                        break
                out.status[label] = status
                out.counts[label] = sum(1 for r in out.resources if r["resourceType"] == rtype)
        return out
