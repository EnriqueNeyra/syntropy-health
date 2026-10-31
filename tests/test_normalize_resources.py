"""Everything besides lab values: names of medications, retractions, notes and their text, older and id-less files."""

import base64
import json

import httpx
import pytest

from app.connectors import fhir_normalize as fn
from app.connectors import smart
from app.services import imports, sync
from app.store import records

RXNORM = fn.RXNORM


def b64(text, encoding="utf-8"):
    return base64.b64encode(text.encode(encoding)).decode()


# ---- F005: medications by reference

def med_request(rid, **kw):
    return {"resourceType": "MedicationRequest", "id": rid, "status": "active", "intent": "order", **kw}


def test_contained_medications_stay_separate_and_named():
    a = med_request("a", medicationReference={"reference": "#m"}, contained=[{"resourceType": "Medication", "id": "m", "code": {"text": "Metformin 500 MG"}}])
    b = med_request("b", medicationReference={"reference": "#m"}, contained=[{"resourceType": "Medication", "id": "m", "code": {
        "coding": [{"system": RXNORM, "code": "197361", "display": "Amlodipine 5 MG"}]}}])
    _, recs = fn.normalize_resources([a, b])
    assert [r["title"] for r in recs] == ["Metformin 500 MG", "Amlodipine 5 MG"] and recs[1]["dedup_key"] == "med|197361"


def test_a_medication_statement_reads_contained_too():
    s = {"resourceType": "MedicationStatement", "id": "s", "status": "active", "medicationReference": {"reference": "#m"},
         "contained": [{"resourceType": "Medication", "id": "m", "code": {"text": "Aspirin"}}]}
    _, recs = fn.normalize_resources([s])
    assert recs[0]["title"] == "Aspirin" and recs[0]["raw"] == s


def test_unnamed_medications_never_merge():
    _, recs = fn.normalize_resources([med_request("a", medicationReference={"reference": "Medication/x"}),
                                      med_request("b", medicationReference={"reference": "Medication/y"})])
    assert all(r["dedup_key"] is None for r in recs) and all(r["title"] == fn.UNNAMED_MEDICATION for r in recs)


def test_a_reference_display_names_a_medication_whose_code_has_no_name():
    med = {"resourceType": "Medication", "id": "m", "code": {"coding": [{"system": "urn:x", "code": "9"}]}}
    _, recs = fn.normalize_resources([med, med_request("a", medicationReference={"reference": "Medication/m", "display": "Coumadin"})])
    assert recs[0]["title"] == "Coumadin"


def test_a_nameless_stopped_medication_doesnt_hide_active_ones(client):
    prof = client.get("/api/profiles").json()["profiles"][0]["id"]
    sync.import_fhir_resources(prof, "F", [med_request("a", medicationReference={"reference": "Medication/x"}, authoredOn="2026-10-01"),
                                           med_request("b", status="stopped", medicationReference={"reference": "Medication/y"}, authoredOn="2026-10-02")])
    assert len(records.summary(prof)["active_medications"]) == 1


@pytest.mark.parametrize("fallback, resource, key", [
    ("Condition", {"resourceType": "Condition", "id": "x", "clinicalStatus": "active"}, fn.normalize_condition),
    ("Allergy", {"resourceType": "AllergyIntolerance", "id": "x"}, fn.normalize_allergy),
    ("Vaccine", {"resourceType": "Immunization", "id": "x", "status": "completed"}, fn.normalize_immunization),
    ("Procedure", {"resourceType": "Procedure", "id": "x", "status": "completed"}, fn.normalize_procedure),
    ("Report", {"resourceType": "DiagnosticReport", "id": "x", "status": "final"}, fn.normalize_diagnostic_report),
    ("Goal", {"resourceType": "Goal", "id": "x"}, fn.normalize_goal),
    ("Device", {"resourceType": "Device", "id": "x"}, fn.normalize_device),
])
def test_a_generic_name_is_no_identity(fallback, resource, key):
    rec = key(resource)
    assert rec["title"] == fallback and rec["dedup_key"] is None


class FakeClient(smart.FhirClient):
    def __init__(self, scope=None):
        super().__init__("https://ehr.example/fhir", {"access_token": "t", "scope": scope}, "p")
        self.reads = []

    async def read(self, client, path):
        self.reads.append(path)
        return {"resourceType": "Medication", "id": path.split("/")[-1], "code": {"text": "Real name"}}


async def read_missing(client_obj, resources):
    out = smart.FetchResult(resources=resources)
    async with httpx.AsyncClient() as http:
        await client_obj._read_missing_medications(http, out)
    return out


@pytest.mark.anyio
async def test_a_server_that_ignores_include_has_its_medications_read():
    c = FakeClient()
    out = await read_missing(c, [med_request("a", medicationReference={"reference": "Medication/m1"}),
                                 med_request("b", medicationReference={"reference": "Medication/m1"}),
                                 med_request("c", medicationReference={"reference": "https://ehr.example/fhir/Medication/m2"})])
    assert c.reads == ["Medication/m1", "Medication/m2"]
    assert not out.status
    _, recs = fn.normalize_resources(out.resources)
    assert {r["title"] for r in recs} == {"Real name"}


@pytest.mark.anyio
async def test_the_token_never_goes_where_the_data_points():
    c = FakeClient()
    await read_missing(c, [med_request("a", medicationReference={"reference": "https://evil.example/Medication/m1"}),
                           med_request("b", medicationReference={"reference": "Medication/../../secret"}),
                           med_request("c", medicationReference={"reference": "Patient/1"}),
                           med_request("d", medicationReference={"reference": "#c"}),
                           med_request("e", medicationReference={"reference": "urn:uuid:1"})])
    assert c.reads == []


@pytest.mark.anyio
async def test_a_name_on_the_reference_or_an_ungranted_type_needs_no_read():
    c = FakeClient()
    await read_missing(c, [med_request("a", medicationReference={"reference": "Medication/m1", "display": "Named"})])
    assert c.reads == []
    c = FakeClient(scope="patient/MedicationRequest.read patient/Patient.read")
    await read_missing(c, [med_request("a", medicationReference={"reference": "Medication/m1"})])
    assert c.reads == []


@pytest.mark.anyio
async def test_reads_are_capped():
    c = FakeClient()
    await read_missing(c, [med_request(f"a{i}", medicationReference={"reference": f"Medication/m{i}"}) for i in range(smart.MAX_MEDICATION_READS + 50)])
    assert len(c.reads) == smart.MAX_MEDICATION_READS


@pytest.mark.anyio
async def test_a_failed_read_leaves_it_unnamed_and_changes_nothing_else():
    class Broken(FakeClient):
        async def read(self, client, path):
            raise httpx.ConnectError("no")
    c = Broken()
    out = await read_missing(c, [med_request("a", medicationReference={"reference": "Medication/m1"})])
    assert len(out.resources) == 1 and not out.status and not out.queries


# ---- F004: vaccines

def test_a_vaccine_not_given_says_why_and_never_merges_with_a_dose():
    code = {"coding": [{"system": fn.CVX, "code": "158"}], "text": "Flu"}
    given = fn.normalize_immunization({"resourceType": "Immunization", "id": "a", "status": "completed", "occurrenceDateTime": "2024-10-01", "vaccineCode": code})
    refused = fn.normalize_immunization({"resourceType": "Immunization", "id": "b", "status": "not-done", "occurrenceDateTime": "2024-10-01", "vaccineCode": code,
                                         "statusReason": {"text": "Patient refused"}})
    assert refused["details"]["status_reason"] == "Patient refused"
    assert given["dedup_key"] == "imm|158|2024-10-01" and refused["dedup_key"] == "imm|158|2024-10-01|not-done"


# ---- F031: retractions

@pytest.mark.parametrize("normalize, resource", [
    (fn.normalize_document_reference, {"resourceType": "DocumentReference", "id": "x", "status": "current", "docStatus": "entered-in-error", "type": {"text": "Note"}}),
    (fn.normalize_document_reference, {"resourceType": "DocumentReference", "id": "x", "status": "entered-in-error", "type": {"text": "Note"}}),
    (fn.normalize_goal, {"resourceType": "Goal", "id": "x", "lifecycleStatus": "entered-in-error", "description": {"text": "x"}}),
    (fn.normalize_care_team, {"resourceType": "CareTeam", "id": "x", "status": "entered-in-error", "participant": [{"member": {"display": "Dr"}}]}),
    (fn.normalize_device, {"resourceType": "Device", "id": "x", "status": "entered-in-error"}),
    (fn.normalize_coverage, {"resourceType": "Coverage", "id": "x", "status": "entered-in-error"}),
])
def test_what_the_source_retracted_is_dropped(normalize, resource):
    assert normalize(resource) is None
    assert fn.retracted(resource)


@pytest.mark.parametrize("normalize, resource", [
    (fn.normalize_goal, {"resourceType": "Goal", "id": "x", "lifecycleStatus": "cancelled", "description": {"text": "x"}}),
    (fn.normalize_coverage, {"resourceType": "Coverage", "id": "x", "status": "cancelled"}),
])
def test_cancelled_is_history_not_a_mistake(normalize, resource):
    assert normalize(resource)["status"] == "cancelled"
    assert not fn.retracted(resource)


def test_a_superseded_note_says_so():
    r = fn.normalize_document_reference({"resourceType": "DocumentReference", "id": "x", "status": "superseded", "docStatus": "final", "type": {"text": "N"}})
    assert r["status"] == "superseded"
    r = fn.normalize_document_reference({"resourceType": "DocumentReference", "id": "x", "status": "current", "docStatus": "amended", "type": {"text": "N"}})
    assert r["status"] == "amended"


# ---- F130: note text

def note(*attachments):
    return {"resourceType": "DocumentReference", "id": "x", "status": "current", "type": {"text": "N"}, "content": [{"attachment": a} for a in attachments]}


def test_table_cells_stay_apart_and_the_title_stays_out():
    html = "<html><head><title>Hidden title</title><style>p{}</style></head><body><h1>Report 123</h1><table><tr><th>Test</th><th>Value</th></tr>" \
           "<tr><td>Sodium</td><td>140</td><td>135-145</td></tr><tr><td>Potassium</td><td>4.1</td><td>3.5-5.0</td></tr></table></body></html>"
    text = fn.strip_html(html)
    assert "Hidden title" not in text
    assert "Sodium\t140\t135-145" in text and "Potassium\t4.1\t3.5-5.0" in text
    assert "Report 123\n" in text


def test_html_is_preferred_whatever_order_the_source_lists_formats_in():
    r = fn.normalize_document_reference(note({"contentType": "text/rtf", "data": b64(r"{\rtf1 RTF text}")},
                                             {"contentType": "text/plain", "data": b64("Plain text")},
                                             {"contentType": "text/html", "data": b64("<p>HTML text</p>")}))
    assert r["narrative"] == "HTML text"


def test_rtf_reads_as_text():
    rtf = r"{\rtf1\ansi\ansicpg1252{\fonttbl{\f0\fnil Arial;}}{\colortbl;\red0\green0\blue0;}{\*\generator Some;}\f0\pard Patient\'92s caf\'e9 visit\par Line two\tab end\par \u8212?dash}"
    text = fn.normalize_document_reference(note({"contentType": "text/rtf", "data": b64(rtf)}))["narrative"]
    assert text == "Patient’s café visit\nLine two\tend\n—dash"


def test_a_named_charset_is_honoured_and_an_unnamed_one_is_guessed():
    assert fn.decode_body("café".encode("latin-1"), "text/plain; charset=iso-8859-1") == "café"
    assert fn.decode_body("café".encode("latin-1"), "text/plain") == "café"
    assert fn.decode_body("café".encode("utf-8"), "text/plain") == "café"
    assert fn.decode_body("café".encode("utf-8"), "text/plain; charset=no-such") == "café"
    assert fn.decode_attachment({"contentType": "text/plain; charset=latin-1", "data": b64("café", "latin-1")}) == "café"


# ---- F041: amended notes through a sync

class Bodies:
    """Stands in for the FHIR client, serving Binary bodies."""
    def __init__(self, text, fail=False):
        self.text, self.fail, self.calls = text, fail, 0

    def time_left(self):
        return 60.0

    async def read_binary_text(self, http, url):
        self.calls += 1
        return None if self.fail else ("text/plain", self.text.encode())


def linked_note(rid="n", doc="final", url="Binary/1?sig=1", version="1", **kw):
    return {"resourceType": "DocumentReference", "id": rid, "status": "current", "docStatus": doc, "date": "2024-01-01", "type": {"text": "Note"},
            "meta": {"versionId": version}, "content": [{"attachment": {"contentType": "text/plain", "url": url}}], **kw}


async def sync_notes(conn, prof, bodies, *resources):
    import asyncio
    _, recs = fn.normalize_resources(list(resources))
    fetched = (await sync._fetch_note_bodies(conn, bodies, recs, True))["fetched"]
    await asyncio.to_thread(records.upsert_records, prof, conn["id"], "Source", recs)
    return fetched, {r["resource_id"]: r for r in recs}


@pytest.fixture
def source(client):
    from app.store import connections
    prof = client.get("/api/profiles").json()["profiles"][0]["id"]
    return prof, connections.create(prof, "import", "fhir_file", "Source")


@pytest.mark.anyio
async def test_an_amended_note_gets_its_new_text(source):
    prof, conn = source
    fetched, _ = await sync_notes(conn, prof, Bodies("Patient has no allergies"), linked_note())
    assert fetched == 1
    fetched, recs = await sync_notes(conn, prof, Bodies("CORRECTION: allergic to penicillin"), linked_note(doc="amended", version="2"))
    assert fetched == 1 and recs["n"]["narrative"] == "CORRECTION: allergic to penicillin"
    assert "body_stale" not in recs["n"]["details"]


@pytest.mark.anyio
async def test_an_unchanged_note_isnt_fetched_again_even_with_a_new_signed_url(source):
    prof, conn = source
    await sync_notes(conn, prof, Bodies("Text"), linked_note())
    bodies = Bodies("Other")
    fetched, recs = await sync_notes(conn, prof, bodies, linked_note(url="Binary/1?sig=2"))
    assert fetched == 0 and bodies.calls == 0 and recs["n"]["narrative"] == "Text"


@pytest.mark.anyio
async def test_a_failed_refetch_keeps_the_old_text_and_tries_again(source):
    prof, conn = source
    await sync_notes(conn, prof, Bodies("Old text"), linked_note())
    fetched, recs = await sync_notes(conn, prof, Bodies("x", fail=True), linked_note(doc="amended", version="2"))
    assert fetched == 0 and recs["n"]["narrative"] == "Old text" and recs["n"]["details"]["body_stale"] is True
    fetched, recs = await sync_notes(conn, prof, Bodies("New text"), linked_note(doc="amended", version="2"))
    assert fetched == 1 and recs["n"]["narrative"] == "New text" and "body_stale" not in recs["n"]["details"]


@pytest.mark.anyio
async def test_a_preliminary_report_gets_its_final_text(source):
    prof, conn = source

    def report(status, version):
        return {"resourceType": "DiagnosticReport", "id": "r", "status": status, "code": {"text": "CT"}, "meta": {"versionId": version},
                "presentedForm": [{"contentType": "text/plain", "url": "Binary/2"}]}
    await sync_notes(conn, prof, Bodies("Preliminary read"), report("preliminary", "1"))
    _, recs = await sync_notes(conn, prof, Bodies("Final read"), report("final", "2"))
    assert recs["r"]["narrative"] == "Final read"


@pytest.mark.anyio
async def test_past_the_cap_changed_notes_come_first_and_nothing_is_blanked(source, monkeypatch):
    prof, conn = source
    await sync_notes(conn, prof, Bodies("Old"), linked_note("a"), linked_note("b"))
    with monkeypatch.context() as patch:
        patch.setattr(sync, "MAX_NOTE_FETCHES", 1)
        fetched, recs = await sync_notes(conn, prof, Bodies("New"), linked_note("new"), linked_note("a", version="2"), linked_note("b", version="2"))
    assert fetched == 1
    assert recs["a"]["narrative"] == "New" and recs["b"]["narrative"] == "Old" and recs["b"]["details"]["body_stale"] is True and not recs["new"]["narrative"]


@pytest.mark.anyio
async def test_a_note_and_a_report_sharing_an_id_keep_their_own_text(source):
    prof, conn = source
    report = {"resourceType": "DiagnosticReport", "id": "same", "status": "final", "code": {"text": "CT"}, "presentedForm": [{"contentType": "text/plain", "url": "Binary/2"}]}
    await sync_notes(conn, prof, Bodies("Report text"), report)
    _, recs = await sync_notes(conn, prof, Bodies("Note text"), linked_note("same"))
    assert recs["same"]["narrative"] == "Note text"


# ---- F132: dates that aren't dates

def test_free_text_dates_arent_dates():
    r = fn.normalize_immunization({"resourceType": "Immunization", "id": "a", "status": "completed", "occurrenceString": "Spring 2020", "recorded": "2024-01-01",
                                   "vaccineCode": {"text": "MMR"}})
    assert r["effective_at"] is None and r["details"]["date_text"] == "Spring 2020"
    r = fn.normalize_procedure({"resourceType": "Procedure", "id": "a", "status": "completed", "performedString": "as a child", "code": {"text": "Appendectomy"}})
    assert r["effective_at"] is None and r["details"]["date_text"] == "as a child"
    r = fn.normalize_procedure({"resourceType": "Procedure", "id": "a", "status": "completed", "performedString": "2015-06", "code": {"text": "X"}})
    assert r["effective_at"] == "2015-06" and r["details"]["date_text"] is None


def test_free_text_never_tops_the_timeline(client):
    prof = client.get("/api/profiles").json()["profiles"][0]["id"]
    sync.import_fhir_resources(prof, "F", [{"resourceType": "Procedure", "id": "a", "status": "completed", "performedString": "as a child", "code": {"text": "Appendectomy"}},
                                           {"resourceType": "Immunization", "id": "b", "status": "completed", "occurrenceDateTime": "2025-10-01", "vaccineCode": {"text": "Flu"}}])
    assert [i["title"] for i in client.get("/api/timeline").json()["items"]] == ["Flu", "Appendectomy"]
    assert client.get("/api/records", params={"category": "procedures"}).json()["items"][0]["details"]["date_text"] == "as a child"


def test_one_partial_medication_date_doesnt_stop_the_medication_timeline(client, monkeypatch):
    from app.services import insights
    prof = client.get("/api/profiles").json()["profiles"][0]["id"]
    with monkeypatch.context() as patch:
        patch.setattr(insights.records, "medication_orders", lambda p: [{"id": "1", "title": "X", "effective_at": "2015-06", "status": "active"},
                                                                        {"id": "2", "title": "Y", "effective_at": "bad", "status": "active"}])
        assert insights.medication_timeline(prof) == []


# ---- F134: what the resources said

@pytest.mark.parametrize("dosage, text, needed", [
    ({"patientInstruction": "Take with food"}, "Take with food", False),
    ({"doseAndRate": [{"doseQuantity": {"value": 1, "unit": "tablet"}}], "timing": {"repeat": {"frequency": 2, "period": 1, "periodUnit": "d"}}}, "1 tablet twice a day", False),
    ({"timing": {"code": {"text": "At bedtime"}}, "route": {"text": "Oral"}}, "At bedtime Oral", False),
    ({"doseAndRate": [{"doseQuantity": {"value": 2, "unit": "puffs"}}], "timing": {"repeat": {"period": 6, "periodUnit": "h"}},
      "asNeededCodeableConcept": {"text": "wheezing"}}, "2 puffs once every 6 hours as needed for wheezing", True),
    ({"text": "As written", "patientInstruction": "other"}, "As written", False),
])
def test_dosage_from_any_field(dosage, text, needed):
    r = fn.normalize_medication_request(med_request("a", medicationCodeableConcept={"text": "X"}, dosageInstruction=[dosage]))
    assert r["details"]["dosage"] == text and r["details"]["as_needed"] is needed


def test_care_plan_narrative_and_reaction_description_and_onset_text():
    plan = fn.normalize_care_plan({"resourceType": "CarePlan", "id": "a", "status": "active", "text": {"div": "<div>Walk <b>daily</b></div>"}})
    assert plan["narrative"] == "Walk daily"
    allergy = fn.normalize_allergy({"resourceType": "AllergyIntolerance", "id": "a", "code": {"text": "Latex"}, "note": [{"text": "Avoid gloves"}],
                                    "reaction": [{"description": "Hives on arms", "manifestation": []}]})
    assert allergy["narrative"] == "Hives on arms" and allergy["details"]["note"] == "Avoid gloves"
    cond = fn.normalize_condition({"resourceType": "Condition", "id": "a", "code": {"text": "Asthma"}, "onsetString": "childhood",
                                   "abatementPeriod": {"start": "2020-01-01"}})
    assert cond["details"]["onset_text"] == "childhood" and cond["effective_end"] == "2020-01-01" and cond["status"] == "resolved"


def test_encounter_reasons_and_diagnoses_from_references():
    e = fn.normalize_encounter({"resourceType": "Encounter", "id": "a", "status": "finished", "class": {"code": "AMB"},
                                "reasonReference": [{"display": "Chest pain"}], "diagnosis": [{"condition": {"display": "GERD"}}]})
    assert e["details"]["reasons"] == ["Chest pain"] and e["details"]["diagnoses"] == ["GERD"] and e["narrative"] == "Chest pain"


def test_a_care_team_with_only_references_is_kept():
    t = fn.normalize_care_team({"resourceType": "CareTeam", "id": "a", "status": "active", "participant": [{"member": {"reference": "Practitioner/1"}}, {"member": {"reference": "Practitioner/2"}}]})
    assert t["details"]["unnamed_members"] == 2 and "2 members" in t["narrative"]


# ---- F136: older FHIR, id-less bundles

DSTU2 = [
    {"resourceType": "Observation", "id": "o", "status": "final", "category": {"coding": [{"code": "laboratory"}]}, "code": {"text": "Glucose"},
     "effectiveDateTime": "2015-01-01", "valueQuantity": {"value": 90, "unit": "mg/dL"}},
    {"resourceType": "MedicationOrder", "id": "m", "status": "active", "dateWritten": "2015-01-01", "medicationCodeableConcept": {"text": "Lisinopril"},
     "prescriber": {"display": "Dr. A"}, "dosageInstruction": [{"text": "daily"}]},
    {"resourceType": "AllergyIntolerance", "id": "a", "status": "confirmed", "substance": {"text": "Peanuts"}, "recordedDate": "2015-01-01"},
    {"resourceType": "Condition", "id": "c", "clinicalStatus": "active", "verificationStatus": "confirmed", "code": {"text": "Asthma"}, "dateRecorded": "2015-01-01"},
    {"resourceType": "Immunization", "id": "i", "status": "completed", "date": "2015-01-01", "wasNotGiven": True, "vaccineCode": {"text": "Flu"}},
    {"resourceType": "Encounter", "id": "e", "status": "finished", "class": "outpatient", "period": {"start": "2015-01-01"}},
]


def test_dstu2_records_come_through():
    _, recs, problems = fn.normalize_report(json.loads(json.dumps(DSTU2)))
    by = {r["resource_id"]: r for r in recs}
    assert set(by) == {"o", "m", "a", "c", "i", "e"} and problems == {"failed": {}, "skipped": {}}
    assert by["o"]["category"] == "labs" and by["o"]["value_num"] == 90
    assert by["m"]["title"] == "Lisinopril" and by["m"]["effective_at"] == "2015-01-01" and by["m"]["details"]["prescriber"] == "Dr. A"
    assert by["a"]["title"] == "Peanuts" and by["a"]["status"] == "active" and by["a"]["effective_at"] == "2015-01-01"
    assert by["c"]["effective_at"] == "2015-01-01" and by["c"]["status"] == "active"
    assert by["i"]["effective_at"] == "2015-01-01" and by["i"]["status"] == "not-done"
    assert by["m"]["raw"]["resourceType"] == "MedicationOrder"


def test_bundle_with_only_full_urls():
    bundle = {"resourceType": "Bundle", "entry": [
        {"fullUrl": "urn:uuid:1111", "resource": {"resourceType": "Medication", "code": {"text": "Metformin"}}},
        {"fullUrl": "urn:uuid:2222", "resource": {"resourceType": "MedicationRequest", "status": "active", "medicationReference": {"reference": "urn:uuid:1111"}}},
        {"fullUrl": "urn:uuid:3333", "resource": {"resourceType": "Observation", "status": "final", "code": {"text": "K"}, "valueQuantity": {"value": 4, "unit": "mmol/L"}}},
        {"fullUrl": "https://x.example/DiagnosticReport/r", "resource": {"resourceType": "DiagnosticReport", "status": "final", "category": [{"coding": [{"code": "LAB"}]}],
                                                                         "code": {"text": "BMP"}, "result": [{"reference": "urn:uuid:3333"}]}},
    ]}
    resources = imports.parse_fhir_payload(json.dumps(bundle).encode())
    _, recs = fn.normalize_resources(resources)
    by = {r["category"]: r for r in recs}
    assert by["medications"]["title"] == "Metformin" and by["medications"]["resource_id"] == "2222"
    assert by["labs"]["details"]["panel"] == "BMP" and by["reports"]["details"]["result_refs"] == ["Observation/3333"]
    assert "_full_url" not in by["labs"]["raw"]


def test_a_full_url_that_isnt_the_id_still_resolves():
    bundle = {"resourceType": "Bundle", "entry": [
        {"fullUrl": "urn:uuid:aaaa", "resource": {"resourceType": "Medication", "id": "local-1", "code": {"text": "Metformin"}}},
        {"resource": {"resourceType": "MedicationRequest", "id": "r", "status": "active", "medicationReference": {"reference": "urn:uuid:aaaa"}}}]}
    _, recs = fn.normalize_resources(imports.parse_fhir_payload(json.dumps(bundle).encode()))
    assert recs[0]["title"] == "Metformin"


def test_an_import_says_what_it_couldnt_read(client):
    bundle = {"resourceType": "Bundle", "entry": [
        {"resource": {"resourceType": "Observation", "id": "a", "status": "final", "code": {"text": "Empty"}}},
        {"resource": {"resourceType": "Observation", "status": "final", "code": {"text": "No id"}, "valueString": "x"}},
        {"resource": {"resourceType": "Observation", "id": "b", "status": "final", "code": {"text": "Fine"}, "valueString": "x"}}]}
    r = client.post("/api/imports/fhir", files={"file": ("f.json", json.dumps(bundle).encode(), "application/json")}).json()
    assert r["records"] == 1 and r["not_read"] == 2 and r["warnings"] == ["Observation: 2 couldn't be read"]


# ---- F137: more than one person in a file

def patient(pid, name, born):
    return {"resourceType": "Patient", "id": pid, "name": [{"text": name}], "birthDate": born}


def test_a_file_with_two_people_is_refused_in_words(client):
    bundle = {"resourceType": "Bundle", "entry": [{"resource": r} for r in (
        patient("1", "Alex Rivera", "1980-01-01"), patient("2", "Sam Other", "2010-05-05"),
        {"resourceType": "Condition", "id": "c", "code": {"text": "Asthma"}, "subject": {"reference": "Patient/2"}})]}
    r = client.post("/api/imports/fhir", files={"file": ("f.json", json.dumps(bundle).encode(), "application/json")})
    assert r.status_code == 400
    assert "Alex Rivera" in r.json()["detail"] and "Sam Other" in r.json()["detail"] and "Nothing was imported" in r.json()["detail"]
    assert client.get("/api/records", params={"category": "conditions"}).json()["items"] == []


def test_one_person_under_two_hospital_ids_is_fine(client):
    bundle = {"resourceType": "Bundle", "entry": [{"resource": r} for r in (
        patient("1", "Alex Rivera", "1980-01-01"), patient("2", "Alex J Rivera", "1980-01-01"),
        {"resourceType": "Condition", "id": "c", "code": {"text": "Asthma"}, "subject": {"reference": "Patient/2"}})]}
    r = client.post("/api/imports/fhir", files={"file": ("f.json", json.dumps(bundle).encode(), "application/json")})
    assert r.status_code == 200 and not r.json()["warnings"]


def test_records_about_a_patient_the_file_doesnt_name_are_mentioned(client):
    bundle = {"resourceType": "Bundle", "entry": [{"resource": r} for r in (
        patient("1", "Alex Rivera", "1980-01-01"),
        {"resourceType": "Condition", "id": "c", "code": {"text": "Asthma"}, "subject": {"reference": "Patient/2"}})]}
    r = client.post("/api/imports/fhir", files={"file": ("f.json", json.dumps(bundle).encode(), "application/json")}).json()
    assert "another patient ID" in r["warnings"][0]
