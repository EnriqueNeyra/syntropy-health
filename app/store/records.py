"""Normalized clinical records: persistence, cross-source de-duplication and queries."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict
from typing import Any, Iterable, Optional

from app.core.db import db, read, dumps, row_to_dict, rows_to_dicts

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

ACTIVE_MED_STATUSES = ("active", "on-hold", "draft", "intended")
INACTIVE_CONDITION_STATUSES = ("resolved", "inactive", "remission", "entered-in-error")
ABNORMAL = ("high", "low", "critical_high", "critical_low", "abnormal")

LIST_COLUMNS = (
    "id, profile_id, connection_id, resource_type, resource_id, category, title, code_system, code, "
    "code_display, effective_at, effective_end, status, value_num, value_text, unit, value_norm, unit_norm, "
    "ref_low, ref_high, ref_text, interpretation, details_json, narrative, dedup_key, source_name, "
    "first_seen_at, updated_at"
)


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


def _dedupe(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapses records describing the same fact from multiple sources, keeping the newest."""
    groups: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for r in records:
        key = r.get("dedup_key") or r["id"]
        src = {"connection_id": r["connection_id"], "source_name": r.get("source_name"), "record_id": r["id"]}
        if key in groups:
            g = groups[key]
            if all(s["connection_id"] != src["connection_id"] for s in g["sources"]):
                g["sources"].append(src)
            continue
        r["sources"] = [src]
        groups[key] = r
    return list(groups.values())


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


def get_record(record_id_: str, profile_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    with read() as conn:
        row = conn.execute("SELECT * FROM clinical_records WHERE id = ?", (record_id_,)).fetchone()
        if not row or (profile_id and row["profile_id"] != profile_id):
            return None
        rec = row_to_dict(row, ["details_json", "raw_json"])
        assert rec is not None
        others = []
        if rec.get("dedup_key"):
            others = rows_to_dicts(conn.execute(
                "SELECT id, connection_id, source_name, effective_at, value_text FROM clinical_records "
                "WHERE profile_id = ? AND dedup_key = ? AND id != ?",
                (rec["profile_id"], rec["dedup_key"], rec["id"]),
            ).fetchall())
        refs = [r for r in (rec.get("details") or {}).get("result_refs") or [] if isinstance(r, str)]
        keys = ["/".join(r.rstrip("/").split("/")[-2:]) for r in refs]   # "Observation/123", also from absolute URLs
        results = rows_to_dicts(conn.execute(
            f"""SELECT id, title, value_num, value_text, unit, interpretation, ref_text, effective_at FROM clinical_records
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
            "WHERE profile_id = ? GROUP BY category",
            (profile_id,),
        ).fetchall()
    return {r["category"]: r["n"] for r in rows}


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
    where = [f"category IN ({','.join('?' * len(cats))})", "profile_id = ?", "effective_at IS NOT NULL"]
    params: list[Any] = [*cats, profile_id]
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

def observation_catalog(profile_id: str, category: str = "labs") -> list[dict[str, Any]]:
    """One entry per distinct observation code with its latest value and history size."""
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
        key = r.get("code") or r["title"].lower()
        entry = catalog.get(key)
        if entry is None:
            entry = catalog[key] = {
                "code": r.get("code"), "code_system": r.get("code_system"), "title": r["title"],
                "latest": r, "count": 0, "values": [],
                "panel": (r.get("details") or {}).get("panel"),
            }
        entry["count"] += 1
        v = r.get("value_norm") if r.get("value_norm") is not None else r.get("value_num")
        if v is not None and len(entry["values"]) < 2:
            entry["values"].append(v)
    out = []
    for e in catalog.values():
        vals = e.pop("values")
        e["trend"] = None
        if len(vals) == 2 and vals[1]:
            change = (vals[0] - vals[1]) / abs(vals[1])
            e["trend"] = "up" if change > 0.03 else "down" if change < -0.03 else "flat"
        out.append(e)
    return out


def observation_series(profile_id: str, code: str, category: Optional[str] = None) -> dict[str, Any]:
    where, params = ["profile_id = ?", "code = ?"], [profile_id, code]
    if category:
        where.append("category = ?")
        params.append(category)
    with read() as conn:
        rows = _load(conn.execute(
            f"SELECT {LIST_COLUMNS} FROM clinical_records WHERE {' AND '.join(where)} ORDER BY effective_at",
            params,
        ).fetchall())
    rows = _dedupe(rows)
    points, components = [], {}
    ref_low = ref_high = None
    unit = None
    title = rows[-1]["title"] if rows else code
    for r in rows:
        if not r.get("effective_at"):
            continue
        comps = (r.get("details") or {}).get("components") or []
        for c in comps:
            if c.get("value") is None:
                continue
            series = components.setdefault(c.get("code") or c.get("name"), {"name": c.get("name"), "unit": c.get("unit"), "points": []})
            series["points"].append({"t": r["effective_at"], "v": c["value"], "record_id": r["id"], "source_name": r.get("source_name")})
        value = r["value_norm"] if r.get("value_norm") is not None else r.get("value_num")
        if value is None:
            continue
        unit = r.get("unit_norm") or r.get("unit") or unit
        ref_low = r.get("ref_low") if r.get("ref_low") is not None else ref_low
        ref_high = r.get("ref_high") if r.get("ref_high") is not None else ref_high
        points.append({
            "t": r["effective_at"], "v": value, "interpretation": r.get("interpretation"),
            "record_id": r["id"], "source_name": r.get("source_name"), "sources": len(r.get("sources") or []),
        })
    return {
        "code": code, "title": title, "unit": unit, "ref_low": ref_low, "ref_high": ref_high,
        "points": points, "components": list(components.values()),
    }


# ---------------------------------------------------------------------------
# Profile summary (overview & visit report)
# ---------------------------------------------------------------------------

def _first(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    return rows[:n]


def summary(profile_id: str) -> dict[str, Any]:
    conds = list_records(profile_id, "conditions", limit=500)["items"]
    active_conditions = [c for c in conds if (c.get("status") or "active").lower() not in INACTIVE_CONDITION_STATUSES]
    meds = list_records(profile_id, "medications", limit=500)["items"]
    active_meds = [m for m in meds if (m.get("status") or "active").lower() in ACTIVE_MED_STATUSES]
    allergy_rows = list_records(profile_id, "allergies", limit=200)["items"]
    statements = {(a.get("status") or "").lower() for a in allergy_rows}
    allergies = [a for a in allergy_rows if (a.get("status") or "active").lower()
                 not in ("resolved", "inactive", "entered-in-error", "no-known-allergies", "not-asked")]
    allergy_note = ("No known allergies." if "no-known-allergies" in statements
                    else "Allergies haven't been recorded by your care team." if "not-asked" in statements else None)
    labs = list_records(profile_id, "labs", limit=2000)["items"]
    latest_by_code: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for lab in labs:
        if not _unnamed(lab):
            latest_by_code.setdefault(lab.get("code") or lab["title"], lab)
    flagged = [l for l in latest_by_code.values() if (l.get("interpretation") or "") in ABNORMAL]
    vitals_latest: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for v in list_records(profile_id, "vitals", limit=2000)["items"]:
        if _unnamed(v):
            continue   # a bare local code with no name or unit means nothing on a summary card
        vitals_latest.setdefault(v.get("code") or v["title"], v)
    idents = identities(profile_id)
    people = distinct_people(idents)
    encounters = list_records(profile_id, "encounters", limit=5)["items"]
    immunizations = list_records(profile_id, "immunizations", limit=100)["items"]
    return {
        "counts": category_counts(profile_id),
        "active_conditions": active_conditions,
        "active_medications": active_meds,
        "allergies": allergies,
        "allergy_note": allergy_note,
        "flagged_labs": flagged,
        "recent_labs": _first(list(latest_by_code.values()), 12),
        "latest_vitals": list(vitals_latest.values())[:12],
        "recent_encounters": encounters,
        "immunizations": immunizations,
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
