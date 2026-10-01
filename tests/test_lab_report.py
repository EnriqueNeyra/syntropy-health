"""Reading lab reports on this machine: PDF text layers, and Tesseract OCR for photos and scans."""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.services import lab_report

needs_ocr = pytest.mark.skipif(not lab_report.ocr_available(), reason="Tesseract isn't installed")

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


def test_photo_without_tesseract_explains(client, monkeypatch):
    monkeypatch.setattr(lab_report, "ocr_available", lambda: False)
    buf = io.BytesIO()
    report_image().save(buf, "JPEG")
    r = client.post("/api/labs/extract", files={"file": ("photo.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 400 and "Tesseract" in r.json()["detail"]
    assert client.get("/api/labs/reader").json() == {"ocr": False}


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
