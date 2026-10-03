"""
Checks that every Epic, Oracle Health and eClinicalWorks health system in the directory takes a patient to its sign-in.

    ./run.sh check-sign-in                     # every health system (eClinicalWorks's 17,000 practices take ~2.5 hours)
    ./run.sh check-sign-in --platform epic     # one vendor's: epic, cerner (Oracle Health) or healow (eClinicalWorks)
    ./run.sh check-sign-in --only kaiser       # names containing "kaiser"
    ./run.sh check-sign-in --json report.json  # also save every result
    ./run.sh check-sign-in --no-browser        # skip the Chrome pass
    ./run.sh check-sign-in --write-status      # record failures in app/data/sign_in_status.json (weekly job)

For each distinct FHIR server it does what the app does when someone connects: reads the SMART configuration, sends
Syntropy Health's production client ID for that vendor and redirect address to the authorize endpoint (with the Kaiser
workaround applied, as in the app), and follows the redirects a browser would until a page loads. It stops at the sign-in page,
so nobody signs in and no records are read.

Some sign-in sites turn away scripts (Mayo's answers 403, others drop the connection), and some only move on by
running JavaScript (Oracle Health's authorize page does). So every server that doesn't come out ``ok`` is opened again in headless Chrome, which settles it.
That needs Playwright (``pip install -e ".[checks]"``) and Google Chrome, or Playwright's own Chromium
(``playwright install chromium``); without them the first pass stands.

| Result | Meaning |
|---|---|
| ``ok`` | The health system's sign-in page loaded. |
| ``client_unknown`` | The authorize endpoint didn't recognize our client ID (at an Epic health system: Epic hasn't delivered it there yet). |
| ``rejected`` | The authorize endpoint sent an OAuth error back to our redirect address. |
| ``portal_error`` | A page on the way to sign-in failed (4xx or 5xx), the way Kaiser's did. A lowercase retry is noted. |
| ``error_page`` | A page loaded, but it is an error page. |
| ``unclear`` | A page loaded that is neither a sign-in nor a recognizable error. |
| ``blocked`` | The sign-in site refused the checker (403, or no connection), and Chrome couldn't confirm it either way. |
| ``unreachable`` | The FHIR server or its authorize endpoint didn't answer. The app's server makes these same requests, \
so the app can't connect either. |

``--write-status`` records the servers that failed in ``app/data/sign_in_status.json``, which the app reads to warn
before sending someone to a sign-in that won't work: each entry has the result, the date it was first seen failing
(``since``), and other listings of the same health system that work (``instead``, by name: UPMC → UPMC Patient
Portal). ``blocked`` and ``unclear`` servers aren't recorded, since nobody knows they're broken.

Exits with status 1 when any health system isn't ``ok``.
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
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.connectors import smart  # noqa: E402
from app.core import config, settings  # noqa: E402

DIRECTORIES = {platform: REPO_ROOT / "app" / "data" / "institutions" / f"{name}.json"
               for platform, name in (("epic", "epic"), ("cerner", "oracle"), ("healow", "ecw"))}
STATUS_FILE = REPO_ROOT / "app" / "data" / "sign_in_status.json"
RECORDED = ("portal_error", "error_page", "unreachable", "client_unknown", "rejected")
MAX_HOPS = 20
CONCURRENCY = 6
PER_HOST = 2
RETRY_DELAYS = (2.0, 6.0)      # a busy name resolver or a moment's 503 isn't the health system's fault

# A real browser runs these; following them is how Kaiser's pages reach sign-in.
_SCRIPT_REDIRECT = re.compile(r"""(?:(?:top|window|document)\.)?location(?:\.href)?\s*=\s*['"]([^'"]+)['"]"""
                              r"""|location\.replace\(\s*['"]([^'"]+)['"]""")
_META_REFRESH = re.compile(r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]+content=["'][^"']*url=([^"'>]+)""", re.I)
# Oracle Health's authorize page is a script that moves on to its session service, named in the page's properties.
_ORACLE_REDIRECT_PAGE = re.compile(r"""data-page-id=["']IdentityProviderRedirectPage["'][^>]*data-page-properties=["']([^"']+)""")
_NOSCRIPT = re.compile(r"<noscript\b.*?</noscript>", re.I | re.S)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_PASSWORD = re.compile(r"""<input[^>]+type=["']?password""", re.I)
_SIGN_IN_TITLE = ("sign in", "sign-in", "log in", "login", "mychart")
_SIGN_IN = ("sign in", "log in", "login", "username")
_ERROR = ("page not found", "needs some care", "oauth2 error", "invalid_client", "unauthorized_client",
          "unknown-client", "invalid client", "something went wrong", "an error has occurred", "error occurred",
          "invalid request")     # eClinicalWorks practices without patient access
_ERROR_TITLE = ("error", "not found", "access denied", "unavailable", "needs some care")
_CLIENT_ERROR = ("oauth2 error", "invalid_client", "unauthorized_client", "unknown-client", "invalid client")


@dataclass
class Result:
    status: str
    detail: str
    fhir_base_url: str
    names: list[str] = field(default_factory=list)
    hops: list[str] = field(default_factory=list)       # "302 host/path" for each response on the way
    workaround: bool = False
    start_url: str = ""                                 # the sign-in address the app hands the browser
    browser: str = ""                                   # what Chrome found, when it looked
    platform: str = ""


def _title(text: str) -> str:
    match = _TITLE.search(text)
    return " ".join(html.unescape(match.group(1)).split())[:120] if match else ""


def _looks_like_sign_in(title: str, has_password: bool) -> bool:
    lower = title.lower()
    return not any(s in lower for s in _ERROR_TITLE) and (has_password or any(s in lower for s in _SIGN_IN_TITLE))


def _page_redirect(url: str, text: str) -> Optional[str]:
    """The next address a browser would load from a page that redirects by script or meta refresh."""
    head = _NOSCRIPT.sub("", text[:20000])      # a browser with scripts on ignores these fallbacks (Mercy's loop)
    if match := _ORACLE_REDIRECT_PAGE.search(head):
        try:
            target = json.loads(html.unescape(match.group(1))).get("sessionServiceUri")
        except ValueError:
            target = None
        if target:
            return urljoin(url, target)
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
        result.workaround, result.start_url = url != first, url
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
                if at_authorize and "unknown-client" in target.lower():      # Oracle's answer to a client it doesn't know
                    result.status, result.detail = "client_unknown", f"{_where(url)} redirected to {_where(target)}"
                    return result
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
            sign_in = _looks_like_sign_in(title, bool(_PASSWORD.search(text)))
            nxt = None if sign_in else _page_redirect(url, text)
            if nxt:
                url = nxt
                continue
            if sign_in:
                result.status, result.detail = "ok", f"{_where(url)}: {title}"
            elif any(s in lower for s in _ERROR) or any(s in title.lower() for s in _ERROR_TITLE):
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


def classify_page(*, status: Optional[int], url: str, title: str, has_password: bool, text: str,
                  redirect_host: str) -> tuple[str, str]:
    """The result for the page a browser ended on."""
    where = _where(url)
    if urlparse(url).hostname == redirect_host:
        return "rejected", f"sent back to our redirect address: {url[:200]}"
    if _looks_like_sign_in(title, has_password):
        return "ok", f"{where}: {title}"
    if status and status >= 400:
        return ("blocked" if status == 403 else "portal_error"), f"HTTP {status} at {where}" + (f" ({title})" if title else "")
    if title.lower().startswith("loading"):       # Oracle Health's script page, caught before it moved on
        return "unclear", f"{where}: still loading ({title[:60]})"
    lower = text.lower()
    if any(s in lower for s in _ERROR) or any(s in title.lower() for s in _ERROR_TITLE):
        return "error_page", f"{where}: {title or 'error page'}"
    if any(s in lower for s in _SIGN_IN):
        return "ok", f"{where}: {title}"
    return "unclear", f"{where}: {title or 'page without a title'}"


async def _open_in_browser(browser: Any, user_agent: str, url: str, redirect_host: str) -> tuple[str, str]:
    context = await browser.new_context(user_agent=user_agent)
    try:
        page = await context.new_page()
        last: dict[str, Any] = {}

        def seen(response: Any) -> None:
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                last["status"] = response.status

        page.on("response", seen)
        page.on("requestfailed", lambda request: last.update(failed=request.url)
                if request.is_navigation_request() else None)
        try:
            await page.goto(url, wait_until="load", timeout=30000)
            await page.wait_for_timeout(2500)          # pages that move on by script
        except Exception as exc:  # noqa: BLE001 - Playwright raises its own error types
            message = str(exc)
            failed = last.get("failed") or url
            if "ERR_NAME_NOT_RESOLVED" in message:
                return "portal_error", f"{urlparse(failed).hostname} doesn't exist (no DNS record), reached from {_where(url)}"
            if urlparse(page.url).hostname == redirect_host:
                return "rejected", f"sent back to our redirect address: {page.url[:200]}"
            return "portal_error", f"{_where(failed)}: {message.splitlines()[0][:150]}"
        return classify_page(status=last.get("status"), url=page.url, title=(await page.title()).strip()[:120],
                             has_password=await page.locator("input[type=password]").count() > 0,
                             text=(await page.content())[:20000], redirect_host=redirect_host)
    finally:
        await context.close()


async def browser_pass(results: list[Result], redirect_uri: str) -> Optional[str]:
    """Opens each server that didn't come out ok in headless Chrome and keeps what it finds. Returns why it couldn't."""
    todo = [r for r in results if r.status not in ("ok", "unreachable", "client_unknown") and r.start_url]
    if not todo:
        return None
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return 'Playwright isn\'t installed (pip install -e ".[checks]"), so nothing was opened in Chrome'
    async with async_playwright() as p:
        browser = None
        for kwargs in ({"channel": "chrome"}, {}):
            try:
                browser = await p.chromium.launch(headless=True, **kwargs)
                break
            except Exception:  # noqa: BLE001, S112 - try the next browser
                continue
        if browser is None:
            return "neither Google Chrome nor Playwright's Chromium could start, so nothing was opened in Chrome"
        probe = await browser.new_page()
        # Bot protection (Mayo's, for one) turns away a browser that calls itself HeadlessChrome.
        user_agent = (await probe.evaluate("navigator.userAgent")).replace("HeadlessChrome", "Chrome")
        await probe.close()
        lanes = asyncio.Semaphore(3)
        redirect_host = urlparse(redirect_uri).hostname or ""
        print(f"Opening {len(todo)} in Chrome …", file=sys.stderr)

        async def one(result: Result) -> None:
            async with lanes:
                try:
                    status, detail = await _open_in_browser(browser, user_agent, result.start_url, redirect_host)
                except Exception as exc:  # noqa: BLE001 - keep the first pass's answer
                    result.browser = f"Chrome failed: {type(exc).__name__}"
                    return
            result.browser = f"Chrome: {status} ({detail})"
            if not (status == result.status == "portal_error"):      # keep the first pass's lowercase note
                result.status, result.detail = status, detail

        await asyncio.gather(*(one(r) for r in todo))
        await browser.close()
    return None


def _entries(platforms: Iterable[str] = DIRECTORIES) -> list[dict[str, Any]]:
    return [inst for platform in platforms for inst in json.loads(DIRECTORIES[platform].read_text())]


def address_key(url: str) -> str:
    return url.strip().rstrip("/").lower()


def sign_in_status(results: list[Result], entries: list[dict[str, Any]], previous: dict[str, Any],
                   today: str) -> dict[str, Any]:
    """What ``--write-status`` records: the failing servers, since when, and listings to use instead."""
    by_server: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for inst in entries:
        if inst.get("fhir_base_url"):
            by_server[address_key(inst["fhir_base_url"])].append(inst)
    working = {address_key(r.fhir_base_url) for r in results if r.status == "ok"}
    before = previous.get("servers", {})
    servers: dict[str, Any] = {}
    for r in results:
        key = address_key(r.fhir_base_url)
        if r.status not in RECORDED:
            continue
        same = before.get(key, {}).get("status") == r.status
        entry: dict[str, Any] = {"status": r.status, "since": before[key]["since"] if same else today}
        names = {i["name"] for i in by_server[key]}
        instead = sorted({i["id"] for k in working for i in by_server[k] if any(i["name"].startswith(n + " ") for n in names)})
        if instead:
            entry["instead"] = instead[:3]
        servers[key] = entry
    return {"servers": dict(sorted(servers.items()))}


def health_system_servers(platforms: Iterable[str], only: Optional[str] = None) -> dict[str, tuple[str, list[str]]]:
    """Distinct FHIR servers, each with its platform and the names of the health systems on it."""
    servers: dict[str, tuple[str, list[str]]] = {}
    for inst in _entries(platforms):
        if not inst.get("fhir_base_url") or (only and only.lower() not in inst["name"].lower()):
            continue
        servers.setdefault(inst["fhir_base_url"].strip().rstrip("/"), (inst["platform"], []))[1].append(inst["name"])
    return servers


async def check_all(servers: dict[str, tuple[str, list[str]]], redirect_uri: str) -> list[Result]:
    overall = asyncio.Semaphore(CONCURRENCY)
    per_host: dict[str, asyncio.Semaphore] = defaultdict(lambda: asyncio.Semaphore(PER_HOST))
    done = 0

    async def one(base: str, platform: str, names: list[str]) -> Result:
        nonlocal done
        client_id = settings.EHR_PLATFORMS[platform]["default_client_ids"]["production"]
        async with overall, per_host[(urlparse(base).hostname or "").lower()]:
            try:
                result = await check_server(base, client_id, redirect_uri)
            except Exception as exc:  # noqa: BLE001 - one odd server shouldn't stop the run
                result = Result("unreachable", f"{type(exc).__name__}: {exc}"[:300], base)
        result.names, result.platform = sorted(names), platform
        done += 1
        if done % 50 == 0 or done == len(servers):
            print(f"  {done}/{len(servers)} checked", file=sys.stderr)
        return result

    return list(await asyncio.gather(*(one(base, platform, names) for base, (platform, names) in servers.items())))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--platform", action="append", choices=sorted(DIRECTORIES),
                        help="check only this vendor's health systems (can be repeated)")
    parser.add_argument("--only", help="check health systems whose name contains this text")
    parser.add_argument("--json", type=Path, help="write every result to this file")
    parser.add_argument("--no-browser", action="store_true", help="don't open anything in Chrome")
    parser.add_argument("--write-status", action="store_true", help=f"record failures in {STATUS_FILE.relative_to(REPO_ROOT)}")
    args = parser.parse_args(argv)
    if args.write_status and (args.only or args.platform):
        parser.error("--write-status needs every health system checked, so it can't be combined with --only or --platform")

    servers = health_system_servers(args.platform or DIRECTORIES, args.only)
    per_platform = Counter(platform for platform, _ in servers.values())
    print(f"Checking {len(servers)} FHIR servers ({', '.join(f'{n} {p}' for p, n in per_platform.items())}) …", file=sys.stderr)
    results = asyncio.run(check_all(servers, config.DEFAULT_RELAY_URL))
    if not args.no_browser and (skipped := asyncio.run(browser_pass(results, config.DEFAULT_RELAY_URL))):
        print(f"Note: {skipped}.", file=sys.stderr)

    counts = Counter(r.status for r in results)
    print("\n" + ", ".join(f"{n} {status}" for status, n in counts.most_common()))
    for r in sorted((r for r in results if r.status != "ok"), key=lambda r: (r.status, r.names[0])):
        names = r.names[0] + (f" (+{len(r.names) - 1} more)" if len(r.names) > 1 else "")
        print(f"\n[{r.status}] {names} ({r.platform})\n  {r.fhir_base_url}\n  {r.detail}")
    if args.json:
        args.json.write_text(json.dumps([asdict(r) for r in results], indent=1))
    if args.write_status:
        previous = json.loads(STATUS_FILE.read_text()) if STATUS_FILE.exists() else {}
        status = sign_in_status(results, _entries(), previous, date.today().isoformat())
        STATUS_FILE.write_text(json.dumps(status, indent=1) + "\n")
    return 0 if counts["ok"] == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
