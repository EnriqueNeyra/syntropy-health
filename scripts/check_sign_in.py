"""
Checks that every Epic, Oracle Health and eClinicalWorks health system in the directory takes a patient to its sign-in.

    ./run.sh check-sign-in                     # every health system (about twenty minutes, most of it eClinicalWorks's)
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
| ``blocked`` | The server or sign-in site refused the checker (401, 403 or 429, or no connection), and Chrome couldn't \
confirm it either way. A hospital's firewall may only be turning away cloud addresses, as this runs from one. |
| ``unreachable`` | The FHIR server or its authorize endpoint didn't answer. |

``--write-status`` records the servers that failed in ``app/data/sign_in_status.json``, which the app reads to warn
before sending someone to a sign-in that won't work: when the check ran (``checked``), and for each server the result, the
date it was first seen failing (``since``), and other listings of the same health system that work (``instead``, by name:
UPMC → UPMC Patient Portal). ``blocked`` and ``unclear`` servers aren't recorded, since nobody knows they're broken. An
``unreachable`` one waits under ``watching`` until it fails a second week running: one bad night isn't worth a warning.
Because a blocked server isn't recorded, a vendor turning the checker away (rate-limiting it, say) would quietly drop real
warnings, so a run where too many of one vendor's servers were blocked leaves the file as it was.

Servers on one host are checked a few at a time, more on a vendor's host that serves many health systems (all of
eClinicalWorks's practices are on one), and every check shares one pool of connections.

Exits with status 1 when any health system isn't ``ok``. With ``--write-status`` a failing health system is what the file
is for, so only a crash, or a run too many servers turned away, does.
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
from scripts.refresh_directory import write_json  # noqa: E402

DIRECTORIES = {platform: REPO_ROOT / "app" / "data" / "institutions" / f"{name}.json"
               for platform, name in (("epic", "epic"), ("cerner", "oracle"), ("healow", "ecw"))}
STATUS_FILE = REPO_ROOT / "app" / "data" / "sign_in_status.json"
RECORDED = ("portal_error", "error_page", "unreachable", "client_unknown", "rejected")
SECOND_WEEK = ("unreachable",)
REFUSALS = (401, 403, 407, 429, 451)     # an answer, and about whoever's asking rather than the server
MAX_HOPS = 20
CONCURRENCY = 24
PER_HOST = 2                   # at once on one health system's own server
SHARED_HOST = 8                # at once on a vendor's host that serves many health systems
SHARED_AT = 50                 # servers on one host that make it a vendor's shared one
RETRY_DELAYS = (2.0, 6.0)      # a busy name resolver or a moment's 503 isn't the health system's fault
RETRY_AFTER_MAX = 30.0         # the longest a 429's Retry-After is waited for
MOST_BLOCKED = 0.02            # --write-status keeps the last file when more of one vendor's servers than this were blocked,
BLOCKED_FLOOR = 20             # and more than this many

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
# What a page shows is what decides whether it's an error page: Oracle Health's sign-in page carries its whole script,
# "An error occurred while setting a property" included.
_HIDDEN = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>", re.I | re.S)
# Pages a browser shows just as the first pass found them, so Chrome isn't asked again: eClinicalWorks's own page for
# a practice that doesn't offer patients online access (Chrome agreed on all 1,072 of them on 3 November 2026).
_SETTLED = (("/apps/jsp/fhir/error.jsp", "invalid request"),)


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


def _shows_an_error(title: str, text: str) -> bool:
    return any(s in _HIDDEN.sub(" ", text).lower() for s in _ERROR) or any(s in title.lower() for s in _ERROR_TITLE)


def _settled(result: Result) -> bool:
    """A first-pass answer that opening the page in Chrome wouldn't change."""
    last = result.hops[-1] if result.hops else ""
    return result.status == "error_page" and any(last.endswith(path) and title in result.detail.lower()
                                                 for path, title in _SETTLED)


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


def _retry_after(resp: httpx.Response) -> float:
    try:
        return min(float(resp.headers.get("retry-after", 0)), RETRY_AFTER_MAX)
    except ValueError:      # a date instead of seconds
        return 0.0


async def _get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """A GET that tries again after a moment's 502, 503 or 504, or a 429 (as long as it asks, within reason)."""
    for delay in (*RETRY_DELAYS, None):
        try:
            resp = await client.get(url, **kwargs)
            if resp.status_code not in (429, 502, 503, 504) or delay is None:
                return resp
            delay = max(delay, _retry_after(resp))
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
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            result.status = "blocked" if code in REFUSALS else "portal_error"
            result.detail = f"SMART configuration: HTTP {code}"
            return result
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
            if resp.status_code == 429 or (resp.status_code == 403 and urlparse(url).hostname != authorize_host):
                result.status = "blocked"
                result.detail = f"HTTP {resp.status_code} at {_where(url)}" + (f" ({title})" if title else "")
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
            elif _shows_an_error(title, text):
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
        return ("blocked" if status in (403, 429) else "portal_error"), f"HTTP {status} at {where}" + (f" ({title})" if title else "")
    if title.lower().startswith("loading"):       # Oracle Health's script page, caught before it moved on
        return "unclear", f"{where}: still loading ({title[:60]})"
    if _shows_an_error(title, text):
        return "error_page", f"{where}: {title or 'error page'}"
    lower = text.lower()
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
    todo = [r for r in results if r.status not in ("ok", "unreachable", "client_unknown") and r.start_url and not _settled(r)]
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
    watched_before = previous.get("watching", {})
    servers: dict[str, Any] = {}
    watching: dict[str, Any] = {}
    for r in results:
        key = address_key(r.fhir_base_url)
        if r.status not in RECORDED:
            continue
        known = next((e for e in (before.get(key), watched_before.get(key)) if e and e.get("status") == r.status), None)
        if r.status in SECOND_WEEK and not known:
            watching[key] = {"status": r.status, "since": today}
            continue
        entry: dict[str, Any] = {"status": r.status, "since": known["since"] if known else today}
        names = {i["name"] for i in by_server[key]}
        instead = sorted({i["id"] for k in working for i in by_server[k] if any(i["name"].startswith(n + " ") for n in names)})
        if instead:
            entry["instead"] = instead[:3]
        servers[key] = entry
    return {"checked": today, "servers": dict(sorted(servers.items())), "watching": dict(sorted(watching.items()))}


def health_system_servers(platforms: Iterable[str], only: Optional[str] = None) -> dict[str, tuple[str, list[str]]]:
    """Distinct FHIR servers, each with its platform and the names of the health systems on it."""
    servers: dict[str, tuple[str, list[str]]] = {}
    for inst in _entries(platforms):
        if not inst.get("fhir_base_url") or (only and only.lower() not in inst["name"].lower()):
            continue
        servers.setdefault(inst["fhir_base_url"].strip().rstrip("/"), (inst["platform"], []))[1].append(inst["name"])
    return servers


class _SharedPool(httpx.AsyncBaseTransport):
    """One pool of connections lent to every server's client, so a vendor's sign-in hosts are connected to once, not
    for each practice. Each check keeps its own client, so no cookies pass between health systems; a client closes
    what it's given when it's done, so closing is left to the run."""

    def __init__(self, pool: httpx.AsyncBaseTransport) -> None:
        self.pool = pool

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.pool.handle_async_request(request)

    async def aclose(self) -> None:
        pass


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


async def check_all(servers: dict[str, tuple[str, list[str]]], redirect_uri: str,
                    transport: Optional[httpx.AsyncBaseTransport] = None) -> list[Result]:
    pool = transport or httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=CONCURRENCY * 4,
                                                                    max_keepalive_connections=CONCURRENCY * 4))
    shared = _SharedPool(pool)
    overall = asyncio.Semaphore(CONCURRENCY)
    on_host = Counter(_host(base) for base in servers)
    per_host = {host: asyncio.Semaphore(SHARED_HOST if n >= SHARED_AT else PER_HOST) for host, n in on_host.items()}
    done = 0

    async def one(base: str, platform: str, names: list[str]) -> Result:
        nonlocal done
        client_id = settings.EHR_PLATFORMS[platform]["default_client_ids"]["production"]
        # The host's turn first: a check waiting for a busy host doesn't hold a place another host could use.
        async with per_host[_host(base)], overall:
            try:
                result = await check_server(base, client_id, redirect_uri, shared)
            except Exception as exc:  # noqa: BLE001 - one odd server shouldn't stop the run
                result = Result("unreachable", f"{type(exc).__name__}: {exc}"[:300], base)
        result.names, result.platform = sorted(names), platform
        done += 1
        if done % 500 == 0 or done == len(servers):
            print(f"  {done}/{len(servers)} checked", file=sys.stderr)
        return result

    try:
        return list(await asyncio.gather(*(one(base, platform, names) for base, (platform, names) in servers.items())))
    finally:
        if transport is None:
            await pool.aclose()


def too_many_blocked(results: list[Result]) -> list[str]:
    """The vendors that turned the checker away too often for this run's warnings to replace the last ones."""
    per_platform: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        per_platform[r.platform][r.status] += 1
    return [f"{platform}: {counts['blocked']} of {sum(counts.values())} blocked"
            for platform, counts in sorted(per_platform.items())
            if counts["blocked"] > max(BLOCKED_FLOOR, MOST_BLOCKED * sum(counts.values()))]


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
        if refused := too_many_blocked(results):
            print(f"\nToo many servers turned the check away to trust its warnings ({'; '.join(refused)}), so "
                  f"{STATUS_FILE.relative_to(REPO_ROOT)} keeps the last ones.", file=sys.stderr)
            return 1
        previous = json.loads(STATUS_FILE.read_text()) if STATUS_FILE.exists() else {}
        write_json(STATUS_FILE, sign_in_status(results, _entries(), previous, date.today().isoformat()), indent=1)
        return 0
    return 0 if counts["ok"] == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
