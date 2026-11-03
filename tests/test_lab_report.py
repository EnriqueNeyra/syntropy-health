"""Reading lab reports on this machine, and keeping them with the results: PDF text layers, what's on a printed line, and saving. See
test_lab_ocr.py for photos and scans."""

from __future__ import annotations

import io
import json
import os
import sys
import zipfile

import httpx
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.api import labs as lab_report_api
from app.core.db import db
from app.services import lab_report

needs_ocr = pytest.mark.skipif(not lab_report.ocr_available(), reason="No OCR engine on this computer")
JPEG = b"\xff\xd8\xff jpeg bytes"

# (test, result, flag, reference range with unit), laid out in columns like a Quest report.
QUEST_ROWS = [
    ("GLUCOSE", "105", "H", "65-99 mg/dL"),
    ("UREA NITROGEN (BUN)", "15", "", "7-25 mg/dL"),
    ("CREATININE", "0.82", "", "0.50-1.03 mg/dL"),
    ("EGFR", "88", "", "> OR = 60 mL/min/1.73m2"),
    ("CHOLESTEROL, TOTAL", "182", "", "<200 mg/dL"),
    ("LDL-CHOLESTEROL", "106", "H", "mg/dL (calc)"),
    ("HEMOGLOBIN A1c", "5.4", "", "<5.7 % of total Hgb"),
    ("VITAMIN D,25-OH,TOTAL,IA", "34", "", "30-100 ng/mL"),
    ("HEPATITIS C ANTIBODY", "NON-REACTIVE", "", "NON-REACTIVE"),
]
HEADER = ["Quest Diagnostics Incorporated", "Patient: DOE, JANE    DOB: 04/12/1980    Age: 46",
          "Collected: 09/12/2026 / 08:15 EDT    Reported: 09/13/2026", "Phone: 555-555-1234"]
FOOTER = "EN QUEST DIAGNOSTICS-EAST, 900 BUSINESS CENTER DRIVE, HORSHAM, PA 19044-3453"


def _pdf_string(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def quest_pdf() -> bytes:
    """A one-page PDF with a positioned text layer, like the reports lab websites let you download."""
    ops, y = [], 750
    for line in HEADER:
        ops.append(f"1 0 0 1 40 {y} Tm ({_pdf_string(line)}) Tj")
        y -= 16
    y -= 10
    for name, value, flag, ref in QUEST_ROWS:
        x_value = 300 if flag else 220                 # Quest puts out-of-range results in their own column
        cells = [(40, name), (x_value, f"{value} {flag}".strip()), (380, ref), (560, "EN")]
        ops += [f"1 0 0 1 {x} {y} Tm ({_pdf_string(text)}) Tj" for x, text in cells]
        y -= 14
    ops.append(f"1 0 0 1 40 {y - 20} Tm ({_pdf_string(FOOTER)}) Tj")
    content = "BT /F1 9 Tf " + " ".join(ops) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    out.write("".join(f"{o:010d} 00000 n \n" for o in offsets).encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def report_image() -> Image.Image:
    """The same report printed on paper, as a clean scan."""
    font = ImageFont.load_default(size=30)
    img = Image.new("RGB", (1700, 1000), "white")
    draw = ImageDraw.Draw(img)
    y = 50
    for line in HEADER:
        draw.text((60, y), line, fill="black", font=font)
        y += 50
    y += 30
    for name, value, flag, ref in QUEST_ROWS:
        draw.text((60, y), name, fill="black", font=font)
        draw.text((700 if not flag else 860, y), f"{value} {flag}".strip(), fill="black", font=font)
        draw.text((1100, y), ref, fill="black", font=font)
        y += 50
    return img


def pdf(pages: list[list[tuple[int, int, str]]]) -> bytes:
    """A PDF with its text placed by hand: for each page, (x, y, text)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for cells in pages:
        content = "BT /F1 9 Tf " + " ".join(f"1 0 0 1 {x} {y} Tm ({_pdf_string(t)}) Tj" for x, y, t in cells) + " ET"
        objects.append(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objects)} 0 R /Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    out.write("".join(f"{o:010d} 00000 n \n" for o in offsets).encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def by_test(out: dict) -> dict:
    return {r["test"].upper(): r for r in out["results"]}


def test_parses_labcorp_style_rows():
    text = """labcorp
Age        Sex     Date Collected        Date Received
46         F       09/12/2026 0815       09/12/2026
Test                   Current Result and Flag   Previous Result and Date   Units       Reference Interval
 WBC 01                6.1                       5.8      03/10/2025        x10E3/uL    3.4-10.8
 Platelets 01          412 High                  380      03/10/2025        x10E3/uL    150-450
 Glucose 01            105 High                  98       03/10/2025        mg/dL       70-99
 BUN/Creatinine Ratio  18                                                               9-23
 Protein               Negative                                                         Negative/Trace
01  LabCorp Burlington   1447 York Court, Burlington, NC 27215-3361"""
    out = lab_report.parse_text(text)
    assert out["collected"] == "2026-09-12" and out["lab"] == "Labcorp"
    rows = {r["test"]: r for r in out["results"]}
    assert set(rows) == {"WBC", "Platelets", "Glucose", "BUN/Creatinine Ratio", "Protein"}
    # The current result, never the previous one; the lab's "01" site code is skipped.
    g = rows["Glucose"]
    assert (g["value"], g["flag"], g["unit"], g["ref_low"], g["ref_high"]) == (105, "H", "mg/dL", 70, 99)
    assert rows["WBC"]["value"] == 6.1 and rows["WBC"]["unit"] == "x10E3/uL" and rows["WBC"]["flag"] is None
    assert rows["Protein"]["value_text"] == "Negative" and rows["Protein"]["unit"] is None


def test_ignores_lines_that_are_not_results():
    for line in ("Page 1 of 2", "Phone: 555-555-1234", "Specimen: EN123456R  Requisition: 0012345",
                 "900 BUSINESS CENTER DRIVE, HORSHAM, PA 19044-3453", "Fasting: Yes", "COMPREHENSIVE METABOLIC PANEL"):
        assert lab_report.parse_line(line) is None, line


@pytest.mark.parametrize("line", ["Prediabetes: 5.7 - 6.4", "Diabetes: >6.4", "Glycemic control for adults with diabetes: <7.0", "Total Volume: Not Provided",
                                  "Please Note: 01", "A1c value of 6.5% or greater indicates that they may have diabetes.",
                                  "Riverside Family Clinic, 12 Sample Road, Springfield, IL 62704-1234   Phone: 555-555-0199", "Name: Jane   MRN: 88213"])
def test_notes_and_addresses_on_a_report_arent_results(line):
    assert lab_report.parse_text(line)["results"] == []


@pytest.mark.parametrize("line, test, value, text, unit, low, high", [
    ("CA 125      14      U/mL    0-35", "CA 125", 14, None, "U/mL", 0, 35),
    ("Troponin I      <0.012      <=0.034 ng/mL", "Troponin I", None, "<0.012", "ng/mL", None, 0.034),
    ("Troponin I      <0.012", "Troponin I", None, "<0.012", None, None, None),
    ("HIV Screen 4th Generation wRfx 01      Non Reactive      Non Reactive", "HIV Screen 4th Generation wRfx", None, "Non reactive", None, None, None),
    ("Urine Culture      No growth      No growth", "Urine Culture", None, "No growth", None, None, None),
    ("Eos 01      3      3      %      Not Estab.", "Eos", 3, None, "%", None, None),
    ("MCV01      91      fL      79-97", "MCV", 91, None, "fL", 79, 97),
    ("Monocytes °1      8      %      Not Estab.", "Monocytes", 8, None, "%", None, None),
    ("WBC O1      6.1      x10E3/uL      3.4-10.8", "WBC", 6.1, None, "x10E3/uL", 3.4, 10.8),
    ("MCH      30.1      27.0-33.0pg", "MCH", 30.1, None, "pg", 27.0, 33.0),
    ("HEMATOCRIT      41.2%      35.0-45.0", "HEMATOCRIT", 41.2, None, "%", 35.0, 45.0),
    ("Base Excess      -1.2      -2.0 - 2.0 mmol/L", "Base Excess", -1.2, None, "mmol/L", -2.0, 2.0),
])
def test_what_a_printed_line_says(line, test, value, text, unit, low, high):
    row = lab_report.parse_text(line)["results"][0]
    assert (row["test"], row["value"], row["value_text"], row["unit"], row["ref_low"], row["ref_high"]) == (test, value, text, unit, low, high)


@pytest.mark.parametrize("printed, unit", [("mmo1/1", "mmol/L"), ("mmoI/L", "mmol/L"), ("mg/dI", "mg/dL"), ("g/d1", "g/dL"), ("U/1", "U/L"), ("TU/L", "IU/L"), ("ng/mI", "ng/mL"),
                                           ("9/dL", "g/dL"), ("FL", "fL"), ("Mi1lion/uL", "Million/uL")])
def test_units_ocr_misreads_are_written_properly(printed, unit):
    row = lab_report.parse_text(f"Sodium      141      {printed}      136-145")["results"][0]
    assert row["unit"] == unit


@pytest.mark.parametrize("edge", ["|", "_", "__", "| _"])
def test_a_photographed_pages_edge_before_a_row_is_left_out(edge):
    # Tesseract reads the edge of a page photographed at an angle as marks at the start of its lines.
    row = lab_report.parse_text(f"   {edge}   GLUCOSE                    105 H          65-99 mg/dL")["results"][0]
    assert (row["test"], row["value"], row["unit"]) == ("GLUCOSE", 105, "mg/dL")
    assert lab_report.parse_text("GLUCOSE_FASTING      92      mg/dL      70-99")["results"][0]["test"] == "GLUCOSE_FASTING"


def test_a_unit_guessed_at_is_marked():
    row = lab_report.parse_text("Hemoglobin      14.2      g/aL      12.0-16.0")["results"][0]
    assert row["unit"] == "g/dL" and row["unsure"] == ["unit"]
    assert lab_report.parse_text("Sodium      141      mmol/L      136-145")["results"][0]["unsure"] == []


def test_results_side_by_side_are_two_results():
    out = lab_report.parse_text("Glucose     92     mg/dL     70-99          WBC     5.8     K/uL     4.5-11.0\n"
                                "BUN         14     mg/dL     7-20           Ketones      Negative      Negative")
    rows = {r["test"]: r for r in out["results"]}
    assert set(rows) == {"Glucose", "WBC", "BUN", "Ketones"}
    assert rows["WBC"]["value"] == 5.8 and rows["WBC"]["ref_high"] == 11.0 and rows["Ketones"]["value_text"] == "Negative"


def test_a_range_printed_on_the_next_line_belongs_to_the_row_above():
    out = lab_report.parse_text("LDL-CHOLESTEROL      106 H      mg/dL (calc)\n     Reference range: <100\nCREATININE   0.82   0.50-1.03 mg/dL")
    rows = {r["test"]: r for r in out["results"]}
    assert rows["LDL-CHOLESTEROL"]["ref_high"] == 100 and rows["CREATININE"]["ref_low"] == 0.5


def line(text, doubtful=()):
    return lab_report.Line(text, 0, 0.1, 0.11, frozenset(doubtful), True)


def test_rows_hard_to_read_say_which_part():
    rows = lab_report.parse_lines([line("Glucose      9Z5      mg/dL      70-99"), line("Glucose      95      mg/dL      70-99", ["95"]), line("Sodium      141      mmol/L      136-145", ["mmol/L"]),
                                   line("Calcium      9.4      mg/dL      8.6-1O.2", ["8.6-1O.2"])])["results"]
    marks = {r["test"]: r["unsure"] for r in rows}
    assert marks == {"Glucose": ["value"], "Sodium": ["unit"], "Calcium": ["range"]}
    assert rows[0]["where"] == {"page": 0, "top": 0.1, "bottom": 0.11}


def test_a_printed_flag_that_doesnt_fit_the_range_is_marked():
    rows = lab_report.parse_text("Glucose      95 H      mg/dL      70-99\nSodium      141      mmol/L      136-145")["results"]
    assert rows[0]["unsure"] == ["value"] and rows[1]["unsure"] == []


def test_a_lost_decimal_point_goes_back_where_the_range_puts_it_and_is_marked():
    rows = lab_report.parse_lines([line("Hemoglobin      136      g/dL      11.1-15.9"), line("Creatinine      82      mg/dL      0.50-1.03"), line("Glucose      136      mg/dL      70-99")])["results"]
    values = {r["test"]: (r["value"], r["unsure"]) for r in rows}
    assert values["Hemoglobin"] == (13.6, ["value"]) and values["Creatinine"] == (0.82, ["value"])
    assert values["Glucose"] == (136, [])       # out of range is a result, not a misreading: no place for a point fits
    # In a PDF's own text, a number is what it says.
    assert lab_report.parse_text("Hemoglobin      136      g/dL      11.1-15.9")["results"][0]["value"] == 136


def test_a_name_is_only_corrected_for_misread_letters():
    rows = [{"test": "HEMOGLOBIN A1¢", "unsure": []}, {"test": "BUN/Creatinine Ratio", "unsure": []}]
    lab_report._correct_ocr_names(rows)
    assert rows[0]["test"] == "Hemoglobin A1c" and rows[0]["unsure"] == ["test"]
    assert rows[1]["test"] == "BUN/Creatinine Ratio" and rows[1]["unsure"] == []


def test_a_pages_own_date_is_kept_when_a_file_holds_several_reports():
    out = lab_report.parse_lines([lab_report.Line("Collected: 09/12/2026", 0, 0.01, 0.02, frozenset(), False), lab_report.Line("Glucose      92      mg/dL      70-99", 0, 0.1, 0.11, frozenset(), False),
                                  lab_report.Line("Collected: 03/10/2025", 1, 0.01, 0.02, frozenset(), False), lab_report.Line("Sodium      141      mmol/L      136-145", 1, 0.1, 0.11, frozenset(), False)])
    rows = {r["test"]: r for r in out["results"]}
    assert out["collected"] == "2026-09-12" and rows["Glucose"]["date"] is None and rows["Sodium"]["date"] == "2025-03-10"


def two_columns() -> bytes:
    cells = [(40, 750, "Date Collected: Sep 12, 2026")]
    for i, (a, b) in enumerate([(("Glucose", "92", "mg/dL", "70-99"), ("WBC", "5.8", "K/uL", "4.5-11.0")), (("Sodium", "141", "mmol/L", "136-145"), ("Hemoglobin", "14.2", "g/dL", "12.0-16.0"))]):
        y = 700 - i * 14
        cells += [(x + dx, y, text) for x, row in ((40, a), (320, b)) for dx, text in zip((0, 90, 130, 190), row)]
    return pdf([cells])


def test_a_two_column_pdf_gives_up_every_result_and_says_where_each_is(client):
    out = client.post("/api/labs/extract", files={"file": ("two.pdf", two_columns(), "application/pdf")}).json()
    rows = by_test(out)
    assert set(rows) == {"GLUCOSE", "SODIUM", "WBC", "HEMOGLOBIN"} and out["collected"] == "2026-09-12"
    assert rows["WBC"]["value"] == 5.8 and rows["WBC"]["unit"] == "K/uL" and rows["WBC"]["ref_high"] == 11.0
    assert rows["GLUCOSE"]["where"]["page"] == 0 and rows["GLUCOSE"]["where"]["top"] == rows["WBC"]["where"]["top"]
    assert len(out["pages"]) == 1 and out["pages"][0]["image"].startswith("data:image/jpeg;base64,") and out["pages"][0]["height"] > out["pages"][0]["width"]


@pytest.mark.parametrize("turn", [0, 90, 180, 270])
@pytest.mark.parametrize("marked", [True, False])
def test_a_page_stored_turned_reads_the_same(turn, marked):
    """A landscape report stored sideways: the words run up the page, and it says how to turn it. Or, from some scanners, it doesn't say."""
    def spot(across, down):
        return {0: (across, 792 - down), 90: (down, across), 180: (612 - across, down), 270: (612 - down, 792 - across)}[turn]
    matrix = {0: "1 0 0 1", 90: "0 1 -1 0", 180: "-1 0 0 -1", 270: "0 -1 1 0"}[turn]
    rows = [(40, 100, "Glucose"), (200, 100, "92"), (260, 100, "mg/dL"), (330, 100, "70-99"), (40, 120, "Sodium"), (200, 120, "141"), (260, 120, "mmol/L"), (330, 120, "136-145"),
            (40, 160, "More text so that the page is not taken for a scan")]
    content = "BT /F1 9 Tf " + " ".join(f"{matrix} {' '.join(map(str, spot(a, d)))} Tm ({t}) Tj" for a, d, t in rows) + " ET"
    data = _with_content(content, turn if marked else 0)
    found = {r["test"]: r for r in lab_report.read(data, "application/pdf", "a.pdf")["results"]}
    assert found["Glucose"]["value"] == 92 and found["Sodium"]["ref_high"] == 145 and found["Glucose"]["unit"] == "mg/dL"


def _with_content(content: str, rotate: int) -> bytes:
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Rotate {rotate} /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
               f"<< /Length {len(content)} >>\nstream\n{content}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    out.write("".join(f"{o:010d} 00000 n \n" for o in offsets).encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def test_several_pages_each_keep_their_place():
    data = pdf([[(40, 700, "Glucose"), (200, 700, "92"), (260, 700, "mg/dL"), (330, 700, "70-99"), (40, 660, "Page one of this report has enough text to read")],
                [(40, 400, "Sodium"), (200, 400, "141"), (260, 400, "mmol/L"), (330, 400, "136-145"), (40, 360, "Page two of this report has enough text to read")]])
    out = lab_report.read(data, "application/pdf", "a.pdf")
    rows = {r["test"]: r for r in out["results"]}
    assert out["page_count"] == 2 and len(out["pages"]) == 2 and out["read_as"] == "text" and out["engine"] is None
    assert rows["Glucose"]["where"]["page"] == 0 and rows["Sodium"]["where"]["page"] == 1
    assert 0.08 < rows["Glucose"]["where"]["top"] < 0.13 and 0.46 < rows["Sodium"]["where"]["top"] < 0.51


def scanned_pdf() -> bytes:
    buf = io.BytesIO()
    report_image().save(buf, "PDF", resolution=200)
    return buf.getvalue()


def test_a_scan_without_a_reader_says_whats_missing(client, monkeypatch):
    monkeypatch.setattr(lab_report.ocr, "engine", lambda: None)
    monkeypatch.setattr(lab_report, "ocr_available", lambda: False)
    r = client.post("/api/labs/extract", files={"file": ("scan.pdf", scanned_pdf(), "application/pdf")})
    assert r.status_code == 400 and lab_report.install_advice() in r.json()["detail"]


def test_a_pdf_part_text_part_scan_reads_what_it_can_and_says_so(monkeypatch):
    from pypdf import PdfReader, PdfWriter

    monkeypatch.setattr(lab_report.ocr, "engine", lambda: None)
    writer = PdfWriter()
    for source in (two_columns(), scanned_pdf()):
        writer.append(PdfReader(io.BytesIO(source)))
    buf = io.BytesIO()
    writer.write(buf)
    out = lab_report.read(buf.getvalue(), "application/pdf", "a.pdf")
    assert {r["test"] for r in out["results"]} >= {"Glucose", "WBC"} and len(out["pages"]) == 2
    assert out["notes"] and out["notes"][0].startswith("Page 2 couldn't be read")


def test_more_pages_than_are_read_are_mentioned():
    page = [(40, 700, "Glucose"), (200, 700, "92"), (260, 700, "mg/dL"), (330, 700, "70-99"), (40, 660, "Enough text on this page to read it as a text page, not a scan")]
    out = lab_report.read(pdf([page] * (lab_report.MAX_PAGES + 2)), "application/pdf", "a.pdf")
    assert out["page_count"] == lab_report.MAX_PAGES + 2 and len(out["pages"]) == lab_report.MAX_PAGES and "first 30" in out["notes"][0]


def test_a_password_protected_pdf_says_so(client):
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(two_columns())))
    writer.encrypt("secret")
    buf = io.BytesIO()
    writer.write(buf)
    r = client.post("/api/labs/extract", files={"file": ("locked.pdf", buf.getvalue(), "application/pdf")})
    assert r.status_code == 400 and "password" in r.json()["detail"]


@pytest.mark.parametrize("head, declared, name, kind", [(b"%PDF-1.7 rest", "", "x", "application/pdf"), (b"\0\0\0\x18ftypheic....", "", "IMG.HEIC", "image/heic"),
                                                        (b"\xff\xd8\xff", "image/jpeg", "a.jpg", "image/jpeg"), (b"\xff\xd8\xff", "", "a.jpeg", "image/jpeg"),
                                                        (b"<svg>", "image/svg+xml", "a.svg", None), (b"hi", "text/plain", "a.txt", None)])
def test_what_a_file_is_goes_by_its_first_bytes(head, declared, name, kind):
    assert lab_report.file_type(head, declared, name) == kind


# ---- Saving: the original stays with the results


def save_report(client, data=None, name="quest.pdf", results=None, collected="2026-09-12", kind="application/pdf"):
    entry = {"collected": collected, "lab": "Quest Diagnostics", "results": results or [
        {"test": "Glucose", "loinc": "2345-7", "value": 105, "unit": "mg/dL", "ref_low": 65, "ref_high": 99, "flag": "H"},
        {"test": "Creatinine", "loinc": "2160-0", "value": 0.82, "unit": "mg/dL", "ref_low": 0.5, "ref_high": 1.03},
        {"test": "Hepatitis C antibody", "value_text": "Non-reactive", "ref_text": "Non-reactive"}]}
    return client.post("/api/labs/report", files={"file": (name, data or two_columns(), kind)}, data={"entry": json.dumps(entry)})


def labs(client):
    return {r["title"]: r for r in client.get("/api/records", params={"category": "labs"}).json()["items"]}


def test_saving_a_report_keeps_the_file_once_and_every_result_reaches_it(client):
    saved = save_report(client).json()
    assert saved["records"] == 3 and saved["report_id"]
    report = client.get(f"/api/records/{saved['report_id']}").json()
    assert report["category"] == "reports" and report["details"]["kind"] == "lab" and {r["title"] for r in report["results"]} == {"Glucose", "Creatinine", "Hepatitis C antibody"}
    file_id = report["files"][0]["id"]
    assert report["files"][0]["name"] == "quest.pdf" and report["files"][0]["content_type"] == "application/pdf"
    for row in labs(client).values():
        one = client.get(f"/api/records/{row['id']}").json()
        assert [f["id"] for f in one["files"]] == [file_id] and one["from_report"]["id"] == saved["report_id"]
        assert (one["details"] or {}).get("panel") is None          # a file's name isn't a panel: Trends would group by it
        opened = client.get(f"/api/records/{row['id']}/files/{file_id}")
        assert opened.status_code == 200 and opened.headers["content-type"] == "application/pdf" and opened.content == two_columns()
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM record_files").fetchone()[0] == 1


def test_a_saved_report_is_read_the_way_any_other_pdf_is(client):
    saved = save_report(client).json()
    report = client.get(f"/api/records/{saved['report_id']}").json()
    assert "Glucose" in (report.get("narrative") or report["details"].get("full_text") or "")


def test_results_from_a_report_are_in_trends_and_the_overviews_numbers_at_once(client):
    client.get("/api/observations", params={"category": "labs"})
    save_report(client)
    names = {i["title"] for i in client.get("/api/observations", params={"category": "labs"}).json()["items"]}
    assert {"Glucose", "Creatinine"} <= names
    glucose = labs(client)["Glucose"]
    assert glucose["value_num"] == 105 and glucose["unit"] == "mg/dL" and glucose["ref_high"] == 99 and glucose["interpretation"]
    assert labs(client)["Hepatitis C antibody"]["ref_text"] == "Non-reactive"


def test_the_same_report_saved_twice_goes_in_once_and_a_correction_updates_it(client):
    save_report(client)
    first = labs(client)
    again = save_report(client, results=[{"test": "Glucose", "loinc": "2345-7", "value": 106, "unit": "mg/dL"}, {"test": "Sodium", "value": 141, "unit": "mmol/L"}]).json()
    after = labs(client)
    assert set(after) == set(first) | {"Sodium"} and after["Glucose"]["value_num"] == 106 and again["records"] == 2
    report = client.get(f"/api/records/{again['report_id']}").json()
    assert {r["title"] for r in report["results"]} == set(after), "the report still lists the results it had"
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM record_files").fetchone()[0] == 1


def test_one_test_twice_in_a_report_is_two_results(client):
    save_report(client, results=[{"test": "Glucose", "value": 92, "unit": "mg/dL"}, {"test": "Glucose", "value_text": "Negative"}])
    assert client.get("/api/records", params={"category": "labs", "q": "Glucose", "dedupe": "false"}).json()["total"] == 2


def test_an_original_belongs_to_its_own_results(client):
    saved = save_report(client).json()
    file_id = client.get(f"/api/records/{saved['report_id']}").json()["files"][0]["id"]
    client.post("/api/labs", json={"collected": "2026-01-01", "results": [{"test": "Ferritin", "value": 40}]})
    other = labs(client)["Ferritin"]
    assert client.get(f"/api/records/{other['id']}/files/{file_id}").status_code == 404
    assert client.get(f"/api/records/{other['id']}/files/{file_id}/pages").status_code == 404


def test_a_file_can_be_shown_as_pictures_where_it_cant_be_opened(client):
    saved = save_report(client).json()
    file_id = client.get(f"/api/records/{saved['report_id']}").json()["files"][0]["id"]
    glucose = labs(client)["Glucose"]
    pages = client.get(f"/api/records/{glucose['id']}/files/{file_id}/pages").json()["pages"]
    assert len(pages) == 1 and pages[0]["image"].startswith("data:image/jpeg;base64,")


def test_a_photo_is_kept_as_what_it_is(client):
    buf = io.BytesIO()
    report_image().save(buf, "JPEG", quality=70)
    saved = save_report(client, data=buf.getvalue(), name="IMG_1.jpg", kind="image/jpeg").json()
    found = client.get(f"/api/records/{saved['report_id']}").json()["files"][0]
    assert found["content_type"] == "image/jpeg" and found["state"] == "image"


def test_a_report_goes_in_exports(client):
    saved = save_report(client).json()
    file_id = client.get(f"/api/records/{saved['report_id']}").json()["files"][0]["id"]
    from app.services import exports
    from app.store import profiles

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        assert exports._write_documents(z, profiles.resolve(None)["id"]) == 1
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
        assert z.read(f"documents/{file_id}.pdf") == two_columns()
        assert json.loads(z.read("documents.json"))[0]["name"] == "quest.pdf"


def test_deleting_a_report_takes_its_file_and_leaves_its_results(client):
    saved = save_report(client).json()
    assert client.delete(f"/api/labs/{saved['report_id']}").status_code == 200
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM record_files").fetchone()[0] == 0
    left = labs(client)
    assert "Glucose" in left
    one = client.get(f"/api/records/{left['Glucose']['id']}").json()
    assert one["files"] == [] and "from_report" not in one


def test_reports_that_cant_be_kept_are_refused(client):
    assert save_report(client, data=b"hello", name="a.txt", kind="text/plain").status_code == 400
    assert client.post("/api/labs/report", files={"file": ("a.pdf", two_columns(), "application/pdf")}, data={"entry": "{not json"}).status_code == 400
    future = save_report(client, collected="2099-01-01")
    assert future.status_code == 400 and "future" in future.json()["detail"]
    too_big = b"%PDF-1.4" + b"0" * lab_report.MAX_BYTES
    assert save_report(client, data=too_big).status_code == 400
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM record_files").fetchone()[0] == 0


def test_a_report_can_be_as_large_as_a_read_one():
    from app.core import uploads

    assert uploads.body_limit("/api/labs/report") == uploads.body_limit("/api/labs/extract") > lab_report.MAX_BYTES


# ---- What's already on file


def check(client, *rows, collected="2026-09-12"):
    return client.post("/api/labs/on-file", json={"collected": collected, "results": list(rows)}).json()["results"]


def test_results_already_on_file_for_that_test_and_day_are_pointed_out(client):
    client.post("/api/labs", json={"collected": "2026-09-12", "lab": "Earlier", "results": [
        {"test": "Glucose", "loinc": "2345-7", "value": 105, "unit": "mg/dL"}, {"test": "Ferritin", "value": 11, "unit": "ng/mL"}, {"test": "Ketones", "value_text": "Negative"},
        {"test": "Troponin I", "value_text": "<0.012", "unit": "ng/mL"}]})
    same, other, words, limit, new = check(client, {"test": "GLUCOSE, SERUM", "loinc": "2345-7", "value": "105"}, {"test": "FERRITIN", "value": "9"}, {"test": "ketones", "value": "negative"},
                                           {"test": "Troponin I", "value": "<0.012"}, {"test": "Sodium", "value": "141"})
    assert same["same"] is True and same["value"] == "105 mg/dL" and same["source"] == "Earlier"
    assert other["same"] is False and other["value"] == "11 ng/mL"
    assert words["same"] is True and limit["same"] is True and new is None


def test_another_day_isnt_on_file(client):
    client.post("/api/labs", json={"collected": "2026-09-12", "results": [{"test": "Glucose", "value": 105}]})
    assert check(client, {"test": "Glucose", "value": "105"}, collected="2026-09-13") == [None]
    assert check(client, {"test": "Glucose", "value": "105", "date": "2026-09-12"}, collected="2026-09-13")[0]["same"] is True


def test_someone_elses_results_arent_on_file(client):
    other = client.post("/api/profiles", json={"name": "Sam"}).json()
    client.post("/api/labs", json={"profile_id": other["id"], "collected": "2026-09-12", "results": [{"test": "Glucose", "value": 105}]})
    assert check(client, {"test": "Glucose", "value": "105"}) == [None]


# ---- Read with AI stays a choice


def test_the_pages_are_drawn_here_whoever_reads_it_and_a_heic_goes_as_a_jpeg(client, monkeypatch):
    sent = {}

    async def read_it(data, content_type, filename, subject):
        sent.update(data=data, content_type=content_type, filename=filename)
        return {"results": [{"test": "Glucose", "value": 92}], "read_as": "image"}
    monkeypatch.setattr(lab_report_api.ai, "extract_lab_results", read_it)
    monkeypatch.setattr(lab_report, "_heic_to_jpeg", lambda data: JPEG)
    monkeypatch.setattr(lab_report_api.lab_report, "_open_images", lambda data, kind: [report_image()])
    heic = b"\0\0\0\x18ftypheic" + b"\0" * 20
    out = client.post("/api/labs/extract", files={"file": ("IMG_1.HEIC", heic, "image/heic")}, data={"method": "ai"}).json()
    assert sent == {"data": JPEG, "content_type": "image/jpeg", "filename": "IMG_1.jpg"}
    assert out["results"][0]["test"] == "Glucose" and len(out["pages"]) == 1


def test_reading_on_this_computer_sends_nothing_anywhere(client, monkeypatch):
    def nothing(*a, **k):
        raise AssertionError("Reading locally must not use an AI or the network")
    monkeypatch.setattr(lab_report_api.ai, "extract_lab_results", nothing)
    monkeypatch.setattr(httpx.AsyncClient, "send", nothing)
    r = client.post("/api/labs/extract", files={"file": ("two.pdf", two_columns(), "application/pdf")})
    assert r.status_code == 200 and r.json()["results"]


def test_reading_with_ai_without_agreeing_is_refused_and_nothing_is_sent(client, monkeypatch):
    monkeypatch.setattr(lab_report_api.ai, "config", lambda include_key=False: {"configured": True, "provider": "claude", "sends_elsewhere": True, "vendor_key": "anthropic", "vendor_name": "Anthropic"})
    monkeypatch.setattr(lab_report_api.ai, "cloud_acknowledged", lambda *a, **k: False)
    monkeypatch.setattr(httpx.AsyncClient, "send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Sent without agreement")))
    r = client.post("/api/labs/extract", files={"file": ("two.pdf", two_columns(), "application/pdf")}, data={"method": "ai"})
    assert r.status_code == 400


def test_what_a_report_says_stays_factual(client):
    """Values, units, dates, the printed range and whether a value is outside it, and no more: the words the dialog can show."""
    source = open(lab_entry_js()).read()
    for word in ("should", "concern", "risk", "recommend", "consider", "healthy", "worrying", "see your doctor", "abnormal"):
        assert word not in source.lower(), word
    assert "Above the printed range" in source and "Below the printed range" in source


def lab_entry_js():
    from app.core import config

    return config.APP_DIR / "static" / "js" / "views" / "lab-entry.js"


def test_reads_pdf_text_layer(client):
    files = {"file": ("quest.pdf", quest_pdf(), "application/pdf")}
    out = client.post("/api/labs/extract", files=files).json()
    assert out["read_as"] == "text" and out["collected"] == "2026-09-12" and out["lab"] == "Quest Diagnostics"
    rows = by_test(out)
    assert len(rows) == len(QUEST_ROWS)
    assert rows["GLUCOSE"]["value"] == 105 and rows["GLUCOSE"]["flag"] == "H" and rows["GLUCOSE"]["loinc"] == "2345-7"
    assert rows["EGFR"]["ref_low"] == 60 and rows["EGFR"]["ref_high"] is None
    assert rows["HEMOGLOBIN A1C"]["ref_high"] == 5.7 and rows["HEMOGLOBIN A1C"]["unit"] == "%"
    assert rows["VITAMIN D,25-OH,TOTAL,IA"]["loinc"] == "1989-3"
    assert rows["HEPATITIS C ANTIBODY"]["value_text"] == "Non-reactive"
    # The rows save through the normal lab entry endpoint and land in the record.
    results = [{k: v for k, v in r.items() if k != "ref_text"} for r in out["results"]]
    saved = client.post("/api/labs", json={"collected": out["collected"], "lab": out["lab"], "results": results})
    assert saved.status_code == 200 and saved.json()["records"] == len(QUEST_ROWS)
    events = client.get("/api/audit").json()["events"]
    assert any(e["action"] == "labs.report_read" and '"local"' in e["detail"] for e in events)


def test_rejects_other_files(client):
    assert client.post("/api/labs/extract", files={"file": ("a.txt", b"hi", "text/plain")}).status_code == 400
    assert client.post("/api/labs/extract", files={"file": ("a.pdf", b"not a pdf", "application/pdf")}).status_code == 400
    bad = client.post("/api/labs/extract", files={"file": ("a.pdf", quest_pdf(), "application/pdf")}, data={"method": "x"})
    assert bad.status_code == 400


def test_photo_without_a_reader_explains(client, monkeypatch):
    monkeypatch.setattr(lab_report, "ocr_available", lambda: False)
    monkeypatch.setattr(lab_report.ocr, "engine", lambda: None)
    buf = io.BytesIO()
    report_image().save(buf, "JPEG")
    r = client.post("/api/labs/extract", files={"file": ("photo.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 400 and "Tesseract" in r.json()["detail"]
    reader = client.get("/api/labs/reader").json()
    assert reader["ocr"] is False and reader["install"] == lab_report.install_advice()


@pytest.mark.parametrize("platform,words", [("darwin", "brew install tesseract"), ("win32", "winget install"), ("linux", "apt install")])
def test_advice_fits_the_computer(monkeypatch, platform, words):
    monkeypatch.setattr(lab_report.sys, "platform", platform)
    assert words in lab_report.install_advice()


def test_tesseract_is_found_outside_a_short_path(monkeypatch, tmp_path):
    # Apps started from the Dock get a short PATH, without /opt/homebrew/bin, where following the advice puts it.
    from app.services import ai_agents
    folder = tmp_path / "homebrew-bin"
    folder.mkdir()
    program = folder / ("tesseract.exe" if sys.platform == "win32" else "tesseract")
    program.write_text("#!/bin/sh\n")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(ai_agents, "_search_dirs", lambda: ["/usr/bin"])
    if lab_report.tesseract_path():
        pytest.skip("this computer has it in /usr/bin")
    monkeypatch.setattr(ai_agents, "_search_dirs", lambda: ["/usr/bin", str(folder)])
    assert os.path.normcase(lab_report.tesseract_path()) == os.path.normcase(str(program))
    assert lab_report.ocr_available()


@needs_ocr
def test_reads_sideways_phone_photo(client):
    # Stored rotated, with the EXIF orientation a phone writes, so it only reads correctly once turned upright.
    img = report_image().rotate(90, expand=True)
    exif = Image.Exif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85, exif=exif)
    out = client.post("/api/labs/extract", files={"file": ("IMG_0412.jpg", buf.getvalue(), "image/jpeg")}).json()
    assert out["read_as"] == "ocr" and out["collected"] == "2026-09-12"
    rows = by_test(out)
    assert rows["GLUCOSE"]["value"] == 105 and rows["GLUCOSE"]["ref_high"] == 99
    assert rows["CREATININE"]["value"] == 0.82
    assert len(rows) >= len(QUEST_ROWS) - 2


@needs_ocr
def test_reads_scanned_pdf(client):
    buf = io.BytesIO()
    report_image().save(buf, "PDF", resolution=200)    # an image-only PDF, like a scanner makes
    out = client.post("/api/labs/extract", files={"file": ("scan.pdf", buf.getvalue(), "application/pdf")}).json()
    assert out["read_as"] == "ocr"
    assert by_test(out)["GLUCOSE"]["value"] == 105
