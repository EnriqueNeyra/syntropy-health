"""
Ask conversations: saved per person and per account (someone who looks after a parent has their own conversations
about them), newest first. Each is the list of messages as the Ask page shows them.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from typing import Any, Optional

from app.core.db import db, note_deleted, read

MAX_CHATS = 200           # per person and account; a new one pushes out the oldest that isn't pinned, and an import stops here instead
MAX_MESSAGES = 120
MAX_CHARS = 600_000       # a whole conversation, as stored
MAX_SEARCH_WORDS = 8
SNIPPET_CHARS = 120

_COLUMNS = "id, title, created_at, updated_at, pinned, title_set"


def _owner(account_id: Optional[str]) -> str:
    return account_id or ""


def _flags(row: dict[str, Any]) -> dict[str, Any]:
    row["pinned"], row["title_set"] = bool(row["pinned"]), bool(row["title_set"])
    return row


def list_chats(profile_id: str, account_id: Optional[str], limit: int = -1) -> list[dict[str, Any]]:
    """Newest first, pinned or not (the page puts pinned ones at the top). All of them unless a limit is asked for: an older
    pinned one must not fall off the end."""
    with read() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS}, json_array_length(messages_json) AS messages FROM chats "
            "WHERE profile_id = ? AND account_id = ? ORDER BY updated_at DESC LIMIT ?",
            (profile_id, _owner(account_id), limit)).fetchall()
    return [_flags(dict(r)) for r in rows]


def get_chat(profile_id: str, account_id: Optional[str], chat_id: str) -> Optional[dict[str, Any]]:
    with read() as conn:
        row = conn.execute(f"SELECT {_COLUMNS}, messages_json FROM chats "
                           "WHERE id = ? AND profile_id = ? AND account_id = ?",
                           (chat_id, profile_id, _owner(account_id))).fetchone()
    if not row:
        return None
    out = _flags(dict(row))
    out["messages"] = json.loads(out.pop("messages_json"))
    return out


def _folded(text: str) -> str:
    """For comparing: "José" and "jose" are one word to someone searching."""
    return "".join(c for c in unicodedata.normalize("NFKD", text).casefold() if not unicodedata.combining(c))


def _snippet(content: str, word: str) -> str:
    """The part of a message around a word, on one line, without the marks that format it."""
    flat = re.sub(r"\s+", " ", re.sub(r"[*`#|]+|:?-{3,}:?", "", content)).strip()
    start = max(_folded(flat).find(word) - SNIPPET_CHARS // 3, 0)
    if start:
        start = flat.find(" ", start) + 1 or start
    return f"{'…' if start else ''}{flat[start:start + SNIPPET_CHARS].strip()}{'…' if start + SNIPPET_CHARS < len(flat) else ''}"


def search_chats(profile_id: str, account_id: Optional[str], query: str) -> list[dict[str, Any]]:
    """Conversations holding every word asked for, in the title or in what was said (not the stored message's field names,
    which would match "content" everywhere). Those with a word in the title first, then newest first. Each comes with a
    `snippet` when a word is only in a message."""
    words = _folded(query).split()[:MAX_SEARCH_WORDS]
    named, others = [], []
    with read() as conn:
        for row in conn.execute(f"SELECT {_COLUMNS}, messages_json FROM chats WHERE profile_id = ? AND account_id = ? "
                                "ORDER BY updated_at DESC", (profile_id, _owner(account_id))):
            chat = _flags(dict(row))
            said = [str(m.get("content") or "") for m in json.loads(chat.pop("messages_json"))]
            chat["messages"] = len(said)
            title, folded = _folded(chat["title"]), [_folded(s) for s in said]
            if not all(w in title or any(w in s for s in folded) for w in words):
                continue
            elsewhere = [w for w in words if w not in title]
            if elsewhere:
                chat["snippet"] = _snippet(next(s for s, f in zip(said, folded) if elsewhere[0] in f), elsewhere[0])
            (others if len(elsewhere) == len(words) else named).append(chat)
    return named + others


def _stored(messages: list[dict[str, Any]]) -> str:
    body = json.dumps(messages[-MAX_MESSAGES:], ensure_ascii=False, separators=(",", ":"))
    if len(body) > MAX_CHARS:
        raise ValueError("This conversation is too long to save. Start a new chat.")
    return body


def add_chat(profile_id: str, account_id: Optional[str], chat_id: str, title: str, messages: list[dict[str, Any]],
             created_at: float, updated_at: float, *, pinned: bool = False, title_set: bool = False) -> bool:
    """A conversation from an import, with the dates it had. Nothing here makes way for it: False if this person already
    has as many as are kept (a pinned one always comes). An id already used by another person or account is refused."""
    body = _stored(messages)
    owner = _owner(account_id)
    with db() as conn:
        if conn.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone():
            raise PermissionError("Not found")
        if not pinned and conn.execute("SELECT COUNT(*) FROM chats WHERE profile_id = ? AND account_id = ?", (profile_id, owner)).fetchone()[0] >= MAX_CHATS:
            return False
        conn.execute("INSERT INTO chats(id, profile_id, account_id, title, messages_json, created_at, updated_at, pinned, title_set) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (chat_id, profile_id, owner, title, body, created_at, updated_at, int(pinned), int(title_set)))
    return True


def save_chat(profile_id: str, account_id: Optional[str], chat_id: str, title: str,
              messages: list[dict[str, Any]], *, title_set: bool = False) -> dict[str, Any]:
    """Creates or replaces a conversation. A title the person chose stays, whatever title comes with the save, unless this
    save is them choosing one (`title_set`). A conversation id already used by another person or account is refused."""
    body = _stored(messages)
    now = time.time()
    owner = _owner(account_id)
    pinned = False
    with db() as conn:
        row = conn.execute("SELECT profile_id, account_id, title, title_set, pinned FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if row and (row["profile_id"] != profile_id or row["account_id"] != owner):
            raise PermissionError("Not found")
        if row:
            pinned = bool(row["pinned"])
            if row["title_set"] and not title_set:
                title, title_set = row["title"], True
            conn.execute("UPDATE chats SET title = ?, title_set = ?, messages_json = ?, updated_at = ? WHERE id = ?",
                         (title, int(title_set), body, now, chat_id))
        else:
            conn.execute("INSERT INTO chats(id, profile_id, account_id, title, title_set, messages_json, created_at, updated_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (chat_id, profile_id, owner, title, int(title_set), body, now, now))
            # Pinned ones are never pushed out and they count towards the limit, so the others make room for them. The one
            # just saved stays even if every other conversation is pinned.
            kept = conn.execute("SELECT COUNT(*) FROM chats WHERE profile_id = ? AND account_id = ? AND pinned = 1", (profile_id, owner)).fetchone()[0]
            conn.execute("DELETE FROM chats WHERE id IN (SELECT id FROM chats WHERE profile_id = ? AND account_id = ? AND pinned = 0 AND id != ? "
                         "ORDER BY updated_at DESC LIMIT -1 OFFSET ?)", (profile_id, owner, chat_id, max(MAX_CHATS - kept - 1, 0)))
    return {"id": chat_id, "title": title, "updated_at": now, "pinned": pinned, "title_set": title_set}


def update_chat(profile_id: str, account_id: Optional[str], chat_id: str, *, title: Optional[str] = None,
                pinned: Optional[bool] = None) -> Optional[dict[str, Any]]:
    """Renames or pins a conversation, which leaves its place among the others alone: nothing new was said in it."""
    where, owner = "id = ? AND profile_id = ? AND account_id = ?", _owner(account_id)
    with db() as conn:
        if title is not None:
            conn.execute(f"UPDATE chats SET title = ?, title_set = 1 WHERE {where}", (title, chat_id, profile_id, owner))
        if pinned is not None:
            conn.execute(f"UPDATE chats SET pinned = ? WHERE {where}", (int(pinned), chat_id, profile_id, owner))
        row = conn.execute(f"SELECT {_COLUMNS} FROM chats WHERE {where}", (chat_id, profile_id, owner)).fetchone()
    return _flags(dict(row)) if row else None


def as_markdown(chat: dict[str, Any], person: str, day: str) -> str:
    """A conversation as a document to keep or share: who said what, and who answered. Its note says what the page says."""
    parts = [f"# {chat['title']}", f"A conversation in Syntropy Health about {person}'s records, exported {day}. "
             "Answers come from an AI and can be wrong. Syntropy Health organizes records and doesn't give medical advice."]
    for m in chat["messages"]:
        parts += [f"## {'You' if m.get('role') == 'user' else m.get('by') or 'Answer'}", str(m.get("content") or "")]
        if m.get("stopped"):
            parts.append("*This answer was stopped before it finished.*")
    return "\n\n".join(parts) + "\n"


def delete_chat(profile_id: str, account_id: Optional[str], chat_id: str) -> bool:
    with db() as conn:
        deleted = conn.execute("DELETE FROM chats WHERE id = ? AND profile_id = ? AND account_id = ?",
                               (chat_id, profile_id, _owner(account_id))).rowcount > 0
    note_deleted()
    return deleted
