"""How many wearable samples each source and person has, without counting millions of rows on every request.

Counting ``biometric_samples`` takes about a second per million rows, and Sources, the alerts and every upload from the
iPhone asked for it. The counts are taken once, kept up to date as samples arrive, and recounted in the background
every few minutes so they never drift far. Deleting samples drops them, so the next request counts afresh.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from app.core.db import read

RECOUNT_AFTER_S = 600

_lock = threading.Lock()
_by_connection: Optional[dict[Optional[str], int]] = None
_by_profile: dict[str, int] = {}
_counted_at = 0.0
_recounting = False
_arrived_while_recounting: list[tuple[str, Optional[str], int]] = []


def _count() -> tuple[dict[Optional[str], int], dict[str, int]]:
    # By source first: that walks the small connection_id index rather than every row. Samples without a source
    # (none are stored that way today) are counted per person separately.
    by_connection: dict[Optional[str], int] = {}
    by_profile: dict[str, int] = {}
    with read() as conn:
        owner = {r[0]: r[1] for r in conn.execute("SELECT id, profile_id FROM connections")}
        for connection_id, n in conn.execute("SELECT connection_id, COUNT(*) FROM biometric_samples GROUP BY connection_id").fetchall():
            by_connection[connection_id] = n
            if connection_id is None:
                continue
            profile_id = owner.get(connection_id) or conn.execute(   # a source removed some other way
                "SELECT profile_id FROM biometric_samples WHERE connection_id = ? LIMIT 1", (connection_id,)).fetchone()[0]
            by_profile[profile_id] = by_profile.get(profile_id, 0) + n
        for profile_id, n in conn.execute("SELECT profile_id, COUNT(*) FROM biometric_samples WHERE connection_id IS NULL GROUP BY profile_id"):
            by_profile[profile_id] = by_profile.get(profile_id, 0) + n
    return by_connection, by_profile


def warm() -> None:
    """Counts in the background when the server starts, so no request waits for it."""
    threading.Thread(target=_ensure, name="sample-count", daemon=True).start()


def _store(by_connection: dict[Optional[str], int], by_profile: dict[str, int]) -> None:
    global _by_connection, _by_profile, _counted_at
    _by_connection, _by_profile, _counted_at = by_connection, by_profile, time.time()


def _recount_in_background() -> None:
    global _recounting
    try:
        by_connection, by_profile = _count()
    except Exception:  # noqa: BLE001 - keep serving the previous counts
        with _lock:
            _recounting = False
            _arrived_while_recounting.clear()
        return
    with _lock:
        # Samples stored while counting may be missing from the count; add them back.
        for profile_id, connection_id, n in _arrived_while_recounting:
            by_connection[connection_id] = by_connection.get(connection_id, 0) + n
            by_profile[profile_id] = by_profile.get(profile_id, 0) + n
        _arrived_while_recounting.clear()
        _recounting = False
        if _by_connection is not None:   # not forgotten meanwhile
            _store(by_connection, by_profile)


def _ensure() -> None:
    """Counts now if there are no counts; starts a recount if they're old."""
    global _recounting
    with _lock:
        have = _by_connection is not None
        stale = have and time.time() - _counted_at > RECOUNT_AFTER_S and not _recounting
        if stale:
            _recounting = True
    if not have:
        by_connection, by_profile = _count()
        with _lock:
            if _by_connection is None:
                _store(by_connection, by_profile)
    elif stale:
        threading.Thread(target=_recount_in_background, name="sample-recount", daemon=True).start()


def for_connection(connection_id: str) -> int:
    _ensure()
    with _lock:
        return (_by_connection or {}).get(connection_id, 0)


def for_profile(profile_id: str) -> int:
    _ensure()
    with _lock:
        return _by_profile.get(profile_id, 0)


def added(profile_id: str, connection_id: Optional[str], n: int) -> None:
    """Records samples that were just stored."""
    if n <= 0:
        return
    with _lock:
        if _by_connection is None:
            return
        _by_connection[connection_id] = _by_connection.get(connection_id, 0) + n
        _by_profile[profile_id] = _by_profile.get(profile_id, 0) + n
        if _recounting:
            _arrived_while_recounting.append((profile_id, connection_id, n))


def forget() -> None:
    """Samples were deleted or moved: count again on the next request."""
    global _by_connection, _by_profile
    with _lock:
        _by_connection, _by_profile = None, {}
        _arrived_while_recounting.clear()
