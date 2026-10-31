"""What a review of correcting records, keeping documents and reading health summaries found."""

from __future__ import annotations

import asyncio
import base64
import io
import time
import zipfile
from pathlib import Path

import pytest

from app.connectors import fhir_normalize as fn
from app.connectors import smart
from app.services import ccda, imports, sync
from app.store import connections, profiles, record_files, records

STATIC = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "views"
NS = 'xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
FONT = {"/Type": "/Font", "/Subtype": "/Type1", "/BaseFont": "/Helvetica"}


def _pdf(text="hello world", stream=None):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    content = DecodedStreamObject()
    content.set_data(stream or f"BT /F1 12 Tf 10 100 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(content)
    font = DictionaryObject({NameObject(k): NameObject(v) for k, v in FONT.items()})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture()
def pid(client):
    return profiles.list_profiles()[0]["id"]


def _note(data, title="scan.pdf", rid="n1"):
    return {"resourceType": "DocumentReference", "id": rid, "status": "current", "type": {"text": "Scan"}, "date": "2025-01-02",
            "content": [{"attachment": {"contentType": "application/pdf", "title": title, "data": base64.b64encode(data).decode()}}]}


def _rows(pid, category):
    return records.list_records(pid, category)["items"]


def _doc(*sections):
    body = "".join(f"<component>{s}</component>" for s in sections)
    return f"""<?xml version="1.0"?><ClinicalDocument {NS}><title>CCD</title><effectiveTime value="20250301"/>
    <recordTarget><patientRole><id root="1.2.3" extension="MRN1"/><patient><name><given>Sam</given><family>Patel</family></name>
    <birthTime value="19700101"/></patient></patientRole></recordTarget>
    <component><structuredBody>{body}</structuredBody></component></ClinicalDocument>""".encode()


def _allergies(substance, negated=False, value="419199007"):
    return f"""<section><templateId root="2.16.840.1.113883.10.20.22.2.6.1"/><code code="48765-2"/><title>Allergies</title><text>From the source</text>
    <entry><act classCode="ACT" moodCode="EVN"><statusCode code="active"/><entryRelationship typeCode="SUBJ">
    <observation classCode="OBS" moodCode="EVN"{' negationInd="true"' if negated else ''}><id root="1" extension="x"/><value xsi:type="CD" code="{value}"/>
    <participant typeCode="CSM"><participantRole><playingEntity>{substance}</playingEntity></participantRole></participant>
    </observation></entryRelationship></act></entry></section>"""


def _words(text):
    return f'<code nullFlavor="OTH"><originalText>{text}</originalText></code>'


def _results(*observations):
    return f"""<section><templateId root="2.16.840.1.113883.10.20.22.2.3.1"/><code code="30954-2"/><title>Results</title><text>t</text>
    <entry><organizer classCode="BATTERY" moodCode="EVN"><id root="9.9" extension="P1"/><code code="24323-8" codeSystem="2.16.840.1.113883.6.1" displayName="CMP"/>
    <statusCode code="completed"/><effectiveTime value="20250201"/>{''.join(observations)}</organizer></entry></section>"""


def _result(extension, code, name, value):
    return f"""<component><observation classCode="OBS" moodCode="EVN"><id root="9.9" extension="{extension}"/>
    <code code="{code}" codeSystem="2.16.840.1.113883.6.1" displayName="{name}"/><statusCode code="completed"/><effectiveTime value="20250201"/>
    <value xsi:type="PQ" value="{value}" unit="mg/dL"/></observation></component>"""


# Allergy statements: a false "no known allergies" is the one thing this must never say.

@pytest.mark.parametrize("words, note", [("No known food allergies", "No known food allergies."), ("No known drug allergies", "No known drug allergies."),
                                         ("NKDA", "No known drug allergies."), ("No known allergies", "No known allergies."), ("NKA", "No known allergies.")])
def test_a_statement_about_one_kind_of_allergy_stays_about_that_kind(client, pid, words, note):
    imports.import_fhir_file(pid, "doc.xml", _doc(_allergies(_words(words))))
    summary = records.summary(pid)
    assert summary["allergy_note"] == note
    assert not summary["allergies"]


@pytest.mark.parametrize("value, note", [("416098002", "No known drug allergies."), ("414285001", "No known food allergies."), ("419199007", "No known allergies.")])
def test_a_negated_entry_with_no_substance_says_only_what_its_type_covers(client, pid, value, note):
    imports.import_fhir_file(pid, "doc.xml", _doc(_allergies('<code nullFlavor="NA"/>', negated=True, value=value)))
    assert records.summary(pid)["allergy_note"] == note


@pytest.mark.parametrize("substance", [_words("Penicillin"), _words("No known latex allergy")])
def test_ruling_out_one_substance_isnt_no_known_allergies(client, pid, substance):
    negated = "Penicillin" in substance
    out = imports.import_fhir_file(pid, "doc.xml", _doc(_allergies(substance, negated=negated)))
    assert not records.summary(pid)["allergy_note"]
    assert not _rows(pid, "allergies")
    assert any("couldn't be read in detail" in w for w in out["warnings"])
    assert any("From the source" in (n.get("narrative") or "") for n in _rows(pid, "notes"))


def test_a_portal_saying_no_known_drug_allergies_isnt_shown_as_no_allergies(client, pid):
    sync.import_fhir_resources(pid, "File", [{"resourceType": "AllergyIntolerance", "id": "a1", "clinicalStatus": {"coding": [{"code": "active"}]},
                                              "code": {"coding": [{"system": "http://snomed.info/sct", "code": "409137002", "display": "No known drug allergy"}]}}])
    assert records.summary(pid)["allergy_note"] == "No known drug allergy."


# C-CDA ids

def test_results_sharing_an_id_are_all_kept_and_stay_one_when_downloaded_again(client, pid):
    raw = _doc(_results(_result("ORD1", "2345-7", "Glucose", 150), _result("ORD1", "2160-0", "Creatinine", 1.1)))
    imports.import_fhir_file(pid, "doc.xml", raw)
    labs = _rows(pid, "labs")
    assert {r["title"] for r in labs} == {"Glucose", "Creatinine"}
    first, second = ccda.read_document(raw, "doc.xml").resources, ccda.read_document(raw, "doc.xml").resources
    assert {r["id"] for r in first} == {r["id"] for r in second}
    panel = next(r for r in first if r["resourceType"] == "DiagnosticReport")
    observations = {f"Observation/{r['id']}" for r in first if r["resourceType"] == "Observation"}
    assert {ref["reference"] for ref in panel["result"]} == observations and len(observations) == 2


def test_the_same_test_twice_under_one_id_keeps_both_values(client, pid):
    imports.import_fhir_file(pid, "doc.xml", _doc(_results(_result("ORD1", "2345-7", "Glucose", 150), _result("ORD1", "2345-7", "Glucose", 90))))
    assert sorted(r["value_num"] for r in _rows(pid, "labs")) == [90, 150]


def test_the_same_entry_twice_stays_one(client, pid):
    one = _result("ORD1", "2345-7", "Glucose", 150)
    imports.import_fhir_file(pid, "doc.xml", _doc(_results(one, one)))
    assert len(_rows(pid, "labs")) == 1


# XDM zips

@pytest.mark.parametrize("member, warned", [("<Slot><Value>ClinicalDocument</Value></Slot>", False), ("<ClinicalDocument><broken", True)])
def test_a_stray_or_damaged_member_doesnt_cost_the_rest_of_a_zip(client, pid, member, warned):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("IHE_XDM/S/DOC1.XML", _doc(_results(_result("a", "2345-7", "Glucose", 150))))
        archive.writestr("IHE_XDM/S/OTHER.XML", member)
    out = imports.import_fhir_file(pid, "x.zip", buffer.getvalue())
    assert _rows(pid, "labs")
    assert any("OTHER.XML" in w for w in out["warnings"]) == warned


def test_a_zip_with_nothing_readable_says_so(client, pid):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("DOC1.XML", "<ClinicalDocument><broken")
    with pytest.raises(ValueError, match="couldn't be read"):
        imports.import_fhir_file(pid, "x.zip", buffer.getvalue())


# Documents

class _Source:
    simulated = True

    def __init__(self, ctype, data):
        self.ctype, self.data = ctype, data

    def time_left(self):
        return 100

    async def read_binary_file(self, http, url, max_bytes, accept="x"):
        return self.ctype, self.data

    async def read_binary_text(self, http, url, max_bytes=512000):
        return self.ctype, self.data


def _linked(pid, ctype, data, declared="application/pdf"):
    res = {"resourceType": "DocumentReference", "id": "n9", "status": "current", "type": {"text": "Scan"}, "date": "2025-01-02",
           "content": [{"attachment": {"contentType": declared, "title": "x.pdf", "url": "Binary/1"}}]}
    _, recs = fn.normalize_resources([res])
    sync.import_fhir_resources(pid, "File", [res])
    conn = connections.list_for_profile(pid, "import")[0]
    done = asyncio.run(sync._fetch_note_bodies(conn, _Source(ctype, data), recs, True))
    return recs[0], record_files.for_record(conn["id"], "DocumentReference", "n9"), done


@pytest.mark.parametrize("served", ["application/octet-stream", "binary/octet-stream", "", "text/plain"])
def test_a_linked_pdf_served_with_a_generic_type_is_kept_as_a_document(client, pid, served):
    rec, files, done = _linked(pid, served, _pdf("REALWORDS"))
    assert "REALWORDS" in rec["narrative"] and not rec["narrative"].startswith("%PDF")
    assert [f["content_type"] for f in files] == ["application/pdf"] and done["fetched"] == 1


def test_a_pdf_with_no_declared_type_is_still_a_pdf(client, pid):
    rec, files, _ = _linked(pid, "application/octet-stream", _pdf("REALWORDS"), declared=None)
    assert "REALWORDS" in rec["narrative"] and files


def test_a_declared_image_served_as_generic_bytes_stays_an_image(client, pid):
    rec, files, _ = _linked(pid, "application/octet-stream", b"\x89PNG\r\n\x1a\n...", declared="image/png")
    assert [f["content_type"] for f in files] == ["image/png"] and not rec.get("narrative")


def test_text_is_still_text_when_a_pdf_was_declared(client, pid):
    rec, files, _ = _linked(pid, "text/plain", b"Plain words")
    assert rec["narrative"] == "Plain words" and not files


@pytest.mark.parametrize("title", ["血液検査.pdf", "Анализ.pdf", "résumé.pdf", "normal name.pdf"])
def test_a_document_title_in_any_script_opens_and_downloads(client, pid, title):
    sync.import_fhir_resources(pid, "File", [_note(_pdf(), title)])
    rid = _rows(pid, "notes")[0]["id"]
    file_id = client.get(f"/api/records/{rid}").json()["files"][0]["id"]
    for query in ("", "?download=true"):
        resp = client.get(f"/api/records/{rid}/files/{file_id}{query}")
        assert resp.status_code == 200
        assert 'filename="' in resp.headers["content-disposition"] and "filename*=UTF-8''" in resp.headers["content-disposition"]


def test_a_corrected_inline_pdf_replaces_the_earlier_one(client, pid):
    sync.import_fhir_resources(pid, "File", [_note(_pdf("first version"))])
    conn = connections.list_for_profile(pid, "import")[0]
    sync.import_fhir_resources(pid, "File", [_note(_pdf("second version"))], reuse=conn)
    full = client.get(f"/api/records/{_rows(pid, 'notes')[0]['id']}").json()
    assert "second" in full["narrative"]
    assert len(full["files"]) == 1


def test_two_inline_pdfs_on_one_note_both_stay(client, pid):
    res = _note(_pdf("one"))
    res["content"].append({"attachment": {"contentType": "application/pdf", "title": "two.pdf", "data": base64.b64encode(_pdf("two")).decode()}})
    sync.import_fhir_resources(pid, "File", [res])
    conn = connections.list_for_profile(pid, "import")[0]
    sync.import_fhir_resources(pid, "File", [res], reuse=conn)
    assert len(client.get(f"/api/records/{_rows(pid, 'notes')[0]['id']}").json()["files"]) == 2


def test_an_inline_file_over_the_limit_is_noted_once(client, pid, monkeypatch):
    monkeypatch.setattr(record_files, "MAX_FILE", 10)
    sync.import_fhir_resources(pid, "File", [_note(_pdf())])
    conn = connections.list_for_profile(pid, "import")[0]
    for _ in range(2):
        sync.import_fhir_resources(pid, "File", [_note(_pdf())], reuse=conn)
    files = client.get(f"/api/records/{_rows(pid, 'notes')[0]['id']}").json()["files"]
    assert [f["state"] for f in files] == ["too_large"]


# Time limits on reading

def _heavy_page():
    return _pdf(stream=b"BT /F1 12 Tf 10 100 Td " + b"(abc) Tj " * 1_000_000 + b"ET")


def test_a_single_heavy_page_cant_outlast_the_time_limit():
    data = _heavy_page()
    started = time.monotonic()
    text, state = record_files.pdf_text(data, seconds=2.0)
    assert time.monotonic() - started < 5
    assert (text, state) == ("", record_files.UNREADABLE)


def test_time_runs_out_inside_a_page_too(monkeypatch):
    monkeypatch.setattr(record_files, "STREAM_PER_SECOND", 100_000_000)
    data = _pdf(stream=b"BT /F1 12 Tf 10 100 Td " + b"(abc) Tj " * 400_000 + b"ET")
    assert record_files.pdf_text(data, seconds=0.05)[1] == record_files.UNREADABLE     # only stopping midway can leave no text


def test_the_data_fix_leaves_a_heavy_pdf_to_read_later_and_a_sync_reads_it(client, pid, monkeypatch):
    from app.core.db import db, read

    data = _pdf("LATERWORDS")
    sync.import_fhir_resources(pid, "File", [_note(data)])
    conn = connections.list_for_profile(pid, "import")[0]
    with db() as c:
        c.execute("DELETE FROM record_files")
        c.execute("UPDATE clinical_records SET narrative = NULL")
    with monkeypatch.context() as patched:
        patched.setattr(record_files, "FIX_READ_SECONDS", 0.0)
        with db() as c:
            assert record_files._keep_inline_files(c, None, 50) is not None
        with read() as c:
            assert c.execute("SELECT state FROM record_files").fetchone()["state"] == record_files.READ_LATER
    sync.import_fhir_resources(pid, "File", [_note(data)], reuse=conn)
    with read() as c:
        row = c.execute("SELECT state, text FROM record_files").fetchone()
    assert row["state"] == record_files.HAS_TEXT and "LATERWORDS" in row["text"]
    assert "LATERWORDS" in _rows(pid, "notes")[0]["narrative"]


# What a person enters

@pytest.mark.parametrize("kind", ["medication", "supplement", "allergy"])
def test_a_name_of_only_spaces_is_refused(client, pid, kind):
    resp = client.post("/api/records/self-reported", json={"profile_id": pid, "kind": kind, "name": "   "})
    assert resp.status_code == 400 and resp.json()["detail"] == "Enter a name."
    summary = records.summary(pid)
    assert not summary["active_medications"] and not summary["allergies"]


# What the page does

def test_the_visit_summary_shows_the_persons_words_on_every_table_of_entries():
    source = (STATIC / "report.js").read_text()
    for rows in ("r.unconfirmed_conditions.map((c)", "r.conditions_status_unknown.map((c)", "r.visit_diagnoses.slice(0, 10).map((c)", "r.medications_to_check.map((m)",
                 "r.medications_planned.map((m)", "r.latest_vitals.map((v)", "byName.values()].map((doses)", "const visitRow = (e)", "const flaggedRow = (l)",
                 "r.procedures.map((p)", "r.procedures_unfinished.map((p)", "r.imaging.map((p)", "r.coverage.map((c)", "r.allergies.map((a)", "r.active_conditions.map((c)"):
        line = next(ln for ln in source.splitlines() if rows in ln)
        assert "saidOf" in line, rows
    assert "${resultStatus(l.status)}${saidOf(l)}" in source and "r.recent_encounters.map(visitRow)" in source and "r.upcoming_encounters.map(visitRow)" in source


def test_a_document_isnt_opened_in_a_new_window_inside_the_apps():
    source = (STATIC / "record-detail.js").read_text()
    assert 'hasAttribute("data-shell")' in source and 'hasAttribute("data-embedded")' in source
    assert "canShow = !inApp &&" in source
    assert "Open it to read it" not in source.split("READ_IT = inApp ? ")[1].split(":")[0]


def test_a_json_binary_with_no_type_doesnt_claim_to_be_text(monkeypatch):
    fhir = smart.FhirClient("https://fhir.hospital.example/r4", {"access_token": "t"}, "p", False)

    class Answer:
        status_code = 200
        headers = {"content-type": "application/fhir+json"}
        content = b"{}"

        def json(self):
            return {"resourceType": "Binary", "data": base64.b64encode(b"%PDF-1.3").decode()}

    async def answer(*_, **__):
        return Answer()

    monkeypatch.setattr(fhir, "_get", answer)
    served, data = asyncio.run(fhir.read_binary_file(None, "Binary/1", 1000))
    assert served == ""
    assert record_files.served_type("application/pdf", served, data) == "application/pdf"
    assert record_files.served_type("image/png", served, b"\x89PNG") == "image/png"
