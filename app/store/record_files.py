"""The documents behind clinical records: PDFs and images a source sent or linked, and the health summaries files were imported from.

Kept in the database, not beside it, so a backup holds them and deleting a source or a person deletes them (the foreign key to
connections). Text read from a PDF goes on the record itself, where search, the report and Ask already find it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import logging
import time
from typing import Any, Iterable, Optional

from app.core import data_fixes
from app.core.db import db, new_id, read

log = logging.getLogger("syntropy.record_files")

MAX_FILE = 32 * 1024 * 1024
MAX_PAGES = 500
MAX_TEXT = 200_000
READ_SECONDS = 20.0
FIX_READ_SECONDS = 1.0
# Opened in the browser. Anything else is only ever downloaded: an HTML or SVG file from a source must not run on this page's address.
SHOWN_INLINE = ("application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp")
FILE_TYPES = ("application/pdf", "image/")
_COLUMNS = "id, profile_id, connection_id, resource_type, resource_id, name, content_type, size, state, stored_at"

HAS_TEXT, SCANNED, IMAGE, ORIGINAL, UNREADABLE, TOO_LARGE, READ_LATER = "text", "scanned", "image", "original", "unreadable", "too_large", "read_later"
# How much of a page's drawing instructions gets read per second allowed: parsing them is the one step that can't be stopped midway.
STREAM_PER_SECOND = 1_000_000
GENERIC_TYPES = ("", "application/octet-stream", "binary/octet-stream", "application/binary", "application/unknown")


def media_type(content_type: Optional[str]) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def is_file_type(content_type: Optional[str]) -> bool:
    return media_type(content_type).startswith(FILE_TYPES)


def served_type(declared: Optional[str], served: Optional[str], data: bytes) -> str:
    """What an attachment really is. Servers often send a PDF as generic bytes, and those must never become a note's text."""
    if is_file_type(served):
        return served or ""
    if data[:1024].lstrip().startswith(b"%PDF-"):
        return "application/pdf"
    return (declared or "") if is_file_type(declared) and media_type(served) in GENERIC_TYPES else (served or "")


class _OutOfTime(Exception):
    pass


def pdf_text(data: bytes, seconds: float = READ_SECONDS, later: bool = False) -> tuple[str, str]:
    """The text in a PDF and what became of reading it. A hostile PDF can't hold this up for long or fill the database: there's
    a limit on pages, time and text, and what was read up to a limit is kept. With ``later``, a PDF that needs longer than this is
    left to be read in full another time."""
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    parts: list[str] = []
    size = 0
    deadline = time.monotonic() + seconds
    ran_out = False

    def in_time(*_: Any) -> None:       # also called inside a page, for every instruction on it
        if time.monotonic() > deadline:
            raise _OutOfTime

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return "", UNREADABLE
        for page in reader.pages[:MAX_PAGES]:
            in_time()
            contents = page.get_contents()
            if contents is not None and len(contents.get_data()) > seconds * STREAM_PER_SECOND:
                ran_out = True
                continue
            found = page.extract_text(visitor_operand_before=in_time) or ""
            parts.append(found)
            size += len(found)
            if size >= MAX_TEXT:
                break
    except _OutOfTime:
        ran_out = True
    except (PyPdfError, ValueError, TypeError, KeyError, AttributeError, IndexError, OSError, RecursionError, MemoryError, AssertionError,
            ZeroDivisionError, OverflowError, NotImplementedError) as exc:
        log.info("Couldn't read a PDF: %s", type(exc).__name__)
        if not "".join(parts).strip():
            return "", UNREADABLE
    if ran_out and later:
        return "", READ_LATER
    text = "\n\n".join(p.strip() for p in parts if p.strip())[:MAX_TEXT]
    return text, HAS_TEXT if text else UNREADABLE if ran_out else SCANNED


def _read_state(data: bytes, content_type: str, seconds: float = READ_SECONDS, later: bool = False) -> tuple[str, str]:
    if len(data) > MAX_FILE:
        return "", TOO_LARGE
    if media_type(content_type) == "application/pdf":
        return pdf_text(data, seconds, later)
    return "", IMAGE if media_type(content_type).startswith("image/") else ORIGINAL


def _write(conn: Any, profile_id: str, connection_id: str, resource_type: str, resource_id: str, name: Optional[str],
           content_type: str, data: bytes, text: str, state: str, fingerprint: Optional[str]) -> None:
    keep = state != TOO_LARGE
    digest = hashlib.sha256(data).hexdigest()
    where = "connection_id = ? AND resource_type = ? AND resource_id = ?"
    key = (connection_id, resource_type, resource_id)
    if conn.execute(f"SELECT 1 FROM record_files WHERE {where} AND sha256 = ?", (*key, digest)).fetchone():
        conn.execute(f"UPDATE record_files SET fingerprint = coalesce(?, fingerprint), state = ?, text = ? WHERE {where} AND sha256 = ?",
                     (fingerprint, state, text or None, *key, digest))
        return
    if fingerprint:     # the source changed this record: the file that was its earlier version goes
        conn.execute(f"DELETE FROM record_files WHERE {where} AND coalesce(fingerprint, '') != ?", (*key, fingerprint))
    conn.execute("""INSERT INTO record_files(id, profile_id, connection_id, resource_type, resource_id, name, content_type, size, sha256,
                        fingerprint, state, text, stored_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (new_id("file"), profile_id, connection_id, resource_type, resource_id, (name or "")[:200] or None, content_type, len(data), digest,
                  fingerprint, state, text or None, time.time(), data if keep else None))


def _known(conn: Any, connection_id: str, resource_type: str, resource_id: str, data: bytes) -> Any:
    """This file, if it's here and has been read."""
    return conn.execute("SELECT text, state FROM record_files WHERE connection_id = ? AND resource_type = ? AND resource_id = ? AND sha256 = ? AND state != ?",
                        (connection_id, resource_type, resource_id, hashlib.sha256(data).hexdigest(), READ_LATER)).fetchone()


def save(profile_id: str, connection_id: str, resource_type: str, resource_id: str, name: Optional[str], content_type: str,
         data: bytes, fingerprint: Optional[str] = None) -> tuple[str, str]:
    """Keeps a file, and returns the text in it and its state. A file already here isn't read again: a source sending a PDF inside
    a note sends it at every sync. The reading happens before the write lock is taken."""
    with read() as conn:
        known = _known(conn, connection_id, resource_type, resource_id, data)
    text, state = (known["text"] or "", known["state"]) if known else _read_state(data, content_type)
    with db() as conn:
        _write(conn, profile_id, connection_id, resource_type, resource_id, name, media_type(content_type), data, text, state, fingerprint)
    return text, state


def note_too_large(profile_id: str, connection_id: str, resource_type: str, resource_id: str, name: Optional[str], content_type: str,
                   size: int, fingerprint: Optional[str]) -> None:
    """Remembered so each sync doesn't download it again to find the same thing, and so the record can say why there's no document."""
    with db() as conn:
        conn.execute("DELETE FROM record_files WHERE connection_id = ? AND resource_type = ? AND resource_id = ?", (connection_id, resource_type, resource_id))
        conn.execute("""INSERT INTO record_files(id, profile_id, connection_id, resource_type, resource_id, name, content_type, size, fingerprint, state, stored_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (new_id("file"), profile_id, connection_id, resource_type, resource_id, (name or "")[:200] or None, media_type(content_type), size, fingerprint, TOO_LARGE, time.time()))


def put_text(rec: dict[str, Any], text: str) -> None:
    """Text belongs in the same places whichever way it came. A report's conclusion stays its one-line summary."""
    if not text:
        return
    if rec.get("category") == "reports" and (rec.get("raw") or {}).get("conclusion"):
        rec.setdefault("details", {})["full_text"] = text
    else:
        rec["narrative"] = text


def inline_attachments(res: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """The PDFs and images a note or report carries inside itself."""
    if not isinstance(res, dict):
        return []
    forms = [c.get("attachment") for c in res.get("content") or [] if isinstance(c, dict)] if res.get("resourceType") == "DocumentReference" \
        else res.get("presentedForm") or []
    return [a for a in forms if isinstance(a, dict) and a.get("data") and is_file_type(a.get("contentType"))]


def _decode(att: dict[str, Any]) -> Optional[bytes]:
    try:
        return base64.b64decode(att["data"], validate=False)
    except (binascii.Error, ValueError, TypeError):
        return None


def has_text(rec: dict[str, Any]) -> bool:
    if rec.get("category") == "reports" and (rec.get("raw") or {}).get("conclusion"):
        return bool((rec.get("details") or {}).get("full_text"))
    return bool(rec.get("narrative"))


def _drop_replaced(connection_id: str, resource_type: str, resource_id: str, current: set[str]) -> None:
    """A record the source corrected comes with its corrected PDF, and the earlier one mustn't stay to be opened by mistake. Files
    fetched from a link have a fingerprint, and go by that."""
    where = "connection_id = ? AND resource_type = ? AND resource_id = ? AND fingerprint IS NULL"
    with read() as conn:
        stale = [r["id"] for r in conn.execute(f"SELECT id, sha256 FROM record_files WHERE {where}", (connection_id, resource_type, resource_id))
                 if r["sha256"] not in current]
    if stale:
        with db() as conn:
            conn.executemany("DELETE FROM record_files WHERE id = ?", [(i,) for i in stale])


def take_inline(profile_id: str, connection_id: str, recs: Iterable[dict[str, Any]]) -> int:
    """From records about to be stored: keeps each PDF and image, and fills in the record's text from a PDF when it has none."""
    kept = 0
    for rec in recs:
        if rec.get("category") not in ("notes", "reports"):
            continue
        current: set[str] = set()
        for att in inline_attachments(rec.get("raw")):
            data = _decode(att)
            if not data:
                continue
            text, _ = save(profile_id, connection_id, rec["resource_type"], rec["resource_id"], att.get("title"), att.get("contentType"), data)
            current.add(hashlib.sha256(data).hexdigest())
            kept += 1
            if text and not has_text(rec):
                put_text(rec, text)
        if current:
            _drop_replaced(connection_id, rec["resource_type"], rec["resource_id"], current)
    return kept


def for_record(connection_id: str, resource_type: str, resource_id: str) -> list[dict[str, Any]]:
    with read() as conn:
        rows = conn.execute(f"SELECT {_COLUMNS} FROM record_files WHERE connection_id = ? AND resource_type = ? AND resource_id = ? ORDER BY stored_at",
                            (connection_id, resource_type, resource_id)).fetchall()
    return [dict(r) for r in rows]


def delete_for_record(connection_id: str, resource_type: str, resource_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM record_files WHERE connection_id = ? AND resource_type = ? AND resource_id = ?", (connection_id, resource_type, resource_id))


def fingerprints(connection_id: str) -> dict[tuple[str, str], Optional[str]]:
    """What each record's file was fetched for, so one with no text in it isn't downloaded again at every sync."""
    with read() as conn:
        rows = conn.execute("SELECT resource_type, resource_id, fingerprint FROM record_files WHERE connection_id = ?", (connection_id,)).fetchall()
    return {(r["resource_type"], r["resource_id"]): r["fingerprint"] for r in rows}


def get(file_id: str) -> Optional[dict[str, Any]]:
    with read() as conn:
        row = conn.execute(f"SELECT {_COLUMNS}, data FROM record_files WHERE id = ?", (file_id,)).fetchone()
    return dict(row) if row else None


def all_for_profile(profile_id: str) -> Iterable[dict[str, Any]]:
    """One at a time, each with its bytes: they can be large, so they're never all held at once."""
    with read() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM record_files WHERE profile_id = ? AND data IS NOT NULL ORDER BY stored_at", (profile_id,))]
    for file_id in ids:
        found = get(file_id)
        if found:
            yield found


def _keep_inline_files(conn: Any, cursor: Any, size: int) -> Any:
    """Notes and reports stored before files were kept: their PDFs and images are in their stored resources. Short reads, because
    this runs in a transaction and others are waiting to write: a PDF needing longer is still kept, and it's read in full the next
    time it arrives."""
    rows = conn.execute("""SELECT rowid, id, profile_id, connection_id, resource_type, resource_id, category, narrative, raw_json, details_json
                           FROM clinical_records WHERE rowid > ? AND category IN ('notes', 'reports') ORDER BY rowid LIMIT ?""",
                        (cursor or 0, min(size, 50))).fetchall()
    started = time.monotonic()
    last = None
    for row in rows:
        last = row["rowid"]
        if '"data"' not in (row["raw_json"] or ""):
            continue
        try:
            res = json.loads(row["raw_json"]) if row["raw_json"] else None
        except ValueError:
            continue
        details = json.loads(row["details_json"]) if row["details_json"] else {}
        rec = {"category": row["category"], "raw": res, "narrative": row["narrative"], "details": details}
        for att in inline_attachments(res):
            data = _decode(att)
            if not data:
                continue
            known = _known(conn, row["connection_id"], row["resource_type"], row["resource_id"], data)
            text, state = (known["text"] or "", known["state"]) if known else _read_state(
                data, att.get("contentType") or "", seconds=max(0.0, FIX_READ_SECONDS - (time.monotonic() - started)), later=True)
            _write(conn, row["profile_id"], row["connection_id"], row["resource_type"], row["resource_id"], att.get("title"),
                   media_type(att.get("contentType")), data, text, state, None)
            if text and not has_text(rec):
                put_text(rec, text)
                conn.execute("UPDATE clinical_records SET narrative = ?, details_json = ? WHERE rowid = ?",
                             (rec["narrative"], json.dumps(rec["details"]), row["rowid"]))
        if time.monotonic() - started > FIX_READ_SECONDS:
            break
    return last


data_fixes.register("record-files-from-stored-resources", _keep_inline_files)
