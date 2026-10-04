"""Who the current request is for: set by the sign-in check (app.core.auth) and read wherever access is decided, and
by the activity log to name the account behind "user"."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

PRINCIPAL: ContextVar[Optional[Any]] = ContextVar("principal", default=None)
# Work done for an account outside its own request: a health system's sign-in returning to /callback, which carries
# no session of its own, acts for whoever started it.
ACTING_FOR: ContextVar[Optional[str]] = ContextVar("acting_for", default=None)


def principal() -> Optional[Any]:
    return PRINCIPAL.get()


def actor() -> str:
    p = PRINCIPAL.get()
    return p.actor if p is not None else ACTING_FOR.get() or "user"
