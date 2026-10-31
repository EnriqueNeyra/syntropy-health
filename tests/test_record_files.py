"""PDFs and images behind notes and reports: kept here, read for text, and never fetched from anywhere the token shouldn't go."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import zipfile

import pytest

from app.connectors import fhir_normalize as fn
from app.connectors import smart
from app.services import exports, sync
from app.store import connections, profiles, record_files, records


def pdf_with(text: str = "", pages: int = 1) -> bytes:
    """A small real PDF, so pypdf reads what it would read from a portal."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=300, height=300)
        if text:
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 10 100 Td ({text}) Tj ET".encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture()
def pid(client):
    return profiles.list_profiles()[0]["id"]


def _note(data: bytes, ctype="application/pdf", rid="n1"):
    return {"resourceType": "DocumentReference", "id": rid, "status": "current", "type": {"text": "Scan"}, "date": "2025-01-02",
            "content": [{"attachment": {"contentType": ctype, "title": "scan.pdf", "data": base64.b64encode(data).decode()}}]}


def test_text_in_an_inline_pdf_is_read_and_can_be_searched(client, pid):
    res = sync.import_fhir_resources(pid, "File", [_note(pdf_with("HEMOGLOBINZEBRA result"))])
    assert res["inserted"] == 1
    found = records.list_records(pid, "notes", q="hemoglobinzebra")["items"]
    assert len(found) == 1 and "HEMOGLOBINZEBRA" in found[0]["narrative"]
    assert record_files.for_record(res["connection_id"], "DocumentReference", "n1")[0]["state"] == record_files.HAS_TEXT


def test_a_scan_says_so_and_can_be_opened_only_by_someone_who_can_see_it(client, pid):
    raw = pdf_with()
    sync.import_fhir_resources(pid, "File", [_note(raw)])
    rec = records.list_records(pid, "notes")["items"][0]
    body = client.get(f"/api/records/{rec['id']}").json()
    assert body["files"][0]["state"] == record_files.SCANNED and not body["narrative"] and "data" not in body["files"][0]
    url = f"/api/records/{rec['id']}/files/{body['files'][0]['id']}"
    got = client.get(url)
    assert got.status_code == 200 and got.content == raw and got.headers["content-type"] == "application/pdf"
    assert got.headers["content-disposition"].startswith("inline") and got.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"{url}?download=true").headers["content-disposition"].startswith("attachment")


def test_a_file_cant_be_reached_through_another_record(client, pid):
    sync.import_fhir_resources(pid, "File", [_note(pdf_with(), rid="n1"), _note(pdf_with("other"), rid="n2")])
    rows = records.list_records(pid, "notes", dedupe=False)["items"]
    first = client.get(f"/api/records/{rows[0]['id']}").json()["files"][0]["id"]
    assert client.get(f"/api/records/{rows[1]['id']}/files/{first}").status_code == 404


def test_html_from_a_source_is_only_ever_downloaded(client, pid):
    res = sync.import_fhir_resources(pid, "File", [_note(b"plain", "text/plain")])
    record_files.save(pid, res["connection_id"], "DocumentReference", "n1", "x.html", "text/html", b"<script>alert(1)</script>")
    rec = records.list_records(pid, "notes")["items"][0]
    file_id = next(f for f in client.get(f"/api/records/{rec['id']}").json()["files"] if f["name"] == "x.html")["id"]
    got = client.get(f"/api/records/{rec['id']}/files/{file_id}")
    assert got.headers["content-type"] == "application/octet-stream" and got.headers["content-disposition"].startswith("attachment")


def test_a_broken_or_locked_pdf_keeps_the_file_and_says_so():
    assert record_files.pdf_text(b"%PDF-1.4 not really") == ("", record_files.UNREADABLE)


def test_pages_are_capped(monkeypatch):
    monkeypatch.setattr(record_files, "MAX_PAGES", 2)
    calls = []
    raw = pdf_with("page", pages=5)
    from pypdf import PageObject
    real = PageObject.extract_text
    monkeypatch.setattr(PageObject, "extract_text", lambda self, *a, **k: calls.append(1) or real(self, *a, **k))
    text, state = record_files.pdf_text(raw)
    assert len(calls) == 2 and state == record_files.HAS_TEXT and text


def test_a_large_file_is_noted_rather_than_kept(client, pid, monkeypatch):
    monkeypatch.setattr(record_files, "MAX_FILE", 10)
    res = sync.import_fhir_resources(pid, "File", [_note(b"0123456789abc")])
    row = record_files.for_record(res["connection_id"], "DocumentReference", "n1")[0]
    assert row["state"] == record_files.TOO_LARGE and not record_files.get(row["id"])["data"]


def test_stored_records_get_their_files_through_the_data_fix(client, pid):
    res = sync.import_fhir_resources(pid, "File", [_note(pdf_with("OLDROWZEBRA"))])
    from app.core.db import db
    with db() as conn:
        conn.execute("DELETE FROM record_files")
        conn.execute("UPDATE clinical_records SET narrative = NULL")
    with db() as conn:
        assert record_files._keep_inline_files(conn, None, 100)
    assert record_files.for_record(res["connection_id"], "DocumentReference", "n1")
    assert "OLDROWZEBRA" in records.list_records(pid, "notes")["items"][0]["narrative"]
    with db() as conn:
        assert record_files._keep_inline_files(conn, 10**9, 100) is None


def test_removing_the_source_or_the_person_removes_the_files(client, pid):
    res = sync.import_fhir_resources(pid, "File", [_note(pdf_with())])
    other = profiles.create_profile("Other", "other")["id"]
    res2 = sync.import_fhir_resources(other, "File", [_note(pdf_with())])
    assert record_files.for_record(res2["connection_id"], "DocumentReference", "n1")
    profiles.delete_profile(other)
    assert not record_files.for_record(res2["connection_id"], "DocumentReference", "n1")
    profiles.delete_all_data(pid)
    assert not record_files.fingerprints(res["connection_id"])


def test_an_archive_carries_documents_and_a_later_copy_finds_them(client, pid):
    raw = pdf_with("ARCHIVEZEBRA")
    sync.import_fhir_resources(pid, "File", [_note(raw)])
    path = exports.build_archive(profiles.get_profile(pid), None)
    with zipfile.ZipFile(path) as z:
        index = json.loads(z.read("documents.json"))
        assert z.read(index[0]["path"]) == raw
    other = profiles.create_profile("Other", "other")["id"]
    assert exports.import_archive(other, None, path)["documents"] == 1


# ---------------------------------------------------------------------------
# Linked files, at sync
# ---------------------------------------------------------------------------

class FakeClient:
    def __init__(self, files):
        self.files = files
        self.calls = []

    def time_left(self):
        return 100

    async def read_binary_text(self, http, url, max_bytes=512_000):
        self.calls.append(url)
        return self.files.get(url)

    async def read_binary_file(self, http, url, max_bytes, accept=""):
        self.calls.append(url)
        found = self.files.get(url)
        if found and len(found[1]) > max_bytes:
            raise smart.BinaryTooLarge(len(found[1]))
        return found


def _fetch(pid, conn, recs, client):
    return asyncio.run(sync._fetch_note_bodies(conn, client, recs, True))


def _store(pid, conn, recs):
    records.upsert_records(pid, conn["id"], conn["display_name"], recs)


def _report(**kw):
    return {"resourceType": "DiagnosticReport", "id": "r1", "status": "final", "effectiveDateTime": "2025-02-01", "code": {"text": "CT chest"},
            "category": [{"coding": [{"code": "RAD"}]}], **kw}


@pytest.fixture()
def source(pid):
    return connections.create(pid, "import", "fhir_file", "Clinic", mode="live")


def test_a_conclusion_no_longer_stops_the_full_report_being_fetched(client, pid, source):
    res = _report(conclusion="No acute findings.", presentedForm=[{"url": "Binary/1", "contentType": "text/plain"}])
    _, recs = fn.normalize_resources([res])
    fake = FakeClient({"Binary/1": ("text/plain", b"FINDINGS: the whole report.")})
    done = _fetch(pid, source, recs, fake)
    assert done["fetched"] == 1
    assert recs[0]["narrative"] == "No acute findings." and "whole report" in recs[0]["details"]["full_text"]
    _store(pid, source, recs)
    # The next sync has it, and doesn't ask again.
    _, again = fn.normalize_resources([res])
    fake2 = FakeClient({})
    _fetch(pid, source, again, fake2)
    assert not fake2.calls and "whole report" in again[0]["details"]["full_text"] and again[0]["narrative"] == "No acute findings."


def test_a_report_stored_with_only_its_conclusion_gets_its_body_at_the_next_sync(client, pid, source):
    res = _report(conclusion="Fine.", presentedForm=[{"url": "Binary/1", "contentType": "text/plain"}])
    _, recs = fn.normalize_resources([res])
    _store(pid, source, recs)
    _, again = fn.normalize_resources([res])
    fake = FakeClient({"Binary/1": ("text/plain", b"The body.")})
    _fetch(pid, source, again, fake)
    assert fake.calls == ["Binary/1"] and again[0]["details"]["full_text"] == "The body."


def test_a_linked_pdf_is_kept_read_and_not_fetched_twice(client, pid, source):
    res = {"resourceType": "DocumentReference", "id": "n9", "status": "current", "type": {"text": "Letter"}, "date": "2025-02-01",
           "content": [{"attachment": {"contentType": "application/pdf", "url": "Binary/9", "title": "letter.pdf"}}]}
    _, recs = fn.normalize_resources([res])
    fake = FakeClient({"Binary/9": ("application/pdf", pdf_with("LINKEDZEBRA"))})
    assert _fetch(pid, source, recs, fake)["fetched"] == 1
    assert "LINKEDZEBRA" in recs[0]["narrative"]
    _store(pid, source, recs)
    assert record_files.for_record(source["id"], "DocumentReference", "n9")[0]["name"] == "letter.pdf"
    _, again = fn.normalize_resources([res])
    fake2 = FakeClient({})
    _fetch(pid, source, again, fake2)
    assert not fake2.calls and "LINKEDZEBRA" in again[0]["narrative"]


def test_a_linked_scan_isnt_downloaded_at_every_sync(client, pid, source):
    res = {"resourceType": "DocumentReference", "id": "n8", "status": "current", "type": {"text": "Scan"}, "date": "2025-02-01",
           "content": [{"attachment": {"contentType": "image/png", "url": "Binary/8"}}]}
    _, recs = fn.normalize_resources([res])
    fake = FakeClient({"Binary/8": ("image/png", b"\x89PNG not really")})
    _fetch(pid, source, recs, fake)
    _store(pid, source, recs)
    _, again = fn.normalize_resources([res])
    fake2 = FakeClient({"Binary/8": ("image/png", b"\x89PNG not really")})
    _fetch(pid, source, again, fake2)
    assert not fake2.calls


def test_a_text_version_wins_over_the_pdf_of_the_same_note(client, pid, source):
    res = {"resourceType": "DocumentReference", "id": "n7", "status": "current", "type": {"text": "Note"}, "date": "2025-02-01",
           "content": [{"attachment": {"contentType": "application/pdf", "url": "Binary/p"}}, {"attachment": {"contentType": "text/plain", "url": "Binary/t"}}]}
    _, recs = fn.normalize_resources([res])
    fake = FakeClient({"Binary/t": ("text/plain", b"As text."), "Binary/p": ("application/pdf", pdf_with("x"))})
    _fetch(pid, source, recs, fake)
    assert fake.calls == ["Binary/t"] and recs[0]["narrative"] == "As text."


def test_a_file_too_large_is_remembered(client, pid, source, monkeypatch):
    monkeypatch.setattr(record_files, "MAX_FILE", 5)
    res = {"resourceType": "DocumentReference", "id": "n6", "status": "current", "type": {"text": "Scan"}, "date": "2025-02-01",
           "content": [{"attachment": {"contentType": "application/pdf", "url": "Binary/6", "title": "big.pdf"}}]}
    _, recs = fn.normalize_resources([res])
    _fetch(pid, source, recs, FakeClient({"Binary/6": ("application/pdf", b"0123456789")}))
    assert record_files.for_record(source["id"], "DocumentReference", "n6")[0]["state"] == record_files.TOO_LARGE
    _, again = fn.normalize_resources([res])
    fake2 = FakeClient({})
    _fetch(pid, source, again, fake2)
    assert not fake2.calls


def test_an_attachment_on_another_server_never_gets_the_token():
    client = smart.FhirClient("https://fhir.hospital.example/r4", {"access_token": "t"}, "p", False)
    assert asyncio.run(client.read_binary_file(None, "https://elsewhere.example/Binary/1", 1000)) is None
