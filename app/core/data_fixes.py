"""
Corrections to data already stored, done once per database, in the background.

A migration changes the schema while nothing else runs, so it has to be quick. A fix that has to look at every row of a
big table doesn't belong there: it would hold up the next start for minutes. Register it here instead. After start-up
one background thread runs each registered fix that hasn't finished on this database, in short transactions, while the
server carries on answering. A restart resumes where the last step stopped. A failure is logged, doesn't stop the other
fixes, and is retried at the next start.

Writing one::

    from app.core import data_fixes

    def _fix_old_units(conn, cursor, size):
        rows = conn.execute("SELECT rowid, value FROM biometric_samples WHERE rowid > ? AND unit = 'mg/L' "
                            "ORDER BY rowid LIMIT ?", (cursor or 0, size)).fetchall()
        for row in rows:
            conn.execute("UPDATE biometric_samples SET value = ?, unit = 'g/L' WHERE rowid = ?", (row[1] / 1000, row[0]))
        return rows[-1][0] if rows else None

    data_fixes.register("samples-old-units", _fix_old_units)

The step is ``step(conn, cursor, size)``. ``conn`` is inside a transaction that also saves the cursor it returns, so a
crash can never apply a step without remembering it, or the other way round. ``cursor`` is what the last step returned
(``None`` the first time), any JSON value. ``size`` is how many rows to handle: it grows and shrinks so each step
holds the write lock for well under a second (see ``db.write_in_batches``), so use it as the LIMIT. Return ``None``
when there's nothing left. Returning the same cursor twice counts as a failure, since it would never finish.

Names are permanent: a name that has finished on a database never runs there again, and a fix that changes needs a new
one. Keep the code of a fix for as long as someone might upgrade from a version without it. Register at import time, in a
module ``app.main`` imports. Fixes run in the order they were registered. They only run in the server, not in the
command line or the MCP server.

``status()`` gives each fix's state, for Settings (it's in the owner's /api/health).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Any, Callable, Optional

from app.core import db

log = logging.getLogger("syntropy.data_fixes")

Step = Callable[[sqlite3.Connection, Any, int], Any]

_fixes: dict[str, Step] = {}
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


class _NoProgress(Exception):
    pass


def register(name: str, step: Step) -> None:
    if name in _fixes and _fixes[name] is not step:
        raise ValueError(f"A data fix named {name} is already registered.")
    _fixes[name] = step


def _load_state(conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM data_fixes WHERE name = ?", (name,)).fetchone()


def _run_fix(name: str, step: Step) -> None:
    now = time.time()
    with db.db() as conn:
        row = _load_state(conn, name)
        if row and row["state"] == "done":
            return
        conn.execute("INSERT INTO data_fixes(name, state, attempts, started_at, updated_at) VALUES (?, 'running', 1, ?, ?) "
                     "ON CONFLICT(name) DO UPDATE SET state = 'running', attempts = attempts + 1, error = NULL, updated_at = ?",
                     (name, now, now, now))
        cursor = json.loads(row["cursor_json"]) if row and row["cursor_json"] else None
    log.info("Running data fix %s", name)

    def one_step(conn: sqlite3.Connection, cursor: Any, size: int) -> Any:
        # Read again here: this write lock is what keeps a second server on this file from repeating a step.
        current = _load_state(conn, name)
        saved = json.loads(current["cursor_json"]) if current and current["cursor_json"] else None
        next_cursor = step(conn, saved, size)
        if next_cursor is not None and next_cursor == saved:
            raise _NoProgress(f"Data fix {name} returned the same cursor twice, so it would never finish.")
        done = next_cursor is None
        stamp = time.time()
        conn.execute("UPDATE data_fixes SET cursor_json = ?, steps = steps + 1, updated_at = ?, state = ?, finished_at = ? "
                     "WHERE name = ?", (None if done else json.dumps(next_cursor), stamp, "done" if done else "running",
                                        stamp if done else None, name))
        return next_cursor
    finished = db.write_in_batches(one_step, cursor=cursor, should_stop=_stop.is_set)
    if finished is None and not _stop.is_set():
        log.info("Data fix %s finished", name)


def run_pending() -> None:
    """Every fix that hasn't finished here. One failing doesn't stop the others; it stays failed until the next start."""
    for name, step in list(_fixes.items()):
        if _stop.is_set():
            return
        try:
            _run_fix(name, step)
        except Exception as exc:  # noqa: BLE001 - whatever a fix raises, the server and the other fixes carry on
            log.exception("Data fix %s failed; it will be tried again at the next start", name)
            try:
                with db.db() as conn:
                    conn.execute("UPDATE data_fixes SET state = 'failed', error = ?, updated_at = ? WHERE name = ?",
                                 (str(exc)[:500], time.time(), name))
            except sqlite3.Error:
                log.exception("Couldn't record that data fix %s failed", name)


def status() -> list[dict[str, Any]]:
    """Every registered fix: state is pending, running, done or failed, with how many steps it has taken."""
    with db.read() as conn:
        rows = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM data_fixes")}
    out = []
    for name in _fixes:
        row = rows.get(name)
        out.append({"name": name, "state": row["state"] if row else "pending", "steps": row["steps"] if row else 0,
                    "attempts": row["attempts"] if row else 0, "error": row["error"] if row else None,
                    "started_at": row["started_at"] if row else None, "finished_at": row["finished_at"] if row else None})
    return out


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    if not _fixes:
        return
    _thread = threading.Thread(target=run_pending, daemon=True, name="data-fixes")
    _thread.start()


def stop() -> None:
    """Between steps: what's been done so far is saved, and the next start carries on from there."""
    global _thread
    _stop.set()
    if _thread:
        _thread.join(10)
        _thread = None
