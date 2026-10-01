"""Who the current request is for: set by the sign-in check (app.core.auth) and read wherever access is decided, and
by the activity log to name the account behind "user"."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

PRINCIPAL: ContextVar[Optional[Any]] = ContextVar("principal", default=None)


def principal() -> Optional[Any]:
    return PRINCIPAL.get()


def actor() -> str:
    p = PRINCIPAL.get()
    return p.actor if p is not None else "user"
