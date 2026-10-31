"""Health summaries from patient portals: C-CDA and CCD files and XDM zips, read into the same records as a live connection.
Every document here is made up."""

from __future__ import annotations

import base64
import io
import json
import zipfile

import pytest

from app.connectors import fhir_normalize as fn
from app.services import ccda
from app.store import connections, profiles, record_files, records

HEADER = """<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:sdtc="urn:hl7-org:sdtc">
  <templateId root="2.16.840.1.113883.10.20.22.1.2"/>
  <id root="1.2.3" extension="doc-1"/>
  <code code="34133-9" codeSystem="2.16.840.1.113883.6.1" displayName="Summary of episode note"/>
  <title>Continuity of Care Document</title>
  <effectiveTime value="20250301101500-0500"/>
  <recordTarget><patientRole>
    <id root="2.16.840.1.113883.4.1" extension="000-00-0000"/>
    <id root="1.2.3.4" extension="MRN-77"/>
    <patient><name><given>Testa</given><family>Example</family></name>
      <administrativeGenderCode code="F" codeSystem="2.16.840.1.113883.5.1"/><birthTime value="19700203"/></patient>
  </patientRole></recordTarget>
  <custodian><assignedCustodian><representedCustodianOrganization><name>Example Clinic</name></representedCustodianOrganization></assignedCustodian></custodian>
"""

SECTIONS = """
<component><structuredBody>
<component><section><templateId root="2.16.840.1.113883.10.20.22.2.6.1"/><code code="48765-2" codeSystem="2.16.840.1.113883.6.1"/><title>Allergies</title>
  <text><content ID="r1">Hives</content></text>
  <entry><act classCode="ACT" moodCode="EVN"><templateId root="2.16.840.1.113883.10.20.22.4.30"/><statusCode code="active"/>
    <entryRelationship typeCode="SUBJ"><observation classCode="OBS" moodCode="EVN"><templateId root="2.16.840.1.113883.10.20.22.4.7"/><id root="a1"/>
      <effectiveTime><low value="20100101"/></effectiveTime>
      <value xsi:type="CD" code="416098002" codeSystem="2.16.840.1.113883.6.96" displayName="Drug allergy"/>
      <participant typeCode="CSM"><participantRole><playingEntity><code code="7980" codeSystem="2.16.840.1.113883.6.88" displayName="Penicillin G"/></playingEntity></participantRole></participant>
      <entryRelationship typeCode="MFST" inversionInd="true"><observation classCode="OBS" moodCode="EVN"><templateId root="2.16.840.1.113883.10.20.22.4.9"/>
        <value xsi:type="CD" code="247472004" codeSystem="2.16.840.1.113883.6.96"><originalText><reference value="#r1"/></originalText></value>
        <entryRelationship typeCode="SUBJ"><observation><value xsi:type="CD" code="24484000" codeSystem="2.16.840.1.113883.6.96"/></observation></entryRelationship>
      </observation></entryRelationship>
    </observation></entryRelationship></act></entry>
</section></component>
<component><section><code code="10160-0" codeSystem="2.16.840.1.113883.6.1"/><title>Medications</title>
  <text><content ID="sig1">Take one tablet daily</content></text>
  <entry><substanceAdministration classCode="SBADM" moodCode="INT"><id root="m1"/><text><reference value="#sig1"/></text><statusCode code="active"/>
    <effectiveTime xsi:type="IVL_TS"><low value="20240110"/><high nullFlavor="NI"/></effectiveTime>
    <effectiveTime xsi:type="PIVL_TS" operator="A"><period value="24" unit="h"/></effectiveTime>
    <routeCode code="C38288" codeSystem="2.16.840.1.113883.3.26.1.1" displayName="Oral"/>
    <doseQuantity value="1"/>
    <consumable><manufacturedProduct><manufacturedMaterial><code code="197361" codeSystem="2.16.840.1.113883.6.88" displayName="Amlodipine 5 MG Oral Tablet"/></manufacturedMaterial></manufacturedProduct></consumable>
    <author><assignedAuthor><assignedPerson><name><given>Pat</given><family>Doctor</family></name></assignedPerson></assignedAuthor></author>
  </substanceAdministration></entry>
  <entry><substanceAdministration classCode="SBADM" moodCode="EVN"><id root="m2"/><statusCode code="aborted"/>
    <effectiveTime xsi:type="IVL_TS"><low value="20200101"/><high value="20210101"/></effectiveTime>
    <consumable><manufacturedProduct><manufacturedMaterial><name>Old Drug</name></manufacturedMaterial></manufacturedProduct></consumable>
  </substanceAdministration></entry>
</section></component>
<component><section><code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/><title>Problems</title>
  <text>Problem table</text>
  <entry><act classCode="ACT" moodCode="EVN"><statusCode code="completed"/>
    <entryRelationship typeCode="SUBJ"><observation classCode="OBS" moodCode="EVN"><id root="p1"/><effectiveTime><low value="20190305"/><high value="20200101"/></effectiveTime>
      <value xsi:type="CD" code="195967001" codeSystem="2.16.840.1.113883.6.96" displayName="Asthma"><translation code="J45.909" codeSystem="2.16.840.1.113883.6.90"/></value>
    </observation></entryRelationship></act></entry>
  <entry><act classCode="ACT" moodCode="EVN"><statusCode code="active"/>
    <entryRelationship typeCode="SUBJ"><observation classCode="OBS" moodCode="EVN" negationInd="true"><id root="p2"/>
      <value xsi:type="CD" code="55822004" codeSystem="2.16.840.1.113883.6.96" displayName="High cholesterol"/>
    </observation></entryRelationship></act></entry>
</section></component>
<component><section><code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/><title>Results</title><text>Results table</text>
  <entry><organizer classCode="BATTERY" moodCode="EVN"><id root="o1"/><code code="24323-8" codeSystem="2.16.840.1.113883.6.1" displayName="Comprehensive metabolic panel"/><statusCode code="completed"/>
    <effectiveTime value="20250210080000"/>
    <component><observation classCode="OBS" moodCode="EVN"><id root="l1"/><code code="2345-7" codeSystem="2.16.840.1.113883.6.1" displayName="Glucose"/><statusCode code="completed"/>
      <value xsi:type="PQ" value="142" unit="mg/dL"/><interpretationCode code="H" codeSystem="2.16.840.1.113883.5.83"/>
      <referenceRange><observationRange><value xsi:type="IVL_PQ"><low value="70" unit="mg/dL"/><high value="99" unit="mg/dL"/></value></observationRange></referenceRange>
    </observation></component>
    <component><observation classCode="OBS" moodCode="EVN"><id root="l2"/><code code="5778-6" codeSystem="2.16.840.1.113883.6.1" displayName="Urine color"/><statusCode code="completed"/>
      <value xsi:type="ST">Yellow</value></observation></component>
  </organizer></entry>
</section></component>
<component><section><code code="8716-3" codeSystem="2.16.840.1.113883.6.1"/><title>Vitals</title><text>Vitals table</text>
  <entry><organizer classCode="CLUSTER" moodCode="EVN"><id root="v0"/><effectiveTime value="20250210"/>
    <component><observation><id root="v1"/><code code="8480-6" codeSystem="2.16.840.1.113883.6.1" displayName="Systolic"/><statusCode code="completed"/><effectiveTime value="20250210"/><value xsi:type="PQ" value="128" unit="mm[Hg]"/></observation></component>
    <component><observation><id root="v2"/><code code="8462-4" codeSystem="2.16.840.1.113883.6.1" displayName="Diastolic"/><statusCode code="completed"/><effectiveTime value="20250210"/><value xsi:type="PQ" value="82" unit="mm[Hg]"/></observation></component>
    <component><observation><id root="v3"/><code code="29463-7" codeSystem="2.16.840.1.113883.6.1" displayName="Body weight"/><statusCode code="completed"/><effectiveTime value="20250210"/><value xsi:type="PQ" value="70" unit="kg"/></observation></component>
  </organizer></entry>
</section></component>
<component><section><code code="11369-6" codeSystem="2.16.840.1.113883.6.1"/><title>Immunizations</title><text>Shots</text>
  <entry><substanceAdministration classCode="SBADM" moodCode="EVN" negationInd="false"><id root="i1"/><statusCode code="completed"/><effectiveTime value="20241001"/>
    <consumable><manufacturedProduct><manufacturedMaterial><code code="140" codeSystem="2.16.840.1.113883.12.292" displayName="Influenza, seasonal"/><lotNumberText>LOT9</lotNumberText></manufacturedMaterial>
    <manufacturerOrganization><name>Maker Inc</name></manufacturerOrganization></manufacturedProduct></consumable></substanceAdministration></entry>
</section></component>
<component><section><code code="47519-4" codeSystem="2.16.840.1.113883.6.1"/><title>Procedures</title><text>Procs</text>
  <entry><procedure classCode="PROC" moodCode="EVN"><id root="pr1"/><code code="80146002" codeSystem="2.16.840.1.113883.6.96" displayName="Appendectomy"/><statusCode code="completed"/><effectiveTime value="20150607"/></procedure></entry>
</section></component>
<component><section><code code="46240-8" codeSystem="2.16.840.1.113883.6.1"/><title>Encounters</title><text>Visits</text>
  <entry><encounter classCode="ENC" moodCode="EVN"><id root="e1"/><code code="99213" codeSystem="2.16.840.1.113883.6.12" displayName="Office visit"><translation code="AMB" codeSystem="2.16.840.1.113883.5.4"/></code>
    <effectiveTime><low value="20250210083000"/><high value="20250210090000"/></effectiveTime>
    <participant typeCode="LOC"><participantRole><playingEntity><name>Main Street Clinic</name></playingEntity></participantRole></participant></encounter></entry>
</section></component>
<component><section><code code="29762-2" codeSystem="2.16.840.1.113883.6.1"/><title>Social History</title>
  <text><table><tr><td>Smoking status</td><td>Never</td></tr></table></text></section></component>
</structuredBody></component>
</ClinicalDocument>"""

CCD = (HEADER + SECTIONS).encode()


@pytest.fixture()
def pid(client):
    return profiles.list_profiles()[0]["id"]


def _summary_records(raw=CCD, name="ccd.xml"):
    summary = ccda.read_upload(raw, name)
    return summary, fn.normalize_report(summary.resources)


def by_category(recs, category):
    return [r for r in recs if r["category"] == category]


def test_every_section_reads_into_the_same_records_as_a_live_connection():
    summary, (patient, recs, problems) = _summary_records()
    assert not problems["failed"]
    assert patient["name"] == "Testa Example" and patient["birth_date"] == "1970-02-03"
    allergy = by_category(recs, "allergies")[0]
    assert allergy["title"] == "Penicillin G" and allergy["status"] == "active" and allergy["details"]["reactions"][0] == {"manifestation": "Hives", "severity": "severe"}
    meds = {m["title"]: m for m in by_category(recs, "medications")}
    assert meds["Amlodipine 5 MG Oral Tablet"]["status"] == "active" and meds["Amlodipine 5 MG Oral Tablet"]["code"] == "197361"
    assert "Take one tablet daily" in meds["Amlodipine 5 MG Oral Tablet"]["details"]["dosage"]
    assert meds["Amlodipine 5 MG Oral Tablet"]["details"]["prescriber"] == "Pat Doctor"
    assert meds["Old Drug"]["status"] == "stopped"
    assert [c["title"] for c in by_category(recs, "conditions")] == ["Asthma"]
    asthma = by_category(recs, "conditions")[0]
    assert asthma["status"] == "resolved" and asthma["effective_at"] == "2019-03-05" and asthma["details"]["icd10"] == "J45.909"
    glucose = next(r for r in by_category(recs, "labs") if r["code"] == "2345-7")
    assert glucose["value_num"] == 142 and glucose["unit"] == "mg/dL" and glucose["interpretation"] == "high" and (glucose["ref_low"], glucose["ref_high"]) == (70, 99)
    assert glucose["details"]["panel"] == "Comprehensive metabolic panel"
    assert next(r for r in by_category(recs, "labs") if r["code"] == "5778-6")["value_text"] == "Yellow"
    assert by_category(recs, "reports")[0]["title"] == "Comprehensive metabolic panel"
    pressure = next(r for r in by_category(recs, "vitals") if r["code"] == "85354-9")
    assert pressure["value_text"].startswith("128/82")
    assert any(r["code"] == "29463-7" for r in by_category(recs, "vitals"))
    assert not any(r["code"] in ("8480-6", "8462-4") for r in by_category(recs, "vitals"))
    shot = by_category(recs, "immunizations")[0]
    assert shot["code"] == "140" and shot["details"]["lot"] == "LOT9" and shot["details"]["manufacturer"] == "Maker Inc"
    assert by_category(recs, "procedures")[0]["title"] == "Appendectomy"
    visit = by_category(recs, "encounters")[0]
    assert visit["title"] == "Office visit" and visit["details"]["locations"] == ["Main Street Clinic"] and visit["details"]["class_code"] == "AMB"


def test_what_cant_be_mapped_stays_readable():
    summary, (_, recs, _) = _summary_records()
    notes = {n["title"]: n for n in by_category(recs, "notes")}
    assert "Never" in notes["Social History"]["narrative"] and "Smoking status" in notes["Social History"]["narrative"]
    assert "Problem table" in notes["Problems"]["narrative"], "a negated problem isn't a problem, and isn't lost either"
    assert any("couldn't be read in detail" in w for w in summary.warnings)


def test_a_social_security_number_is_not_kept_as_an_identifier():
    summary = ccda.read_upload(CCD, "ccd.xml")
    patient = next(r for r in summary.resources if r["resourceType"] == "Patient")
    assert json.dumps(patient).count("000-00-0000") == 0


@pytest.mark.parametrize("value, expected", [
    ("2025", "2025"), ("202503", "2025-03"), ("20250301", "2025-03-01"), ("202503011015", "2025-03-01T10:15:00"),
    ("20250301101500-0500", "2025-03-01T10:15:00-05:00"), ("20251301", None), ("not a date", None), ("", None), (None, None),
])
def test_dates_are_only_as_precise_as_the_source(value, expected):
    assert ccda._time(value) == expected


@pytest.mark.parametrize("doc", [
    b'<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]><ClinicalDocument xmlns="urn:hl7-org:v3">&lol2;</ClinicalDocument>',
    b'<!DOCTYPE x SYSTEM "http://example.invalid/x.dtd"><ClinicalDocument xmlns="urn:hl7-org:v3"/>',
])
def test_a_document_type_and_entities_are_refused(doc):
    with pytest.raises(ValueError, match="aren't allowed"):
        ccda.read_upload(doc, "x.xml")


def test_too_deep_is_refused_and_so_is_other_xml():
    with pytest.raises(ValueError, match="too large"):
        ccda.read_upload(b'<ClinicalDocument xmlns="urn:hl7-org:v3">' + b"<a>" * 200 + b"</a>" * 200 + b"</ClinicalDocument>", "x.xml")
    with pytest.raises(ValueError, match="isn't a health summary"):
        ccda.read_upload(b"<HealthData/>", "export.xml")
    with pytest.raises(ValueError, match="couldn't be read"):
        ccda.read_upload(b'<ClinicalDocument xmlns="urn:hl7-org:v3"><oops', "x.xml")


def _zip(**members: bytes) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in members.items():
            z.writestr(name, body)
    return out.getvalue()


def test_a_zip_bomb_is_refused_before_it_is_unpacked():
    with pytest.raises(ValueError, match="too large"):
        ccda.read_upload(_zip(**{"bomb.xml": b"\0" * (70 * 1024 * 1024)}), "x.zip")


def test_a_zip_with_no_summary_says_so():
    with pytest.raises(ValueError, match="No health summary"):
        ccda.read_upload(_zip(**{"export.xml": b"<HealthData/>", "readme.txt": b"hi"}), "x.zip")


def test_portal_files_import_through_the_route_and_a_second_copy_changes_nothing(client, pid):
    res = client.post("/api/imports/fhir", files={"file": ("ccd.xml", CCD, "text/xml")}, data={"profile_id": pid})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["inserted"] > 8 and any("couldn't be read in detail" in w for w in body["warnings"])
    again = client.post("/api/imports/fhir", files={"file": ("ccd.xml", CCD, "text/xml")}, data={"profile_id": pid}).json()
    assert again["inserted"] == 0 and again.get("already_imported")
    meds = records.list_records(pid, "medications")["items"]
    assert {m["title"] for m in meds} >= {"Amlodipine 5 MG Oral Tablet"}
    # Raw is what the mapping made, and the document itself is kept.
    found = client.get(f"/api/records?profile={pid}&category=notes&q=original").json()["items"]
    assert len(found) == 1
    full = records.get_record(found[0]["id"])
    files = record_files.for_record(full["connection_id"], full["resource_type"], full["resource_id"])
    assert files[0]["content_type"] == "application/xml" and files[0]["size"] == len(CCD)


def test_an_xdm_zip_imports_its_documents_and_leaves_its_metadata(client, pid):
    second = CCD.replace(b'root="p1"', b'root="p9"').replace(b"Asthma", b"Eczema").replace(b'code="195967001"', b'code="43116000"')
    raw = _zip(**{"IHE_XDM/SUBSET01/DOC0001.XML": CCD, "IHE_XDM/SUBSET01/DOC0002.XML": second,
                  "IHE_XDM/SUBSET01/METADATA.XML": b"<SubmitObjectsRequest/>", "INDEX.HTM": b"<html/>", "IHE_XDM/SUBSET01/scan.pdf": b"%PDF-"})
    res = client.post("/api/imports/fhir", files={"file": ("portal.zip", raw, "application/zip")}, data={"profile_id": pid})
    assert res.status_code == 200, res.text
    assert any("scan.pdf" in w for w in res.json()["warnings"])
    titles = {c["title"] for c in records.list_records(pid, "conditions")["items"]}
    assert titles == {"Asthma", "Eczema"}


def test_a_later_download_is_the_same_source_and_the_same_facts(client, pid):
    client.post("/api/imports/fhir", files={"file": ("ccd.xml", CCD, "text/xml")}, data={"profile_id": pid})
    later = CCD.replace(b'value="20250301101500-0500"', b'value="20250401101500-0500"')
    res = client.post("/api/imports/fhir", files={"file": ("other-name.xml", later, "text/xml")}, data={"profile_id": pid}).json()
    assert res.get("matched_earlier")
    assert len(records.list_records(pid, "conditions")["items"]) == 1
    assert len(records.list_records(pid, "labs")["items"]) == 2


def test_a_summary_naming_someone_else_does_not_mix_into_their_source(client, pid):
    client.post("/api/imports/fhir", files={"file": ("ccd.xml", CCD, "text/xml")}, data={"profile_id": pid})
    other = CCD.replace(b"MRN-77", b"MRN-88").replace(b"<given>Testa</given>", b"<given>Other</given>").replace(b'value="19700203"', b'value="19800101"')
    res = client.post("/api/imports/fhir", files={"file": ("ccd2.xml", other, "text/xml")}, data={"profile_id": pid}).json()
    assert not res.get("matched_earlier")
    assert len({c["id"] for c in connections.list_for_profile(pid, "import")}) == 2


def test_a_document_that_is_only_a_pdf_keeps_it(client, pid):
    pdf = base64.b64encode(b"%PDF-1.4 not really").decode()
    doc = HEADER + f'<component><nonXMLBody><text mediaType="application/pdf" representation="B64">{pdf}</text></nonXMLBody></component></ClinicalDocument>'
    res = client.post("/api/imports/fhir", files={"file": ("scan.xml", doc.encode(), "text/xml")}, data={"profile_id": pid})
    assert res.status_code == 200, res.text
    notes = records.list_records(pid, "notes")["items"]
    assert len(notes) == 2


def test_a_zip_with_two_people_in_it_is_refused(client, pid):
    other = CCD.replace(b"MRN-77", b"MRN-88").replace(b"<given>Testa</given>", b"<given>Other</given>").replace(b'value="19700203"', b'value="19800101"')
    raw = _zip(**{"DOC0001.XML": CCD, "DOC0002.XML": other})
    res = client.post("/api/imports/fhir", files={"file": ("family.zip", raw, "application/zip")}, data={"profile_id": pid})
    assert res.status_code == 400 and "different people" in res.text
    assert not records.list_records(pid, "conditions")["items"]


def test_an_apple_health_export_in_the_wrong_box_says_where_it_goes():
    with pytest.raises(ValueError, match="Apple Health box"):
        ccda.read_upload(_zip(**{"apple_health_export/export.xml": b"<HealthData/>"}), "export.zip")

