"""
Which site the app's window is showing, and what a page may do to the app. The same rules on a Mac and on Windows.

- Syntropy Health's own pages are the ones whose origin is the home address, and the few pages the app writes itself
  (starting up, can't start, can't reach the server). Everything else is another site, including pages with no origin
  of their own: blob:, data: and about:blank. Another site always shows its name and a way back.
- Pages the app writes itself carry OWN_PAGE_TOKEN, made anew each time the app starts. No other page can read it, so
  it's what lets them ask the app for something (try again, restore a backup) without a web origin to check.
- The window never starts another program by itself. A link leaves the window only as http, https or mailto, in the
  default browser or mail app, and only when a person clicked it.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import types
import urllib.parse
import webbrowser
from typing import Any, Callable, Optional

log = logging.getLogger("syntropy.desktop")

Origin = tuple[str, str, int]

OWN_PAGE_TOKEN = secrets.token_urlsafe(24)
EXTERNAL_SCHEMES = {"http", "https", "mailto"}


def is_own_page_token(value: Any) -> bool:
    return isinstance(value, str) and hmac.compare_digest(value.encode(), OWN_PAGE_TOKEN.encode())


def origin(url: Optional[str]) -> Optional[Origin]:
    """An http or https address's origin; None for anything else, or an address that can't be read."""
    try:
        parts = urllib.parse.urlsplit(url or "")
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https") or not parts.hostname:
            return None
        return scheme, parts.hostname.lower(), parts.port or {"http": 80, "https": 443}[scheme]
    except ValueError:
        return None


def home_origins(home_url: Optional[str]) -> set[Origin]:
    home = origin(home_url)
    if home is None:
        return set()
    if home[1] in ("localhost", "127.0.0.1"):       # this computer's server answers to both names
        return {(home[0], name, home[2]) for name in ("localhost", "127.0.0.1")}
    return {home}


def site_name(url: Optional[str]) -> str:
    """What the title bar says on another site: its host, as a browser would show it. A blob: page belongs to the site
    that made it; about:blank and data: pages belong to nobody, and say so."""
    text = url or ""
    if text.lower().startswith("blob:"):
        text = text[5:]
    here = origin(text)
    if here:
        return here[1]
    return "A page with no address"


class Location:
    """Whether the window is away from Syntropy Health's own pages."""

    def __init__(self, home_url: Callable[[], Optional[str]], own_page: bool = False):
        self.home_url = home_url
        self.own_page = own_page

    def showing_own_page(self) -> None:
        """Call just before loading a page the app wrote: those report about:blank or a file address, which from
        anything else would count as another site."""
        self.own_page = True

    def is_away(self, url: Optional[str]) -> bool:
        if not url:
            return False        # nothing has loaded yet
        here = origin(url)
        if here is None:
            return not self.own_page
        self.own_page = False
        return here not in home_origins(self.home_url())


# ---------------------------------------------------------------------------
# Links leaving the window
# ---------------------------------------------------------------------------

def may_leave(url: Any) -> bool:
    scheme = urllib.parse.urlsplit(str(url)).scheme.lower() if url else ""
    return scheme in EXTERNAL_SCHEMES


def guard_external_links() -> None:
    """pywebview hands every link meant for a new window to webbrowser.open, and on Windows that ends in os.startfile,
    which runs whatever handler claims the scheme. The app's own uses (Open in Browser, Help) pass through this too."""
    real = webbrowser.open
    if getattr(real, "syntropy_guard", False):
        return

    def checked(url: str, *args: Any, **kwargs: Any) -> bool:
        if not may_leave(url):
            log.warning("Refused to open a link with an unknown scheme: %s", _scheme_only(url))
            return False
        return real(url, *args, **kwargs)

    checked.syntropy_guard = True       # type: ignore[attr-defined]
    webbrowser.open = checked


def only_when_clicked(person_acted: Callable[[], bool]) -> Any:
    """What pywebview's platform modules get in place of the webbrowser module: a page can click a link from script,
    with nobody there, so opening it also takes a person's click just now."""

    def page_link(url: str, *args: Any, **kwargs: Any) -> bool:
        if not person_acted():
            log.warning("Refused a link opened by a page without a click: %s", _scheme_only(url))
            return False
        return webbrowser.open(url, *args, **kwargs)

    return types.SimpleNamespace(open=page_link)


def _scheme_only(url: Any) -> str:
    return str(url).split(":", 1)[0][:40]        # addresses can carry tokens: the log keeps the scheme


def drop_bridge_call(*_args: Any, **_kwargs: Any) -> None:
    """In place of pywebview's js_bridge_call, which every page can reach and which moves the window. The app doesn't
    use that bridge: its Content-Security-Policy stops it working, and the app has its own (mac_window.py)."""
