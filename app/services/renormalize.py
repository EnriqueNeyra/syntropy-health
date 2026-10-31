"""
Stored records read again by today's normalizer, once per database, from the original resource each one keeps.

Live connections repair themselves on their next sync, but a file import, a one-time healow connection or a result typed in
never syncs again. This runs through ``data_fixes``, in short transactions, resuming after a restart. A later change to what
normalization produces registers a new version of the name.

Replaying isn't the same as importing again, so it only ever writes the fields normalization derives, and keeps what the raw
resource can't reproduce: the name and code of a medication whose Medication resource was never stored, a note's text fetched
separately, the panel a result belonged to. A row the normalizer can't read stays exactly as it is. The only row removed is one the
source itself says was a mistake, which is also what the next sync of a live connection would do.
"""

from __future__ import annotations

import json
from app.core.sqlite import sqlite3
import time
from typing import Any, Optional

from app.connectors import fhir_normalize
from app.core import data_fixes
from app.core.db import audit, dumps

NAME = "records-renormalize-2"

_DERIVED = ("category", "title", "code_system", "code", "code_display", "effective_at", "effective_end", "status", "value_num", "value_text",
            "unit", "value_norm", "unit_norm", "ref_low", "ref_high", "ref_text", "interpretation", "details_json", "narrative", "dedup_key")
# Details added after normalization, from outside the resource.
_KEPT_DETAILS = ("panel", "body_fingerprint", "body_stale", "result_refs")
_NO_NAME = {fhir_normalize.UNNAMED_MEDICATION, "Medication"}


def _stored_medication(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """A referenced Medication isn't stored, but its name and code were copied onto the record when it was read."""
    if row.get("title") in _NO_NAME:
        return None
    coding = [{"system": row.get("code_system"), "code": row["code"], "display": row.get("code_display")}] if row.get("code") else []
    return {"code": {"text": row.get("title"), "coding": coding}}


def reread(row: dict[str, Any], person: Optional[dict[str, Any]]) -> Any:
    """The derived fields for one stored row, "remove" for one the source retracted, or None to leave it as it is."""
    try:
        res = json.loads(row["raw_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(res, dict) or not res.get("resourceType"):
        return None
    try:
        rec = fhir_normalize.normalize_one(res, lambda ref: _stored_medication(row), person)
    except Exception:  # noqa: BLE001 - a bug in reading one resource never costs the stored record
        return None
    if rec is None:
        return "remove" if fhir_normalize.retracted(res) else None
    try:
        stored = json.loads(row["details_json"]) if row.get("details_json") else {}
    except ValueError:
        stored = {}
    details = rec.get("details") or {}
    for key in _KEPT_DETAILS:
        if stored.get(key) not in (None, ""):
            details[key] = stored[key]
    narrative = rec.get("narrative") or row.get("narrative")
    if narrative and narrative.startswith("{\\rtf"):        # a body fetched before notes in RTF could be read
        narrative = fhir_normalize.rtf_to_text(narrative)
    return {
        **{k: rec.get(k) for k in _DERIVED if k not in ("details_json", "narrative", "title")},
        "title": (rec.get("title") or "Untitled")[:500], "details_json": dumps(details), "narrative": narrative,
    }


def _person(conn: sqlite3.Connection, connection_id: str, known: dict[str, Any]) -> Optional[dict[str, Any]]:
    if connection_id not in known:
        row = conn.execute("SELECT gender, birth_date FROM patient_identities WHERE connection_id = ?", (connection_id,)).fetchone()
        known[connection_id] = dict(row) if row else None
    return known[connection_id]


def _date_by_visit(conn: sqlite3.Connection, row: dict[str, Any], fresh: dict[str, Any]) -> None:
    """A document that only points at its visit takes that visit's dates, as it does during a sync."""
    res = json.loads(row["raw_json"])
    visit_id = fhir_normalize.document_visit(res)
    visit = visit_id and conn.execute("SELECT effective_at, effective_end FROM clinical_records "
                                      "WHERE connection_id = ? AND resource_type = 'Encounter' AND resource_id = ?",
                                      (row["connection_id"], visit_id)).fetchone()
    if visit and visit["effective_at"]:
        fresh["effective_at"] = fhir_normalize.date_within_visit(res.get("date"), visit["effective_at"], visit["effective_end"])


def step(conn: sqlite3.Connection, cursor: Any, size: int) -> Any:
    state = cursor or {"after": 0, "changed": 0, "removed": 0}
    rows = conn.execute(
        "SELECT rowid AS row_number, * FROM clinical_records WHERE rowid > ? AND raw_json IS NOT NULL ORDER BY rowid LIMIT ?",
        (state["after"], size)).fetchall()
    if not rows:
        if state["changed"] or state["removed"]:
            audit(conn, "system", "records.renormalized", {"changed": state["changed"], "removed": state["removed"]})
        return None
    changed = removed = 0
    people: dict[str, Any] = {}
    now = time.time()
    for raw_row in rows:
        row = dict(raw_row)
        fresh = reread(row, _person(conn, row["connection_id"], people))
        if isinstance(fresh, dict):
            _date_by_visit(conn, row, fresh)
        if fresh == "remove":
            conn.execute("DELETE FROM clinical_records WHERE id = ?", (row["id"],))
            removed += 1
        elif fresh and any(fresh[k] != row[k] for k in _DERIVED):
            conn.execute(f"UPDATE clinical_records SET {', '.join(f'{k} = ?' for k in _DERIVED)}, updated_at = ? WHERE id = ?",
                         (*(fresh[k] for k in _DERIVED), now, row["id"]))
            changed += 1
    return {"after": rows[-1]["row_number"], "changed": state["changed"] + changed, "removed": state["removed"] + removed}


def written_ranges_step(conn: sqlite3.Connection, cursor: Any, size: int) -> Any:
    """Results whose range came only as text were never compared with it. They're read again now that simple ones can be."""
    after = cursor or 0
    top = conn.execute("SELECT max(rowid) FROM (SELECT rowid FROM clinical_records WHERE rowid > ? ORDER BY rowid LIMIT ?)", (after, size)).fetchone()[0]
    if top is None:
        return None
    rows = conn.execute("SELECT * FROM clinical_records WHERE rowid > ? AND rowid <= ? AND raw_json IS NOT NULL AND ref_text IS NOT NULL "
                        "AND ref_low IS NULL AND ref_high IS NULL AND ref_text GLOB '*[0-9]*'", (after, top)).fetchall()
    people: dict[str, Any] = {}
    now = time.time()
    for raw_row in rows:
        row = dict(raw_row)
        fresh = reread(row, _person(conn, row["connection_id"], people))
        if isinstance(fresh, dict) and any(fresh[k] != row[k] for k in _DERIVED):
            conn.execute(f"UPDATE clinical_records SET {', '.join(f'{k} = ?' for k in _DERIVED)}, updated_at = ? WHERE id = ?",
                         (*(fresh[k] for k in _DERIVED), now, row["id"]))
    return top


data_fixes.register(NAME, step)
data_fixes.register("records-written-ranges-compared", written_ranges_step)
