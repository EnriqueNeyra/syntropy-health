"""
Built-in SMART on FHIR EHR simulator.

Until Syntropy Health is registered with each EHR vendor, connections run against
this simulator by default. It implements the same protocol surface a real
patient-access endpoint exposes, so the production code path is exercised end to end:

* ``GET  /sim/fhir/{platform}/{tenant}/r4/.well-known/smart-configuration``
* ``GET  /sim/fhir/{platform}/{tenant}/r4/metadata``          (CapabilityStatement)
* ``GET  /sim/oauth/authorize``                               sign-in + consent pages
* ``POST /sim/oauth/token``                                   authorization_code (PKCE S256), refresh_token & jwt-bearer
* ``POST /sim/oauth/register``                                Epic-style dynamic client registration
* ``GET  /sim/fhir/{platform}/{tenant}/r4/{Type}[/{id}]``     paged searchset bundles, scope-enforced

Tokens are HMAC-signed and self-contained, so the simulator is stateless apart from
replay caches for authorization codes and client assertions. Like Epic, Epic tenants
issue no refresh token to a public client; the app registers a key pair instead, and
the dynamic client id it receives is itself a signed token carrying that public key. All data is synthetic (see ``personas``).
"""

from __future__ import annotations

import html
import json
import time
from datetime import date
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app import directory
from app.core import security
from app.simulator import generator
from app.simulator.personas import PERSONAS, SIM_PASSWORD, authenticate

router = APIRouter(prefix="/sim", include_in_schema=False)

CODE_TTL = 300
ACCESS_TTL = 3600
REFRESH_TTL = 90 * 24 * 3600
ASSERTION_MAX_TTL = 300
_used_codes: dict[str, float] = {}
_used_assertions: dict[str, float] = {}

SEARCH_PARAMS_PATIENT = ("patient", "subject", "beneficiary")
SUPPORTED_TYPES = (
    "Patient", "Observation", "Condition", "MedicationRequest", "Medication", "AllergyIntolerance", "Immunization",
    "Encounter", "Procedure", "DiagnosticReport", "DocumentReference", "Binary", "CareTeam", "CarePlan", "Goal",
    "Device", "Coverage",
)


def fhir_base_path(platform: str, tenant: str) -> str:
    return f"/sim/fhir/{platform}/{tenant}/r4"


def tenant_name(tenant: str) -> str:
    inst = directory.get_institution(tenant)
    if inst:
        return inst["name"]
    return tenant.replace("-", " ").title()


def _outcome(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": code, "diagnostics": message}]},
        status_code=status, media_type="application/fhir+json",
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@router.get("/fhir/{platform}/{tenant}/r4/.well-known/smart-configuration")
async def smart_configuration(platform: str, tenant: str, request: Request) -> JSONResponse:
    origin = str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": f"{origin}{fhir_base_path(platform, tenant)}",
        "authorization_endpoint": f"{origin}/sim/oauth/authorize",
        "token_endpoint": f"{origin}/sim/oauth/token",
        "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
        "grant_types_supported": ["authorization_code", "refresh_token", "urn:ietf:params:oauth:grant-type:jwt-bearer"],
        "scopes_supported": ["openid", "fhirUser", "launch/patient", "offline_access", "patient/*.read"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "capabilities": ["launch-standalone", "client-public", "context-standalone-patient",
                         "permission-patient", "permission-offline", "sso-openid-connect"],
    })


@router.get("/fhir/{platform}/{tenant}/r4/metadata")
async def capability_statement(platform: str, tenant: str, request: Request) -> JSONResponse:
    origin = str(request.base_url).rstrip("/")
    return JSONResponse({
        "resourceType": "CapabilityStatement", "status": "active", "kind": "instance", "fhirVersion": "4.0.1",
        "format": ["json"], "software": {"name": "Syntropy EHR Simulator", "version": "1.0"},
        "implementation": {"description": f"{tenant_name(tenant)} (simulated)",
                           "url": f"{origin}{fhir_base_path(platform, tenant)}"},
        "rest": [{
            "mode": "server",
            "security": {"extension": [{
                "url": "http://fhir-registry.smarthealthit.org/StructureDefinition/oauth-uris",
                "extension": [{"url": "authorize", "valueUri": f"{origin}/sim/oauth/authorize"},
                              {"url": "token", "valueUri": f"{origin}/sim/oauth/token"}],
            }]},
            "resource": [{"type": t, "interaction": [{"code": "read"}, {"code": "search-type"}]} for t in SUPPORTED_TYPES],
        }],
    }, media_type="application/fhir+json")


# ---------------------------------------------------------------------------
# Authorization (browser-facing)
# ---------------------------------------------------------------------------

def _parse_aud(aud: str) -> Optional[tuple[str, str]]:
    path = urlparse(aud).path.rstrip("/")
    parts = path.split("/")
    try:
        i = parts.index("fhir")
        if parts[i - 1] == "sim" and parts[i + 3] == "r4":
            return parts[i + 1], parts[i + 2]
    except (ValueError, IndexError):
        return None
    return None


AUTH_PARAMS = ("response_type", "client_id", "redirect_uri", "scope", "state", "aud", "code_challenge", "code_challenge_method")


def _redirect_error(redirect_uri: str, state: str, error: str, desc: str) -> RedirectResponse:
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode({'error': error, 'error_description': desc, 'state': state})}", 302)


def _validate_request(params: dict[str, str]) -> Optional[str]:
    if params.get("response_type") != "code":
        return "response_type must be 'code'."
    if not params.get("client_id"):
        return "client_id is required."
    if not params.get("redirect_uri", "").startswith(("http://", "https://")):
        return "redirect_uri must be an absolute http(s) URL."
    if not params.get("state"):
        return "state is required."
    if not _parse_aud(params.get("aud", "")):
        return "aud must be the FHIR base URL of a simulated institution."
    if params.get("code_challenge_method") != "S256" or not params.get("code_challenge"):
        return "PKCE with code_challenge_method=S256 is required."
    return None


@router.get("/oauth/authorize", response_class=HTMLResponse)
async def authorize_page(request: Request) -> Response:
    params = {k: request.query_params.get(k, "") for k in AUTH_PARAMS}
    problem = _validate_request(params)
    if problem:
        return HTMLResponse(_page("Invalid authorization request", f"<p class='err'>{html.escape(problem)}</p>"), status_code=400)
    platform, tenant = _parse_aud(params["aud"])  # type: ignore[misc]
    return HTMLResponse(_login_page(params, platform, tenant))


@router.post("/oauth/authorize", response_class=HTMLResponse)
async def authorize_submit(
    request: Request,
    step: str = Form(...),
    username: str = Form(""),
    password: str = Form(""),
    ticket: str = Form(""),
    decision: str = Form(""),
) -> Response:
    form = await request.form()
    params = {k: str(form.get(k, "")) for k in AUTH_PARAMS}
    problem = _validate_request(params)
    if problem:
        return HTMLResponse(_page("Invalid authorization request", f"<p class='err'>{html.escape(problem)}</p>"), status_code=400)
    platform, tenant = _parse_aud(params["aud"])  # type: ignore[misc]

    if step == "login":
        persona = authenticate(username, password)
        if not persona:
            return HTMLResponse(_login_page(params, platform, tenant, error="Incorrect username or password."), status_code=401)
        ticket = security.sign({"persona": persona.key, "exp": time.time() + 600}, "sim-login")
        return HTMLResponse(_consent_page(params, platform, tenant, persona.key, ticket))

    if step == "consent":
        login = security.unsign(ticket, "sim-login")
        if not login:
            return HTMLResponse(_login_page(params, platform, tenant, error="Your session expired. Sign in again."), status_code=401)
        if decision != "allow":
            return _redirect_error(params["redirect_uri"], params["state"], "access_denied", "The patient declined to share records.")
        code = security.sign({
            "typ": "code", "persona": login["persona"], "platform": platform, "tenant": tenant,
            "cid": params["client_id"], "ruri": params["redirect_uri"], "cc": params["code_challenge"],
            "scope": params["scope"], "n": security.random_token(8), "exp": time.time() + CODE_TTL,
        }, "sim-code")
        sep = "&" if "?" in params["redirect_uri"] else "?"
        return RedirectResponse(f"{params['redirect_uri']}{sep}{urlencode({'code': code, 'state': params['state']})}", 302)

    return HTMLResponse(_page("Invalid request", "<p class='err'>Unknown step.</p>"), status_code=400)


# ---------------------------------------------------------------------------
# Token endpoint (back channel)
# ---------------------------------------------------------------------------

def _token_error(error: str, desc: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": desc}, status_code=status)


def _issue_tokens(claims: dict[str, Any], origin: str) -> dict[str, Any]:
    now = time.time()
    persona = claims["persona"]
    patient = generator.patient_id_for(persona, claims["tenant"])
    base = {"persona": persona, "platform": claims["platform"], "tenant": claims["tenant"], "scope": claims["scope"],
            "cid": claims["cid"]}
    access = security.sign({**base, "typ": "access", "exp": now + ACCESS_TTL}, "sim-token")
    body: dict[str, Any] = {
        "access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
        "scope": claims["scope"], "patient": patient,
    }
    offline = "offline_access" in claims["scope"] or "online_access" in claims["scope"]
    if offline and claims["platform"] != "epic":
        body["refresh_token"] = security.sign({**base, "typ": "refresh", "exp": now + REFRESH_TTL}, "sim-token")
    if "openid" in claims["scope"]:
        fhir_user = f"{origin}{fhir_base_path(claims['platform'], claims['tenant'])}/Patient/{patient}"
        body["id_token"] = security.sign({"iss": f"{origin}{fhir_base_path(claims['platform'], claims['tenant'])}",
                                          "sub": patient, "aud": claims["cid"], "fhirUser": fhir_user,
                                          "iat": int(now), "exp": int(now + ACCESS_TTL)}, "sim-id")
    return body


@router.post("/oauth/token")
async def token(request: Request) -> JSONResponse:
    form = await request.form()
    grant = form.get("grant_type")
    origin = str(request.base_url).rstrip("/")
    if grant == "authorization_code":
        code = str(form.get("code", ""))
        claims = security.unsign(code, "sim-code")
        if not claims or claims.get("typ") != "code":
            return _token_error("invalid_grant", "Authorization code is invalid or expired.")
        now = time.time()
        for k, exp in list(_used_codes.items()):
            if exp < now:
                _used_codes.pop(k, None)
        if code in _used_codes:
            return _token_error("invalid_grant", "Authorization code was already used.")
        if form.get("redirect_uri") != claims["ruri"]:
            return _token_error("invalid_grant", "redirect_uri does not match the authorization request.")
        if form.get("client_id") and form.get("client_id") != claims["cid"]:
            return _token_error("invalid_client", "client_id does not match the authorization request.")
        verifier = str(form.get("code_verifier", ""))
        if not verifier or security.pkce_challenge(verifier) != claims["cc"]:
            return _token_error("invalid_grant", "PKCE verification failed.")
        _used_codes[code] = claims["exp"]
        return JSONResponse(_issue_tokens(claims, origin), headers={"Cache-Control": "no-store"})
    if grant == "refresh_token":
        claims = security.unsign(str(form.get("refresh_token", "")), "sim-token")
        if not claims or claims.get("typ") != "refresh":
            return _token_error("invalid_grant", "Refresh token is invalid or expired.")
        if not form.get("scope"):
            return _token_error("invalid_request", "scope is required with a refresh token.")  # as athenahealth's is
        return JSONResponse(_issue_tokens(claims, origin), headers={"Cache-Control": "no-store"})
    if grant == "urn:ietf:params:oauth:grant-type:jwt-bearer":
        claims, error = _verify_assertion(str(form.get("assertion", "")), f"{origin}/sim/oauth/token")
        if error:
            return _token_error("invalid_client", error)
        return JSONResponse(_issue_tokens(claims, origin), headers={"Cache-Control": "no-store"})
    return _token_error("unsupported_grant_type", f"grant_type '{grant}' is not supported.")


def _verify_assertion(assertion: str, token_endpoint: str) -> tuple[dict[str, Any], Optional[str]]:
    """Checks a dynamic client's signed JWT the way Epic does; returns the grant it stands for."""
    try:
        unverified = json.loads(security._b64d(assertion.split(".")[1]))
    except (ValueError, IndexError):
        return {}, "Client assertion is not a JWT."
    client = security.unsign(str(unverified.get("iss", "")), "sim-dyn")
    if not client or client.get("typ") != "dynamic":
        return {}, "Unknown or expired dynamic client."
    claims = security.verify_jwt_rs384(assertion, client["jwk"])
    if claims is None:
        return {}, "Client assertion signature is invalid."
    now = time.time()
    if claims.get("sub") != claims.get("iss") or claims.get("aud") != token_endpoint:
        return {}, "Client assertion iss/sub/aud are wrong."
    exp = float(claims.get("exp", 0))
    if not now < exp <= now + ASSERTION_MAX_TTL + 30 or not claims.get("jti"):
        return {}, "Client assertion is expired, too long-lived or has no jti."
    for k, e in list(_used_assertions.items()):
        if e < now:
            _used_assertions.pop(k, None)
    if claims["jti"] in _used_assertions:
        return {}, "Client assertion was already used."
    _used_assertions[claims["jti"]] = exp
    return client["grant"], None


@router.post("/oauth/register")
async def register(request: Request) -> JSONResponse:
    auth = request.headers.get("authorization", "")
    access = security.unsign(auth[7:], "sim-token") if auth.lower().startswith("bearer ") else None
    if not access or access.get("typ") != "access":
        return _token_error("invalid_token", "A valid access token from the patient's sign-in is required.", 401)
    try:
        body = await request.json()
        keys = body["jwks"]["keys"]
        jwk = {k: str(keys[0][k]) for k in ("kty", "n", "e", "kid")}
    except (ValueError, KeyError, IndexError, TypeError):
        return _token_error("invalid_client_metadata", "Expected software_id and a jwks with one RSA key.")
    if jwk["kty"] != "RSA":
        return _token_error("invalid_client_metadata", "Only RSA keys are accepted.")
    if body.get("software_id") != access["cid"]:
        return _token_error("invalid_client_metadata", "software_id must be the client ID that signed the patient in.")
    grant = {k: access[k] for k in ("persona", "platform", "tenant", "scope", "cid")}
    client_id = security.sign({"typ": "dynamic", "jwk": jwk, "grant": grant, "exp": time.time() + REFRESH_TTL}, "sim-dyn")
    return JSONResponse({"client_id": client_id, "software_id": access["cid"], "jwks": {"keys": [jwk]},
                         "grant_types": ["urn:ietf:params:oauth:grant-type:jwt-bearer"],
                         "token_endpoint_auth_method": "private_key_jwt"}, status_code=201)


# ---------------------------------------------------------------------------
# FHIR API
# ---------------------------------------------------------------------------

def _authorize_api(request: Request, platform: str, tenant: str, rtype: str) -> tuple[Optional[dict[str, Any]], Optional[JSONResponse]]:
    authz = request.headers.get("authorization", "")
    if not authz.lower().startswith("bearer "):
        return None, _outcome(401, "login", "Missing bearer token.")
    claims = security.unsign(authz[7:].strip(), "sim-token")
    if not claims or claims.get("typ") != "access":
        return None, _outcome(401, "expired", "Access token is invalid or expired.")
    if claims["platform"] != platform or claims["tenant"] != tenant:
        return None, _outcome(403, "forbidden", "Token was not issued for this FHIR server.")
    scopes = claims.get("scope", "").split()
    allowed = any(s in scopes for s in (f"patient/{rtype}.read", f"patient/{rtype}.rs", "patient/*.read", "patient/*.rs"))
    if not allowed:
        return None, _outcome(403, "forbidden", f"Scope does not permit reading {rtype}.")
    return claims, None


def _chart(claims: dict[str, Any]) -> list[dict[str, Any]]:
    return generator.build_chart(claims["persona"], claims["tenant"], tenant_name(claims["tenant"]), claims["platform"])


def _patient_of(res: dict[str, Any]) -> Optional[str]:
    for key in ("subject", "patient", "beneficiary"):
        ref = res.get(key)
        if isinstance(ref, dict) and ref.get("reference", "").startswith("Patient/"):
            return ref["reference"].split("/", 1)[1]
    if res["resourceType"] == "Patient":
        return res["id"]
    return None


def _matches_category(res: dict[str, Any], category: str) -> bool:
    wanted = set(category.split(","))
    for cat in res.get("category", []):
        if isinstance(cat, dict):
            for c in cat.get("coding", []):
                if c.get("code") in wanted:
                    return True
        elif cat in wanted:
            return True
    return False


@router.get("/fhir/{platform}/{tenant}/r4/{rtype}/{rid}")
async def fhir_read(platform: str, tenant: str, rtype: str, rid: str, request: Request) -> JSONResponse:
    if rtype not in SUPPORTED_TYPES:
        return _outcome(404, "not-supported", f"Resource type {rtype} is not supported.")
    claims, err = _authorize_api(request, platform, tenant, rtype)
    if err:
        return err
    patient = generator.patient_id_for(claims["persona"], tenant)  # type: ignore[index]
    for res in _chart(claims):  # type: ignore[arg-type]
        if res["resourceType"] == rtype and res["id"] == rid:
            owner = _patient_of(res)
            if owner not in (None, patient):
                break
            return JSONResponse(res, media_type="application/fhir+json")
    return _outcome(404, "not-found", f"{rtype}/{rid} was not found.")


@router.get("/fhir/{platform}/{tenant}/r4/{rtype}")
async def fhir_search(platform: str, tenant: str, rtype: str, request: Request) -> JSONResponse:
    if rtype not in SUPPORTED_TYPES:
        return _outcome(404, "not-supported", f"Resource type {rtype} is not supported.")
    claims, err = _authorize_api(request, platform, tenant, rtype)
    if err:
        return err
    qp = request.query_params
    patient = generator.patient_id_for(claims["persona"], tenant)  # type: ignore[index]
    requested = next((qp.get(k) for k in SEARCH_PARAMS_PATIENT if qp.get(k)), None)
    if rtype != "Patient" and not requested:
        return _outcome(400, "required", "A patient search parameter is required.")
    if platform == "athena" and rtype == "MedicationRequest" and not qp.get("intent"):  # as athenahealth's is
        return _outcome(403, "forbidden", "One of the required parameter combinations [[patient,intent],[_id]] was not provided.")
    if requested:
        requested = requested.split("/")[-1]
        if requested != patient:
            return _outcome(403, "forbidden", "Token is not authorized for this patient.")

    chart = _chart(claims)  # type: ignore[arg-type]
    matches = [r for r in chart if r["resourceType"] == rtype and (_patient_of(r) in (patient, None))]
    if rtype == "Patient":
        matches = [r for r in matches if r["id"] == patient]
    if qp.get("category"):
        matches = [r for r in matches if _matches_category(r, qp["category"])]
    if qp.get("code"):
        codes = {c.split("|")[-1] for c in qp["code"].split(",")}
        matches = [r for r in matches if any(c.get("code") in codes for c in r.get("code", {}).get("coding", []))]
    if rtype == "Condition" and qp.get("clinical-status"):
        wanted = set(qp["clinical-status"].split(","))
        matches = [r for r in matches if r["clinicalStatus"]["coding"][0]["code"] in wanted]

    count = max(1, min(int(qp.get("_count", 50)), 200))
    offset = max(0, int(qp.get("_offset", 0)))
    page = matches[offset:offset + count]
    base = str(request.url).split("?")[0]
    entries = [{"fullUrl": f"{base}/{r['id']}", "resource": r, "search": {"mode": "match"}} for r in page]

    if rtype == "MedicationRequest" and "MedicationRequest:medication" in qp.getlist("_include"):
        by_id = {r["id"]: r for r in chart if r["resourceType"] == "Medication"}
        seen = set()
        for r in page:
            ref = r.get("medicationReference", {}).get("reference", "")
            mid = ref.split("/", 1)[1] if ref.startswith("Medication/") else None
            if mid and mid in by_id and mid not in seen:
                seen.add(mid)
                entries.append({"fullUrl": f"{base.rsplit('/', 1)[0]}/Medication/{mid}", "resource": by_id[mid],
                                "search": {"mode": "include"}})

    links = [{"relation": "self", "url": str(request.url)}]
    if offset + count < len(matches):
        params = [(k, v) for k, v in qp.multi_items() if k != "_offset"] + [("_offset", str(offset + count))]
        links.append({"relation": "next", "url": f"{base}?{urlencode(params)}"})
    return JSONResponse({
        "resourceType": "Bundle", "type": "searchset", "total": len(matches), "link": links, "entry": entries,
    }, media_type="application/fhir+json")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

_CSS = """
*{box-sizing:border-box}body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:#f4f5f7;color:#1f2330;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px}
.card{background:#fff;border:1px solid #dfe2e8;border-radius:14px;max-width:460px;width:100%;padding:28px;
box-shadow:0 10px 30px rgba(20,30,60,.08)}.banner{background:#fff7e6;border:1px solid #f3d48b;color:#6b4e00;
border-radius:10px;padding:10px 12px;font-size:12.5px;margin-bottom:18px}.inst{font-size:12px;color:#667;
text-transform:uppercase;letter-spacing:.06em}h1{font-size:21px;margin:4px 0 2px}.sub{color:#667;font-size:13px;margin:0 0 18px}
label{display:block;font-size:13px;font-weight:600;margin:12px 0 4px}input[type=text],input[type=password]{width:100%;
padding:10px 12px;border:1px solid #cfd4dc;border-radius:9px;font-size:15px}button{border:0;border-radius:9px;
padding:11px 14px;font-size:15px;font-weight:600;cursor:pointer;width:100%;margin-top:16px}.primary{background:#2f5bd3;color:#fff}
.secondary{background:#eef0f4;color:#1f2330}.err{color:#b42318;background:#fef3f2;border:1px solid #fecdca;padding:8px 10px;
border-radius:8px;font-size:13px}.personas{display:grid;gap:8px;margin-top:6px}.persona{border:1px solid #dfe2e8;border-radius:10px;
padding:9px 11px;cursor:pointer;text-align:left;background:#fafbfc;width:100%;margin:0;font-weight:400}
.persona:hover{border-color:#2f5bd3}.persona b{display:block;font-size:14px}.persona span{font-size:12px;color:#667}
.hint{font-size:12px;color:#667;margin-top:14px}ul{padding-left:18px;margin:8px 0}li{font-size:13.5px;margin:3px 0}
.row{display:flex;gap:10px}.row button{flex:1}.foot{font-size:11.5px;color:#889;margin-top:18px;border-top:1px solid #eee;padding-top:12px}
"""


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<meta name='referrer' content='no-referrer'><title>{html.escape(title)}</title><style>{_CSS}</style></head>"
            f"<body><main class='card'>{body}</main></body></html>")


def _hidden(params: dict[str, str]) -> str:
    return "".join(f"<input type='hidden' name='{k}' value='{html.escape(v, quote=True)}'>" for k, v in params.items())


def _header(platform: str, tenant: str) -> str:
    meta = directory.PLATFORMS.get(platform, {})
    portal = meta.get("portal", "Patient Portal")
    return (
        "<div class='banner'><b>Simulated sign-in.</b> Syntropy Health is not yet registered with "
        f"{html.escape(meta.get('label', platform))}, so this built-in simulator stands in for the "
        f"{html.escape(portal)} sign-in page. All records are synthetic.</div>"
        f"<div class='inst'>{html.escape(meta.get('label', platform))} · {html.escape(portal)}</div>"
        f"<h1>{html.escape(tenant_name(tenant))}</h1>"
    )


def _age(birth_date: str) -> int:
    born, today = date.fromisoformat(birth_date), date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _login_page(params: dict[str, str], platform: str, tenant: str, error: str = "") -> str:
    personas = "".join(
        f"<button type='button' class='persona' onclick=\"document.getElementById('u').value='{p.username}';"
        f"document.getElementById('p').value='{SIM_PASSWORD}';\"><b>{html.escape(p.full_name)}</b>"
        f"<span>{html.escape(p.username)} · {_age(p.birth_date)} · {html.escape(p.summary)}</span></button>"
        for p in PERSONAS.values()
    )
    body = (
        _header(platform, tenant)
        + "<p class='sub'>Sign in to share your health records with Syntropy Health.</p>"
        + (f"<p class='err'>{html.escape(error)}</p>" if error else "")
        + "<form method='post' action='/sim/oauth/authorize' autocomplete='off'>"
        + _hidden(params) + "<input type='hidden' name='step' value='login'>"
        + "<label for='u'>Username</label><input id='u' name='username' type='text' required>"
        + "<label for='p'>Password</label><input id='p' name='password' type='password' required>"
        + "<button class='primary' type='submit'>Sign in</button></form>"
        + f"<p class='hint'>Test patients (password <code>{SIM_PASSWORD}</code>) — tap to fill:</p>"
        + f"<div class='personas'>{personas}</div>"
        + "<p class='foot'>Protocol: SMART App Launch 2.0 · OAuth 2.0 authorization code + PKCE (S256)</p>"
    )
    return _page(f"Sign in — {tenant_name(tenant)}", body)


SCOPE_LABELS = {
    "Patient": "Demographics", "Observation": "Lab results and vital signs", "Condition": "Health conditions",
    "MedicationRequest": "Medications", "AllergyIntolerance": "Allergies", "Immunization": "Immunizations",
    "Encounter": "Visits", "Procedure": "Procedures", "DiagnosticReport": "Diagnostic reports",
    "DocumentReference": "Clinical notes", "CareTeam": "Care team", "CarePlan": "Care plans", "Goal": "Goals",
    "Device": "Implants and devices", "Coverage": "Insurance",
}


def _consent_page(params: dict[str, str], platform: str, tenant: str, persona_key: str, ticket: str) -> str:
    scopes = params["scope"].split()
    items: list[str] = []
    if any(s.startswith("patient/*") for s in scopes):
        items = list(SCOPE_LABELS.values())
    else:
        for s in scopes:
            if s.startswith("patient/"):
                label = SCOPE_LABELS.get(s.split("/", 1)[1].split(".")[0])
                if label and label not in items:
                    items.append(label)
    offline = "offline_access" in scopes
    persona = PERSONAS[persona_key]
    body = (
        _header(platform, tenant)
        + f"<p class='sub'>Signed in as <b>{html.escape(persona.full_name)}</b></p>"
        + f"<p><b>Syntropy Health</b> (client <code>{html.escape(params['client_id'])}</code>) is requesting read-only access to:</p>"
        + "<ul>" + "".join(f"<li>{html.escape(i)}</li>" for i in items) + "</ul>"
        + ("<p class='hint'>Access continues until you revoke it (offline access).</p>" if offline else
           "<p class='hint'>Access ends when you close the app.</p>")
        + "<form method='post' action='/sim/oauth/authorize'>" + _hidden(params)
        + "<input type='hidden' name='step' value='consent'>"
        + f"<input type='hidden' name='ticket' value='{html.escape(ticket, quote=True)}'>"
        + "<div class='row'><button class='secondary' name='decision' value='deny'>Deny</button>"
        + "<button class='primary' name='decision' value='allow'>Allow</button></div></form>"
    )
    return _page(f"Allow access — {tenant_name(tenant)}", body)


# A standalone ASGI app used for in-process back-channel calls (token + FHIR API),
# so simulated connections never need network access or a reachable port.
_asgi_app: Optional[FastAPI] = None


def asgi_app() -> FastAPI:
    global _asgi_app
    if _asgi_app is None:
        _asgi_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        _asgi_app.include_router(router)
    return _asgi_app
