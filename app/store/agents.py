"""Access tokens for AI agents connecting over MCP: read-only, revocable, optionally limited to one profile."""

from __future__ import annotations

import logging
import math
from app.core.sqlite import sqlite3
import threading
import time
from collections import deque
from typing import Any, Optional

from app.core import security
from app.core.db import audit, db, new_id, read, rows_to_dicts

log = logging.getLogger("syntropy.agents")

TOKEN_PREFIX = "sh_agent_"


def create(name: str, profile_id: Optional[str] = None, account_id: Optional[str] = None,
           expires_days: Optional[int] = None) -> dict[str, Any]:
    """Returns the plaintext token exactly once. It reads what ``account_id`` can see (or only ``profile_id``)."""
    token = TOKEN_PREFIX + security.random_token(32)
    now = time.time()
    row = {"id": new_id("agt"), "name": (name or "Agent").strip()[:80], "profile_id": profile_id, "created_at": now,
           "expires_at": now + expires_days * 86400 if expires_days else None}
    with db() as conn:
        conn.execute("INSERT INTO agent_tokens(id, name, token_hash, profile_id, created_at, account_id, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (row["id"], row["name"], security.hash_token(token), profile_id, row["created_at"], account_id, row["expires_at"]))
        audit(conn, "user", "agent.token_created", {"id": row["id"], "name": row["name"], "profile": profile_id,
                                                    "expires_after_days": expires_days})
    return {**row, "token": token}


def list_tokens() -> list[dict[str, Any]]:
    with read() as conn:
        return rows_to_dicts(conn.execute(
            """SELECT t.id, t.name, t.profile_id, p.name AS profile_name, t.created_at, t.last_used_at, t.last_call,
                      t.last_call_at, t.account_id, t.expires_at
               FROM agent_tokens t LEFT JOIN profiles p ON p.id = t.profile_id
               WHERE t.revoked_at IS NULL ORDER BY t.created_at DESC""").fetchall())


def revoke(token_id: str, account_id: Optional[str] = None) -> bool:
    """Revokes a token (only one of ``account_id``'s, when given)."""
    with db() as conn:
        cur = conn.execute("UPDATE agent_tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL"
                           + (" AND account_id = ?" if account_id else ""),
                           (time.time(), token_id, *([account_id] if account_id else [])))
        if cur.rowcount:
            audit(conn, "user", "agent.token_revoked", {"id": token_id})
    return bool(cur.rowcount)


def note_use(agent: dict[str, Any], tool: Optional[str]) -> None:
    """For Settings: when the token was last used and for what. Runs after the reply has gone and may fail: it must never
    hold up or fail a request. A client that only lists tools notes it at most once a minute."""
    now = time.time()
    if not tool and (agent.get("last_used_at") or 0) >= now - 60:
        return
    try:
        with db() as conn:
            if tool:
                conn.execute("UPDATE agent_tokens SET last_call = ?, last_call_at = ?, last_used_at = ? WHERE id = ?",
                             (tool, now, now, agent["id"]))
            else:
                conn.execute("UPDATE agent_tokens SET last_used_at = ? WHERE id = ?", (now, agent["id"]))
    except sqlite3.Error:
        log.warning("Couldn't note when an AI app's token was last used", exc_info=True)


def authenticate(token: Optional[str]) -> Optional[dict[str, Any]]:
    """Only reads: anyone on the network can send a made-up token, and that mustn't wait for the write lock."""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    with read() as conn:
        row = conn.execute("SELECT * FROM agent_tokens WHERE token_hash = ? AND revoked_at IS NULL "
                           "AND (expires_at IS NULL OR expires_at > ?)", (security.hash_token(token), time.time())).fetchone()
    return dict(row) if row else None


# So a runaway agent or a leaked token can't read a whole record, or fill the log, as fast as a script can ask. Well above
# what a person's questions take, even with an agent making several lookups a second for a while.
CALLS_PER_MINUTE = 120
_clock = time.monotonic
_recent_calls: dict[str, deque[float]] = {}
_refused: set[str] = set()
_calls_lock = threading.Lock()


def wait_before_call(token_id: str) -> tuple[int, bool]:
    """(seconds to wait, whether this is the first refusal in a row). 0 means go ahead, and counts this call. Kept in memory:
    there's one server process, and starting again with a clean slate does no harm."""
    now = _clock()
    with _calls_lock:
        calls = _recent_calls.setdefault(token_id, deque())
        while calls and calls[0] <= now - 60:
            calls.popleft()
        if len(calls) < CALLS_PER_MINUTE:
            calls.append(now)
            _refused.discard(token_id)
            return 0, False
        first = token_id not in _refused
        _refused.add(token_id)
        return max(1, math.ceil(calls[0] + 60 - now)), first


def log_actors(account_id: Optional[str], owner: bool) -> set[str]:
    """How the access log names this account's AI apps. Rows from before it used ids say "mcp:<token name>", and those
    count only when no other account has a token by that name."""
    with read() as conn:
        tokens = conn.execute("SELECT id, name, account_id FROM agent_tokens").fetchall()
    mine = [t for t in tokens if (t["account_id"] == account_id if t["account_id"] else owner)]
    others = {t["name"] for t in tokens if t not in mine}
    return {f"mcp:{t['id']}" for t in mine} | {f"mcp:{t['name']}" for t in mine if t["name"] not in others}
