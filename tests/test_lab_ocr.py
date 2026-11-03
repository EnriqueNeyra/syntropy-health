"""Reading a page on this computer: what comes out of each OCR engine, and turning it into lines. The tests marked needs_ocr use the real engine,
whichever this computer has; CI runs this file on the Mac and Windows apps' runners, so each engine gets used where it lives."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.services import lab_report, ocr
from app.services.ocr import Word
from tests.test_lab_report import report_image

needs_ocr = pytest.mark.skipif(not ocr.available(), reason="No OCR engine on this computer")


def word(text, left, top, conf=1.0, width=None, height=10):
    return Word(text, left, top, left + (width or len(text) * 6), top + height, conf)


# ---- From words to lines


def test_words_stay_on_their_printed_lines_on_a_page_shot_at_an_angle():
    # Each row climbs across the page by more than half a line, so a fixed height across the page would mix it with the next.
    words = []
    for row, name in enumerate(("Glucose", "Sodium", "Calcium")):
        top = 100 + row * 22
        words += [word(name, 10, top), word("92", 200, top + 4), word("mg/dL", 300, top + 7), word("70-99", 400, top + 11)]
    lines = ocr.lay_out(words, 0, 1000)
    assert [line.text.split()[0] for line in lines] == ["Glucose", "Sodium", "Calcium"]
    assert all(line.text.split()[1:] == ["92", "mg/dL", "70-99"] for line in lines)


def test_a_gap_on_the_page_is_a_gap_in_the_text():
    line = ocr.lay_out([word("Glucose", 10, 100), word("92", 300, 100)], 0, 1000)[0]
    assert "  " in line.text.strip()
    close = ocr.lay_out([word("Hemoglobin", 10, 100), word("A1c", 72, 100)], 0, 1000)[0]
    assert close.text.strip() == "Hemoglobin A1c"


def test_a_line_knows_where_it_is_and_what_the_reader_doubted():
    line = ocr.lay_out([word("Glucose", 10, 100), word("9Z", 300, 100, conf=0.2)], 2, 1000, unsure_below=0.5)[0]
    assert (line.page, line.top, line.bottom) == (2, 0.1, 0.11)
    assert line.doubtful == frozenset({"9Z"})


def test_letters_that_only_look_latin_are_made_latin():
    assert ocr.lay_out([word("СВС", 10, 100)], 0, 1000)[0].text.strip() == "CBC"


def test_the_looks_vote_and_a_word_few_agree_on_is_doubtful():
    looks = [[word("15-150", 100, 100)], [word("091-91", 100, 100)], [word("15-150", 101, 100)], [word("15-150", 99, 101)]]
    out = ocr._vote(looks)
    assert [(w.text, w.conf) for w in out] == [("15-150", 1.0)]
    alone = ocr._vote([[word("3", 100, 100, width=8)], [], [word("7", 300, 100)], []])
    assert sorted(w.text for w in alone) == ["3", "7"] and all(w.conf < 0.5 for w in alone)


def test_a_look_that_misses_a_word_doesnt_lose_it():
    out = ocr._vote([[word("Eos", 10, 100)], [word("Eos", 10, 100), word("3", 200, 100, width=8)], [word("3", 200, 100, width=8)]])
    assert sorted(w.text for w in out) == ["3", "Eos"]


def test_a_word_two_bands_saw_is_not_two_votes():
    out = ocr._vote([[word("Glucose", 10, 100), word("Glucose", 11, 100)], [word("Glucosc", 10, 100)]])
    assert [w.conf for w in out] == [0.3]


# ---- Engines


def test_tesseract_words_come_with_their_places(monkeypatch):
    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
           "4\t1\t1\t1\t1\t0\t10\t20\t300\t30\t-1\t\n"
           "5\t1\t1\t1\t1\t1\t10\t20\t80\t30\t96.5\tGlucose\n"
           "5\t1\t1\t1\t1\t2\t200\t20\t30\t30\t41\t\"105\n")
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout=tsv.encode(), stderr=b"")
    monkeypatch.setattr(ocr.subprocess, "run", run)
    words = ocr._tesseract_words(Image.new("L", (100, 100), 255))
    assert seen["cmd"][-1] == "tsv"
    assert words == [Word("Glucose", 10, 20, 90, 50, 0.965), Word('"105', 200, 20, 230, 50, 0.41)]


def test_tesseract_failing_or_hanging_says_so(monkeypatch):
    monkeypatch.setattr(ocr.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"", stderr=b"broken"))
    with pytest.raises(ocr.OcrError):
        ocr._tesseract_words(Image.new("L", (10, 10), 255))

    def hang(*a, **k):
        raise subprocess.TimeoutExpired("tesseract", 1)
    monkeypatch.setattr(ocr.subprocess, "run", hang)
    with pytest.raises(ocr.OcrError):
        ocr._tesseract_words(Image.new("L", (10, 10), 255))


def test_windows_words_come_from_powershell_and_the_image_is_cleaned_up(monkeypatch):
    # The script itself runs on Windows: CI's Windows runner reads a real page below. This checks what's done with its answer.
    kept = {}

    def run(script, env=None, timeout=90):
        kept["script"], kept["path"] = script, env["SYNTROPY_OCR_IMAGE"]
        assert Image.open(env["SYNTROPY_OCR_IMAGE"]).size == (10, 10)
        return SimpleNamespace(returncode=0, stderr=b"", stdout="10\t20\t80\t30\tGlucose\r\n200,5\t20\t30\t30\t105\r\nnot a word line\r\n".encode())
    monkeypatch.setattr(ocr, "_windows", run)
    words = ocr._windows_words(Image.new("L", (10, 10), 255))
    assert "Windows.Media.Ocr.OcrEngine" in kept["script"] and "SYNTROPY_OCR_IMAGE" in kept["script"]
    assert words == [Word("Glucose", 10, 20, 90, 50, None), Word("105", 200.5, 20, 230.5, 50, None)]
    assert not os.path.exists(kept["path"])


def test_the_windows_script_ends_with_an_empty_line(monkeypatch):
    # PowerShell runs a block that spans lines, read from standard input, only once an empty line follows it.
    sent = {}
    monkeypatch.setattr(ocr.subprocess, "run", lambda cmd, **k: sent.update(cmd=cmd, input=k["input"]))
    ocr._windows(ocr._WINDOWS_READ)
    assert sent["cmd"][-2:] == ["-Command", "-"] and sent["input"].endswith(b"}\n\n")


@pytest.mark.parametrize("vision, windows, path, want", [(True, False, None, "vision"), (False, True, None, "windows"), (False, False, "/x/tesseract", "tesseract"),
                                                         (False, False, None, None), (True, False, "/x/tesseract", "vision")])
def test_the_computers_own_reader_comes_first(monkeypatch, vision, windows, path, want):
    monkeypatch.setattr(ocr, "_vision_ready", lambda: vision)
    monkeypatch.setattr(ocr, "_windows_ready", lambda: windows)
    monkeypatch.setattr(ocr, "tesseract_path", lambda: path)
    assert ocr.engine() == want
    assert ocr.available() is (want is not None)


def test_windows_without_a_language_says_how_to_add_one(monkeypatch):
    monkeypatch.setattr(ocr.sys, "platform", "win32")
    monkeypatch.setattr(ocr, "_windows_ok", False)
    monkeypatch.setattr(ocr, "_windows", lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"", stderr=b""))
    assert ocr._windows_ready() is False
    assert "language" in ocr.install_advice() and "winget install" in ocr.install_advice()


# ---- The real engine, whichever this computer has


def _rows(out, img) -> dict:
    """The rows read, by name. Without the one these pages always have, says what the engine saw, since each engine reads a
    page its own way and CI is where the others are."""
    rows = {r["test"].upper(): r for r in out["results"]}
    if "GLUCOSE" not in rows:
        prepared, _ = lab_report._prepare(img, ocr.engine())        # as the app reads it
        seen = "\n".join(line.text for line in ocr.lay_out(ocr.read_words(prepared), 0, prepared.height, read_by_ocr=True))
        pytest.fail(f"No glucose row. Rows: {sorted(rows)}\nThe {ocr.engine()} engine read:\n{seen}")
    return rows


@needs_ocr
def test_reads_a_photo_at_a_slight_angle_with_a_shadow(client):
    img = report_image().rotate(2.5, expand=True, fillcolor=(190, 180, 160), resample=Image.BICUBIC)
    shade = Image.linear_gradient("L").resize(img.size).point(lambda v: 150 + v * 105 // 255)
    img = Image.composite(img, Image.new("RGB", img.size, "black"), shade)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    out = client.post("/api/labs/extract", files={"file": ("IMG_0412.jpg", buf.getvalue(), "image/jpeg")}).json()
    assert out["read_as"] == "ocr" and out["engine"] == ocr.engine()
    rows = _rows(out, img)
    assert rows["GLUCOSE"]["value"] == 105 and rows["CREATININE"]["value"] == 0.82
    # Its place on the picture came back, turned as it was read.
    assert out["pages"][0]["image"].startswith("data:image/jpeg;base64,") and 0 < rows["GLUCOSE"]["where"]["top"] < rows["CREATININE"]["where"]["top"] < 1


@needs_ocr
def test_reads_every_page_of_a_scanned_pdf_and_keeps_each_rows_page(client):
    first = report_image()
    second = Image.new("RGB", first.size, "white")
    draw = ImageDraw.Draw(second)
    draw.text((60, 60), "TSH    2.10    0.40-4.50 mIU/L", fill="black", font=ImageFont.load_default(size=30))
    draw.text((60, 120), "FERRITIN    9 L    16-232 ng/mL", fill="black", font=ImageFont.load_default(size=30))
    buf = io.BytesIO()
    first.save(buf, "PDF", save_all=True, append_images=[second], resolution=200)
    out = client.post("/api/labs/extract", files={"file": ("scan.pdf", buf.getvalue(), "application/pdf")}).json()
    assert out["read_as"] == "ocr" and out["page_count"] == 2 and len(out["pages"]) == 2
    rows = _rows(out, first)
    assert rows["GLUCOSE"]["where"]["page"] == 0 and rows["TSH"]["where"]["page"] == 1
    assert rows["TSH"]["value"] == 2.1 and rows["FERRITIN"]["value"] == 9


@needs_ocr
def test_reads_a_scanners_black_and_white_pdf(client):
    buf = io.BytesIO()
    page = report_image().convert("1")
    page.save(buf, "PDF", resolution=200)
    out = client.post("/api/labs/extract", files={"file": ("bw.pdf", buf.getvalue(), "application/pdf")}).json()
    assert _rows(out, page.convert("RGB"))["GLUCOSE"]["value"] == 105


@needs_ocr
def test_reads_an_iphone_photo(client):
    if sys.platform != "darwin":
        pytest.importorskip("pillow_heif")
    try:
        import pillow_heif
    except ImportError:
        pytest.skip("Nothing here makes a HEIC to read")
    buf = io.BytesIO()
    pillow_heif.from_pillow(report_image()).save(buf, quality=80)
    # Browsers send no type for one, and it's read as what it is.
    out = client.post("/api/labs/extract", files={"file": ("IMG_0413.HEIC", buf.getvalue(), "application/octet-stream")}).json()
    assert {r["test"].upper(): r for r in out["results"]}["GLUCOSE"]["value"] == 105
    assert lab_report.file_type(buf.getvalue(), "", "IMG_0413.HEIC") == "image/heic"
