"""
What a person can take with them: FHIR (a collection of everything, and an International Patient Summary), CSVs, and one
archive with all of that plus their journal, goals, conversations, samples and workouts with routes. Reading an archive
back in is ``import_archive``.

Every exported resource says which source it came from (``meta.tag``) and has a ``fullUrl``, so importing it elsewhere
can keep hospitals apart. What's stored is the resource as the source sent it; these steps make the exported copy whole:
Medication resources behind a reference, note text fetched from a link, and ids valid in FHIR.
"""

from __future__ import annotations

import base64
import copy
import csv
import hashlib
import html
import io
import json
import logging
import math
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from app.core import config, db, private_files
from app.core.db import read
from app.services.sync import SOURCE_TAG, SOURCE_URN
from app.store import activity, biometrics, chats, connections, goals, record_files, record_overrides, records

log = logging.getLogger("syntropy.exports")

ARCHIVE_FORMAT = "syntropy-person-archive"
ARCHIVE_VERSION = 1
MAX_ARCHIVE_MEMBER = 4 * 1024 * 1024 * 1024
_ID_BAD = re.compile(r"[^A-Za-z0-9\-.]")
_ID_IN_REFERENCE = re.compile(r"^(.*/)?([A-Z][A-Za-z]+)/([^/]+)(/_history/[^/]+)?$")


# ---------------------------------------------------------------------------
# One exported resource
# ---------------------------------------------------------------------------

def _fhir_id(value: Any) -> str:
    """A FHIR id holds letters, digits, - and . up to 64 long. Manual lab results were saved as man_..., and some servers
    send longer ones, which made a strict reader refuse the whole file. Two ids that differ stay different, or a reference
    would find the wrong medication."""
    text = str(value or "") or "unknown"
    if len(text) <= 64 and not _ID_BAD.search(text):
        return text
    return f"{_ID_BAD.sub('-', text)[:51]}-{hashlib.sha1(text.encode()).hexdigest()[:12]}"


def _with_valid_ids(value: Any) -> Any:
    """A changed id has to change everywhere it's pointed to, or nothing in the file finds it."""
    if isinstance(value, list):
        return [_with_valid_ids(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {k: _with_valid_ids(v) for k, v in value.items()}
    ref = out.get("reference")
    if isinstance(ref, str):
        if ref.startswith("#"):
            out["reference"] = "#" + _fhir_id(ref[1:])
        elif match := _ID_IN_REFERENCE.match(ref):
            out["reference"] = f"{match[1] or ''}{match[2]}/{_fhir_id(match[3])}{match[4] or ''}"
    if isinstance(out.get("contained"), list):
        out["contained"] = [{**c, "id": _fhir_id(c.get("id"))} if isinstance(c, dict) and c.get("id") else c for c in out["contained"]]
    return out


def _base_url(source: dict[str, Any]) -> str:
    # .invalid is reserved for names that must not resolve: an import has no server, but relative references between
    # its resources still need one base to resolve against.
    return (source.get("fhir_base_url") or f"https://syntropy.invalid/fhir/{source['id']}").rstrip("/")


def _tagged(res: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    out = _with_valid_ids(res)
    out["id"] = _fhir_id(res.get("id"))
    meta = dict(res.get("meta") or {})
    meta["tag"] = [t for t in meta.get("tag") or [] if t.get("system") != SOURCE_TAG] + [
        {"system": SOURCE_TAG, "code": source["id"], "display": source.get("display_name") or source["id"]}]
    meta.setdefault("source", source.get("fhir_base_url") or f"{SOURCE_URN}{source['id']}")
    out["meta"] = meta
    return out


def _text_like(att: dict[str, Any]) -> bool:
    kind = (att.get("contentType") or "").lower()
    return bool(att.get("url")) and not att.get("data") and (kind.startswith("text/") or "xml" in kind or not kind)


def _with_note_text(res: dict[str, Any], narrative: Optional[str]) -> dict[str, Any]:
    """Notes served through a link come to the record as text fetched at sync; the resource still holds only the link, to
    a portal that needs the original sign-in. The export carries the text, keeping the link."""
    if not narrative:
        return res
    if res.get("resourceType") == "DocumentReference":
        holders = [c.get("attachment") or {} for c in res.get("content") or []]
    elif res.get("resourceType") == "DiagnosticReport" and narrative != res.get("conclusion"):
        holders = list(res.get("presentedForm") or [])
    else:
        return res
    if any(a.get("data") for a in holders) or not any(_text_like(a) for a in holders):
        return res
    res = copy.deepcopy(res)
    holders = ([c.get("attachment") or {} for c in res["content"]] if res["resourceType"] == "DocumentReference"
               else res["presentedForm"])
    target = next(a for a in holders if _text_like(a))
    target["data"] = base64.b64encode(narrative.encode()).decode()
    # What was fetched had its markup removed, and the original's size and hash don't describe this.
    target["contentType"] = "text/plain; charset=utf-8"
    target.pop("size", None)
    target.pop("hash", None)
    return res


def _report_body(res: dict[str, Any], details_json: Optional[str]) -> Optional[str]:
    """A report with a conclusion can also have its full text, fetched separately."""
    if res.get("resourceType") != "DiagnosticReport" or not res.get("conclusion") or not details_json:
        return None
    try:
        return json.loads(details_json).get("full_text")
    except ValueError:
        return None


# The resources with an Annotation note: what a person said about a record stays beside it in the file, and is labelled.
_TAKES_A_NOTE = {"MedicationRequest", "MedicationStatement", "Condition", "AllergyIntolerance", "Observation", "Immunization", "Procedure"}


def _with_person_note(res: dict[str, Any], override: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not override or res.get("resourceType") not in _TAKES_A_NOTE:
        return res
    words = record_overrides.said(override)
    note = {"authorString": record_overrides.NOTE_AUTHOR, "text": f"Patient-reported: {words['text']}"}
    return {**res, "note": [*(res.get("note") or []), note]}


def _medication_from_record(ref: str, row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Saved before referenced resources were kept: the name and code live on the record, so the Medication is made from
    those. Without it, a reader of the file (this app included) has only a reference to nothing."""
    if row.get("title") in (None, "", "Medication"):
        return None
    code: dict[str, Any] = {"text": row["title"]}
    if row.get("code"):
        code["coding"] = [{k: v for k, v in (("system", row.get("code_system")), ("code", row["code"]),
                                             ("display", row.get("code_display"))) if v}]
    return {"resourceType": "Medication", "id": ref.split("/")[-1], "code": code}


def _reference_key(ref: Optional[str]) -> Optional[str]:
    if not ref or ref.startswith("#"):
        return None
    parts = ref.rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def _collect(profile_id: str) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """(resource as it should be exported, its source), each patient first, then what records refer to, then the records."""
    with read() as conn:
        sources = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, display_name, fhir_base_url FROM connections WHERE profile_id = ?", (profile_id,))}
        identities = conn.execute("SELECT connection_id, raw_json FROM patient_identities WHERE profile_id = ? AND raw_json IS NOT NULL",
                                  (profile_id,)).fetchall()
        references = conn.execute(
            "SELECT f.connection_id, f.resource_type, f.resource_id, f.raw_json FROM fhir_references f "
            "JOIN connections c ON c.id = f.connection_id WHERE c.profile_id = ?", (profile_id,)).fetchall()
        rows = conn.execute(
            "SELECT id, dedup_key, raw_json, narrative, details_json, connection_id, category, title, code_system, code, code_display, resource_type FROM clinical_records "
            "WHERE profile_id = ? AND raw_json IS NOT NULL ORDER BY category, effective_at", (profile_id,)).fetchall()

    said = record_overrides.for_profile(profile_id)

    def load(raw: str) -> Optional[dict[str, Any]]:
        try:
            res = json.loads(raw)
        except ValueError:
            return None
        return res if isinstance(res, dict) and res.get("resourceType") else None

    for row in identities:
        if (res := load(row["raw_json"])) and row["connection_id"] in sources:
            yield res, sources[row["connection_id"]]
    have = {(r["connection_id"], f"{r['resource_type']}/{r['resource_id']}") for r in references}
    for row in references:
        if (res := load(row["raw_json"])) and row["connection_id"] in sources:
            yield res, sources[row["connection_id"]]
    for row in rows:
        res, source = load(row["raw_json"]), sources.get(row["connection_id"])
        if not res or not source:
            continue
        key = _reference_key((res.get("medicationReference") or {}).get("reference"))
        if key and key.startswith("Medication/") and (row["connection_id"], key) not in have:
            if made := _medication_from_record(key, dict(row)):
                have.add((row["connection_id"], key))
                yield made, source
        text = _report_body(res, row["details_json"]) or row["narrative"]
        yield _with_person_note(_with_note_text(res, text), said.get(record_overrides.key_for(dict(row)))), source


def entries(profile_id: str) -> Iterator[dict[str, Any]]:
    seen: set[str] = set()
    for res, source in _collect(profile_id):
        tagged = _tagged(res, source)
        full_url = f"{_base_url(source)}/{tagged['resourceType']}/{tagged['id']}"
        if full_url in seen:     # two sources at one address, or one resource twice
            full_url = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, full_url + '|' + source['id'] + '|' + str(len(seen)))}"
        seen.add(full_url)
        yield {"fullUrl": full_url, "resource": tagged}


def bundle_stream(profile_id: str) -> Iterator[bytes]:
    """The collection bundle, written as it's read: a record can be large."""
    yield json.dumps({"resourceType": "Bundle", "type": "collection",
                      "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "meta": {"tag": [{"system": "urn:syntropy-health", "code": "export"}]}})[:-1].encode()
    yield b',"entry":['
    first = True
    for entry in entries(profile_id):
        yield (b"" if first else b",") + json.dumps(entry).encode()
        first = False
    yield b"]}"


# ---------------------------------------------------------------------------
# International Patient Summary
# ---------------------------------------------------------------------------

_LOINC = "http://loinc.org"
_XHTML = 'xmlns="http://www.w3.org/1999/xhtml"'
_EMPTY = {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/list-empty-reason", "code": "unavailable",
                      "display": "Unavailable"}]}
# Where each kind of clinical resource names its patient, for one with none (a lab typed in by hand).
_SUBJECT = {"AllergyIntolerance": "patient", "Immunization": "patient", "Device": "patient", "Coverage": "beneficiary"}
_PROFILE_SEX = {"male": "male", "female": "female", "m": "male", "f": "female", "other": "other"}


def _sections(summary: dict[str, Any], profile_id: str) -> list[tuple[str, str, str, bool, list[dict[str, Any]]]]:
    """Title, LOINC code, what to say if there's nothing, whether the section is required, and its records (deduplicated
    across sources, the current ones, as the Overview shows them)."""
    def recent(category: str, limit: int) -> list[dict[str, Any]]:
        return records.list_records(profile_id, category, limit=limit)["items"]
    allergy_note = summary.get("allergy_note") or "No allergy information is on record."
    return [
        ("Medication Summary", "10160-0", "No current medications are on record.", True, [*summary["active_medications"], *summary["medications_to_check"], *summary["medications_corrected"]]),
        ("Allergies and Intolerances", "48765-2", allergy_note, True, [*summary["allergies"], *summary["allergies_corrected"]]),
        ("Problem List", "11450-4", "No current problems are on record.", True, [*summary["active_conditions"], *summary["conditions_corrected"]]),
        ("History of Immunizations", "11369-6", "", False, summary["immunizations"]),
        ("History of Procedures", "47519-4", "", False, recent("procedures", 100)),
        ("Medical Devices", "46264-8", "", False, recent("devices", 50)),
        ("Diagnostic Results", "30954-2", "", False, summary["recent_labs"]),
        ("Vital Signs", "8716-3", "", False, summary["latest_vitals"]),
        ("Plan of Care", "18776-5", "", False, [p for p in recent("care_plans", 50) if (p.get("status") or "active").lower() == "active"]),
    ]


def _urn(*parts: Any) -> str:
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, '|'.join(str(p) for p in parts))}"


def _rewrite_references(value: Any, patient_urn: str, known: dict[str, str]) -> Any:
    """A document has to stand alone, so every reference points inside it. What isn't in the summary (a visit, a
    prescriber) keeps its name and loses the link."""
    if isinstance(value, list):
        return [_rewrite_references(v, patient_urn, known) for v in value]
    if not isinstance(value, dict):
        return value
    out = {k: _rewrite_references(v, patient_urn, known) for k, v in value.items()}
    ref = out.get("reference")
    if isinstance(ref, str) and not ref.startswith("#"):
        key = _reference_key(ref) or ""
        if key.startswith("Patient/"):
            out["reference"] = patient_urn
        elif key in known:
            out["reference"] = known[key]
        else:
            del out["reference"]
            out.setdefault("display", "Not included in this summary")
    return out


def _line(row: dict[str, Any]) -> str:
    bits = [str(row.get("title") or "Untitled")]
    if row.get("value_num") is not None:
        bits.append(f"{row['value_num']:g} {row.get('unit') or ''}".strip())
    elif row.get("value_text"):
        bits.append(str(row["value_text"]))
    if row.get("status"):
        bits.append(str(row["status"]))
    if row.get("effective_at"):
        bits.append(str(row["effective_at"])[:10])
    if row.get("override"):
        bits.append(f"Patient-reported: {row['override']['text']}")
    return " · ".join(bits)


def _section_text(rows: list[dict[str, Any]], otherwise: str) -> dict[str, str]:
    body = ("<ul>" + "".join(f"<li>{html.escape(_line(r))}</li>" for r in rows) + "</ul>") if rows else f"<p>{html.escape(otherwise)}</p>"
    return {"status": "generated", "div": f"<div {_XHTML}>{body}</div>"}


def _summary_patient(profile: dict[str, Any], summary: dict[str, Any], urn: str) -> dict[str, Any]:
    """The newest identity a source gave, or what's known here. Who the summary is about is the one thing that must be there."""
    res: Optional[dict[str, Any]] = None
    with read() as conn:
        for row in conn.execute("SELECT raw_json FROM patient_identities WHERE profile_id = ? AND raw_json IS NOT NULL "
                                "ORDER BY updated_at DESC LIMIT 1", (profile["id"],)):
            res = json.loads(row[0])
    if not isinstance(res, dict) or res.get("resourceType") != "Patient":
        res = {"resourceType": "Patient"}
    res = _rewrite_references(res, urn, {})
    res["id"] = _fhir_id(res.get("id") or profile["id"])
    res.pop("meta", None)
    if not res.get("name"):
        res["name"] = [{"text": profile["name"]}]
    if not res.get("birthDate") and profile.get("birth_date"):
        res["birthDate"] = profile["birth_date"]
    if not res.get("gender") and _PROFILE_SEX.get((profile.get("sex") or "").lower()):
        res["gender"] = _PROFILE_SEX[profile["sex"].lower()]
    return res


def patient_summary(profile: dict[str, Any]) -> dict[str, Any]:
    """An International Patient Summary: a FHIR document Bundle that starts with a Composition and holds the sections a
    clinician anywhere expects, ready to stand alone."""
    profile_id = profile["id"]
    summary = records.summary(profile_id)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    patient_urn = _urn("patient", profile_id)
    device_urn = _urn("device", profile_id)
    patient = _summary_patient(profile, summary, patient_urn)
    sections = _sections(summary, profile_id)
    ids = [r["id"] for _, _, _, _, rows in sections for r in rows]
    raws: dict[str, str] = {}
    with read() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for row in conn.execute(f"SELECT id, raw_json FROM clinical_records WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                raws[row["id"]] = row["raw_json"]
        references = {(r["connection_id"], f"{r['resource_type']}/{r['resource_id']}"): r["raw_json"] for r in conn.execute(
            "SELECT f.connection_id, f.resource_type, f.resource_id, f.raw_json FROM fhir_references f "
            "JOIN connections c ON c.id = f.connection_id WHERE c.profile_id = ? AND f.resource_type = 'Medication'", (profile_id,))}

    entries_out: list[dict[str, Any]] = [{"fullUrl": patient_urn, "resource": patient}]
    medications: dict[tuple[str, str], str] = {}
    composed: list[dict[str, Any]] = []
    for title, code, otherwise, required, rows in sections:
        refs = []
        kept: list[dict[str, Any]] = []
        for row in rows:
            try:
                res = json.loads(raws.get(row["id"]) or "null")
            except ValueError:
                res = None
            if not isinstance(res, dict) or not res.get("resourceType"):
                continue
            known: dict[str, str] = {}
            source = row["connection_id"]
            key = _reference_key((res.get("medicationReference") or {}).get("reference"))
            if key and key.startswith("Medication/"):
                if (source, key) not in medications:
                    stored = references.get((source, key))
                    med = json.loads(stored) if stored else _medication_from_record(key, row)
                    if med:
                        med = {**med, "id": _fhir_id(med.get("id"))}
                        med.pop("meta", None)
                        medications[(source, key)] = _urn(source, key)
                        entries_out.append({"fullUrl": medications[(source, key)], "resource": _with_valid_ids(_rewrite_references(med, patient_urn, {}))})
                if (source, key) in medications:
                    known[key] = medications[(source, key)]
            res = _with_valid_ids(_rewrite_references(_with_note_text(res, row.get("narrative")), patient_urn, known))
            res["id"] = _fhir_id(res.get("id"))
            res.pop("meta", None)
            subject = _SUBJECT.get(res["resourceType"], "subject")
            if subject in ("patient", "subject", "beneficiary") and not res.get(subject):
                res[subject] = {"reference": patient_urn}
            urn = _urn(row["id"])
            entries_out.append({"fullUrl": urn, "resource": res})
            refs.append({"reference": urn, "display": str(row.get("title") or "Untitled")[:200]})
            kept.append(row)
        if not refs and not required:
            continue
        section: dict[str, Any] = {"title": title, "code": {"coding": [{"system": _LOINC, "code": code, "display": title}]},
                                   "text": _section_text(kept, otherwise)}
        if refs:
            section["entry"] = refs
        else:
            section["emptyReason"] = _EMPTY
        composed.append(section)
    sources = sorted({i["source_name"] for i in summary["identities"] if i.get("source_name")})
    note = ["Made by Syntropy Health from the person's own records" + (f" from {', '.join(sources)}" if sources else "")
            + ". It hasn't been reviewed by a clinician."]
    if summary.get("identity_conflict"):
        note.append("Warning: these records name more than one person ("
                    + ", ".join(str(p.get("full_name")) for p in summary["identity_conflict"]) + "). Check them before relying on this.")
    composition = {
        "resourceType": "Composition", "id": str(uuid.uuid4()), "status": "final",
        "type": {"coding": [{"system": _LOINC, "code": "60591-5", "display": "Patient summary Document"}]},
        "subject": {"reference": patient_urn, "display": profile["name"]}, "date": now, "author": [{"reference": device_urn, "display": "Syntropy Health"}],
        "title": f"Patient summary: {profile['name']}", "confidentiality": "N",
        "text": {"status": "generated", "div": f"<div {_XHTML}>" + "".join(f"<p>{html.escape(n)}</p>" for n in note) + "</div>"},
        "event": [{"code": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ActClass", "code": "PCPI"}]}], "period": {"end": now}}],
        "section": composed,
    }
    device = {"resourceType": "Device", "id": "syntropy-health", "deviceName": [{"name": "Syntropy Health", "type": "user-friendly-name"}],
              "version": [{"value": config.APP_VERSION}]}
    return {"resourceType": "Bundle", "identifier": {"system": "urn:ietf:rfc:3986", "value": f"urn:uuid:{uuid.uuid4()}"},
            "type": "document", "timestamp": now,
            "entry": [{"fullUrl": f"urn:uuid:{composition['id']}", "resource": composition}, *entries_out,
                      {"fullUrl": device_urn, "resource": device}]}


# ---------------------------------------------------------------------------
# CSVs
# ---------------------------------------------------------------------------

class _SafeWriter:
    """csv.writer that neutralizes spreadsheet formulas in text cells (CSV injection): a leading
    = + - @ tab or CR is prefixed with an apostrophe. Numbers are written unchanged."""

    def __init__(self, buf: io.StringIO) -> None:
        self._w = csv.writer(buf)

    @staticmethod
    def _cell(v: Any) -> Any:
        if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + v
        return v

    def writerow(self, row: list[Any]) -> None:
        self._w.writerow([self._cell(v) for v in row])


CSV_KINDS = ("records", "daily", "workouts", "events")


def csv_bytes(kind: str, profile_id: str) -> bytes:
    """New columns go on the end: anything reading these by position keeps working."""
    buf = io.StringIO()
    writer = _SafeWriter(buf)
    if kind == "records":
        writer.writerow(["category", "title", "code_system", "code", "effective_at", "status", "value", "unit",
                         "value_normalized", "unit_normalized", "reference_low", "reference_high", "interpretation",
                         "source", "narrative", "value_as_reported", "comparator", "reference_as_reported", "patient_reported_correction"])
        for r in records.list_records(profile_id, dedupe=False, limit=10**7)["items"]:
            writer.writerow([r["category"], r["title"], r.get("code_system"), r.get("code"), r.get("effective_at"),
                             r.get("status"), r.get("value_num") if r.get("value_num") is not None else r.get("value_text"),
                             r.get("unit"), r.get("value_norm"), r.get("unit_norm"), r.get("ref_low"), r.get("ref_high"),
                             r.get("interpretation"), r.get("source_name"), r.get("narrative") or "",
                             r.get("value_text"), (r.get("details") or {}).get("comparator"), r.get("ref_text"),
                             (r.get("override") or {}).get("text")])
    elif kind == "daily":
        writer.writerow(["day", "metric", "source", "value", "unit", "min", "max", "samples"])
        with read() as conn:
            for row in conn.execute("SELECT * FROM biometric_daily WHERE profile_id = ? ORDER BY day, metric_type", (profile_id,)):
                writer.writerow([row["day"], row["metric_type"], row["source_name"], row["value"], row["unit"],
                                 row["min_value"], row["max_value"], row["sample_count"]])
    elif kind == "workouts":
        writer.writerow(["start", "end", "name", "duration_min", "distance_km", "active_energy_kcal", "total_energy_kcal",
                         "avg_hr", "max_hr", "elevation_gain_m", "indoor", "source", "id",
                         "steps", "min_hr", "elevation_loss_m", "temperature_c", "humidity_pct", "device"])
        for w in activity.list_workouts(profile_id, None, 10**7, dedupe=False):   # every recording, as stored
            writer.writerow([w["start_date"], w["end_date"], w["name"],
                             round(w["duration_s"] / 60, 2) if w["duration_s"] is not None else None,
                             round(w["distance_m"] / 1000, 3) if w["distance_m"] is not None else None,
                             *[round(w[k], 1) if w[k] is not None else None
                               for k in ("active_energy_kcal", "total_energy_kcal", "avg_hr", "max_hr", "elevation_ascent_m")],
                             w["indoor"], w["source_name"], w["id"], w.get("step_count"), w.get("min_hr"),
                             w.get("elevation_descent_m"), w.get("temperature_c"), w.get("humidity_pct"), w.get("device_name")])
    elif kind == "events":
        writer.writerow(["start", "end", "category", "type", "name", "value", "label", "source", "note", "entered_by_you", "details"])
        for e in reversed(activity.list_events(profile_id, None, limit=10**7)):
            writer.writerow([e["start_date"], e["end_date"], e["category"], e["event_type"], e["name"], e["value"],
                             e["value_label"], e["source_name"], e.get("note"), "yes" if e.get("manual") else "",
                             json.dumps(e["metadata"], ensure_ascii=False) if e.get("metadata") else ""])
    else:
        raise ValueError(kind)
    return buf.getvalue().encode()


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------

_README = """Everything Syntropy Health keeps about {name}, made {date}.

manifest.json     what this is and how much of each is in here
fhir.json         health records, as a FHIR R4 collection Bundle, with the source of each
summary-ips.json  an International Patient Summary, a FHIR document for a clinician
records.csv       health records, one row each
daily.csv         wearable data, one row per metric per day
workouts.csv      workouts, one row each
events.csv        the journal and device events, one row each
samples.ndjson    every wearable sample, one JSON object per line
workouts.ndjson   every workout with its heart rate and route, one JSON object per line
journal.ndjson    the journal and device events with notes, one JSON object per line
documents/        PDFs, images and original health summaries that came with records; documents.json says whose
corrections.json  what this person said about entries in their record: not taking, taken differently, wrong, or a note
goals.json        daily goals
chats.ndjson      {whose} conversations with the assistant, one JSON object per line

This file isn't encrypted. Keep it somewhere private. To bring it into another Syntropy Health, use Settings > Your data > Import an archive.
"""


def _sample_rows(profile_id: str) -> Iterator[dict[str, Any]]:
    with read() as conn:
        for row in conn.execute("SELECT id, connection_id, metric_type, hk_identifier, value, unit, start_date, end_date, device_id, "
                                "device_name, source_name, metadata_json FROM biometric_samples WHERE profile_id = ?", (profile_id,)):
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "null")
            yield item


def _workout_rows(profile_id: str) -> Iterator[dict[str, Any]]:
    with read() as conn:
        for row in conn.execute("SELECT * FROM workouts WHERE profile_id = ?", (profile_id,)):
            item = dict(row)
            for name in ("metadata", "heart_rate", "route"):
                item[name] = json.loads(item.pop(f"{name}_json") or "null")
            item.pop("profile_id", None)
            yield item


def _journal_rows(profile_id: str) -> Iterator[dict[str, Any]]:
    with read() as conn:
        for row in conn.execute("SELECT * FROM health_events WHERE profile_id = ?", (profile_id,)):
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json") or "null")
            item["manual"] = bool(item.get("manual"))
            item.pop("profile_id", None)
            yield item


def _chat_rows(profile_id: str, account_id: Optional[str]) -> Iterator[dict[str, Any]]:
    for chat in chats.list_chats(profile_id, account_id):
        full = chats.get_chat(profile_id, account_id, chat["id"])
        if full:
            yield full


def _write_lines(z: zipfile.ZipFile, name: str, rows: Iterator[dict[str, Any]]) -> int:
    count = 0
    with z.open(name, "w", force_zip64=True) as raw:
        out = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
        for row in rows:
            out.write(json.dumps(db.finite(row), ensure_ascii=False, separators=(",", ":"), default=str))
            out.write("\n")
            count += 1
        out.flush()
        out.detach()
    return count


def _write_documents(z: zipfile.ZipFile, profile_id: str) -> int:
    """PDFs, images and original health summaries, one at a time, with an index saying which record each belongs to."""
    index = []
    for found in record_files.all_for_profile(profile_id):
        extension = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
                     "image/tiff": ".tif", "image/heic": ".heic", "application/xml": ".xml"}.get(found["content_type"], "")
        path = f"documents/{found['id']}{extension}"
        z.writestr(path, found["data"])
        index.append({"path": path, **{k: found.get(k) for k in ("resource_type", "resource_id", "name", "content_type")}})
    z.writestr("documents.json", json.dumps(index, indent=2, ensure_ascii=False))
    return len(index)


def _implausible(info: zipfile.ZipInfo) -> bool:
    # A person's own export reaches over 200 to 1 on repeated letters, while deflate tops out near 1030 to 1
    return info.file_size > 1024 * 1024 and info.file_size > max(info.compress_size, 1) * 1000


def _whole_member(z: zipfile.ZipFile, name: str) -> bytes:
    """A part of an archive that's read all at once: one that would fill the memory is refused before it's unpacked."""
    info = z.getinfo(name)
    if info.file_size > MAX_ARCHIVE_MEMBER or _implausible(info):
        raise ValueError("Part of this archive is too large to read, so it wasn't imported.")
    return z.read(name)


def _read_documents(z: zipfile.ZipFile, profile_id: str) -> int:
    try:
        index = json.loads(_whole_member(z, "documents.json"))
    except (KeyError, ValueError):
        return 0
    added = 0
    for item in index if isinstance(index, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("content_type"), str):
            continue
        with read() as conn:
            row = conn.execute("SELECT connection_id FROM clinical_records WHERE profile_id = ? AND resource_type = ? AND resource_id = ?",
                               (profile_id, item.get("resource_type"), item.get("resource_id"))).fetchone()
        try:
            if not row or z.getinfo(item["path"]).file_size > record_files.MAX_FILE:
                continue
            with z.open(item["path"]) as member:
                data = member.read(record_files.MAX_FILE + 1)
        except (KeyError, zipfile.BadZipFile, RuntimeError, OSError):
            continue
        if len(data) <= record_files.MAX_FILE:
            record_files.save(profile_id, row["connection_id"], item["resource_type"], item["resource_id"], item.get("name"), item["content_type"], data)
            added += 1
    return added


@dataclass
class BuiltArchive:
    file: private_files.PrivateFile
    folder: Path


def build_archive(profile: dict[str, Any], account_id: Optional[str]) -> BuiltArchive:
    """On disk, encrypted with a key only this process has, in a folder of its own that the caller removes (and a crash leaves
    for clean_leftovers). A chat belongs to the account that had it, so only this account's are in here."""
    profile_id = profile["id"]
    folder = Path(tempfile.mkdtemp(dir=config.data_dir(), prefix=".backup-work-"))
    target = private_files.create(folder / "archive.zip.encrypted", streaming=True)
    date = time.strftime("%Y-%m-%d")
    counts: dict[str, int] = {}
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            with z.open("fhir.json", "w", force_zip64=True) as out:
                for part in bundle_stream(profile_id):
                    out.write(part)
            z.writestr("summary-ips.json", json.dumps(patient_summary(profile), indent=2, ensure_ascii=False))
            for kind in CSV_KINDS:
                z.writestr(f"{kind}.csv", csv_bytes(kind, profile_id))
            counts["samples"] = _write_lines(z, "samples.ndjson", _sample_rows(profile_id))
            counts["workouts"] = _write_lines(z, "workouts.ndjson", _workout_rows(profile_id))
            counts["journal"] = _write_lines(z, "journal.ndjson", _journal_rows(profile_id))
            counts["chats"] = _write_lines(z, "chats.ndjson", _chat_rows(profile_id, account_id))
            person_goals = goals.get(profile_id)
            z.writestr("goals.json", json.dumps(person_goals, indent=2))
            counts["goals"] = len(person_goals)
            counts["documents"] = _write_documents(z, profile_id)
            said = list(record_overrides.for_profile(profile_id).values())
            z.writestr("corrections.json", json.dumps(said, indent=2, ensure_ascii=False))
            counts["corrections"] = len(said)
            sources = [{k: c.get(k) for k in ("id", "kind", "provider", "display_name", "fhir_base_url", "created_at", "last_sync_at")}
                       for c in connections.list_for_profile(profile_id, include_disconnected=True)]
            with read() as conn:
                counts["records"] = conn.execute("SELECT COUNT(*) FROM clinical_records WHERE profile_id = ?", (profile_id,)).fetchone()[0]
            z.writestr("manifest.json", json.dumps({
                "format": ARCHIVE_FORMAT, "version": ARCHIVE_VERSION, "app_version": config.APP_VERSION,
                "schema_version": db.newest_schema(), "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "person": {k: profile.get(k) for k in ("id", "name", "relationship", "birth_date", "sex")},
                "counts": counts, "sources": sources}, indent=2, ensure_ascii=False))
            z.writestr("README.txt", _README.format(name=profile["name"], date=date, whose="this account's" if account_id else "these"))
    except BaseException:
        target.close()
        shutil.rmtree(folder, ignore_errors=True)
        raise
    target.finish()
    return BuiltArchive(target, folder)


# ---------------------------------------------------------------------------
# Reading an archive back in
# ---------------------------------------------------------------------------

def _source_for(profile_id: str, origin: Optional[str], sources: dict[str, dict[str, Any]], made: dict[Optional[str], str]) -> Optional[str]:
    """One source here per source in the archive, found again when the same archive is imported twice. Rows that had none
    (typed into the Journal, say) stay without one."""
    if origin in made:
        return made[origin]
    if not origin:
        return None
    info = sources.get(origin) or {}
    conn = connections.find_import(profile_id, origin)
    if not conn:
        conn = connections.create(profile_id, "import", "syntropy_archive", info.get("display_name") or "Imported archive",
                                  mode="live", metadata={"origin": origin, "imported_at": time.time()})
    made[origin] = conn["id"]
    return conn["id"]


def _lines(z: zipfile.ZipFile, name: str) -> Iterator[dict[str, Any]]:
    if name not in z.namelist():
        return
    if _implausible(z.getinfo(name)):
        raise ValueError("Part of this archive is too large to read, so it wasn't imported.")
    with z.open(name) as raw:
        for line in io.TextIOWrapper(raw, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    yield item


def _by_source(rows: Iterator[dict[str, Any]], size: int) -> Iterator[tuple[Optional[str], list[dict[str, Any]]]]:
    pending: dict[Optional[str], list[dict[str, Any]]] = {}
    for row in rows:
        group = pending.setdefault(row.get("connection_id"), [])
        group.append(row)
        if len(group) >= size:
            yield row.get("connection_id"), group
            pending[row.get("connection_id")] = []
    for origin, group in pending.items():
        if group:
            yield origin, group


def import_archive(profile_id: str, account_id: Optional[str], path: Path, on_progress: Any = None) -> dict[str, Any]:
    """Safe to run twice: rows keep their ids, so what's already here is left alone, and nothing here is overwritten. A goal
    or conversation that's already here stays as it is and is counted as kept. Conversations that don't fit are counted as
    skipped; the archive has the newest first, so those are the oldest."""
    from app.services import imports, sync

    try:
        z = zipfile.ZipFile(private_files.open_read(path))
    except zipfile.BadZipFile as exc:
        raise ValueError("This file isn't an archive from Syntropy Health.") from exc
    with z:
        try:
            manifest = json.loads(_whole_member(z, "manifest.json"))
        except (KeyError, ValueError) as exc:
            raise ValueError("This file isn't an archive from Syntropy Health.") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != ARCHIVE_FORMAT:
            raise ValueError("This file isn't an archive from Syntropy Health.")
        if int(manifest.get("version") or 0) > ARCHIVE_VERSION:
            raise ValueError("This archive was made by a newer version of Syntropy Health. Install the latest version to import it.")
        sources = {s["id"]: s for s in manifest.get("sources") or [] if isinstance(s, dict) and s.get("id")}
        made: dict[Optional[str], str] = {}
        out: dict[str, Any] = {"samples": 0, "workouts": 0, "journal": 0, "goals": 0, "goals_kept": 0, "chats": 0, "chats_kept": 0, "chats_skipped": 0}
        if "fhir.json" in z.namelist():
            resources = imports.parse_fhir_payload(_whole_member(z, "fhir.json"))
            if resources:
                out["clinical"] = sync.import_fhir_resources(profile_id, "Imported archive", resources)
        for origin, group in _by_source(_lines(z, "samples.ndjson"), 5000):
            result = biometrics.insert_samples(profile_id, _source_for(profile_id, origin, sources, made), group)
            out["samples"] += result["inserted"]
            if on_progress:
                on_progress(out["samples"])
        for origin, group in _by_source((_as_workout(w) for w in _lines(z, "workouts.ndjson")), 200):
            out["workouts"] += activity.insert_workouts(profile_id, _source_for(profile_id, origin, sources, made), group)
        for origin, group in _by_source((_as_event(e) for e in _lines(z, "journal.ndjson")), 500):
            out["journal"] += activity.insert_events(profile_id, _source_for(profile_id, origin, sources, made), group)
        if "goals.json" in z.namelist():
            wanted = json.loads(_whole_member(z, "goals.json"))
            here = goals.get(profile_id)
            for metric, goal in (wanted.items() if isinstance(wanted, dict) else []):
                if metric in here:
                    out["goals_kept"] += 1
                    continue
                try:
                    goals.set_goal(profile_id, metric, goal.get("target"), goal.get("direction") or "min")
                    out["goals"] += 1
                except (ValueError, AttributeError, TypeError):
                    pass
        for chat in _lines(z, "chats.ndjson"):
            out[_bring_in_chat(profile_id, account_id, chat)] += 1
        out["documents"] = _read_documents(z, profile_id)
        if "corrections.json" in z.namelist():
            wanted = json.loads(_whole_member(z, "corrections.json"))
            out["corrections"] = record_overrides.bring_in(profile_id, wanted if isinstance(wanted, list) else [])
    return out


def _bring_in_chat(profile_id: str, account_id: Optional[str], chat: dict[str, Any]) -> str:
    """Which count this conversation goes in. An id another person here already holds gets one of its own, the same one
    each time."""
    original = str(chat.get("id") or db.new_id("chat"))
    theirs = activity.stable_id("chat", original, profile_id, account_id)
    if chats.get_chat(profile_id, account_id, original) or chats.get_chat(profile_id, account_id, theirs):
        return "chats_kept"
    title, messages = str(chat.get("title") or "Conversation")[:200], chat.get("messages") or []
    updated = _time_or(chat.get("updated_at"), time.time())
    dates = (_time_or(chat.get("created_at"), updated), updated)
    # An archive from before these existed has neither: nothing was pinned, and its titles came from the first question.
    marks = {"pinned": chat.get("pinned") is True, "title_set": chat.get("title_set") is True}
    try:
        try:
            added = chats.add_chat(profile_id, account_id, original, title, messages, *dates, **marks)
        except PermissionError:
            added = chats.add_chat(profile_id, account_id, theirs, title, messages, *dates, **marks)
    except (ValueError, TypeError):      # longer than a conversation can be
        return "chats_skipped"
    return "chats" if added else "chats_skipped"


def _time_or(value: Any, otherwise: float) -> float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else otherwise


def _as_workout(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "start": row.get("start_date"), "end": row.get("end_date")}


def _as_event(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "start": row.get("start_date"), "end": row.get("end_date")}
