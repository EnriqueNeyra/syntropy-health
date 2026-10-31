"""Normalized clinical records: persistence, cross-source de-duplication and queries."""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections import Counter, OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from app.connectors import units
from app.connectors.fhir_normalize import partial_pressure
from app.core import data_fixes, settings
from app.core.db import audit, db, finite, note_deleted, read, dumps, row_to_dict, rows_to_dicts, write_in_batches
from app.store import lab_catalog, record_overrides, search

CATEGORIES = [
    "conditions", "medications", "allergies", "labs", "vitals", "immunizations", "encounters",
    "procedures", "notes", "reports", "care_team", "care_plans", "goals", "devices", "coverage", "observations", "other",
]

CATEGORY_LABELS = {
    "conditions": "Conditions", "medications": "Medications", "allergies": "Allergies",
    "labs": "Lab results", "vitals": "Vital signs", "immunizations": "Immunizations",
    "encounters": "Visits", "procedures": "Procedures", "notes": "Clinical notes",
    "reports": "Diagnostic reports", "care_team": "Care team", "care_plans": "Care plans",
    "goals": "Goals", "devices": "Devices", "coverage": "Insurance", "observations": "Other observations", "other": "Other records",
}

ENDED_MED_STATUSES = ("stopped", "completed", "cancelled", "entered-in-error", "not-taken", "superseded")
NOT_STARTED_MED_STATUSES = ("draft", "intended")
NOT_STARTED_MED_INTENTS = ("proposal", "option")
MED_RENEWAL_DAYS = 548    # about 18 months: an order this old with no renewal may well be out of date
INACTIVE_CONDITION_STATUSES = ("resolved", "inactive", "remission", "entered-in-error")
ACTIVE_CONDITION_STATUSES = ("active", "recurrence", "relapse")
UNCONFIRMED_VERIFICATION = ("unconfirmed", "provisional", "differential")
INACTIVE_ALLERGY_STATUSES = ("resolved", "inactive", "entered-in-error")
NOT_ALLERGIES = ("no-known-allergies", "not-asked")
ABNORMAL = ("high", "low", "critical_high", "critical_low", "abnormal")
CRITICAL = ("critical_high", "critical_low")
# A flagged result this old is shown as older and left out of alerts: longer than the gap between yearly check-ups.
OLDER_RESULT_DAYS = 548

LIST_COLUMNS = (
    "id, profile_id, connection_id, resource_type, resource_id, category, title, code_system, code, "
    "code_display, effective_at, effective_end, status, value_num, value_text, unit, value_norm, unit_norm, "
    "ref_low, ref_high, ref_text, interpretation, details_json, narrative, dedup_key, source_name, "
    "first_seen_at, updated_at"
)
_LAB_FLAG_COLUMNS = ("id, connection_id, title, code, code_system, code_display, unit, value_num, value_text, effective_at, status, "
                     "interpretation, ref_text, dedup_key, source_name")
SUMMARY_LABS = 2000
SMOKING_STATUS_CODE = "72166-2"
SERIES_COLUMNS = ("id, connection_id, title, category, code, code_system, effective_at, status, value_num, value_text, unit, value_norm, unit_norm, "
                  "ref_low, ref_high, interpretation, details_json, dedup_key, source_name")

SHRINK_FLOOR = 3                # up to this many records can go at once: the source correcting a few things is normal
SHRINK_FRACTION = 0.25          # more than this share of a category vanishing looks like the source's trouble, not corrections
SHRINK_WAIT_DAYS = 14           # how long such a loss has to last before it's believed
REMOVED_KEEP_DAYS = 365         # how long a removed record stays available to put back


_DOUBLED_PRESSURE = re.compile(r"^([\d.]+)/\1 mmHg$")


def record_id(connection_id: str, resource_type: str, resource_id: str) -> str:
    return hashlib.sha1(f"{connection_id}|{resource_type}|{resource_id}".encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def upsert_identity(connection_id: str, profile_id: str, patient: dict[str, Any]) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO patient_identities(connection_id, profile_id, full_name, birth_date, gender, mrn,
                   telecom_json, address_json, raw_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(connection_id) DO UPDATE SET full_name = excluded.full_name,
                   birth_date = excluded.birth_date, gender = excluded.gender, mrn = excluded.mrn,
                   telecom_json = excluded.telecom_json, address_json = excluded.address_json,
                   raw_json = excluded.raw_json, updated_at = excluded.updated_at""",
            (connection_id, profile_id, patient.get("name"), patient.get("birth_date"), patient.get("gender"),
             patient.get("mrn"), dumps(patient.get("telecom") or []), dumps(patient.get("address") or []),
             dumps(patient.get("raw")), time.time()),
        )


def identity_of(connection_id: str) -> Optional[dict[str, Any]]:
    with read() as conn:
        row = conn.execute("SELECT full_name, birth_date FROM patient_identities WHERE connection_id = ?", (connection_id,)).fetchone()
    return dict(row) if row else None


def upsert_references(connection_id: str, resources: Iterable[dict[str, Any]]) -> None:
    now = time.time()
    with db() as conn:
        conn.executemany(
            "INSERT INTO fhir_references(connection_id, resource_type, resource_id, raw_json, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(connection_id, resource_type, resource_id) DO UPDATE SET raw_json = excluded.raw_json, updated_at = excluded.updated_at",
            [(connection_id, r["resourceType"], str(r["id"]), dumps(r), now) for r in resources])


_COMPARED = ("profile_id, connection_id, resource_type, resource_id, category, title, code_system, code, code_display, "
             "effective_at, effective_end, status, value_num, value_text, unit, value_norm, unit_norm, ref_low, ref_high, "
             "ref_text, interpretation, details_json, narrative, dedup_key, source_name, raw_json")


def upsert_records(profile_id: str, connection_id: str, source_name: str, records: Iterable[dict[str, Any]]) -> dict[str, int]:
    """In batches, so a long history doesn't hold the write lock from a phone's upload. Safe to repeat."""
    items = list(records)
    now = time.time()
    totals = {"inserted": 0, "updated": 0}

    def step(conn: Any, cursor: Any, size: int) -> Optional[int]:
        start = cursor or 0
        chunk = items[start:start + size]
        added, changed = _upsert_batch(conn, profile_id, connection_id, source_name, chunk, now)
        totals["inserted"] += added
        totals["updated"] += changed
        return start + size if start + size < len(items) else None
    write_in_batches(step)
    return totals


def _upsert_batch(conn: Any, profile_id: str, connection_id: str, source_name: str, records: list[dict[str, Any]],
                  now: float) -> tuple[int, int]:
    inserted = updated = 0
    stopped: list[str] = []
    for r in records:
        rid = record_id(connection_id, r["resource_type"], r["resource_id"])
        existing = conn.execute(f"SELECT {_COMPARED} FROM clinical_records WHERE id = ?", (rid,)).fetchone()
        values = (
            profile_id, connection_id, r["resource_type"], r["resource_id"], r["category"],
            (r.get("title") or "Untitled")[:500], r.get("code_system"), r.get("code"), r.get("code_display"),
            r.get("effective_at"), r.get("effective_end"), r.get("status"), finite(r.get("value_num")),
            r.get("value_text"), r.get("unit"), finite(r.get("value_norm")), r.get("unit_norm"),
            finite(r.get("ref_low")), finite(r.get("ref_high")), r.get("ref_text"), r.get("interpretation"),
            dumps(r.get("details") or {}), r.get("narrative"), r.get("dedup_key"), source_name,
            dumps(r.get("raw")), now,
        )
        if existing and tuple(existing) == values[:-1]:
            continue   # unchanged: keep updated_at meaningful and the sync stats honest
        if existing:
            if r["category"] == "medications" and (r.get("status") or "").lower() in ENDED_MED_STATUSES \
                    and (existing["status"] or "").lower() not in ENDED_MED_STATUSES:
                stopped.append(rid)
            conn.execute(
                """UPDATE clinical_records SET profile_id=?, connection_id=?, resource_type=?, resource_id=?,
                       category=?, title=?, code_system=?, code=?, code_display=?, effective_at=?, effective_end=?,
                       status=?, value_num=?, value_text=?, unit=?, value_norm=?, unit_norm=?, ref_low=?, ref_high=?,
                       ref_text=?, interpretation=?, details_json=?, narrative=?, dedup_key=?, source_name=?,
                       raw_json=?, updated_at=? WHERE id=?""",
                (*values, rid),
            )
            updated += 1
        else:
            # A record its source had dropped and now sends again: it comes back as what it was, not as new. (An
            # outage must not make old results look like they just arrived.)
            back = conn.execute("SELECT first_seen_at FROM clinical_records_removed WHERE id = ?", (rid,)).fetchone()
            conn.execute(
                """INSERT INTO clinical_records(profile_id, connection_id, resource_type, resource_id, category,
                       title, code_system, code, code_display, effective_at, effective_end, status, value_num,
                       value_text, unit, value_norm, unit_norm, ref_low, ref_high, ref_text, interpretation,
                       details_json, narrative, dedup_key, source_name, raw_json, updated_at, id, first_seen_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*values, rid, back["first_seen_at"] if back else now),
            )
            if back:
                conn.execute("DELETE FROM clinical_records_removed WHERE id = ?", (rid,))
            inserted += 1
    if stopped:
        _note_stopped(conn, profile_id, stopped, now)
    return inserted, updated


def _note_stopped(conn: Any, profile_id: str, ids: list[str], now: float) -> None:
    """When a medication went from going to ended, written down as it happens: updated_at moves for any change to a row at all."""
    key = stopped_key(profile_id)
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    log = (json.loads(row["value"]) if row else []) + [[rid, now] for rid in ids]
    conn.execute("INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                 (key, json.dumps(log[-STOPPED_LOG_MOST:]), now))


def _set_aside(conn: Any, ids: list[str], reason: str, now: float) -> None:
    """Moves records out of clinical_records, every read of which then stops seeing them, to where they can be put back."""
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT * FROM clinical_records WHERE id IN ({marks})", chunk).fetchall():
            conn.execute(
                """INSERT OR REPLACE INTO clinical_records_removed(id, profile_id, connection_id, resource_type, resource_id,
                       category, title, effective_at, source_name, first_seen_at, row_json, removed_at, reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["id"], row["profile_id"], row["connection_id"], row["resource_type"], row["resource_id"], row["category"],
                 row["title"], row["effective_at"], row["source_name"], row["first_seen_at"], json.dumps(dict(row)), now, reason),
            )
        conn.execute(f"DELETE FROM clinical_records WHERE id IN ({marks})", chunk)


def reconcile(connection_id: str, seen_ids: set[str], verdict: Any, retracted: Iterable[str] = ()) -> dict[str, Any]:
    """Squares what's stored with what the source just said. ``verdict(resource)`` says what a stored resource missing
    from its answer means (see smart.FetchResult.prune_verdict); ``retracted`` are resources it returned marked as wrong.

    Nothing goes on one answer alone. A record leaves only when a second complete answer agrees, never on an empty answer,
    and if many go at once they stay until that has lasted SHRINK_WAIT_DAYS. What goes is set aside, not deleted."""
    now = time.time()
    with read() as conn:
        rows = conn.execute("SELECT id, resource_type, resource_id, category, missing_since, kept_at "
                            "FROM clinical_records WHERE connection_id = ?", (connection_id,)).fetchall()
    def key(r: Any) -> str:
        return f"{r['resource_type']}/{r['resource_id']}"
    gone = set(retracted)
    took_back = [r["id"] for r in rows if key(r) in gone and r["kept_at"] is None]
    in_answer = [r["id"] for r in rows if key(r) in seen_ids and r["missing_since"] is not None]
    absent = [r for r in rows if key(r) not in seen_ids and key(r) not in gone and r["kept_at"] is None]
    verdicts = _verdicts(absent, verdict)
    stored: dict[str, int] = {}
    for r in rows:
        stored[r["category"]] = stored.get(r["category"], 0) + 1

    newly_missing, leaving_by_category, held = [], {}, {}
    for r in absent:
        v = verdicts[r["id"]]
        if v == "hold":
            held[r["category"]] = held.get(r["category"], 0) + 1
        elif v == "prune":
            if r["missing_since"] is None:
                newly_missing.append(r["id"])
            else:
                leaving_by_category.setdefault(r["category"], []).append(r)
    removing: list[str] = []
    wait = SHRINK_WAIT_DAYS * 86400
    for category, leaving in leaving_by_category.items():
        if len(leaving) > max(SHRINK_FLOOR, SHRINK_FRACTION * stored[category]):
            for r in leaving:
                if now - r["missing_since"] >= wait:
                    removing.append(r["id"])
                else:
                    held[category] = held.get(category, 0) + 1
        else:
            removing.extend(r["id"] for r in leaving)

    work = [("retracted", i) for i in took_back] + [("missing", i) for i in removing] \
        + [("mark", i) for i in newly_missing] + [("clear", i) for i in in_answer]
    if work:
        def step(conn: Any, cursor: Any, size: int) -> Optional[int]:
            start = cursor or 0
            for what, rid in work[start:start + size]:
                if what == "mark":
                    conn.execute("UPDATE clinical_records SET missing_since = ? WHERE id = ?", (now, rid))
                elif what == "clear":
                    conn.execute("UPDATE clinical_records SET missing_since = NULL WHERE id = ?", (rid,))
                else:
                    _set_aside(conn, [rid], what, now)
            return start + size if start + size < len(work) else None
        write_in_batches(step)
    with db() as conn:
        conn.execute("DELETE FROM clinical_records_removed WHERE connection_id = ? AND removed_at < ?",
                     (connection_id, now - REMOVED_KEEP_DAYS * 86400))
        # A document stays as long as its record could be put back.
        conn.execute("""DELETE FROM record_files WHERE connection_id = ?1 AND NOT EXISTS (SELECT 1 FROM clinical_records r WHERE r.connection_id = ?1
                            AND r.resource_type = record_files.resource_type AND r.resource_id = record_files.resource_id)
                        AND NOT EXISTS (SELECT 1 FROM clinical_records_removed x WHERE x.connection_id = ?1
                            AND x.resource_type = record_files.resource_type AND x.resource_id = record_files.resource_id)""", (connection_id,))
    return {"removed": len(took_back) + len(removing), "retracted": len(took_back), "held": held, "missing": len(newly_missing)}


def _verdicts(rows: list[Any], verdict: Any) -> dict[str, str]:
    """Only the records missing from an answer need their full resource read."""
    out: dict[str, str] = {}
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        with read() as conn:
            raws = dict(conn.execute(f"SELECT id, raw_json FROM clinical_records WHERE id IN ({','.join('?' * len(chunk))})",
                                     [r["id"] for r in chunk]).fetchall())
        for r in chunk:
            try:
                raw = json.loads(raws.get(r["id"]) or "null")
            except ValueError:
                raw = None
            out[r["id"]] = verdict(raw if isinstance(raw, dict) else {"resourceType": r["resource_type"]})
    return out


def retract(connection_id: str, resource_keys: Iterable[str]) -> int:
    """A corrected file said these were wrong. Same as a source saying it, but with nothing else to square up."""
    wanted = set(resource_keys)
    if not wanted:
        return 0
    with read() as conn:
        rows = conn.execute("SELECT id, resource_type, resource_id, kept_at FROM clinical_records WHERE connection_id = ?",
                            (connection_id,)).fetchall()
    ids = [r["id"] for r in rows if f"{r['resource_type']}/{r['resource_id']}" in wanted and r["kept_at"] is None]
    if ids:
        with db() as conn:
            _set_aside(conn, ids, "retracted", time.time())
    return len(ids)


def removed_for_connection(connection_id: str) -> list[dict[str, Any]]:
    with read() as conn:
        rows = conn.execute(
            "SELECT id, connection_id, profile_id, resource_type, category, title, effective_at, source_name, removed_at, reason "
            "FROM clinical_records_removed WHERE connection_id = ? ORDER BY removed_at DESC, title LIMIT 500", (connection_id,)).fetchall()
    return [dict(r) for r in rows]


def removed_count(connection_id: str) -> int:
    with read() as conn:
        return conn.execute("SELECT COUNT(*) FROM clinical_records_removed WHERE connection_id = ?", (connection_id,)).fetchone()[0]


def get_removed(record_id_: str) -> Optional[dict[str, Any]]:
    with read() as conn:
        row = conn.execute("SELECT id, connection_id, profile_id, title FROM clinical_records_removed WHERE id = ?", (record_id_,)).fetchone()
    return dict(row) if row else None


def restore_removed(record_id_: str, actor: str = "user") -> bool:
    """Puts a removed record back and keeps it: the person has looked and wants it, whatever its source says from now on."""
    with db() as conn:
        row = conn.execute("SELECT profile_id, title, row_json FROM clinical_records_removed WHERE id = ?", (record_id_,)).fetchone()
        if not row:
            return False
        saved = json.loads(row["row_json"])
        columns = [c["name"] for c in conn.execute("PRAGMA table_info(clinical_records)").fetchall()]
        values = {**{c: saved[c] for c in columns if c in saved}, "kept_at": time.time(), "missing_since": None}
        names = list(values)
        conn.execute(f"INSERT OR REPLACE INTO clinical_records({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                     [values[n] for n in names])
        conn.execute("DELETE FROM clinical_records_removed WHERE id = ?", (record_id_,))
        audit(conn, actor, "record.restored", {"record": record_id_, "title": row["title"]}, row["profile_id"])
    return True


def _fix_partial_blood_pressure(conn: Any, cursor: Any, size: int) -> Any:
    """A panel with one reading was stored as "95/95 mmHg", an invented pressure. Sync only rewrites what it fetches
    again, and one-time imports never do. Filtered here rather than in SQL, so a step is ``size`` rows at most."""
    rows = conn.execute("SELECT rowid, value_text, CASE WHEN category = 'vitals' THEN details_json END FROM clinical_records "
                        "WHERE rowid > ? ORDER BY rowid LIMIT ?", (cursor or 0, size)).fetchall()
    for rowid, text, details in rows:
        match = _DOUBLED_PRESSURE.match(text or "") if details else None
        if not match:
            continue
        readings = [c for c in (json.loads(details).get("components") or []) if c.get("value") is not None]
        if len(readings) == 1 and float(match[1]) == float(readings[0]["value"]):
            value = readings[0]["value"]
            fixed = partial_pressure(value, None) if readings[0].get("code") == "8480-6" else partial_pressure(None, value)
            conn.execute("UPDATE clinical_records SET value_text = ? WHERE rowid = ?", (fixed, rowid))
    return rows[-1][0] if rows else None


data_fixes.register("partial-blood-pressure-text", _fix_partial_blood_pressure)


def delete_record(record_id_: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM clinical_records WHERE id = ?", (record_id_,))
    note_deleted()


def delete_for_connection(connection_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM clinical_records WHERE connection_id = ?", (connection_id,))
        conn.execute("DELETE FROM clinical_records_removed WHERE connection_id = ?", (connection_id,))
        conn.execute("DELETE FROM record_files WHERE connection_id = ?", (connection_id,))
        conn.execute("DELETE FROM sync_query_state WHERE connection_id = ?", (connection_id,))
        conn.execute("DELETE FROM patient_identities WHERE connection_id = ?", (connection_id,))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _load(rows) -> list[dict[str, Any]]:
    return rows_to_dicts(rows, ["details_json"])


NO_INFINITY_FIX = "records-no-infinity"
_NUMBERS = ("value_num", "value_norm", "ref_low", "ref_high")


def _clear_infinities(conn: Any, cursor: Any, size: int) -> Any:
    """A portal's 1e999 used to be kept, as infinity: no measurement, and it can't be sent as JSON, so whatever held the
    record failed to load."""
    rows = conn.execute("SELECT id FROM clinical_records WHERE id > ? ORDER BY id LIMIT ?", (cursor or "", size)).fetchall()
    if not rows:
        return None
    last = rows[-1]["id"]
    conn.execute(f"UPDATE clinical_records SET {', '.join(f'{c} = CASE WHEN abs({c}) = 9e999 THEN NULL ELSE {c} END' for c in _NUMBERS)} "
                 f"WHERE id > ? AND id <= ? AND ({' OR '.join(f'abs({c}) = 9e999' for c in _NUMBERS)})", (cursor or "", last))
    return last


data_fixes.register(NO_INFINITY_FIX, _clear_infinities)


def friendly_day(when: Optional[str]) -> str:
    """A date at the precision it was given, in words for a note or a reason: "Mar 1, 2024", "Jun 2015", "2015"."""
    text = str(when or "")
    try:
        if len(text) >= 10:
            d = date.fromisoformat(text[:10])
            return f"{d:%b} {d.day}, {d.year}"
        if len(text) == 7:
            return f"{date.fromisoformat(text + '-01'):%b %Y}"
    except ValueError:
        pass
    return text


def _ended(rec: dict[str, Any], today: date) -> bool:
    """A condition with an end in the past, whatever its status says. An end stated only in words counts: it has ended."""
    end, details = rec.get("effective_end"), rec.get("details") or {}
    if end:
        return str(end)[:10] <= today.isoformat()
    return bool(details.get("abated"))


def _category_words(rec: dict[str, Any]) -> set[str]:
    """A Condition's categories as codes. Rows stored before those were kept have only the display text."""
    details = rec.get("details") or {}
    return {str(c).strip().lower().replace(" ", "-") for c in (*(details.get("category_codes") or []), *(details.get("categories") or [])) if c}


def _visit_only(rec: dict[str, Any]) -> bool:
    words = _category_words(rec)
    return bool(words & {"encounter-diagnosis", "encounter-dx"}) and not words & {"problem-list-item", "problem-list", "health-concern"}


def condition_class(rec: dict[str, Any], today: Optional[date] = None) -> str:
    """The one rule for what a condition means right now. "active" is an active problem: clinical status active, recurrence or
    relapse, on the problem list or with no category, and not ended. The rest stay visible under their own name: "unconfirmed",
    "unknown" (no status stated), "visit" (a diagnosis made at a visit) and "inactive"."""
    if _said_wrong(rec):
        return "corrected"
    today = today or date.today()
    status = (rec.get("status") or "").lower()
    verification = ((rec.get("details") or {}).get("verification") or "").lower()
    if status in INACTIVE_CONDITION_STATUSES or _ended(rec, today):
        return "inactive"
    if _visit_only(rec):
        return "visit"
    if status not in ACTIVE_CONDITION_STATUSES:
        return "unknown"
    return "unconfirmed" if verification in UNCONFIRMED_VERIFICATION else "active"


def _said_wrong(rec: dict[str, Any]) -> bool:
    return (rec.get("override") or {}).get("state") == record_overrides.WRONG


def _allergy_active(rec: dict[str, Any]) -> bool:
    if _said_wrong(rec):
        return False
    return (rec.get("status") or "active").lower() not in INACTIVE_ALLERGY_STATUSES + NOT_ALLERGIES


def _med_not_started(rec: dict[str, Any]) -> bool:
    status = (rec.get("status") or "").lower()
    return status in NOT_STARTED_MED_STATUSES or ((rec.get("details") or {}).get("intent") or "").lower() in NOT_STARTED_MED_INTENTS


def _when(rec: dict[str, Any]) -> str:
    """When a source last said this. For a condition the onset is years old however recently it was reviewed."""
    details = rec.get("details") or {}
    return str(details.get("last_updated") or details.get("recorded") or rec.get("effective_at") or "")


def _newest_by_source(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    newest: dict[str, dict[str, Any]] = {}
    for r in sorted(rows, key=_when, reverse=True):
        newest.setdefault(r["connection_id"], r)
    return list(newest.values())


def _settled(rows: list[dict[str, Any]], kind: str) -> tuple[dict[str, Any], list[dict[str, Any]], Optional[str]]:
    """Which row speaks for one fact reported several times, and what each source says now. Each source's newest word counts, and
    safety beats tidiness: a condition, allergy or medication still current at any source stays current, noting the other view."""
    if kind == "cond":
        # A visit's diagnosis is what was true that day: it neither revives a resolved problem nor closes an open one.
        problems = [r for r in rows if not _visit_only(r)]
        votes = _newest_by_source([r for r in problems if r.get("status")])
        current = [r for r in votes if condition_class(r) == "active"] or [r for r in votes if condition_class(r) == "unconfirmed"]
        lead = current or votes or problems or rows
    elif kind == "alg":
        votes = _newest_by_source(rows)
        # A dated "inactive" doesn't outrank an active row with no date from the same source: nothing says which is newer.
        current = [r for r in rows if _allergy_active(r) and (r in votes or not _when(r))]
        lead = current or votes
    else:
        # A draft of the next renewal says nothing about the prescription in force.
        votes = _newest_by_source([r for r in rows if not _med_not_started(r)])
        current = [r for r in votes if (r.get("status") or "").lower() not in ENDED_MED_STATUSES]
        lead = current or votes or rows
    lead = sorted(lead, key=_when, reverse=True)
    others = [r for r in votes if r not in current]
    note = None
    if current and others:
        still = ", ".join(dict.fromkeys(str(r.get("source_name") or "another source") for r in current))
        said = "; ".join(f"Marked {(r.get('status') or 'inactive').lower()} by {r.get('source_name') or 'another source'}"
                         f"{' on ' + friendly_day(_when(r)) if _when(r) else ''}" for r in others)
        note = f"{said}. Still active at {still}."
    return lead[0], votes, note


def _condition_aliases(records: list[dict[str, Any]]) -> dict[str, str]:
    """A condition coded only in ICD-10 is the same as one coded in SNOMED with that ICD code, when just one SNOMED concept on
    this record carries it. ICD codes are coarser, so with two, merging would hide a real diagnosis; those stay apart."""
    by_icd: dict[str, set[str]] = {}
    for r in records:
        if (r.get("dedup_key") or "").startswith("cond|"):
            details = r.get("details") or {}
            if details.get("icd10") and details.get("snomed"):
                by_icd.setdefault(details["icd10"], set()).add(details["snomed"])
    return {f"cond|{icd}": f"cond|{next(iter(s))}" for icd, s in by_icd.items() if len(s) == 1}


def _report_text(rec: dict[str, Any]) -> str:
    return (rec.get("narrative") or "").strip()[:500]


def _report_slot(key: str, r: dict[str, Any], slots: dict[str, list[list[dict[str, Any]]]]) -> str:
    """Reports with a generic code on one day aren't the same report: two chest films, two CBCs in a hospital stay. One source's
    reports never merge with each other. Another source's copy pairs with one of them: the same id if it's the same file imported
    again, otherwise the next one with no conflicting text."""
    groups = slots.setdefault(key, [])

    def fits(rows: list[dict[str, Any]]) -> bool:
        text = _report_text(r)
        return (all(m["connection_id"] != r["connection_id"] for m in rows)
                and all(not text or not _report_text(m) or _report_text(m) == text for m in rows))
    same_id = next((i for i, rows in enumerate(groups) if fits(rows) and any(m.get("resource_id") == r.get("resource_id") for m in rows)), None)
    slot = same_id if same_id is not None else next((i for i, rows in enumerate(groups) if fits(rows)), None)
    if slot is None:
        slot = len(groups)
        groups.append([])
    groups[slot].append(r)
    return key if slot == 0 else f"{key}#{slot}"


def _group_rows(records: list[dict[str, Any]]) -> "OrderedDict[str, list[dict[str, Any]]]":
    """The one grouping behind lists, counts and "also reported by": which rows are one real-world fact."""
    aliases = _condition_aliases(records)
    slots: dict[str, list[list[dict[str, Any]]]] = {}
    groups: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
    for r in records:
        key = r.get("dedup_key") or r["id"]
        key = aliases.get(key, key)
        if key.startswith("rep|"):
            key = _report_slot(key, r, slots)
        groups.setdefault(key, []).append(r)
    return groups


def _dedupe(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapses records describing the same fact from multiple sources, keeping the newest, or the row that keeps the person safe."""
    out = []
    for key, rows in _group_rows(records).items():
        lead, votes, note = rows[0], [], None
        kind = key.split("|", 1)[0]
        if len(rows) > 1 and kind in ("cond", "alg", "med"):
            lead, votes, note = _settled(rows, kind)
        sources: dict[str, dict[str, Any]] = {}
        for r in rows:
            sources.setdefault(r["connection_id"], {"connection_id": r["connection_id"], "source_name": r.get("source_name"), "record_id": r["id"],
                                                    "status": r.get("status"), "effective_at": r.get("effective_at")})
        lead["sources"] = list(sources.values())
        if note:
            lead["status_conflict"] = True
            lead["status_note"] = note
        if len(votes) > 1:
            lead["status_by_source"] = [{"source_name": v.get("source_name"), "status": v.get("status"), "as_of": _when(v) or None} for v in votes]
        out.append(lead)
    return out


def _local_midnight_utc(day: date) -> str:
    try:
        tz = ZoneInfo(settings.timezone_name())
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    return datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_bound(value: Optional[str]) -> tuple[Optional[date], bool]:
    """The bound's date, and whether that's all it is."""
    try:
        return date.fromisoformat(value), True      # type: ignore[arg-type]
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00")).date(), False
    except ValueError:
        return None, False


# A record's time as stored, which can carry any offset, or be just a date (a whole day, and not in any time zone).
_WHEN = "coalesce(datetime(effective_at), effective_at)"
_DATE_ONLY = "length(effective_at) <= 10"


def _since_clause(since: Optional[str]) -> tuple[str, list[Any]]:
    """A plain date is a whole day where the person is. The calendar's first year is how to say "no limit" (it can't be
    worked with: midnight there can be before the calendar starts)."""
    day, plain = _parse_bound(since)
    if since and not day:
        return "effective_at >= ?", [since]
    if not day or day.year == date.min.year:
        return "", []
    return (f"(CASE WHEN {_DATE_ONLY} THEN effective_at >= ? ELSE {_WHEN} >= datetime(?) END)",
            [day.isoformat(), _local_midnight_utc(day) if plain else since])


def _until_clause(until: Optional[str]) -> tuple[str, list[Any]]:
    """Inclusive, so a plain date runs to the end of that day: '2026-08-05T08:05:00Z' <= '2026-08-05' isn't true as text.
    The calendar's last year is how to say "no limit": 9999-12-31 is common, and has no next day."""
    day, plain = _parse_bound(until)
    if until and not day:
        return "effective_at <= ?", [until]
    if not day or day.year == date.max.year:
        return "", []
    if plain:
        return (f"(CASE WHEN {_DATE_ONLY} THEN effective_at <= ? ELSE {_WHEN} < datetime(?) END)",
                [day.isoformat(), _local_midnight_utc(day + timedelta(days=1))])
    return f"(CASE WHEN {_DATE_ONLY} THEN effective_at <= ? ELSE {_WHEN} <= datetime(?) END)", [day.isoformat(), until]


# Narrowing by what's been added to an entry: a document kept with it, or the person's own words.
HAS_CLAUSES = {
    "documents": "EXISTS (SELECT 1 FROM record_files f WHERE f.connection_id = clinical_records.connection_id "
                 "AND f.resource_type = clinical_records.resource_type AND f.resource_id = clinical_records.resource_id)",
    "corrected": "coalesce(dedup_key, 'id:' || id) IN (SELECT dedup_key FROM record_overrides o WHERE o.profile_id = clinical_records.profile_id)",
}


def list_records(
    profile_id: str,
    category: Optional[str] = None,
    *,
    q: Optional[str] = None,
    connection_id: Optional[str] = None,
    code: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    status: Optional[str] = None,
    dedupe: bool = True,
    limit: int = 200,
    offset: int = 0,
    kind: Optional[str] = None,
    note_type: Optional[str] = None,
    author: Optional[str] = None,
    has: Optional[str] = None,
) -> dict[str, Any]:
    where, params = ["profile_id = ?"], [profile_id]
    if has in HAS_CLAUSES:
        where.append(HAS_CLAUSES[has])
    if kind:
        where.append("json_extract(details_json, '$.kind') = ?")
        params.append(kind)
    if note_type:
        where.append("json_extract(details_json, '$.type') = ?")
        params.append(note_type)
    if author:
        where.append("EXISTS (SELECT 1 FROM json_each(details_json, '$.authors') WHERE value = ?)")
        params.append(author)
    if category:
        cats = category.split(",")
        where.append(f"category IN ({','.join('?' * len(cats))})")
        params.extend(cats)
    if connection_id:
        where.append("connection_id = ?")
        params.append(connection_id)
    if code:
        where.append("code = ?")
        params.append(code)
    for clause, values in (_since_clause(since), _until_clause(until)):
        if clause:
            where.append(clause)
            params.extend(values)
    if status:
        where.append("lower(status) = ?")
        params.append(status.lower())
    if q and q.strip():
        clause, values = search.where(q, profile_id)
        where.append(clause)
        params.extend(values)
    sql = (f"SELECT {LIST_COLUMNS} FROM clinical_records WHERE {' AND '.join(where)} "
           "ORDER BY coalesce(effective_at, '') DESC, updated_at DESC")
    with read() as conn:
        rows = _load(conn.execute(sql, params).fetchall())
    if dedupe:
        rows = _dedupe(rows)
    record_overrides.attach(profile_id, rows)
    total = len(rows)
    return {"total": total, "offset": offset, "limit": limit, "items": rows[offset:offset + limit]}


_LIGHT_ROWS = {
    "conditions": ("id, connection_id, resource_id, dedup_key, json_extract(details_json, '$.icd10') AS icd10, "
                   "json_extract(details_json, '$.snomed') AS snomed"),
    "reports": "id, connection_id, resource_id, dedup_key, substr(narrative, 1, 600) AS narrative",
}


def _light_rows(conn, profile_id: str, category: str) -> list[dict[str, Any]]:
    """Just enough of a category's rows to group them, where grouping takes more than comparing dedup_key."""
    rows = [dict(r) for r in conn.execute(
        f"SELECT {_LIGHT_ROWS[category]} FROM clinical_records WHERE profile_id = ? AND category = ? "
        "ORDER BY coalesce(effective_at, '') DESC, updated_at DESC", (profile_id, category)).fetchall()]
    if category == "conditions":
        for r in rows:
            r["details"] = {"icd10": r.pop("icd10"), "snomed": r.pop("snomed")}
    return rows


def _reported_elsewhere(conn, rec: dict[str, Any]) -> list[dict[str, Any]]:
    """The other sources' rows of the same fact, by the grouping the lists use. Another row from the same source isn't "also reported"."""
    if not rec.get("dedup_key"):
        return []
    columns = "id, connection_id, source_name, effective_at, value_text, status"
    if rec["category"] not in _LIGHT_ROWS:
        return rows_to_dicts(conn.execute(
            f"SELECT {columns} FROM clinical_records WHERE profile_id = ? AND dedup_key = ? AND id != ? AND connection_id != ?",
            (rec["profile_id"], rec["dedup_key"], rec["id"], rec["connection_id"])).fetchall())
    group = next((rows for rows in _group_rows(_light_rows(conn, rec["profile_id"], rec["category"])).values()
                  if any(r["id"] == rec["id"] for r in rows)), [])
    ids = [r["id"] for r in group if r["connection_id"] != rec["connection_id"]]
    if not ids:
        return []
    return rows_to_dicts(conn.execute(
        f"SELECT {columns} FROM clinical_records WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall())


def _same_source_orders(conn, rec: dict[str, Any]) -> list[dict[str, Any]]:
    """A prescription renewed: the same drug from the same source, on rows of its own, which a list folds into the newest."""
    if rec["category"] != "medications" or not rec.get("dedup_key"):
        return []
    return rows_to_dicts(conn.execute(
        "SELECT id, source_name, effective_at, value_text, status FROM clinical_records WHERE profile_id = ? AND dedup_key = ? AND id != ? "
        "AND connection_id = ? ORDER BY coalesce(effective_at, '') DESC LIMIT 50",
        (rec["profile_id"], rec["dedup_key"], rec["id"], rec["connection_id"])).fetchall())


def get_record(record_id_: str, profile_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    with read() as conn:
        row = conn.execute("SELECT * FROM clinical_records WHERE id = ?", (record_id_,)).fetchone()
        if not row or (profile_id and row["profile_id"] != profile_id):
            return None
        rec = row_to_dict(row, ["details_json", "raw_json"])
        assert rec is not None
        others = _reported_elsewhere(conn, rec)
        renewals = _same_source_orders(conn, rec)
        refs = [r for r in (rec.get("details") or {}).get("result_refs") or [] if isinstance(r, str)]
        keys = ["/".join(r.rstrip("/").split("/")[-2:]) for r in refs]   # "Observation/123", also from absolute URLs
        results = rows_to_dicts(conn.execute(
            f"""SELECT id, title, category, status, value_num, value_text, unit, interpretation, ref_text, effective_at FROM clinical_records
                WHERE connection_id = ? AND resource_type || '/' || resource_id IN ({','.join('?' * len(keys))})
                ORDER BY title""",
            (rec["connection_id"], *keys),
        ).fetchall()) if keys else []
    rec["duplicates"] = others
    rec["results"] = results
    rec["renewals"] = renewals
    record_overrides.attach(rec["profile_id"], [rec])
    return rec


_LINK_COLUMNS = "id, title, category, status, value_text, unit, value_num, interpretation, ref_text, effective_at, details_json, source_name"
_ENCOUNTER_OF = ("coalesce(json_extract(raw_json, '$.encounter.reference'), json_extract(raw_json, '$.context.encounter[0].reference'), "
                 "json_extract(raw_json, '$.context.reference'))")


def record_filters(profile_id: str, category: Optional[str] = None) -> dict[str, Any]:
    """What a list of records can be narrowed by, from what's actually there."""
    with read() as conn:
        sources = [dict(r) for r in conn.execute(
            "SELECT connection_id AS id, source_name AS name, COUNT(*) AS n FROM clinical_records WHERE profile_id = ? "
            + ("AND category = ? " if category else "") + "GROUP BY connection_id, source_name ORDER BY n DESC",
            (profile_id, category) if category else (profile_id,)).fetchall()]
        kinds: list[Any] = []
        types: list[Any] = []
        authors: list[Any] = []
        if category == "reports":
            kinds = [r[0] for r in conn.execute("SELECT DISTINCT json_extract(details_json, '$.kind') FROM clinical_records "
                                                "WHERE profile_id = ? AND category = 'reports'", (profile_id,)).fetchall() if r[0]]
        if category == "notes":
            types = [r[0] for r in conn.execute("SELECT DISTINCT json_extract(details_json, '$.type') FROM clinical_records "
                                                "WHERE profile_id = ? AND category = 'notes' ORDER BY 1", (profile_id,)).fetchall() if r[0]]
            authors = [r[0] for r in conn.execute("SELECT DISTINCT a.value FROM clinical_records c, json_each(c.details_json, '$.authors') a "
                                                  "WHERE c.profile_id = ? AND c.category = 'notes' ORDER BY 1", (profile_id,)).fetchall() if r[0]]
        narrowed = "AND category = ? " if category else ""
        has = [k for k, clause in HAS_CLAUSES.items() if conn.execute(
            f"SELECT 1 FROM clinical_records WHERE profile_id = ? {narrowed}AND {clause} LIMIT 1", (profile_id, category) if category else (profile_id,)).fetchone()]
    return {"sources": sources, "kinds": kinds, "note_types": types, "authors": authors, "has": has}


def visit_links(rec: dict[str, Any]) -> dict[str, Any]:
    """Records that go together by what the source itself says: a visit's notes, results, diagnoses and orders; the visit a
    record came from; and for a result, the one before it. Read from the stored resources, so older records have them too."""
    out: dict[str, Any] = {}
    with read() as conn:
        if rec["category"] == "encounters" and rec.get("resource_id"):
            rows = rows_to_dicts(conn.execute(
                f"SELECT {_LINK_COLUMNS} FROM clinical_records WHERE connection_id = ? AND id != ? AND {_ENCOUNTER_OF} LIKE ? "
                "ORDER BY category, title LIMIT 300", (rec["connection_id"], rec["id"], f"%Encounter/{rec['resource_id']}")).fetchall(), ["details_json"])
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                grouped.setdefault(row["category"], []).append(row)
            out["visit_records"] = grouped
        else:
            ref = None
            raw = rec.get("raw") or {}
            context = raw.get("context") if isinstance(raw.get("context"), dict) else {}
            encounter = raw.get("encounter") or (context.get("encounter") or [None])[0] or (context if "reference" in context else None)
            if isinstance(encounter, dict):
                ref = encounter.get("reference")
            if isinstance(ref, str) and "Encounter/" in ref:
                row = conn.execute("SELECT id, title, effective_at FROM clinical_records WHERE connection_id = ? AND resource_type = 'Encounter' AND resource_id = ?",
                                   (rec["connection_id"], ref.rstrip("/").split("/")[-1])).fetchone()
                if row:
                    out["visit"] = dict(row)
        if rec["category"] in ("labs", "vitals", "observations") and rec.get("effective_at"):
            where = ("code = ?", rec["code"]) if rec.get("code") else ("title = ?", rec["title"])
            earlier = rows_to_dicts(conn.execute(
                f"SELECT id, code, code_system, title, value_text, interpretation, effective_at, source_name FROM clinical_records "
                f"WHERE profile_id = ? AND category = ? AND {where[0]} AND effective_at < ? ORDER BY effective_at DESC LIMIT 20",
                (rec["profile_id"], rec["category"], where[1], rec["effective_at"])).fetchall())
            day = str(rec["effective_at"])[:10]
            prior = next((e for e in earlier if _row_key(e) == _row_key(rec) and str(e["effective_at"])[:10] != day), None)
            if prior:
                out["previous"] = {k: prior[k] for k in ("id", "value_text", "interpretation", "effective_at", "source_name")}
    return out


def stopped_key(profile_id: str) -> str:
    return f"records.stopped.{profile_id}"


def seen_key(profile_id: str, account_id: Optional[str]) -> str:
    """Each account's own mark: one person looking doesn't mark it for others looking after the same person."""
    return f"records.seen.{profile_id}.{account_id or ''}"


NEW_ITEMS_SHOWN = 5
STOPPED_LOG_MOST = 1000
NEW_OLD_HISTORY_DAYS = 90       # a record dated this long before the last look is old history that has just arrived, not news
NEW_GROUPS = [("encounters", "New visits"), ("conditions", "New diagnoses"), ("medications", "New medications"), ("allergies", "New allergies"),
              ("notes", "New notes"), ("labs", "New results"), ("reports", "New reports"), ("procedures", "New procedures"),
              ("immunizations", "New vaccines"), ("vitals", "New vital signs")]


def new_since(profile_id: str, mark: float) -> dict[str, Any]:
    """What arrived after a moment: counts, titles and dates by kind. A source's first import is one line rather than years of history,
    and a record another source had already sent isn't new."""
    columns = "r.id, r.dedup_key, r.category, r.title, r.effective_at, r.value_text, r.interpretation, r.source_name, r.connection_id, r.status"
    stopped_ids = sorted({rid for rid, at in settings.get(stopped_key(profile_id)) or [] if at > mark})
    with read() as conn:
        first_time = {r[0] for r in conn.execute("SELECT id FROM connections WHERE profile_id = ? AND created_at > ?", (profile_id, mark)).fetchall()}
        rows = rows_to_dicts(conn.execute(
            f"SELECT {columns} FROM clinical_records r WHERE r.profile_id = ? AND r.first_seen_at > ? AND NOT EXISTS "
            "(SELECT 1 FROM clinical_records o WHERE o.profile_id = r.profile_id AND o.dedup_key = r.dedup_key AND o.first_seen_at <= ?) "
            "ORDER BY coalesce(r.effective_at, '') DESC", (profile_id, mark, mark)).fetchall())
        brought = rows_to_dicts(conn.execute(
            f"SELECT {columns} FROM clinical_records r WHERE r.profile_id = ? AND r.first_seen_at > ?", (profile_id, mark)).fetchall())
        stopped = rows_to_dicts(conn.execute(
            f"SELECT {columns} FROM clinical_records r WHERE r.profile_id = ? AND r.id IN ({','.join('?' * len(stopped_ids))}) AND r.first_seen_at <= ? "
            f"AND lower(coalesce(r.status, '')) IN ({','.join('?' * len(ENDED_MED_STATUSES))}) ORDER BY coalesce(r.effective_at, '') DESC",
            (profile_id, *stopped_ids, mark, *ENDED_MED_STATUSES)).fetchall())
        # A document that reached a record already known: one on a new record arrives with it.
        documents = rows_to_dicts(conn.execute(
            f"SELECT DISTINCT {columns} FROM record_files f JOIN clinical_records r ON r.connection_id = f.connection_id AND r.resource_type = f.resource_type "
            "AND r.resource_id = f.resource_id WHERE f.profile_id = ? AND f.stored_at > ? AND f.data IS NOT NULL AND r.first_seen_at <= ? "
            "ORDER BY coalesce(r.effective_at, '') DESC", (profile_id, mark, mark)).fetchall())
    # What the person has said is wrong or isn't them is on record, but it isn't news about them.
    said = record_overrides.for_profile(profile_id)
    rows, stopped, documents, brought = ([r for r in group if said.get(record_overrides.key_for(r), {}).get("state") != record_overrides.WRONG]
                                         for group in (rows, stopped, documents, brought))
    brought = Counter(r["connection_id"] for r in brought)
    imported: dict[str, dict[str, Any]] = {}
    by_category: dict[str, list[dict[str, Any]]] = {}
    older = 0
    cutoff = datetime.fromtimestamp(mark - NEW_OLD_HISTORY_DAYS * 86400, timezone.utc).isoformat()
    for row in rows:
        if row["connection_id"] in first_time:
            # The line is about the import, so it counts what the import brought, as its own message did, records another source also sent among them.
            imported.setdefault(row["connection_id"], {"source": row["source_name"], "count": brought[row["connection_id"]]})
        elif row["effective_at"] and row["effective_at"] < cutoff:
            older += 1
        else:
            by_category.setdefault(row["category"], []).append(row)
    known = {key for key, _ in NEW_GROUPS}
    other = [row for key, group in by_category.items() if key not in known for row in group]
    groups = [(key, label, by_category.get(key, [])) for key, label in NEW_GROUPS] + [("medications_stopped", "Medications marked stopped", stopped), ("documents", "New documents", documents), ("other", "Other new records", other)]
    return {"since": mark, "imported": list(imported.values()), "older": older,
            "groups": [{"key": key, "label": label, "count": len(group),
                        "items": [{k: r[k] for k in ("id", "title", "effective_at", "value_text", "interpretation", "source_name")} for r in group[:NEW_ITEMS_SHOWN]]}
                       for key, label, group in groups if group]}


def counts_for_connection(connection_id: str) -> list[dict[str, Any]]:
    """What one source brought in, by kind of record, in the usual order."""
    with read() as conn:
        found = {r[0]: r[1] for r in conn.execute("SELECT category, COUNT(*) FROM clinical_records WHERE connection_id = ? GROUP BY category", (connection_id,)).fetchall()}
    return [{"category": c, "label": CATEGORY_LABELS[c], "count": found[c]} for c in CATEGORIES if found.get(c)]


def category_counts(profile_id: str) -> dict[str, int]:
    with read() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(DISTINCT coalesce(dedup_key, id)) AS n FROM clinical_records "
            f"WHERE profile_id = ? AND category NOT IN ({','.join('?' * len(_LIGHT_ROWS))}) GROUP BY category",
            (profile_id, *_LIGHT_ROWS),
        ).fetchall()
        counts = {r["category"]: r["n"] for r in rows}
        for category in _LIGHT_ROWS:
            n = len(_group_rows(_light_rows(conn, profile_id, category)))
            if n:
                counts[category] = n
    return counts


def undated_counts(profile_id: str) -> list[dict[str, Any]]:
    """Records with no date, by kind: what a list of things since a date can't place."""
    with read() as conn:
        found = {r[0]: r[1] for r in conn.execute(
            "SELECT category, COUNT(DISTINCT coalesce(dedup_key, id)) FROM clinical_records WHERE profile_id = ? AND coalesce(effective_at, '') = '' GROUP BY category",
            (profile_id,)).fetchall()}
    return [{"label": CATEGORY_LABELS[c], "count": found[c]} for c in CATEGORIES if found.get(c)]


def identities(profile_id: str) -> list[dict[str, Any]]:
    with read() as conn:
        rows = conn.execute(
            """SELECT i.connection_id, i.full_name, i.birth_date, i.gender, i.mrn, i.telecom_json, i.address_json,
                      c.display_name AS source_name
               FROM patient_identities i JOIN connections c ON c.id = i.connection_id
               WHERE i.profile_id = ? ORDER BY i.updated_at DESC""",
            (profile_id,),
        ).fetchall()
    return rows_to_dicts(rows, ["telecom_json", "address_json"])


def identity_raw(connection_id: str) -> Optional[dict[str, Any]]:
    """The Patient resource a source last gave, which identities() leaves out of what it hands to the page."""
    with read() as conn:
        row = conn.execute("SELECT raw_json FROM patient_identities WHERE connection_id = ?", (connection_id,)).fetchone()
    try:
        raw = json.loads(row["raw_json"]) if row and row["raw_json"] else None
    except ValueError:
        return None
    return raw if isinstance(raw, dict) else None


def _unnamed(rec: dict[str, Any]) -> bool:
    """Observations the source sent without any name (normalization fell back to the resource type)."""
    return rec.get("title") == "Observation" and not rec.get("code_display")


def _no_name(name: Optional[str]) -> bool:
    """A Patient with no name is stored as "Unknown", which is nobody's name."""
    return not (name or "").strip() or (name or "").strip().lower() == "unknown"


def _name_tokens(name: Optional[str]) -> set[str]:
    if _no_name(name):
        return set()
    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()   # José and Jose are one name
    return {t for t in re.split(r"[^a-z]+", plain) if len(t) > 1}


def shown_date(value: Any) -> str:
    """A date as the app writes it, at the precision it came in: "Feb 3, 1970", "Feb 1970", "1970". Anything else as written."""
    text = str(value or "")
    for pattern, length in (("%Y-%m-%d", 10), ("%Y-%m", 7)):
        try:
            day = datetime.strptime(text[:length], pattern)
        except ValueError:
            continue
        return f"{day:%b} {day.day}, {day.year}" if length == 10 else f"{day:%b} {day.year}"
    return text


def _same_birth(a: Any, b: Any) -> bool:
    """1970 and 1970-02-03 can be one birth date, and so can the same day with a time on it."""
    a, b = str(a)[:10], str(b)[:10]
    return a.startswith(b) or b.startswith(a)


def distinct_people(idents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Groups connected-record identities into people: same birth date and at least one shared name part.
    More than one group means records for different people are mixed into this profile. What one row leaves out, another fills in:
    a nameless row joins whoever it could be, and that person's name then decides who else belongs."""
    people: list[dict[str, Any]] = []
    for i in idents:
        tokens = _name_tokens(i.get("full_name"))
        name = None if _no_name(i.get("full_name")) else i.get("full_name")
        for p in people:
            same_birth = not i.get("birth_date") or not p["birth_date"] or _same_birth(i["birth_date"], p["birth_date"])
            same_name = not tokens or not p["tokens"] or bool(tokens & p["tokens"])
            if same_birth and same_name:
                p["sources"].append(i["source_name"])
                p["tokens"] |= tokens
                p["full_name"] = p["full_name"] or name
                p["birth_date"] = p["birth_date"] or i.get("birth_date")
                break
        else:
            people.append({"full_name": name, "birth_date": i.get("birth_date"), "tokens": tokens,
                           "sources": [i["source_name"]]})
    return [{k: v for k, v in p.items() if k != "tokens"} for p in people]


def timeline_undated(profile_id: str, *, categories: Optional[list[str]] = None, q: Optional[str] = None) -> list[dict[str, Any]]:
    """Vaccines and procedures dated only in words, like "as a child": they have no place in the order, so they follow everything that has."""
    cats = [c for c in ("immunizations", "procedures") if not categories or c in categories]
    if not cats:
        return []
    where = [f"category IN ({','.join('?' * len(cats))})", "profile_id = ?", "effective_at IS NULL", "json_extract(details_json, '$.date_text') IS NOT NULL",
             "NOT (category = 'procedures' AND lower(coalesce(status, '')) = 'preparation')"]
    params: list[Any] = [*cats, profile_id]
    if q:
        where.append("(lower(title) LIKE ? OR lower(coalesce(narrative,'')) LIKE ?)")
        params.extend([f"%{q.lower()}%"] * 2)
    with read() as conn:
        rows = _load(conn.execute(f"SELECT {LIST_COLUMNS} FROM clinical_records WHERE {' AND '.join(where)} ORDER BY title LIMIT 500", params).fetchall())
    return _dedupe(rows)


TIMELINE_REPORT_KINDS = {"lab_reports": "json_extract(details_json, '$.kind') = 'lab'",
                         "imaging": "coalesce(json_extract(details_json, '$.kind'), '') != 'lab'"}


def timeline(profile_id: str, *, categories: Optional[list[str]] = None, limit: int = 150,
             before: Optional[str] = None, q: Optional[str] = None, inclusive: bool = False) -> list[dict[str, Any]]:
    """Clinical records newest first, before ``before`` (or at it too, with ``inclusive``)."""
    cats = categories or ["encounters", "labs", "conditions", "medications", "procedures", "immunizations",
                          "notes", "reports", "allergies", "other"]
    added = [HAS_CLAUSES[c] for c in cats if c in HAS_CLAUSES]
    cats = [c for c in cats if c not in HAS_CLAUSES]
    # Reports split by what they are: a lab panel is with labs, not among imaging.
    kinds = [c for c in cats if c in TIMELINE_REPORT_KINDS]
    cats = [c for c in cats if c not in TIMELINE_REPORT_KINDS]
    wanted = f"category IN ({','.join('?' * len(cats))})"
    for name in kinds:
        wanted += f" OR (category = 'reports' AND {TIMELINE_REPORT_KINDS[name]})"
    for clause in added:
        wanted += f" OR ({clause})"
    # What hasn't happened yet isn't history: a planned visit is under Upcoming on the Overview, and in Records.
    where = [f"({wanted})", "profile_id = ?", "effective_at IS NOT NULL",
             "NOT (category = 'encounters' AND (lower(coalesce(status, '')) = 'planned' OR effective_at > ?))",
             "NOT (category = 'procedures' AND lower(coalesce(status, '')) = 'preparation')"]
    params: list[Any] = [*cats, profile_id, (date.today() + timedelta(days=2)).isoformat()]
    if before:
        where.append("effective_at <= ?" if inclusive else "effective_at < ?")
        params.append(before)
    if q and q.strip():
        clause, values = search.where(q, profile_id)
        where.append(clause)
        params.extend(values)
    with read() as conn:
        rows = _load(conn.execute(
            f"SELECT {LIST_COLUMNS} FROM clinical_records WHERE {' AND '.join(where)} "
            "ORDER BY effective_at DESC LIMIT ?",
            (*params, limit * 3),
        ).fetchall())
    shown = _dedupe(rows)[:limit]
    record_overrides.attach(profile_id, shown)
    return shown


# ---------------------------------------------------------------------------
# Observations as series (labs & vitals trends)
# ---------------------------------------------------------------------------

def _row_key(r: dict[str, Any]) -> str:
    return lab_catalog.series_key(r.get("code"), r.get("code_system"), r.get("title"))


def _unit_group(r: dict[str, Any]) -> str:
    """Results that can share an axis: the same unit however it's spelled, after conversion where one is known."""
    return units.unit_key(r.get("unit_norm") or r.get("unit"))


def _comparator(r: dict[str, Any]) -> Optional[str]:
    return (r.get("details") or {}).get("comparator")


def _row_value(r: dict[str, Any]) -> Optional[float]:
    return r["value_norm"] if r.get("value_norm") is not None else r.get("value_num")


def observation_catalog(profile_id: str, category: str = "labs") -> list[dict[str, Any]]:
    """One entry per test with its latest value and history size. Different codes for one test are one entry."""
    with read() as conn:
        rows = _load(conn.execute(
            f"""SELECT {LIST_COLUMNS} FROM clinical_records
                WHERE profile_id = ? AND category = ? AND (value_num IS NOT NULL OR details_json LIKE '%components%')
                ORDER BY effective_at DESC""",
            (profile_id, category),
        ).fetchall())
    catalog: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for r in _dedupe(rows):
        if _unnamed(r):
            continue   # nothing to label a chart with; still listed under Records
        key = _row_key(r)
        entry = catalog.get(key)
        if entry is None:
            entry = catalog[key] = {
                "key": key, "code": r.get("code"), "code_system": r.get("code_system"), "title": r["title"],
                "latest": r, "count": 0, "values": [], "codes": [],
                "panel": (r.get("details") or {}).get("panel"),
            }
        entry["count"] += 1
        if r.get("code") and r["code"] not in entry["codes"]:
            entry["codes"].append(r["code"])
        # Only results in the latest one's unit compare with it, and a bound like "<5" isn't a level to measure a change from.
        if _row_value(r) is not None and _unit_group(r) == _unit_group(entry["latest"]) and len(entry["values"]) < 2:
            entry["values"].append(None if _comparator(r) else _row_value(r))
    out = []
    for e in catalog.values():
        vals = e.pop("values")
        e["trend"] = None
        if len(vals) == 2 and vals[0] is not None and vals[1]:
            change = (vals[0] - vals[1]) / abs(vals[1])
            e["trend"] = "up" if change > 0.03 else "down" if change < -0.03 else "flat"
        out.append(e)
    return out


def _test_rows(conn, profile_id: str, code: Optional[str], title: Optional[str], category: Optional[str]) -> list[dict[str, Any]]:
    """Every result of one test, oldest first: under any of its standard codes, or an institution's own code or none with a name
    that means the same test."""
    in_category = "AND category = ?" if category else "AND category IN ('labs', 'vitals', 'observations')"
    extra = [category] if category else []
    unstandard = conn.execute(
        f"SELECT DISTINCT title, code, code_system FROM clinical_records WHERE profile_id = ? {in_category} "
        f"AND (code IS NULL OR code_system IS NULL OR code_system != '{lab_catalog.LOINC}')", (profile_id, *extra)).fetchall()
    known = [dict(r) for r in unstandard]
    named_code = next((r for r in known if code and r["code"] == code), None)
    wanted = (_row_key(named_code) if named_code else lab_catalog.series_key(code, lab_catalog.LOINC, None) if code
              else lab_catalog.series_key(None, None, title))
    titles = [r["title"] for r in known if _row_key(r) == wanted]
    codes = [*lab_catalog.group_codes(wanted), *({code} if code else ())]
    clauses = [f"code IN ({','.join('?' * len(codes))})"]
    params: list[Any] = [*codes]
    if titles:
        clauses.append(f"((code IS NULL OR code_system IS NULL OR code_system != '{lab_catalog.LOINC}') AND title IN ({','.join('?' * len(titles))}))")
        params += titles
    rows = _load(conn.execute(
        f"SELECT {SERIES_COLUMNS} FROM clinical_records WHERE profile_id = ? {in_category} AND ({' OR '.join(clauses)}) ORDER BY effective_at",
        (profile_id, *extra, *params)).fetchall())
    # A title can be shared by a test it doesn't belong to: two local codes called "Glucose" are two tests.
    return [r for r in rows if _row_key(r) == wanted or (code and r.get("code") == code)]


def _range_fits_reported_unit(r: dict[str, Any]) -> bool:
    """A range the lab gave in another unit than its result is only held in the canonical unit, with nothing to show beside the result as reported."""
    if not units.converted(r.get("unit"), r.get("unit_norm")) or (r.get("ref_low") is None and r.get("ref_high") is None):
        return True
    return bool((r.get("details") or {}).get("source_range"))


def _unit_series(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """One axis' worth of results. A lab's results all reported in one unit stay in it, with its range; a mix is shown in the canonical
    unit they were converted to, each still carrying what the lab reported. Vital signs are always canonical: they follow the chosen units."""
    reported = {units.unit_key(r.get("unit")) for r in rows}
    as_reported = len(reported) == 1 and all(r.get("category") == "labs" and _range_fits_reported_unit(r) for r in rows)
    points = []
    ref_low = ref_high = unit = None
    for r in rows:
        source_range = (r.get("details") or {}).get("source_range") if as_reported else None
        low, high = (source_range["low"], source_range["high"]) if source_range else (r.get("ref_low"), r.get("ref_high"))
        unit = (r.get("unit") if as_reported and units.converted(r.get("unit"), r.get("unit_norm")) else r.get("unit_norm") or r.get("unit")) or unit
        if low is not None or high is not None:     # as a pair, from one lab: two labs' limits don't make a range
            ref_low, ref_high = low, high
        value = r["value_num"] if as_reported else _row_value(r)
        point = {
            "t": r["effective_at"], "v": value, "interpretation": r.get("interpretation"),
            "record_id": r["id"], "source_name": r.get("source_name"), "sources": len(r.get("sources") or []),
            "text": r.get("value_text"), "status": r.get("status"),
        }
        if _comparator(r):
            point["comparator"] = _comparator(r)
        if not as_reported and units.converted(r.get("unit"), unit):
            point["converted"] = True
        points.append(point)
    return {"unit": unit, "ref_low": ref_low, "ref_high": ref_high, "points": points}


def _side(points: list[dict[str, Any]]) -> dict[str, Any]:
    """The values on one side of a date, as they are."""
    if not points:
        return {"count": 0}
    values = [p["v"] for p in points]
    return {"count": len(values), "avg": round(sum(values) / len(values), 2), "low": min(values), "high": max(values),
            "first_at": points[0]["t"], "last_at": points[-1]["t"], "latest": points[-1]["v"]}


def before_and_after(profile_id: str, start: str, *, code: Optional[str] = None, title: Optional[str] = None, category: Optional[str] = None,
                     window_days: Optional[int] = None) -> dict[str, Any]:
    """A test's values on each side of a date, counted and left at that: two sides of a date say nothing about why they differ.
    A limit like "<5" isn't a level, so it's left out and counted. Results on the day itself are on neither side: which came first isn't known."""
    day = date.fromisoformat(start[:10])
    series = observation_series(profile_id, code, category, title=title)
    lines = [{"name": series["title"], "unit": series["unit"], "points": series["points"]}] if series["points"] else [
        {"name": c["name"], "unit": c["unit"], "points": c["points"]} for c in series["components"]]
    lo = (day - timedelta(days=window_days)).isoformat() if window_days else ""
    hi = (day + timedelta(days=window_days)).isoformat() if window_days else "9999"
    out = []
    for line in lines:
        measured = sorted((p for p in line["points"] if not p.get("comparator") and p.get("v") is not None), key=lambda p: str(p["t"]))
        before = [p for p in measured if lo <= str(p["t"])[:10] < day.isoformat()]
        after = [p for p in measured if day.isoformat() < str(p["t"])[:10] <= hi]
        out.append({"name": line["name"], "unit": line["unit"], "before": _side(before), "after": _side(after),
                    "limits_left_out": sum(1 for p in line["points"] if p.get("comparator"))})
    return {"start": day.isoformat(), "window_days": window_days, "lines": out}


def observation_series(profile_id: str, code: Optional[str] = None, category: Optional[str] = None,
                       title: Optional[str] = None) -> dict[str, Any]:
    """One test's history. ``points`` never mixes units: they're all in the unit of the latest result (or convert to it), and results
    in any other unit are under ``other_units``. Results that aren't numbers are in ``text_results``, with nothing to plot."""
    with read() as conn:
        rows = _test_rows(conn, profile_id, code, title, category)
    # A named code with nothing under it may be a test filed under a name alone (the assistant passes one or the other).
    if not rows and code and not title:
        with read() as conn:
            rows = _test_rows(conn, profile_id, None, code, category)
    rows = _dedupe(rows)
    components: dict[str, dict[str, Any]] = {}
    numeric: list[dict[str, Any]] = []
    text_results: list[dict[str, Any]] = []
    for r in rows:
        if not r.get("effective_at"):
            continue
        for c in (r.get("details") or {}).get("components") or []:
            if c.get("value") is None:
                continue
            series = components.setdefault(c.get("code") or c.get("name"), {"name": c.get("name"), "unit": c.get("unit"), "points": []})
            series["points"].append({"t": r["effective_at"], "v": c["value"], "record_id": r["id"], "source_name": r.get("source_name")})
        if _row_value(r) is not None:
            numeric.append(r)
        elif r.get("value_text") and not (r.get("details") or {}).get("components"):
            text_results.append({"t": r["effective_at"], "text": r["value_text"], "interpretation": r.get("interpretation"),
                                 "record_id": r["id"], "source_name": r.get("source_name"), "status": r.get("status")})
    groups: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
    for r in numeric:
        groups.setdefault(_unit_group(r), []).append(r)
    # A result with no unit joins the one unit this test has; with more than one, nothing says which it is.
    named = [k for k in groups if k]
    if "" in groups and len(named) == 1:
        groups[named[0]] = sorted(groups[named[0]] + groups.pop(""), key=lambda r: r["effective_at"])
    main_key = _unit_group(numeric[-1]) if numeric else None
    if main_key not in groups and named:
        main_key = named[0]
    main = _unit_series(groups[main_key]) if main_key in groups else {"unit": None, "ref_low": None, "ref_high": None, "points": []}
    return {
        "code": code or (rows[-1].get("code") if rows else None), "code_system": rows[-1].get("code_system") if rows else None,
        "title": rows[-1]["title"] if rows else (title or code),
        "codes": list(dict.fromkeys(r["code"] for r in rows if r.get("code"))), **main, "components": list(components.values()),
        "other_units": [{**_unit_series(group), "count": len(group)} for key, group in reversed(groups.items()) if key != main_key],
        "text_results": text_results,
    }


# ---------------------------------------------------------------------------
# Profile summary (overview & visit report)
# ---------------------------------------------------------------------------

def _first(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    return rows[:n]


def _latest_labs(labs: list[dict[str, Any]]) -> "OrderedDict[str, dict[str, Any]]":
    """Newest first going in. Each keeps the result before it, from an earlier day."""
    latest_by_code: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for lab in labs:
        if _unnamed(lab):
            continue
        latest = latest_by_code.setdefault(_row_key(lab), lab)
        if latest is not lab and "previous" not in latest and str(lab.get("effective_at") or "")[:10] != str(latest.get("effective_at") or "")[:10]:
            latest["previous"] = {"value_text": lab.get("value_text"), "effective_at": lab.get("effective_at"), "interpretation": lab.get("interpretation")}
    return latest_by_code


def _out_of_range(latest_by_code: dict[str, dict[str, Any]], today: Optional[date] = None) -> list[dict[str, Any]]:
    """Latest results that are flagged: critical first, then recent ones, then those old enough to be marked ``older``.
    Each group stays newest first."""
    cutoff = ((today or date.today()) - timedelta(days=OLDER_RESULT_DAYS)).isoformat()
    flagged = [l for l in latest_by_code.values() if (l.get("interpretation") or "") in ABNORMAL]
    for lab in flagged:
        if str(lab.get("effective_at") or "")[:10] < cutoff:
            lab["older"] = True
    return sorted(flagged, key=lambda l: 2 if l.get("older") else 0 if l["interpretation"] in CRITICAL else 1)


def lab_status(latest_by_code: dict[str, dict[str, Any]]) -> dict[str, int]:
    """How each test's latest result stands. In range only when it was actually assessed as normal: having no range isn't a good result."""
    interpretations = [l.get("interpretation") or "" for l in latest_by_code.values()]
    flagged = sum(i in ABNORMAL for i in interpretations)
    in_range = interpretations.count("normal")
    return {"total": len(interpretations), "flagged": flagged, "in_range": in_range, "not_assessed": len(interpretations) - flagged - in_range}


def flagged_labs(profile_id: str) -> list[dict[str, Any]]:
    """Tests whose latest result is out of range, as in the summary, but for Insights on every Overview: without reading
    the rest of the summary, or the full text of every result. These rows have no details or narrative."""
    with read() as conn:
        rows = rows_to_dicts(conn.execute(
            f"SELECT {_LAB_FLAG_COLUMNS} FROM clinical_records WHERE profile_id = ? AND category = 'labs' "
            "ORDER BY coalesce(effective_at, '') DESC, updated_at DESC", (profile_id,)).fetchall())
    record_overrides.attach(profile_id, rows)
    return _out_of_range(_latest_labs(_dedupe([r for r in rows if not _said_wrong(r)])[:SUMMARY_LABS]))


def procedures_by_finished(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Procedures that were done, and those whose source doesn't say so: planned, under way, on hold, stopped or not known.
    A source that states no status at all is taken at its word that this is a procedure on the person's history."""
    done = [r for r in rows if (r.get("status") or "completed").lower() == "completed"]
    return done, [r for r in rows if (r.get("status") or "completed").lower() != "completed"]


def report_history(profile_id: str) -> dict[str, Any]:
    """What a visit summary adds to the Overview's summary: surgery, imaging and smoking status."""
    finished, unfinished = procedures_by_finished(list_records(profile_id, "procedures", limit=1000)["items"])
    imaging = [r for r in list_records(profile_id, "reports", limit=500)["items"] if (r.get("details") or {}).get("kind") == "imaging"]
    smoking = list_records(profile_id, code=SMOKING_STATUS_CODE, limit=1)["items"]
    return {"procedures": finished[:10], "procedures_more": max(0, len(finished) - 10), "procedures_unfinished": unfinished,
            "imaging": imaging[:5], "imaging_more": max(0, len(imaging) - 5), "smoking": smoking[0] if smoking else None}


def recently_normal_lab_codes(profile_id: str, since: str, limit: int) -> list[str]:
    """Tests with an in-range result since a date, newest first."""
    with read() as conn:
        return [r[0] for r in conn.execute(
            "SELECT code FROM clinical_records WHERE profile_id = ? AND category = 'labs' AND code IS NOT NULL "
            "AND effective_at >= ? AND interpretation = 'normal' GROUP BY code ORDER BY MAX(effective_at) DESC LIMIT ?",
            (profile_id, since, limit)).fetchall()]


def medication_orders(profile_id: str) -> list[dict[str, Any]]:
    """Every medication row, with no folding: each renewal is a row of its own, which is what a drug's history is made
    of (see insights.medication_timeline). The list of records keeps only the newest of each."""
    with read() as conn:
        return _load(conn.execute(
            "SELECT id, resource_type, title, effective_at, status, details_json, dedup_key, source_name FROM clinical_records "
            "WHERE profile_id = ? AND category = 'medications'", (profile_id,)).fetchall())
_STRENGTH = re.compile(r"\d[\d,.]*\s*(?:mg|mcg|ug|µg|g|ml|l|meq|mmol|unt|units?|iu|%|hr|h)\b(?:\s*/\s*(?:\d[\d.]*\s*)?(?:ml|l|hr|h|actuat|dose|mg)\b)?", re.I)
_DOSE_FORM = {"oral", "tablet", "tablets", "tab", "tabs", "capsule", "capsules", "cap", "caps", "solution", "suspension", "injection", "injectable",
              "inhaler", "inhalation", "cream", "ointment", "gel", "patch", "topical", "transdermal", "spray", "nasal", "drops", "ophthalmic",
              "syrup", "powder", "extended", "delayed", "release", "er", "xr", "sr", "dr", "xl", "cr", "ec", "film", "coated", "chewable",
              "disintegrating", "actuat", "metered", "dose", "pen", "prefilled", "syringe", "ml", "mg"}
_SALTS = {"hcl", "hydrochloride", "sodium", "potassium", "sulfate", "besylate", "tartrate", "succinate", "maleate", "citrate", "mesylate",
          "calcium", "magnesium", "acetate", "phosphate", "fumarate", "hydrobromide"}


def drug_key(title: Optional[str]) -> Optional[str]:
    """The ingredients in a drug's name, for putting its strengths and duplicates next to each other: "Lisinopril 10 MG Oral Tablet"
    and "lisinopril 20mg" are one drug, "Lisinopril 20 MG / Hydrochlorothiazide 12.5 MG" another. Worked out here from the name, never
    by asking a service. It only arranges rows, it never merges or hides one, so a name it doesn't understand just stands alone."""
    name = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", (title or "").lower())
    name = _STRENGTH.sub(" ", name)
    tokens = [t for t in re.split(r"[^a-z0-9-]+", name) if t and not t[0].isdigit() and t not in _DOSE_FORM]
    kept = [t for i, t in enumerate(tokens) if i == 0 or t not in _SALTS]
    kept = [t for t in kept if t not in ("medication", "name", "not", "sent", "by", "the", "source", "and", "with")]
    return " ".join(sorted(set(kept))) or None


def medication_tier(med: dict[str, Any], today: Optional[date] = None) -> tuple[Optional[str], Optional[str]]:
    """The one rule for what a medication on record means right now, with the reason in words. "current": an active order in date.
    "check": still listed, but it may be out of date, so it's shown with why rather than hidden or presented as being taken.
    "planned": a draft or proposal nobody has started. "corrected": the person said it isn't so, which is shown beside what the source
    says. None: stopped, completed, cancelled or a mistake."""
    tier, reason = _source_tier(med, today)
    override = med.get("override") or {}
    if tier is None or override.get("state") not in (record_overrides.NOT_TAKING, record_overrides.WRONG):
        return tier, reason
    if override["state"] == record_overrides.NOT_TAKING and record_overrides.ordered_again_since(med, override):
        return tier, "Ordered again after it was marked not taking."
    return "corrected", override["words"]


def _source_tier(med: dict[str, Any], today: Optional[date] = None) -> tuple[Optional[str], Optional[str]]:
    today = today or date.today()
    details = med.get("details") or {}
    status = (med.get("status") or "").lower()
    intent = (details.get("intent") or "").lower()
    if status in ENDED_MED_STATUSES:
        return None, None
    if _med_not_started(med):
        return "planned", "Proposed or drafted, not started."
    if status == "on-hold":
        return "check", "On hold."
    if status != "active":
        return "check", "The record doesn't say whether this is still being taken."
    reported = details.get("reported") or med.get("resource_type") == "MedicationStatement"
    if intent == "plan":
        # US Core files reported home medications this way, so a plan may well be being taken. It can't be presented as prescribed.
        return "check", ("Reported rather than prescribed. The record doesn't say whether it's still being taken." if reported
                         else "Listed as a plan, not a prescription.")
    if "inpatient" in (details.get("category_codes") or []):
        return "check", "Ordered during a hospital stay."
    valid_until = str(details.get("valid_until") or "")
    if valid_until and valid_until[:10] < today.isoformat():
        return "check", f"The prescription ran out on {friendly_day(valid_until)}."
    when = str(med.get("effective_at") or "")
    # A reported medication is dated when it was documented, not prescribed: its age says nothing.
    if not reported and when and when[:10] < (today - timedelta(days=MED_RENEWAL_DAYS)).isoformat():
        return "check", f"Last prescribed {friendly_day(when)}, with no renewal on record since."
    return "current", None


def medication_lists(rows: list[dict[str, Any]], today: Optional[date] = None) -> dict[str, list[dict[str, Any]]]:
    """De-duplicated medication rows sorted by what they mean now, each saying why, with one drug's strengths and duplicates side by
    side and newest first. Different strengths are never collapsed: two can be a real regimen, and which is right is for a person to say."""
    tiers: dict[str, list[dict[str, Any]]] = {"current": [], "check": [], "planned": [], "corrected": []}
    groups: dict[str, list[dict[str, Any]]] = {}
    for m in rows:
        tier, reason = medication_tier(m, today)
        if not tier:
            continue
        m.update(med_tier=tier, med_reason=reason, last_prescribed=m.get("effective_at"), drug_group=drug_key(m.get("title")))
        tiers[tier].append(m)
        if tier not in ("planned", "corrected") and m["drug_group"]:
            groups.setdefault(m["drug_group"], []).append(m)
    newest: dict[str, str] = {}
    for group, members in groups.items():
        members.sort(key=lambda m: str(m.get("effective_at") or ""), reverse=True)
        newest[group] = str(members[0].get("effective_at") or "")
        if len(members) < 2:
            continue
        strengths = {" ".join(s.replace(" ", "") for s in _STRENGTH.findall((m.get("title") or "").lower())) for m in members}
        note = f"{len(members)} strengths on record" if len(strengths) > 1 else f"Listed {len(members)} times"
        for m in members:
            m["group_note"] = note
            m["also_on_record"] = [{"title": o.get("title"), "effective_at": o.get("effective_at"), "source_name": o.get("source_name"),
                                    "med_tier": o["med_tier"], "record_id": o["id"]} for o in members if o is not m]
            newer = next((o for o in members if o["med_tier"] == "current"), None)
            if m["med_tier"] == "check" and newer is not None:
                m["med_reason"] = f"{m['med_reason']} {newer.get('title')} is on record from {friendly_day(newer.get('effective_at'))}."
    for tier in tiers.values():
        tier.sort(key=lambda m: (newest.get(m["drug_group"] or "", ""), m["drug_group"] or "", str(m.get("effective_at") or "")), reverse=True)
    return tiers


def _no_known_note(rows: list[dict[str, Any]]) -> str:
    """A source ruling out drug allergies hasn't ruled out food ones, so a statement about one kind stays in the source's words."""
    said = [a for a in rows if (a.get("status") or "").lower() == "no-known-allergies"]
    if any(a.get("code") == "716186003" for a in said):
        return "No known allergies."
    return " ".join(dict.fromkeys(f"{(a.get('title') or 'No known allergies').rstrip('.')}." for a in said))


def conflicting_people(profile_id: str, people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Who's named when these records can't all be this person's: more than one person, or one born on another day than
    the birth date held for this person. Empty when nothing disagrees."""
    if len(people) > 1:
        return people
    if not people or not people[0].get("birth_date"):
        return []
    with read() as conn:
        row = conn.execute("SELECT name, birth_date FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    if not row or not row["birth_date"] or _same_birth(row["birth_date"], people[0]["birth_date"]):
        return []
    return [*people, {"full_name": row["name"], "birth_date": row["birth_date"], "sources": ["this person's details"]}]


def summary(profile_id: str, lab_rows: int = 12) -> dict[str, Any]:
    today = date.today()
    conds = list_records(profile_id, "conditions", limit=500)["items"]
    by_class: dict[str, list[dict[str, Any]]] = {"active": [], "unconfirmed": [], "unknown": [], "visit": [], "inactive": [], "corrected": []}
    for c in conds:
        by_class[condition_class(c, today)].append(c)
    meds = medication_lists(list_records(profile_id, "medications", limit=500)["items"], today)
    allergy_rows = list_records(profile_id, "allergies", limit=200)["items"]
    statements = {(a.get("status") or "").lower() for a in allergy_rows}
    allergies = [a for a in allergy_rows if _allergy_active(a)]
    inactive_allergies = [a for a in allergy_rows if (a.get("status") or "").lower() in INACTIVE_ALLERGY_STATUSES]
    allergy_note = (f"No active allergies. {len(inactive_allergies)} marked inactive." if inactive_allergies and not allergies
                    else _no_known_note(allergy_rows) if "no-known-allergies" in statements
                    else "The records say allergies haven't been asked about." if "not-asked" in statements else None)
    latest_by_code = _latest_labs(list_records(profile_id, "labs", limit=SUMMARY_LABS)["items"])
    flagged = _out_of_range(latest_by_code, today)
    vitals_latest: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for v in list_records(profile_id, "vitals", limit=2000)["items"]:
        if _unnamed(v):
            continue   # a bare local code with no name or unit means nothing on a summary card
        vitals_latest.setdefault(_row_key(v), v)
    idents = identities(profile_id)
    people = distinct_people(idents)
    soon = (today + timedelta(days=2)).isoformat()
    visits = list_records(profile_id, "encounters", limit=100)["items"]
    # A planned visit counts from today. One whose day has passed never happened, as far as the record says: it's only in Records.
    planned = lambda e: (e.get("status") or "").lower() == "planned"  # noqa: E731
    upcoming = [e for e in visits if str(e.get("effective_at") or "") > soon or (planned(e) and str(e.get("effective_at") or "")[:10] >= today.isoformat())]
    happened = [e for e in visits if str(e.get("effective_at") or "") <= soon and not planned(e)]
    vaccines = list_records(profile_id, "immunizations", limit=1000)["items"]
    counts = category_counts(profile_id)
    return {
        "counts": counts,
        "active_conditions": by_class["active"],
        "unconfirmed_conditions": by_class["unconfirmed"],
        "conditions_status_unknown": by_class["unknown"],
        "visit_diagnoses": by_class["visit"],
        "active_medications": meds["current"],
        "medications_to_check": meds["check"],
        "medications_planned": meds["planned"],
        "medications_corrected": meds["corrected"],
        "conditions_corrected": by_class["corrected"],
        "allergies_corrected": [a for a in allergy_rows if _said_wrong(a)],
        "allergies": allergies,
        "allergy_note": allergy_note,
        "flagged_labs": flagged,
        "lab_status": lab_status(latest_by_code),
        "recent_labs": _first(list(latest_by_code.values()), lab_rows),
        "labs_more": max(0, len(latest_by_code) - lab_rows),
        "latest_vitals": list(vitals_latest.values())[:12],
        "vitals_more": max(0, len(vitals_latest) - 12),
        "recent_encounters": happened[:5],
        "encounters_more": max(0, counts.get("encounters", 0) - len(upcoming) - 5),
        "upcoming_encounters": sorted(upcoming, key=lambda e: e["effective_at"])[:5],
        "immunizations": [i for i in vaccines if (i.get("status") or "").lower() != "not-done"],
        "immunizations_not_given": [i for i in vaccines if (i.get("status") or "").lower() == "not-done"],
        "identities": idents,
        "identity_conflict": conflicting_people(profile_id, people),
    }
