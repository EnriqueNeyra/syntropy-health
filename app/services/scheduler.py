"""
Background scheduler.

Runs inside the server process: every few minutes it syncs connections that are due
(EHRs daily, wearables every few hours by default) and prunes expired sessions and
OAuth state. Disable with ``SYNTROPY_SCHEDULER=false``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from app.core import settings
from app.core.db import db
from app.services import sync

log = logging.getLogger("syntropy.scheduler")

TICK_SECONDS = 300
_task: Optional[asyncio.Task] = None


async def run_once() -> list[str]:
    synced = []
    for conn in sync.due_connections(float(settings.get("sync.ehr_interval_hours")),
                                     float(settings.get("sync.wearable_interval_hours"))):
        try:
            await sync.sync_connection(conn["id"], "scheduled")
            synced.append(conn["id"])
        except Exception:  # noqa: BLE001
            log.exception("Scheduled sync failed for %s", conn["id"])
    now = time.time()
    with db() as c:
        c.execute("DELETE FROM user_sessions WHERE expires_at < ?", (now,))
        c.execute("DELETE FROM oauth_pending WHERE expires_at < ?", (now,))
        c.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (now,))
        c.execute("DELETE FROM audit_log WHERE at < ?", (now - 365 * 86400,))
    return synced


async def _loop() -> None:
    await asyncio.sleep(20)  # let the server finish starting
    while True:
        try:
            await run_once()
        except Exception:  # noqa: BLE001
            log.exception("Scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop())


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _task = None
