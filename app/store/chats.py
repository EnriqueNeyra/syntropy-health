"""
Ask conversations: saved per person and per account (someone who looks after a parent has their own conversations
about them), newest first. Each is the list of messages as the Ask page shows them.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from app.core.db import db, read

MAX_CHATS = 200           # per person and account; the oldest go first
MAX_MESSAGES = 120
MAX_CHARS = 600_000       # a whole conversation, as stored


def _owner(account_id: Optional[str]) -> str:
    return account_id or ""


def list_chats(profile_id: str, account_id: Optional[str], limit: int = 100) -> list[dict[str, Any]]:
    with read() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at, json_array_length(messages_json) AS messages FROM chats "
            "WHERE profile_id = ? AND account_id = ? ORDER BY updated_at DESC LIMIT ?",
            (profile_id, _owner(account_id), limit)).fetchall()
    return [dict(r) for r in rows]


def get_chat(profile_id: str, account_id: Optional[str], chat_id: str) -> Optional[dict[str, Any]]:
    with read() as conn:
        row = conn.execute("SELECT id, title, messages_json, created_at, updated_at FROM chats "
                           "WHERE id = ? AND profile_id = ? AND account_id = ?",
                           (chat_id, profile_id, _owner(account_id))).fetchone()
    if not row:
        return None
    out = dict(row)
    out["messages"] = json.loads(out.pop("messages_json"))
    return out


def save_chat(profile_id: str, account_id: Optional[str], chat_id: str, title: str,
              messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Creates or replaces a conversation. A conversation id already used by another person or account is refused."""
    body = json.dumps(messages[-MAX_MESSAGES:], ensure_ascii=False, separators=(",", ":"))
    if len(body) > MAX_CHARS:
        raise ValueError("This conversation is too long to save. Start a new chat.")
    now = time.time()
    owner = _owner(account_id)
    with db() as conn:
        row = conn.execute("SELECT profile_id, account_id FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if row and (row["profile_id"] != profile_id or row["account_id"] != owner):
            raise PermissionError("Not found")
        if row:
            conn.execute("UPDATE chats SET title = ?, messages_json = ?, updated_at = ? WHERE id = ?",
                         (title, body, now, chat_id))
        else:
            conn.execute("INSERT INTO chats(id, profile_id, account_id, title, messages_json, created_at, updated_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (chat_id, profile_id, owner, title, body, now, now))
            conn.execute("DELETE FROM chats WHERE id IN (SELECT id FROM chats WHERE profile_id = ? AND account_id = ? "
                         "ORDER BY updated_at DESC LIMIT -1 OFFSET ?)", (profile_id, owner, MAX_CHATS))
    return {"id": chat_id, "title": title, "updated_at": now}


def delete_chat(profile_id: str, account_id: Optional[str], chat_id: str) -> bool:
    with db() as conn:
        return conn.execute("DELETE FROM chats WHERE id = ? AND profile_id = ? AND account_id = ?",
                            (chat_id, profile_id, _owner(account_id))).rowcount > 0
