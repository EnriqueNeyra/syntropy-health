"""Normalized clinical records: persistence, cross-source de-duplication and queries."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict
from datetime import date, timedelta
from typing import Any, Iterable, Optional

from app.connectors import units
from app.core.db import db, read, dumps, row_to_dict, rows_to_dicts
from app.store import lab_catalog

CATEGORIES = [
    "conditions", "medications", "allergies", "labs", "vitals", "immunizations", "encounters",
    "procedures", "notes", "reports", "care_team", "care_plans", "goals", "devices", "coverage", "observations",
]

CATEGORY_LABELS = {
    "conditions": "Conditions", "medications": "Medications", "allergies": "Allergies",
    "labs": "Lab results", "vitals": "Vital signs", "immunizations": "Immunizations",
    "encounters": "Visits", "procedures": "Procedures", "notes": "Clinical notes",
    "reports": "Diagnostic reports", "care_team": "Care team", "care_plans": "Care plans",
    "goals": "Goals", "devices": "Devices", "coverage": "Insurance", "observations": "Other observations",
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
OLDER_RESULT_DAYS = 730   # a flagged result this old is shown as an older result rather than a current problem

LIST_COLUMNS = (
    "id, profile_id, connection_id, resource_type, resource_id, category, title, code_system, code, "
    "code_display, effective_at, effective_end, status, value_num, value_text, unit, value_norm, unit_norm, "
    "ref_low, ref_high, ref_text, interpretation, details_json, narrative, dedup_key, source_name, "
    "first_seen_at, updated_at"
)
_LAB_FLAG_COLUMNS = ("id, connection_id, title, code, code_system, code_display, unit, value_num, value_text, effective_at, status, "
                     "interpretation, ref_text, dedup_key, source_name")
SUMMARY_LABS = 2000
SERIES_COLUMNS = ("id, connection_id, title, category, code, effective_at, status, value_num, value_text, unit, value_norm, unit_norm, "
                  "ref_low, ref_high, interpretation, details_json, dedup_key, source_name")


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


_COMPARED = ("profile_id, connection_id, resource_type, resource_id, category, title, code_system, code, code_display, "
             "effective_at, effective_end, status, value_num, value_text, unit, value_norm, unit_norm, ref_low, ref_high, "
             "ref_text, interpretation, details_json, narrative, dedup_key, source_name, raw_json")


def upsert_records(profile_id: str, connection_id: str, source_name: str, records: Iterable[dict[str, Any]]) -> dict[str, int]:
    now = time.time()
    inserted = updated = 0
    with db() as conn:
        for r in records:
            rid = record_id(connection_id, r["resource_type"], r["resource_id"])
            existing = conn.execute(f"SELECT {_COMPARED} FROM clinical_records WHERE id = ?", (rid,)).fetchone()
            values = (
                profile_id, connection_id, r["resource_type"], r["resource_id"], r["category"],
                (r.get("title") or "Untitled")[:500], r.get("code_system"), r.get("code"), r.get("code_display"),
                r.get("effective_at"), r.get("effective_end"), r.get("status"), r.get("value_num"),
                r.get("value_text"), r.get("unit"), r.get("value_norm"), r.get("unit_norm"),
                r.get("ref_low"), r.get("ref_high"), r.get("ref_text"), r.get("interpretation"),
                dumps(r.get("details") or {}), r.get("narrative"), r.get("dedup_key"), source_name,
                dumps(r.get("raw")), now,
            )
            if existing and tuple(existing) == values[:-1]:
                continue   # unchanged: keep updated_at meaningful and the sync stats honest
            if existing:
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
                conn.execute(
                    """INSERT INTO clinical_records(profile_id, connection_id, resource_type, resource_id, category,
                           title, code_system, code, code_display, effective_at, effective_end, status, value_num,
                           value_text, unit, value_norm, unit_norm, ref_low, ref_high, ref_text, interpretation,
                           details_json, narrative, dedup_key, source_name, raw_json, updated_at, id, first_seen_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (*values, rid, now),
                )
                inserted += 1
    return {"inserted": inserted, "updated": updated}


def prune_missing(connection_id: str, resource_types: Iterable[str], seen_ids: set[str]) -> int:
    """Removes records the source no longer returns (e.g. entered-in-error), for fully-fetched types."""
    types = list(resource_types)
    if not types:
        return 0
    with db() as conn:
        rows = conn.execute(
            f"SELECT id, resource_type, resource_id FROM clinical_records WHERE connection_id = ? "
            f"AND resource_type IN ({','.join('?' * len(types))})",
            (connection_id, *types),
        ).fetchall()
        stale = [r["id"] for r in rows if f"{r['resource_type']}/{r['resource_id']}" not in seen_ids]
        for i in range(0, len(stale), 500):
            chunk = stale[i:i + 500]
            conn.execute(f"DELETE FROM clinical_records WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    return len(stale)


def delete_record(record_id_: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM clinical_records WHERE id = ?", (record_id_,))


def delete_for_connection(connection_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM clinical_records WHERE connection_id = ?", (connection_id,))
        conn.execute("DELETE FROM patient_identities WHERE connection_id = ?", (connection_id,))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _load(rows) -> list[dict[str, Any]]:
    return rows_to_dicts(rows, ["details_json"])


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


def _allergy_active(rec: dict[str, Any]) -> bool:
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
) -> dict[str, Any]:
    where, params = ["profile_id = ?"], [profile_id]
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
    if since:
        where.append("effective_at >= ?")
        params.append(since)
    if until:
        where.append("effective_at <= ?")
        params.append(until)
    if status:
        where.append("lower(status) = ?")
        params.append(status.lower())
    if q:
        like = f"%{q.strip().lower()}%"
        where.append("(lower(title) LIKE ? OR lower(coalesce(narrative,'')) LIKE ? OR lower(coalesce(code_display,'')) "
                     "LIKE ? OR coalesce(code,'') = ? OR lower(coalesce(value_text,'')) LIKE ?)")
        params.extend([like, like, like, q.strip(), like])
    sql = (f"SELECT {LIST_COLUMNS} FROM clinical_records WHERE {' AND '.join(where)} "
           "ORDER BY coalesce(effective_at, '') DESC, updated_at DESC")
    with read() as conn:
        rows = _load(conn.execute(sql, params).fetchall())
    if dedupe:
        rows = _dedupe(rows)
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


def get_record(record_id_: str, profile_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    with read() as conn:
        row = conn.execute("SELECT * FROM clinical_records WHERE id = ?", (record_id_,)).fetchone()
        if not row or (profile_id and row["profile_id"] != profile_id):
            return None
        rec = row_to_dict(row, ["details_json", "raw_json"])
        assert rec is not None
        others = _reported_elsewhere(conn, rec)
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
    return rec


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


def _unnamed(rec: dict[str, Any]) -> bool:
    """Observations the source sent without any name (normalization fell back to the resource type)."""
    return rec.get("title") == "Observation" and not rec.get("code_display")


def _name_tokens(name: Optional[str]) -> set[str]:
    return {t for t in re.split(r"[^a-z]+", (name or "").lower()) if len(t) > 1}


def distinct_people(idents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Groups connected-record identities into people: same birth date and at least one shared name part.
    More than one group means records for different people are mixed into this profile."""
    people: list[dict[str, Any]] = []
    for i in idents:
        tokens = _name_tokens(i.get("full_name"))
        for p in people:
            same_birth = not i.get("birth_date") or not p["birth_date"] or i["birth_date"] == p["birth_date"]
            same_name = not tokens or not p["tokens"] or bool(tokens & p["tokens"])
            if same_birth and same_name:
                p["sources"].append(i["source_name"])
                break
        else:
            people.append({"full_name": i.get("full_name"), "birth_date": i.get("birth_date"), "tokens": tokens,
                           "sources": [i["source_name"]]})
    return [{k: v for k, v in p.items() if k != "tokens"} for p in people]


def timeline(profile_id: str, *, categories: Optional[list[str]] = None, limit: int = 150,
             before: Optional[str] = None, q: Optional[str] = None, inclusive: bool = False) -> list[dict[str, Any]]:
    """Clinical records newest first, before ``before`` (or at it too, with ``inclusive``)."""
    cats = categories or ["encounters", "labs", "conditions", "medications", "procedures", "immunizations",
                          "notes", "reports", "allergies"]
    # What hasn't happened yet isn't history: a planned visit is under Upcoming on the Overview, and in Records.
    where = [f"category IN ({','.join('?' * len(cats))})", "profile_id = ?", "effective_at IS NOT NULL",
             "NOT (category = 'encounters' AND (lower(coalesce(status, '')) = 'planned' OR effective_at > ?))",
             "NOT (category = 'procedures' AND lower(coalesce(status, '')) = 'preparation')"]
    params: list[Any] = [*cats, profile_id, (date.today() + timedelta(days=2)).isoformat()]
    if before:
        where.append("effective_at <= ?" if inclusive else "effective_at < ?")
        params.append(before)
    if q:
        where.append("(lower(title) LIKE ? OR lower(coalesce(narrative,'')) LIKE ?)")
        params.extend([f"%{q.lower()}%"] * 2)
    with read() as conn:
        rows = _load(conn.execute(
            f"SELECT {LIST_COLUMNS} FROM clinical_records WHERE {' AND '.join(where)} "
            "ORDER BY effective_at DESC LIMIT ?",
            (*params, limit * 3),
        ).fetchall())
    return _dedupe(rows)[:limit]


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
    return _load(conn.execute(
        f"SELECT {SERIES_COLUMNS} FROM clinical_records WHERE profile_id = ? {in_category} AND ({' OR '.join(clauses)}) ORDER BY effective_at",
        (profile_id, *extra, *params)).fetchall())


def _unit_series(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """One axis' worth of results. A lab's results all reported in one unit stay in it, with its range; a mix is shown in the canonical
    unit they were converted to, each still carrying what the lab reported. Vital signs are always canonical: they follow the chosen units."""
    reported = {units.unit_key(r.get("unit")) for r in rows}
    as_reported = len(reported) == 1 and all(r.get("category") == "labs" for r in rows)
    points = []
    ref_low = ref_high = unit = None
    for r in rows:
        source_range = (r.get("details") or {}).get("source_range") if as_reported else None
        low, high = (source_range["low"], source_range["high"]) if source_range else (r.get("ref_low"), r.get("ref_high"))
        unit = (r.get("unit") if as_reported and units.converted(r.get("unit"), r.get("unit_norm")) else r.get("unit_norm") or r.get("unit")) or unit
        ref_low = low if low is not None else ref_low
        ref_high = high if high is not None else ref_high
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
        "code": code or (rows[-1].get("code") if rows else None), "title": rows[-1]["title"] if rows else (title or code),
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
    latest_by_code: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for lab in labs:
        if not _unnamed(lab):
            latest_by_code.setdefault(_row_key(lab), lab)
    return latest_by_code


def _out_of_range(latest_by_code: dict[str, dict[str, Any]], today: Optional[date] = None) -> list[dict[str, Any]]:
    cutoff = ((today or date.today()) - timedelta(days=OLDER_RESULT_DAYS)).isoformat()
    flagged = [l for l in latest_by_code.values() if (l.get("interpretation") or "") in ABNORMAL]
    for l in flagged:
        if str(l.get("effective_at") or "")[:10] < cutoff:
            l["older_result"] = True
    return flagged


def flagged_labs(profile_id: str) -> list[dict[str, Any]]:
    """Tests whose latest result is out of range, as in the summary, but for Insights on every Overview: without reading
    the rest of the summary, or the full text of every result. These rows have no details or narrative."""
    with read() as conn:
        rows = rows_to_dicts(conn.execute(
            f"SELECT {_LAB_FLAG_COLUMNS} FROM clinical_records WHERE profile_id = ? AND category = 'labs' "
            "ORDER BY coalesce(effective_at, '') DESC, updated_at DESC", (profile_id,)).fetchall())
    return _out_of_range(_latest_labs(_dedupe(rows)[:SUMMARY_LABS]))


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
    "planned": a draft or proposal nobody has started. None: stopped, completed, cancelled or a mistake."""
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
        return "check", ("Reported rather than prescribed. Confirm it's still being taken." if reported
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
    tiers: dict[str, list[dict[str, Any]]] = {"current": [], "check": [], "planned": []}
    groups: dict[str, list[dict[str, Any]]] = {}
    for m in rows:
        tier, reason = medication_tier(m, today)
        if not tier:
            continue
        m.update(med_tier=tier, med_reason=reason, last_prescribed=m.get("effective_at"), drug_group=drug_key(m.get("title")))
        tiers[tier].append(m)
        if tier != "planned" and m["drug_group"]:
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


def summary(profile_id: str) -> dict[str, Any]:
    today = date.today()
    conds = list_records(profile_id, "conditions", limit=500)["items"]
    by_class: dict[str, list[dict[str, Any]]] = {"active": [], "unconfirmed": [], "unknown": [], "visit": [], "inactive": []}
    for c in conds:
        by_class[condition_class(c, today)].append(c)
    meds = medication_lists(list_records(profile_id, "medications", limit=500)["items"], today)
    allergy_rows = list_records(profile_id, "allergies", limit=200)["items"]
    statements = {(a.get("status") or "").lower() for a in allergy_rows}
    allergies = [a for a in allergy_rows if _allergy_active(a)]
    inactive_allergies = [a for a in allergy_rows if (a.get("status") or "").lower() in INACTIVE_ALLERGY_STATUSES]
    allergy_note = (f"No active allergies. {len(inactive_allergies)} marked inactive." if inactive_allergies and not allergies
                    else "No known allergies." if "no-known-allergies" in statements
                    else "Allergies haven't been recorded by your care team." if "not-asked" in statements else None)
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
    upcoming = [e for e in visits if str(e.get("effective_at") or "") > soon]
    happened = [e for e in visits if str(e.get("effective_at") or "") <= soon and (e.get("status") or "").lower() != "planned"]
    vaccines = list_records(profile_id, "immunizations", limit=100)["items"]
    return {
        "counts": category_counts(profile_id),
        "active_conditions": by_class["active"],
        "unconfirmed_conditions": by_class["unconfirmed"],
        "conditions_status_unknown": by_class["unknown"],
        "visit_diagnoses": by_class["visit"],
        "active_medications": meds["current"],
        "medications_to_check": meds["check"],
        "medications_planned": meds["planned"],
        "allergies": allergies,
        "allergy_note": allergy_note,
        "flagged_labs": flagged,
        "recent_labs": _first(list(latest_by_code.values()), 12),
        "latest_vitals": list(vitals_latest.values())[:12],
        "recent_encounters": happened[:5],
        "upcoming_encounters": sorted(upcoming, key=lambda e: e["effective_at"])[:5],
        "immunizations": [i for i in vaccines if (i.get("status") or "").lower() != "not-done"],
        "immunizations_not_given": [i for i in vaccines if (i.get("status") or "").lower() == "not-done"],
        "identities": idents,
        "identity_conflict": people if len(people) > 1 else [],
    }


def raw_resources(profile_id: str) -> Iterable[dict[str, Any]]:
    with read() as conn:
        # Patient first: without it an exported bundle has records but no name, birth date or identifiers.
        for (raw,) in conn.execute(
                "SELECT raw_json FROM patient_identities WHERE profile_id = ? AND raw_json IS NOT NULL", (profile_id,)):
            yield json.loads(raw)
        rows = conn.execute(
            "SELECT raw_json FROM clinical_records WHERE profile_id = ? AND raw_json IS NOT NULL ORDER BY category, effective_at",
            (profile_id,),
        ).fetchall()
    for r in rows:
        try:
            res = json.loads(r["raw_json"])
            if isinstance(res, dict) and res.get("resourceType"):
                yield res
        except ValueError:
            continue
