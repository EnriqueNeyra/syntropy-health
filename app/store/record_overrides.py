"""What a person says about an entry in their record, kept beside what the source says and never written over it.

Portal lists are wrong in places, and only the person knows which. So: "not taking", "taking differently", "wrong or not me", and a
note. Keyed by the fact (a record's dedup_key), so it covers every source reporting it and is still there after a re-sync. Whatever
shows the record shows both, with this labelled as the person's words.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from app.core.db import db, read

NOT_TAKING, TAKING_DIFFERENTLY, WRONG = "not_taking", "taking_differently", "wrong"
STATES = (NOT_TAKING, TAKING_DIFFERENTLY, WRONG)
MEDICATION_ONLY = (NOT_TAKING, TAKING_DIFFERENTLY)
WORDS = {NOT_TAKING: "Not taking", TAKING_DIFFERENTLY: "Taking differently", WRONG: "Wrong or not this person"}
MAX_NOTE = 2000
# On the note an export adds, so that bringing the file back in recognises the resource as the one it came from.
NOTE_AUTHOR = "The patient or their caregiver, through Syntropy Health"


def key_for(rec: dict[str, Any]) -> str:
    return rec.get("dedup_key") or f"id:{rec['id']}"


def said(row: dict[str, Any]) -> dict[str, Any]:
    state = row.get("state")
    words = WORDS.get(state or "")
    return {"state": state, "words": words, "note": row.get("note"), "set_by": row.get("set_by"), "set_at": row.get("set_at"),
            "text": ": ".join(p for p in (words, row.get("note")) if p)}


def for_profile(profile_id: str) -> dict[str, dict[str, Any]]:
    with read() as conn:
        rows = conn.execute("SELECT * FROM record_overrides WHERE profile_id = ?", (profile_id,)).fetchall()
    return {r["dedup_key"]: dict(r) for r in rows}


def attach(profile_id: str, rows: Iterable[dict[str, Any]]) -> None:
    known = for_profile(profile_id)
    if known:
        for rec in rows:
            found = known.get(key_for(rec))
            if found:
                rec["override"] = said(found)


def set_override(profile_id: str, rec: dict[str, Any], state: Optional[str], note: Optional[str], set_by: Optional[str]) -> dict[str, Any]:
    if state and state not in STATES:
        raise ValueError("That isn't one of the choices.")
    if state in MEDICATION_ONLY and rec.get("category") != "medications":
        raise ValueError("That only applies to a medication.")
    note = (note or "").strip()[:MAX_NOTE] or None
    if not state and not note:
        raise ValueError("Choose what's wrong, or write a note.")
    key = key_for(rec)
    now = time.time()
    with db() as conn:
        conn.execute("""INSERT INTO record_overrides(profile_id, dedup_key, state, note, title, category, set_by, set_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(profile_id, dedup_key) DO UPDATE SET state = excluded.state, note = excluded.note, title = excluded.title,
                            category = excluded.category, set_by = excluded.set_by, set_at = excluded.set_at""",
                     (profile_id, key, state, note, rec.get("title"), rec.get("category"), set_by, now))
    return said({"state": state, "note": note, "set_by": set_by, "set_at": now})


def clear(profile_id: str, rec: dict[str, Any]) -> None:
    with db() as conn:
        conn.execute("DELETE FROM record_overrides WHERE profile_id = ? AND dedup_key = ?", (profile_id, key_for(rec)))


def bring_in(profile_id: str, rows: Iterable[dict[str, Any]]) -> int:
    """From an archive. What's here already stays: as everywhere in an archive import, nothing here is overwritten."""
    added = 0
    with db() as conn:
        for r in rows:
            if not (isinstance(r, dict) and isinstance(r.get("dedup_key"), str)) or (r.get("state") and r["state"] not in STATES):
                continue
            added += conn.execute("""INSERT OR IGNORE INTO record_overrides(profile_id, dedup_key, state, note, title, category, set_by, set_at)
                                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                  (profile_id, r["dedup_key"], r.get("state"), r.get("note"), r.get("title"), r.get("category"), r.get("set_by"),
                                   float(r.get("set_at") or time.time()))).rowcount
    return added


def ordered_again_since(med: dict[str, Any], override: dict[str, Any]) -> bool:
    """A prescription dated after the person said they'd stopped: they may have started again, so it isn't filed away on old words."""
    when = str(med.get("effective_at") or "")[:10]
    if not when or not override.get("set_at"):
        return False
    try:
        return date.fromisoformat(when) > datetime.fromtimestamp(override["set_at"], timezone.utc).date()
    except ValueError:
        return False
