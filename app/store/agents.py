"""Access tokens for AI agents connecting over MCP: read-only, revocable, optionally limited to one profile."""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Optional

from app.core import security
from app.core.db import audit, db, new_id, read, rows_to_dicts

log = logging.getLogger("syntropy.agents")

TOKEN_PREFIX = "sh_agent_"


def create(name: str, profile_id: Optional[str] = None, account_id: Optional[str] = None) -> dict[str, Any]:
    """Returns the plaintext token exactly once. It reads what ``account_id`` can see (or only ``profile_id``)."""
    token = TOKEN_PREFIX + security.random_token(32)
    row = {"id": new_id("agt"), "name": (name or "Agent").strip()[:80], "profile_id": profile_id, "created_at": time.time()}
    with db() as conn:
        conn.execute("INSERT INTO agent_tokens(id, name, token_hash, profile_id, created_at, account_id) VALUES (?, ?, ?, ?, ?, ?)",
                     (row["id"], row["name"], security.hash_token(token), profile_id, row["created_at"], account_id))
        audit(conn, "user", "agent.token_created", {"id": row["id"], "name": row["name"], "profile": profile_id})
    return {**row, "token": token}


def list_tokens() -> list[dict[str, Any]]:
    with read() as conn:
        return rows_to_dicts(conn.execute(
            """SELECT t.id, t.name, t.profile_id, p.name AS profile_name, t.created_at, t.last_used_at, t.last_call,
                      t.last_call_at, t.account_id
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
        row = conn.execute("SELECT * FROM agent_tokens WHERE token_hash = ? AND revoked_at IS NULL",
                           (security.hash_token(token),)).fetchone()
    return dict(row) if row else None


def log_actors(account_id: Optional[str], owner: bool) -> set[str]:
    """How the access log names this account's AI apps. Rows from before it used ids say "mcp:<token name>", and those
    count only when no other account has a token by that name."""
    with read() as conn:
        tokens = conn.execute("SELECT id, name, account_id FROM agent_tokens").fetchall()
    mine = [t for t in tokens if (t["account_id"] == account_id if t["account_id"] else owner)]
    others = {t["name"] for t in tokens if t not in mine}
    return {f"mcp:{t['id']}" for t in mine} | {f"mcp:{t['name']}" for t in mine if t["name"] not in others}
