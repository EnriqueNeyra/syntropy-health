"""
Finding words in clinical records: titles, note and report text, clinicians, places, anything else a record's details hold.

A search matches records holding every word, in any order. Nothing leaves this file: it's all in the one SQLite database.

With SQLite's FTS5 (nearly every build has it) the words are looked up in an index, kept right by triggers on
clinical_records, so every way of writing or deleting a record is covered, foreign key cascades included. The index is
created and filled by a background data fix. Until that has finished, or on a build without FTS5, the same search runs by
reading the person's records one by one: slower, with the same meaning. Triggers can't be created by a migration
(app/core/db.py splits those on semicolons), and a migration can't depend on a compile option anyway.
"""

from __future__ import annotations

import logging
import sqlite3
from functools import lru_cache
from typing import Any, Optional

from app.core import data_fixes
from app.core.db import db, read
from app.store import lab_catalog

log = logging.getLogger("syntropy.search")

FIX = "record-search-index"
_TABLE = "record_search_fts"
_TRIGGERS = ("record_search_inserted", "record_search_updated", "record_search_deleted")
MAX_WORDS = 12
# Words that only make a search miss. Both languages the app is most used in.
_NOISE = {"a", "an", "and", "the", "of", "for", "in", "on", "with", "my", "to", "y", "de", "del", "la", "el", "los", "las"}


@lru_cache(maxsize=1)
def fts5_available() -> bool:
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.Error:
        log.warning("This build of SQLite has no FTS5, so searching records reads them one by one")
        return False


def words(query: Optional[str]) -> list[str]:
    """The words to look for: lower case, without a plural's s, without words that only make a search miss. A query of
    nothing but those keeps them."""
    found = [w.lower() for w in (query or "").split() if any(c.isalnum() for c in w)]
    kept = [w for w in found if w not in _NOISE] or found
    singular = [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")) else w for w in kept]
    return list(dict.fromkeys(singular))[:MAX_WORDS]


def _text_of(row: str) -> str:
    """SQL for all of a record's words. Only values from its details: the keys would match every record, and the rest
    (links, references between records) isn't anything a person would search for."""
    return (f"coalesce({row}.code_display, '') || ' ' || coalesce({row}.code, '') || ' ' || coalesce({row}.value_text, '') || ' ' || "
            f"coalesce({row}.narrative, '') || ' ' || coalesce({row}.source_name, '') || ' ' || coalesce("
            f"(SELECT group_concat(atom, ' ') FROM json_tree(CASE WHEN json_valid({row}.details_json) THEN {row}.details_json ELSE '{{}}' END) "
            "WHERE type = 'text' AND replace(fullkey, '\"', '') NOT LIKE '$.attachments%' "       # some keys are quoted there
            "AND replace(fullkey, '\"', '') NOT LIKE '$.result_refs%' AND replace(fullkey, '\"', '') NOT LIKE '$.encounter%'), '')")


def _write_document(row: str) -> str:
    return (f"DELETE FROM {_TABLE} WHERE rowid = (SELECT n FROM record_search_ids WHERE record_id = {row}.id); "
            f"INSERT OR IGNORE INTO record_search_ids(record_id) VALUES ({row}.id); "
            f"INSERT INTO {_TABLE}(rowid, title, body) SELECT n, {row}.title, {_text_of(row)} FROM record_search_ids "
            f"WHERE record_id = {row}.id;")


def _create_index(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (_TABLE,)).fetchone():
        # From before the triggers were taken away (see at_start_up): anything could have changed since.
        conn.execute(f"DELETE FROM {_TABLE}")
        conn.execute("DELETE FROM record_search_ids")
    else:
        conn.execute(f"CREATE VIRTUAL TABLE {_TABLE} USING fts5(title, body, tokenize = 'unicode61 remove_diacritics 2')")
    inserted, updated, deleted = _TRIGGERS
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {inserted} AFTER INSERT ON clinical_records BEGIN {_write_document('new')} END")
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {updated} AFTER UPDATE OF title, code_display, code, value_text, narrative, "
                 f"source_name, details_json ON clinical_records BEGIN {_write_document('new')} END")
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {deleted} AFTER DELETE ON clinical_records BEGIN "
                 f"DELETE FROM {_TABLE} WHERE rowid = (SELECT n FROM record_search_ids WHERE record_id = old.id); "
                 "DELETE FROM record_search_ids WHERE record_id = old.id; END")


def _build(conn: sqlite3.Connection, cursor: Any, size: int) -> Any:
    """A data fix step. Records written while this runs are indexed by the triggers, and skipped here."""
    if not fts5_available():
        return None
    if cursor is None:
        _create_index(conn)
    rows = conn.execute("SELECT id FROM clinical_records WHERE id > ? ORDER BY id LIMIT ?", (cursor or "", size)).fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        missing = (f"FROM clinical_records r WHERE r.id IN ({marks}) "
                   "AND NOT EXISTS (SELECT 1 FROM record_search_ids i WHERE i.record_id = r.id)")
        fresh = [r[0] for r in conn.execute(f"SELECT r.id {missing}", ids)]
        if fresh:
            marks = ",".join("?" * len(fresh))
            conn.execute(f"INSERT INTO record_search_ids(record_id) SELECT id FROM clinical_records WHERE id IN ({marks})", fresh)
            conn.execute(f"INSERT INTO {_TABLE}(rowid, title, body) SELECT i.n, r.title, {_text_of('r')} FROM clinical_records r "
                         f"JOIN record_search_ids i ON i.record_id = r.id WHERE r.id IN ({marks})", fresh)
    return rows[-1]["id"] if rows else None


data_fixes.register(FIX, _build)


def ready() -> bool:
    if not fts5_available():
        return False
    with read() as conn:
        state = conn.execute("SELECT state FROM data_fixes WHERE name = ?", (FIX,)).fetchone()
        return bool(state and state["state"] == "done"
                    and conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (_TABLE,)).fetchone())


def at_start_up() -> None:
    """A database moved to a build without FTS5: a trigger naming the index would make every write to a record fail, so
    they go. Back on a build that has it, the index is built again."""
    with read() as conn:
        triggers = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?", (_TRIGGERS[0],)).fetchone()
        state = conn.execute("SELECT state FROM data_fixes WHERE name = ?", (FIX,)).fetchone()
    if bool(triggers) == fts5_available() or (fts5_available() and not state):
        return
    with db() as conn:
        for trigger in _TRIGGERS:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DELETE FROM data_fixes WHERE name = ?", (FIX,))


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _fts_query(found: list[str]) -> str:
    return " ".join('"' + w.replace('"', '""') + '"*' for w in found)


def where(query: str, profile_id: str, column: str = "id") -> tuple[str, list[Any]]:
    """A WHERE condition on clinical_records for "holds every word of this query". The caller still filters by person;
    the index covers everyone, and only says which records match."""
    found = words(query)
    if not found:
        return "0", []
    # Test names and abbreviations ("HbA1c") the source lab may have titled differently.
    codes = lab_catalog.loinc_codes(query)
    also, also_params = (f" OR code IN ({','.join('?' * len(codes))})", codes) if codes else ("", [])
    if ready():
        return (f"({column} IN (SELECT i.record_id FROM record_search_ids i WHERE i.n IN "
                f"(SELECT rowid FROM {_TABLE} WHERE {_TABLE} MATCH ?)){also})", [_fts_query(found), *also_params])
    matches = " AND ".join("t LIKE ? ESCAPE '\\'" for _ in found)
    return (f"({column} IN (SELECT id FROM (SELECT r.id, lower(r.title || ' ' || {_text_of('r')}) AS t FROM clinical_records r "
            f"WHERE r.profile_id = ?) WHERE {matches}){also})", [profile_id, *[_like(w) for w in found], *also_params])


def snippets(query: str, record_ids: list[str]) -> dict[str, str]:
    """Where each record matched, as a line of its text, when the index can say."""
    found = words(query)
    if not found or not record_ids or not ready():
        return {}
    marks = ",".join("?" * len(record_ids))
    with read() as conn:
        rows = conn.execute(
            f"SELECT i.record_id, snippet({_TABLE}, 1, '', '', '…', 24) AS text FROM {_TABLE} JOIN record_search_ids i ON i.n = {_TABLE}.rowid "
            f"WHERE {_TABLE} MATCH ? AND i.record_id IN ({marks})", (_fts_query(found), *record_ids)).fetchall()
    return {r["record_id"]: r["text"] for r in rows if r["text"]}
