"""Findings from a review of records: what a source sends, how a sync walks it, and what's kept and shown."""

import os
import subprocess
import sys
import textwrap

import pytest

from app.connectors import fhir_normalize as fn
from app.connectors import units

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---- Range text padded with spaces

def lab(ranges, value=24, **kw):
    res = {"resourceType": "Observation", "id": "o1", "status": "final", "category": [{"coding": [{"code": "laboratory"}]}],
           "code": {"coding": [{"system": "http://loinc.org", "code": "1989-3", "display": "Vitamin D"}]},
           "effectiveDateTime": "2025-01-01T08:00:00Z", "valueQuantity": {"value": value, "unit": "ng/mL"}, "referenceRange": ranges}
    res.update(kw)
    if "valueString" in kw:
        del res["valueQuantity"]
    return fn.normalize_observation(res)


def test_range_text_padded_with_twenty_thousand_spaces_is_read_at_once():
    """Patterns that could share a run of spaces between them tried every way of doing it: 800 spaces took a second, 20,000 would
    take hours. In a child process, so that a regression fails at the time limit instead of holding the run."""
    script = textwrap.dedent("""
        import time
        from app.connectors import fhir_normalize as fn, units
        pad = " " * 20000
        texts = [pad, "a" + pad + "x", "1" + pad + "x", "1" + pad + "normalx", "1" + pad + "or", "1" + pad + "-" + pad,
                 "Normal:" + pad + "30-100" + pad + "x", "1" * 20000 + "x", "\\n" * 20000 + "x", " ," * 20000]
        res = {"resourceType": "Observation", "status": "final", "category": [{"coding": [{"code": "laboratory"}]}],
               "code": {"coding": [{"system": "http://loinc.org", "code": "1989-3"}]}, "effectiveDateTime": "2025-01-01"}
        start = time.perf_counter()
        for text in texts:
            fn.normalize_observation({**res, "valueQuantity": {"value": 24, "unit": text}, "referenceRange": [{"text": text}]})
            fn.normalize_observation({**res, "valueQuantity": {"value": 24}, "referenceRange": [{"text": text}, {"text": text + "y"}]})
            fn.normalize_observation({**res, "valueString": text})
            units.unit_key(text), units.unit_key("{1.73" * 4000), units.pretty_unit("{" + text)
        print(time.perf_counter() - start)
    """)
    done = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert float(done.stdout) < 2.0


@pytest.mark.parametrize("text, low, high", [
    ("Normal:      30   -    100", 30, 100), ("  30 \t-\t 100  ng/mL  ", 30, 100), ("Sufficient\t30-100", 30, 100),
    ("30-100     (normal)", 30, 100), ("Deficient:   <20;   Sufficient:    30-100", 30, 100), (">   OR   =   30", 30, None),
    # A lab's long note around its bands: the band is still read, and the note isn't put to a pattern whole.
    ("Vitamin D Status 25-OH Vitamin D:\nDeficiency: <20 ng/mL\nInsufficiency: 20 - 29 ng/mL\nOptimal: > or = 30 ng/mL\n"
     + "For testing on patients on D2-supplementation " * 10, 30, None),
])
def test_range_text_with_spaces_and_line_breaks_reads_as_before(text, low, high):
    r = lab([{"text": text}])
    assert (r["ref_low"], r["ref_high"]) == (low, high)


def test_a_band_longer_than_any_range_stays_unread():
    r = lab([{"text": "Normal: 30-100 " + "x" * 300}])
    assert r["ref_low"] is None and r["ref_high"] is None and r["interpretation"] is None


@pytest.mark.parametrize("unit, key", [
    ("mg / dL", "mg/dl"), ("mL/min/{1.73_m2}", "ml/min/1.73m2"), ("mL/min/1.73 m2", "ml/min/1.73m2"), ("mg/dL (calc)", "mg/dl"),
    ("{beats}/min", "/min"), ("mmol / L  (estimated)", "mmol/l"),
])
def test_unit_spellings_still_meet(unit, key):
    assert units.unit_key(unit) == key


def test_a_number_written_with_spaces_around_it_is_still_a_number():
    r = lab([], valueString="   < 5.2   ")
    assert r["value_num"] == 5.2 and r["details"]["comparator"] == "<"


# ---- A sync against a mock FHIR server

BASE = "https://ehr.example/fhir"


@pytest.fixture
def fhir_server(monkeypatch):
    """A FHIR server answering through ``handler``, which a test sets; every request is noted in ``asked``."""
    import httpx

    from app.connectors import smart

    server = type("Server", (), {"handler": None, "asked": []})()

    def answer(request):
        server.asked.append(request.url)
        if request.url.path.endswith("/Patient/p"):
            return httpx.Response(200, json={"resourceType": "Patient", "id": "p"})
        return server.handler(request) or httpx.Response(200, json={"resourceType": "Bundle", "entry": []})

    real = httpx.AsyncClient
    monkeypatch.setattr(smart, "http_client", lambda simulated=False, timeout=30.0: real(transport=httpx.MockTransport(answer), timeout=timeout))
    monkeypatch.setattr(smart, "_sleep", lambda seconds: _no_wait())
    return server


async def _no_wait():
    return None


async def test_a_medication_sent_only_as_a_reference_is_read_and_named_during_a_sync(fhir_server):
    import httpx

    from app.connectors import smart

    def handler(request):
        if request.url.path.endswith("/Medication/m1"):
            return httpx.Response(200, json={"resourceType": "Medication", "id": "m1", "code": {"text": "Lisinopril 10 mg"}})
        if request.url.path.endswith("/MedicationRequest"):        # ignores _include: a bare reference, no Medication
            return httpx.Response(200, json={"resourceType": "Bundle", "entry": [{"resource": {
                "resourceType": "MedicationRequest", "id": "mr1", "status": "active", "intent": "order",
                "medicationReference": {"reference": "Medication/m1"}}}]})
        return None

    fhir_server.handler = handler
    out = await smart.FhirClient(BASE, {"access_token": "t"}, "p").fetch_all({})
    assert out.stopped is None
    assert [u.path for u in fhir_server.asked if "/Medication/" in u.path] == ["/fhir/Medication/m1"]
    _, recs = fn.normalize_resources([out.patient] + out.resources)
    assert [r["title"] for r in recs if r["category"] == "medications"] == ["Lisinopril 10 mg"]


LAB = [{"coding": [{"code": "laboratory"}]}]
LABS = "Observation?category=laboratory"
PAGE_2 = f"{BASE}/Observation?page=2&category=laboratory"


def _labs_then(page_2):
    """Labs on two pages, the second answered by ``page_2``: the first page's lab is o1."""
    import httpx

    def handler(request):
        query = dict(request.url.params)
        if request.url.path.endswith("/Observation") and query.get("category") == "laboratory" and "page" not in query:
            return httpx.Response(200, json={"resourceType": "Bundle", "link": [{"relation": "next", "url": PAGE_2}], "entry": [
                {"resource": {"resourceType": "Observation", "id": "o1", "status": "final", "category": LAB}}]})
        if query.get("page") == "2":
            return page_2()
        return None
    return handler


@pytest.mark.parametrize("status, body", [
    (200, {"resourceType": "OperationOutcome", "issue": [{"severity": "information", "code": "informational", "diagnostics": "Paging session expired"}]}),
    (200, {"resourceType": "OperationOutcome", "issue": [{"severity": "warning", "code": "processing"}]}),
    (410, {"resourceType": "OperationOutcome", "issue": [{"severity": "information", "code": "informational"}]}),
    (404, {"resourceType": "OperationOutcome", "issue": [{"severity": "error", "code": "not-found"}]}),
    (400, {}), (403, {}), (422, {}),
    (200, {"resourceType": "Parameters"}),
])
async def test_a_later_page_that_fails_leaves_the_search_unfinished(fhir_server, status, body):
    """A page after the first answered with anything but a Bundle, or refused, isn't the end of the results: what was stored from
    the pages not read is kept, the source isn't said not to offer labs, and the next sync carries on from that page."""
    import httpx

    from app.connectors import smart
    from app.services import sync

    fhir_server.handler = _labs_then(lambda: httpx.Response(status, json=body))
    out = await smart.FhirClient(BASE, {"access_token": "t"}, "p").fetch_all({})
    assert out.status[LABS].startswith("error:")
    assert out.counts[LABS] == 1
    assert out.prune_verdict({"resourceType": "Observation", "id": "o2", "status": "final", "category": LAB}) == "keep"
    assert out.state()[LABS]["next_url"] == PAGE_2
    described = next(d for d in sync.describe_queries([{"stats": {"queries": out.status, "counts": out.counts}}]) if d["query"] == LABS)
    assert not described["ok"] and "try again" in described["words"]


async def test_the_next_sync_carries_on_from_the_page_that_failed(fhir_server):
    import httpx

    from app.connectors import smart

    pages = iter([httpx.Response(410, json={}), httpx.Response(200, json={"resourceType": "Bundle", "entry": [
        {"resource": {"resourceType": "Observation", "id": "o2", "status": "final", "category": LAB}}]})])
    fhir_server.handler = _labs_then(lambda: next(pages))
    first = await smart.FhirClient(BASE, {"access_token": "t"}, "p").fetch_all({})
    second = await smart.FhirClient(BASE, {"access_token": "t"}, "p").fetch_all(first.state())
    assert "o2" in {r.get("id") for r in second.resources}
    assert second.state()[LABS]["next_url"] is None


async def test_a_first_page_that_says_there_is_nothing_is_still_an_answer(fhir_server):
    import httpx

    from app.connectors import smart

    def handler(request):
        if request.url.path.endswith("/Observation") and request.url.params.get("category") == "laboratory":
            return httpx.Response(200, json={"resourceType": "OperationOutcome", "issue": [{"severity": "information", "code": "informational"}]})
        return None

    fhir_server.handler = handler
    out = await smart.FhirClient(BASE, {"access_token": "t"}, "p").fetch_all({})
    assert out.status[LABS] == "empty"


# ---- Reading stored records again

def test_reading_a_report_again_keeps_the_full_text_fetched_from_its_link():
    """A report with a conclusion keeps the text fetched from its link beside it, in details. Read again by a later normalizer,
    it lost that text, which the stored resource can't give back: a file import never fetches it again."""
    import json

    from app.services import renormalize

    res = {"resourceType": "DiagnosticReport", "id": "r1", "status": "final", "code": {"text": "CT chest"},
           "category": [{"coding": [{"code": "RAD"}]}], "effectiveDateTime": "2024-03-01", "conclusion": "No acute findings.",
           "presentedForm": [{"contentType": "text/html", "url": "Binary/abc"}]}
    rec = fn.normalize_one(res)
    full = "FULL REPORT: 4 mm nodule in the right upper lobe; follow-up CT in 12 months."
    rec["details"].update(full_text=full, body_fingerprint=fn.body_fingerprint(res), body_stale=True, panel="Imaging",
                          result_refs=["Observation/x"])
    row = {k: rec.get(k) for k in renormalize._DERIVED if k != "details_json"}
    row.update(raw_json=json.dumps(res), details_json=json.dumps(rec["details"]), narrative=rec["narrative"], connection_id="c")
    details = json.loads(renormalize.reread(row, None)["details_json"])
    assert details["full_text"] == full
    assert {k: details.get(k) for k in ("body_fingerprint", "body_stale", "panel", "result_refs")} == \
        {"body_fingerprint": fn.body_fingerprint(res), "body_stale": True, "panel": "Imaging", "result_refs": ["Observation/x"]}


# ---- Image files with many pages, or very large ones

def _tiff(frames, size=(1000, 1000)):
    import io

    from PIL import Image

    page = Image.new("L", size, 255)
    buf = io.BytesIO()
    page.save(buf, "TIFF", save_all=True, append_images=[page] * (frames - 1), compression="tiff_deflate")
    return buf.getvalue()


@pytest.fixture
def decoded(monkeypatch):
    """How many frames were decoded to colour."""
    from PIL import Image

    count = [0]
    real = Image.Image.convert

    def counting(self, *args, **kwargs):
        count[0] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "convert", counting)
    return count


def test_only_the_pages_read_of_a_long_tiff_are_decoded(decoded):
    """A 300 KB TIFF of 200 blank pages was decoded whole, 3 MB a page, before all but 30 were dropped: 2,000 such pages took 3.5 GB."""
    from app.services import lab_report

    pages = lab_report._open_images(_tiff(200), "image/tiff")
    assert len(pages) == lab_report.MAX_PAGES and decoded[0] == lab_report.MAX_PAGES


def test_pages_declaring_more_pixels_than_can_be_kept_are_refused_before_decoding(decoded, monkeypatch):
    from app.services import lab_report

    monkeypatch.setattr(lab_report, "MAX_PIXELS", 3_500_000)
    with pytest.raises(lab_report.ReportError, match="too large"):
        lab_report._open_images(_tiff(5), "image/tiff")
    assert decoded[0] == 3


def test_a_heic_declaring_too_many_pixels_is_refused_before_decoding(monkeypatch):
    import types

    from app.services import lab_report

    heif = types.SimpleNamespace(size=(40_000, 40_000), to_pillow=lambda: pytest.fail("decoded"))
    monkeypatch.setitem(sys.modules, "pi_heif", types.SimpleNamespace(open_heif=lambda data: heif))
    monkeypatch.setattr(lab_report, "_heic_from_macos", lambda data: None)
    with pytest.raises(lab_report.ReportError, match="too large"):
        lab_report._heic_to_jpeg(b"heic")


@pytest.mark.parametrize("size", [(100, 12_000), (10, 100_000), (12_000, 100)])
@pytest.mark.parametrize("used", ["tesseract", "vision"])
def test_a_narrow_strip_isnt_widened_into_a_billion_pixels_for_ocr(monkeypatch, size, used):
    """OCR reads a page at least 1,800 wide, which made a strip 10 pixels wide and 100,000 tall into 32 billion pixels."""
    from PIL import Image

    from app.services import lab_report

    monkeypatch.setattr(lab_report, "PREPARED_PIXELS", 1_000_000, raising=False)
    real = Image.Image.resize

    def resize(self, target, *args, **kwargs):
        assert target[0] * target[1] <= 1_000_000, f"resized to {target}"
        return real(self, target, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "resize", resize)
    prepared, _ = lab_report._prepare(Image.new("RGB", size, "white"), used)
    assert prepared.width * prepared.height <= 1_000_000


# ---- A blood pressure's flags

def _ccda_pressure(systolic_flag, diastolic_flag, systolic_range=""):
    from tests.test_ccda_import import HEADER

    def part(root, code, name, value, flag, ranges=""):
        flagged = f'<interpretationCode code="{flag}" codeSystem="2.16.840.1.113883.5.83"/>' if flag else ""
        return f"""<component><observation><id root="{root}"/><code code="{code}" codeSystem="2.16.840.1.113883.6.1" displayName="{name}"/>
          <statusCode code="completed"/><effectiveTime value="20250210"/><value xsi:type="PQ" value="{value}" unit="mm[Hg]"/>{flagged}{ranges}
          </observation></component>"""

    return HEADER + f"""<component><structuredBody><component><section><code code="8716-3" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Vitals</title><text>v</text><entry><organizer classCode="CLUSTER" moodCode="EVN"><id root="v0"/><effectiveTime value="20250210"/>
      {part("v1", "8480-6", "Systolic", 182, systolic_flag, systolic_range)}{part("v2", "8462-4", "Diastolic", 88, diastolic_flag)}
      </organizer></entry></section></component></structuredBody></component></ClinicalDocument>"""


SYSTOLIC_RANGE = """<referenceRange><observationRange><value xsi:type="IVL_PQ"><low value="90" unit="mm[Hg]"/><high value="129" unit="mm[Hg]"/>
  </value></observationRange></referenceRange>"""


def _pressure(raw):
    from app.services import ccda

    resources = ccda.read_upload(raw.encode(), "v.xml").resources
    _, recs = fn.normalize_resources(resources)
    panel = next(r for r in resources if r["resourceType"] == "Observation" and r.get("component"))
    return panel, next(r for r in recs if r["title"] == "Blood pressure")


@pytest.mark.parametrize("systolic_flag, diastolic_flag, systolic_range, flag", [
    ("HH", "N", SYSTOLIC_RANGE, "critical_high"), ("H", "", "", "high"), ("N", "H", "", "high"),
    ("", "N", SYSTOLIC_RANGE, "high"),        # flagged by its own range, as the lone systolic would have been
    ("N", "N", "", "normal"), ("", "", "", None),
])
def test_a_ccda_blood_pressure_keeps_the_flags_of_its_two_parts(systolic_flag, diastolic_flag, systolic_range, flag):
    """C-CDA has two observations where FHIR has one with two parts. Merged, the parts lost the flags and range the source gave them."""
    panel, rec = _pressure(_ccda_pressure(systolic_flag, diastolic_flag, systolic_range))
    assert rec["value_text"] == "182/88 mmHg" and rec["interpretation"] == flag
    systolic, diastolic = panel["component"]
    assert bool(systolic.get("referenceRange")) == bool(systolic_range)
    assert bool(systolic.get("interpretation")) == bool(systolic_flag) and bool(diastolic.get("interpretation")) == bool(diastolic_flag)


def test_a_blood_pressure_flagged_only_on_a_part_is_flagged():
    """As Epic sends one: the panel has no flag of its own, its systolic part does."""
    res = {"resourceType": "Observation", "id": "bp", "status": "final", "category": [{"coding": [{"code": "vital-signs"}]}],
           "code": {"coding": [{"system": fn.LOINC, "code": "85354-6", "display": "Blood pressure"}]}, "effectiveDateTime": "2025-02-10",
           "component": [
               {"code": {"coding": [{"system": fn.LOINC, "code": "8480-6"}]}, "valueQuantity": {"value": 151, "unit": "mm[Hg]"},
                "interpretation": [{"coding": [{"system": fn.V3_INTERPRETATION, "code": "H"}]}]},
               {"code": {"coding": [{"system": fn.LOINC, "code": "8462-4"}]}, "valueQuantity": {"value": 79, "unit": "mm[Hg]"},
                "referenceRange": [{"low": {"value": 60, "unit": "mm[Hg]"}, "high": {"value": 80, "unit": "mm[Hg]"}}]}]}
    rec = fn.normalize_observation(res)
    assert rec["interpretation"] == "high"
    assert [c.get("interpretation") for c in rec["details"]["components"]] == ["high", "normal"]


# ---- One draw reported two ways

def _lab(rid, code, value, when, unit="mL/min/{1.73_m2}"):
    return {"resourceType": "Observation", "id": rid, "status": "final", "category": [{"coding": [{"code": "laboratory"}]}],
            "code": {"coding": [{"system": fn.LOINC, "code": code}], "text": f"Test {code}"}, "effectiveDateTime": when,
            "valueQuantity": {"value": value, "unit": unit, "system": "http://unitsofmeasure.org", "code": unit},
            "referenceRange": [{"low": {"value": 60, "unit": unit}}]}


def _catalog(*resources):
    from app.store import connections, profiles, records

    pid = profiles.list_profiles()[0]["id"]
    conn = connections.create(pid, "import", "fhir_file", "Lab", mode="live")
    _, recs = fn.normalize_resources(list(resources))
    records.upsert_records(pid, conn["id"], "Lab", recs)
    return {e["key"]: e for e in records.observation_catalog(pid)}


def test_an_egfr_given_for_black_patients_and_for_everyone_else_is_two_tests(client):
    """Labs gave both estimates from one draw until 2021. As one test, the draw showed as a trend between them, and the latest was a tie."""
    cat = _catalog(_lab("a", "48642-3", 52, "2019-06-01T08:00:00Z"), _lab("b", "48643-1", 63, "2019-06-01T08:00:00Z"),
                   _lab("c", "88294-4", 50, "2020-06-01T08:00:00Z"), _lab("d", "88293-6", 58, "2020-06-01T08:00:00Z"),
                   _lab("e", "98979-8", 55, "2022-06-01T08:00:00Z"))
    assert {k: (e["latest"]["value_num"], e["count"], e["trend"]) for k, e in cat.items()} == {
        "88294-4": (50, 2, "down"), "88293-6": (58, 2, "down"), "98979-8": (55, 1, None)}


def test_one_sample_reported_under_two_codes_for_one_test_isnt_a_change(client):
    cat = _catalog(_lab("a", "13457-7", 100, "2025-01-01T08:00:00Z", "mg/dL"), _lab("b", "18262-6", 110, "2025-01-01T08:00:00Z", "mg/dL"))
    assert cat["13457-7"]["count"] == 2 and cat["13457-7"]["trend"] is None
    cat = _catalog(_lab("c", "13457-7", 130, "2024-01-01T08:00:00Z", "mg/dL"))
    assert cat["13457-7"]["trend"] == "down"


# ---- A time a source gave without an offset

def test_a_time_without_an_offset_is_filtered_by_the_day_it_says(client, monkeypatch):
    """01:30 on 2 March with no offset is a time on the clock where it happened: it's the 2nd, as its date is, though 01:30 UTC is
    still the 1st in New York. It was compared as UTC with New York's midnight, so it fell on the wrong day."""
    from app.core import settings
    from app.store import connections, profiles, records

    monkeypatch.setattr(settings, "timezone_name", lambda: "America/New_York")
    pid = profiles.list_profiles()[0]["id"]
    conn = connections.create(pid, "import", "fhir_file", "Lab", mode="live")
    times = {"late": "2025-03-01T23:30:00", "early": "2025-03-02T01:30:00", "evening": "2025-03-02T22:30:00",
             "after": "2025-03-03T00:30:00", "zoned": "2025-03-02T03:00:00Z", "day": "2025-03-02"}
    _, recs = fn.normalize_resources([_lab(rid, "2345-7", 90, when, "mg/dL") for rid, when in times.items()])
    records.upsert_records(pid, conn["id"], "Lab", recs)

    def ids(**bounds):
        return {r["resource_id"] for r in records.list_records(pid, "labs", dedupe=False, **bounds)["items"]}

    assert ids(since="2025-03-02", until="2025-03-02") == {"early", "evening", "day"}      # 03:00Z is 22:00 on the 1st in New York
    assert ids(since="2025-03-02T06:00:00Z", until="2025-03-03T04:00:00Z") == {"early", "evening", "day"}   # 01:00 to 23:00 there
    assert ids(until="2025-03-01") == {"late", "zoned"}


# ---- Which day it is

def test_which_flagged_results_are_older_is_decided_by_the_callers_day(client, monkeypatch):
    """Insights asks on the person's day, which a test can set. The flagged list used the server's own, so the golden snapshot,
    frozen at 1 June 2026, would have found its results older than 548 days from 15 September 2027 on."""
    from datetime import date

    from app.services import insights
    from app.store import connections, profiles, records

    pid = profiles.list_profiles()[0]["id"]
    conn = connections.create(pid, "import", "fhir_file", "Lab", mode="live")
    _, recs = fn.normalize_resources([_lab("a", "2345-7", 250, "2019-12-01T08:00:00Z", "mg/dL") | {
        "referenceRange": [{"low": {"value": 70, "unit": "mg/dL"}, "high": {"value": 99, "unit": "mg/dL"}}]}])
    records.upsert_records(pid, conn["id"], "Lab", recs)
    assert [bool(l.get("older")) for l in records.flagged_labs(pid, date(2020, 1, 1))] == [False]
    assert [bool(l.get("older")) for l in records.flagged_labs(pid)] == [True]

    seen = []
    real = records.flagged_labs
    monkeypatch.setattr(records, "flagged_labs", lambda profile_id, today=None: seen.append(today) or real(profile_id, today))
    monkeypatch.setattr(insights, "_today", lambda profile_id=None: date(2020, 1, 1))
    insights._lab_notes(pid)
    assert seen == [date(2020, 1, 1)]


# ---- Disconnecting

def _live_source(credentials):
    from app.store import connections, profiles

    pid = profiles.list_profiles()[0]["id"]
    return connections.create(pid, "ehr", "epic", "Clinic", mode="live", fhir_base_url=BASE, patient_ref="p",
                              credentials={"access_token": "at", "client_id": "app", "token_endpoint": f"{BASE}/token", **credentials})


@pytest.mark.parametrize("delete_data", [False, True])
def test_disconnecting_asks_the_source_to_end_the_access_it_granted(client, fhir_server, delete_data):
    import httpx

    from app.store import connections

    sent = []

    def handler(request):
        if request.url.path.endswith("/revoke"):
            sent.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200)
        return None

    fhir_server.handler = handler
    conn = _live_source({"refresh_token": "rt", "revocation_endpoint": f"{BASE}/revoke"})
    assert client.delete(f"/api/connections/{conn['id']}", params={"delete_data": delete_data}).status_code == 200
    assert sent == [{"token": "rt", "token_type_hint": "refresh_token", "client_id": "app"}]
    gone = connections.get(conn["id"], include_credentials=True)
    assert gone is None if delete_data else gone["status"] == "disconnected" and not gone["credentials"]


def test_a_source_connected_before_its_revocation_address_was_kept_is_asked_where_it_is(client, fhir_server):
    import httpx

    sent = []

    def handler(request):
        if request.url.path.endswith("/.well-known/smart-configuration"):
            return httpx.Response(200, json={"authorization_endpoint": f"{BASE}/authorize", "token_endpoint": f"{BASE}/token",
                                             "revocation_endpoint": f"{BASE}/revoke"})
        if request.url.path.endswith("/revoke"):
            sent.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200)
        return None

    fhir_server.handler = handler
    conn = _live_source({})
    assert client.delete(f"/api/connections/{conn['id']}").status_code == 200
    assert sent == [{"token": "at", "token_type_hint": "access_token", "client_id": "app"}]


@pytest.mark.parametrize("answer", ["refuses", "fails", "hangs", "insecure", "none"])
def test_a_source_that_wont_end_its_access_never_stops_a_disconnect(client, fhir_server, monkeypatch, answer):
    import asyncio
    import time

    import httpx

    from app.connectors import smart
    from app.store import connections

    monkeypatch.setattr(smart, "REVOKE_SECONDS", 0.3)

    async def handler(request):
        if answer == "hangs":
            await asyncio.sleep(5)
        if answer == "fails":
            raise httpx.ConnectError("down")
        return httpx.Response(503 if answer == "refuses" else 404)

    fhir_server.handler = lambda request: None if request.url.path.endswith("/Patient/p") else handler(request)
    real = httpx.AsyncClient
    monkeypatch.setattr(smart, "http_client", lambda simulated=False, timeout=30.0: real(transport=httpx.MockTransport(handler), timeout=timeout))
    endpoint = {"insecure": "http://ehr.example/revoke", "none": None}.get(answer, f"{BASE}/revoke")
    conn = _live_source({"refresh_token": "rt", "revocation_endpoint": endpoint})
    started = time.monotonic()
    assert client.delete(f"/api/connections/{conn['id']}").status_code == 200
    assert time.monotonic() - started < 3
    assert connections.get(conn["id"])["status"] == "disconnected"
