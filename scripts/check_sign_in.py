"""
Checks that every Epic health system in the directory takes a patient to its MyChart sign-in.

    ./run.sh check-sign-in                     # every Epic health system
    ./run.sh check-sign-in --only kaiser       # names containing "kaiser"
    ./run.sh check-sign-in --json report.json  # also save every result

For each distinct FHIR server it does what the app does when someone connects: reads the SMART configuration, sends
Syntropy Health's production client ID and redirect address to the authorize endpoint (with the Kaiser workaround
applied, as in the app), and follows the redirects a browser would until a page loads. It stops at the sign-in page,
so nobody signs in and no records are read.

| Result | Meaning |
|---|---|
| ``ok`` | The health system's sign-in page loaded. |
| ``client_unknown`` | The authorize endpoint showed an error instead of redirecting: Epic hasn't delivered our client ID there yet. |
| ``rejected`` | The authorize endpoint sent an OAuth error back to our redirect address. |
| ``portal_error`` | A page on the way to sign-in failed (4xx or 5xx), the way Kaiser's did. A lowercase retry is noted. |
| ``error_page`` | A page loaded, but it is an error page. |
| ``unclear`` | A page loaded that is neither a sign-in nor a recognizable error. |
| ``blocked`` | The sign-in site refused the checker (403, or no connection). Bot protection usually does this to scripts \
while browsers get through, so open it in a browser before calling it broken. |
| ``unreachable`` | The FHIR server or its authorize endpoint didn't answer. The app's server makes these same requests, \
so the app can't connect either. |

Exits with status 1 when any health system isn't ``ok`` or ``blocked``.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.connectors import smart  # noqa: E402
from app.core import config, settings  # noqa: E402

EPIC_DIRECTORY = REPO_ROOT / "app" / "data" / "institutions" / "epic.json"
MAX_HOPS = 20
CONCURRENCY = 6
PER_HOST = 2
RETRY_DELAYS = (2.0, 6.0)      # a busy name resolver or a moment's 503 isn't the health system's fault

# A real browser runs these; following them is how Kaiser's pages reach sign-in.
_SCRIPT_REDIRECT = re.compile(r"""(?:(?:top|window|document)\.)?location(?:\.href)?\s*=\s*['"]([^'"]+)['"]"""
                              r"""|location\.replace\(\s*['"]([^'"]+)['"]""")
_META_REFRESH = re.compile(r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]+content=["'][^"']*url=([^"'>]+)""", re.I)
_NOSCRIPT = re.compile(r"<noscript\b.*?</noscript>", re.I | re.S)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_PASSWORD = re.compile(r"""<input[^>]+type=["']?password""", re.I)
_SIGN_IN_TITLE = ("sign in", "sign-in", "log in", "login", "mychart")
_SIGN_IN = ("sign in", "log in", "login", "username")
_ERROR = ("page not found", "needs some care", "oauth2 error", "invalid_client", "unauthorized_client",
          "unknown-client", "invalid client", "something went wrong", "an error has occurred", "error occurred")
_CLIENT_ERROR = ("oauth2 error", "invalid_client", "unauthorized_client", "unknown-client", "invalid client")


@dataclass
class Result:
    status: str
    detail: str
    fhir_base_url: str
    names: list[str] = field(default_factory=list)
    hops: list[str] = field(default_factory=list)       # "302 host/path" for each response on the way
    workaround: bool = False


def _title(text: str) -> str:
    match = _TITLE.search(text)
    return " ".join(html.unescape(match.group(1)).split())[:120] if match else ""


def _page_redirect(url: str, text: str) -> Optional[str]:
    """The next address a browser would load from a page that redirects by script or meta refresh."""
    head = _NOSCRIPT.sub("", text[:20000])      # a browser with scripts on ignores these fallbacks (Mercy's loop)
    for pattern in (_META_REFRESH, _SCRIPT_REDIRECT):
        match = pattern.search(head)
        if match:
            target = html.unescape(next(g for g in match.groups() if g)).strip()
            if target and not target.lower().startswith("javascript:"):
                return urljoin(url, target)
    return None


async def _get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    for delay in (*RETRY_DELAYS, None):
        try:
            resp = await client.get(url, **kwargs)
            if resp.status_code not in (502, 503, 504) or delay is None:
                return resp
        except httpx.TransportError:
            if delay is None:
                raise
        await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def _where(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.hostname}{parsed.path}"


async def _authorize_url(client: httpx.AsyncClient, base: str, client_id: str, redirect_uri: str) -> str:
    resp = await _get(client, f"{base}/.well-known/smart-configuration",
                            headers={"Accept": "application/json"}, follow_redirects=True)
    resp.raise_for_status()
    endpoint = resp.json().get("authorization_endpoint")
    if not endpoint:
        raise ValueError("no authorization_endpoint in the SMART configuration")
    return smart.authorization_url(endpoint, client_id=client_id, redirect_uri=redirect_uri,
                                   scope="openid fhirUser launch/patient patient/Patient.read", state="sign-in-check",
                                   aud=base, code_challenge="E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")


async def check_server(base: str, client_id: str, redirect_uri: str,
                       transport: Optional[httpx.AsyncBaseTransport] = None) -> Result:
    base = base.rstrip("/")
    result = Result("unreachable", "", base)
    async with httpx.AsyncClient(timeout=20.0, transport=transport, headers={"User-Agent": smart.USER_AGENT}) as client:
        try:
            first = await _authorize_url(client, base, client_id, redirect_uri)
        except (httpx.HTTPError, ValueError) as exc:
            result.detail = f"SMART configuration: {type(exc).__name__}: {exc}"[:300]
            return result
        url = await smart.kaiser_sign_in_url(first, transport)
        result.workaround = url != first
        authorize_host = urlparse(first).hostname
        for _ in range(MAX_HOPS):
            try:
                resp = await _get(client, url)
            except httpx.HTTPError as exc:
                if urlparse(url).hostname != authorize_host:
                    result.status = "blocked"
                result.detail = f"{_where(url)}: {type(exc).__name__}"
                return result
            result.hops.append(f"{resp.status_code} {_where(url)}")
            at_authorize = urlparse(url).hostname == authorize_host and len(result.hops) == 1
            if resp.is_redirect:
                target = urljoin(url, resp.headers.get("location", ""))
                if urlparse(target).hostname == urlparse(redirect_uri).hostname:
                    result.status, result.detail = "rejected", f"sent back to our redirect address: {target[:200]}"
                    return result
                url = target
                continue
            text = resp.text
            lower = text.lower()
            title = _title(text)
            if resp.status_code == 403 and urlparse(url).hostname != authorize_host:
                result.status, result.detail = "blocked", f"HTTP 403 at {_where(url)}" + (f" ({title})" if title else "")
                return result
            if resp.status_code >= 400:
                result.status = "client_unknown" if at_authorize and any(s in lower for s in _CLIENT_ERROR) else "portal_error"
                result.detail = f"HTTP {resp.status_code} at {_where(url)}" + (f" ({title})" if title else "")
                if result.status == "portal_error":
                    result.detail += await _lowercase_note(client, url)
                return result
            if at_authorize and any(s in lower for s in _CLIENT_ERROR):
                result.status, result.detail = "client_unknown", f"{_where(url)} answered with an error page ({title})"
                return result
            sign_in = _PASSWORD.search(text) or any(s in title.lower() for s in _SIGN_IN_TITLE)
            nxt = None if sign_in else _page_redirect(url, text)
            if nxt:
                url = nxt
                continue
            if sign_in:
                result.status, result.detail = "ok", f"{_where(url)}: {title}"
            elif any(s in lower for s in _ERROR):
                result.status, result.detail = "error_page", f"{_where(url)}: {title or 'error page'}"
            elif any(s in lower for s in _SIGN_IN):
                result.status, result.detail = "ok", f"{_where(url)}: {title}"
            else:
                result.status, result.detail = "unclear", f"{_where(url)}: {title or 'page without a title'}"
            return result
        result.status, result.detail = "portal_error", f"more than {MAX_HOPS} redirects, last at {_where(url)}"
        return result


async def _lowercase_note(client: httpx.AsyncClient, url: str) -> str:
    parsed = urlparse(url)
    if parsed.path == parsed.path.lower():
        return ""
    try:
        resp = await _get(client, parsed._replace(path=parsed.path.lower()).geturl())
    except httpx.HTTPError:
        return ""
    return f"; the lowercase path answers {resp.status_code}"


def epic_servers(only: Optional[str] = None) -> dict[str, list[str]]:
    """Distinct Epic FHIR servers and the names of the health systems on each."""
    data = json.loads(EPIC_DIRECTORY.read_text())
    entries = data if isinstance(data, list) else data.get("institutions", data)
    servers: dict[str, list[str]] = defaultdict(list)
    for inst in entries:
        if not inst.get("fhir_base_url") or (only and only.lower() not in inst["name"].lower()):
            continue
        servers[inst["fhir_base_url"].strip().rstrip("/")].append(inst["name"])
    return dict(servers)


async def check_all(servers: dict[str, list[str]], client_id: str, redirect_uri: str) -> list[Result]:
    overall = asyncio.Semaphore(CONCURRENCY)
    per_host: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(PER_HOST))
    done = 0

    async def one(base: str, names: list[str]) -> Result:
        nonlocal done
        async with overall, per_host[(urlparse(base).hostname or "").lower()]:
            try:
                result = await check_server(base, client_id, redirect_uri)
            except Exception as exc:  # noqa: BLE001 - one odd server shouldn't stop the run
                result = Result("unreachable", f"{type(exc).__name__}: {exc}"[:300], base)
        result.names = sorted(names)
        done += 1
        if done % 50 == 0 or done == len(servers):
            print(f"  {done}/{len(servers)} checked", file=sys.stderr)
        return result

    return list(await asyncio.gather(*(one(base, names) for base, names in servers.items())))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--only", help="check health systems whose name contains this text")
    parser.add_argument("--json", type=Path, help="write every result to this file")
    args = parser.parse_args(argv)

    client_id = settings.EHR_PLATFORMS["epic"]["default_client_ids"]["production"]
    servers = epic_servers(args.only)
    print(f"Checking {len(servers)} Epic FHIR servers …", file=sys.stderr)
    results = asyncio.run(check_all(servers, client_id, config.DEFAULT_RELAY_URL))

    counts = Counter(r.status for r in results)
    print("\n" + ", ".join(f"{n} {status}" for status, n in counts.most_common()))
    for r in sorted((r for r in results if r.status != "ok"), key=lambda r: (r.status, r.names[0])):
        names = r.names[0] + (f" (+{len(r.names) - 1} more)" if len(r.names) > 1 else "")
        print(f"\n[{r.status}] {names}\n  {r.fhir_base_url}\n  {r.detail}")
    if args.json:
        args.json.write_text(json.dumps([asdict(r) for r in results], indent=1))
    return 0 if counts["ok"] + counts["blocked"] == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
