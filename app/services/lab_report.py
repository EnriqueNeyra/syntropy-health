"""Reads lab results from a report on this machine, without AI or any network call: the text in a PDF from a lab's website, or
what the computer's own OCR reads from photos and scanned pages (see ocr.py). Everything keeps its place on the page, so columns
stay apart, each row can be pointed out on the original, and the person reviews every row beside it before saving."""

from __future__ import annotations

import base64
import difflib
import io
import logging
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from functools import lru_cache
from typing import Any, NamedTuple, Optional

from app.services import ocr
from app.services.ocr import Line, OcrError, Word, install_advice, tesseract_path  # noqa: F401 - part of this module's answers
from app.store import lab_catalog

log = logging.getLogger("syntropy.lab_report")

MAX_BYTES = 15 * 1024 * 1024
MAX_PAGES = 30
OCR_WORKERS = 4
PREVIEW_WIDTH = 1240        # sharp enough to check a decimal point against, small enough to send to a phone
PREVIEW_QUALITY = 72
LONGEST_SIDE = 2000.0       # a hostile page can declare any size: rendering is scaled down to fit within this many points
MIN_TEXT = 40               # a page with less than this in its text layer is a scan
STAMP_TEXT = 200            # with less than this and a picture over most of the page, likewise: a fax header on a scan
HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1")
UNSURE_BELOW = {"vision": 0.5, "tesseract": 0.6}
_pdfium = threading.Lock()  # PDFium can't be used from two threads at once, even on different documents


class ReportError(Exception):
    pass


def ocr_available() -> bool:
    return ocr.available()


# ---------------------------------------------------------------------------
# Getting the pages out of the file
# ---------------------------------------------------------------------------

def file_type(data: bytes, content_type: str, filename: str) -> Optional[str]:
    """What the file is, from its first bytes where they say: browsers send no type for a .heic, and a wrong one for many scans."""
    name = filename.lower()
    if data[:1024].lstrip().startswith(b"%PDF-"):
        return "application/pdf"
    if data[4:8] == b"ftyp" and data[8:12] in HEIC_BRANDS:
        return "image/heic"
    if content_type == "application/pdf" or name.endswith(".pdf"):
        return "application/pdf"
    if content_type.startswith("image/") and "svg" not in content_type:
        return "image/heic" if content_type in ("image/heic", "image/heif") else content_type
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".tif": "image/tiff", ".tiff": "image/tiff", ".webp": "image/webp",
            ".heic": "image/heic", ".heif": "image/heic"}.get(os.path.splitext(name)[1])


def _flatten(img: Any) -> Any:
    """Evens out shadows and uneven light: subtracts a blurred copy of the paper, leaving dark text on white."""
    from PIL import ImageChops, ImageFilter, ImageOps

    paper = img.filter(ImageFilter.MaxFilter(15)).filter(ImageFilter.GaussianBlur(25))
    return ImageOps.autocontrast(ImageOps.invert(ImageChops.subtract(paper, img)), cutoff=1)


def _skew(img: Any) -> float:
    """The angle that lines the text rows up horizontally: rows of text and the gaps between them then give the
    most uneven row darkness."""
    from PIL import Image

    small = img.resize((600, max(1, round(img.height * 600 / img.width)))).point(lambda v: 255 if v < 140 else 0)

    def unevenness(angle: float) -> float:
        rows = small.rotate(angle, expand=False, resample=Image.BILINEAR).resize((1, small.height), Image.BOX).tobytes()
        mean = sum(rows) / len(rows)
        return sum((r - mean) ** 2 for r in rows)

    best = max((a / 2 for a in range(-20, 21)), key=unevenness)                # -10° to 10° in 0.5° steps
    return max((best + a / 10 for a in range(-4, 5)), key=unevenness)


def _prepare(image: Any, used: Optional[str]) -> tuple[Any, float]:
    """A page image the way this computer's OCR reads it best, and how far it was turned to straighten it."""
    from PIL import Image, ImageFilter, ImageOps

    def at_width(img: Any, width: int) -> Any:
        return img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)

    img = ImageOps.grayscale(image)
    # Reads best at print resolution: enlarge small images, shrink huge ones so it stays quick.
    if img.width < 1800 or img.width > 4000:
        img = at_width(img, 1800 if img.width < 1800 else 4000)
    angle = _skew(_flatten(at_width(img, 1200)))
    # Tesseract needs a blurred photo sharpened and its shadows gone. Vision needs the opposite: on the flattened page it found 5 values
    # of 22, where the page as it was gave all 22. So it (and Windows' OCR, also made for camera images) gets the page as it was.
    if used == "tesseract":
        img = _flatten(at_width(img, 2400).filter(ImageFilter.UnsharpMask(radius=3, percent=180, threshold=2)))
    if abs(angle) < 0.3:
        return img, 0.0
    return img.rotate(angle, expand=True, fillcolor=255, resample=Image.BICUBIC), angle


def _preview(image: Any, angle: float = 0.0) -> dict[str, Any]:
    """The page as a small JPEG: shown beside the rows it was read into, in every browser and web view, none of which can be
    relied on to show a PDF or a HEIC. Turned as OCR turned it, so a row's place on the page lines up."""
    from PIL import Image

    img = image.convert("RGB")
    if img.width > PREVIEW_WIDTH:
        img = img.resize((PREVIEW_WIDTH, round(img.height * PREVIEW_WIDTH / img.width)), Image.LANCZOS)
    if angle:
        img = img.rotate(angle, expand=True, fillcolor=(255, 255, 255), resample=Image.BICUBIC)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=PREVIEW_QUALITY, optimize=True)
    return {"image": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(), "width": img.width, "height": img.height}


def _heic_from_macos(data: bytes) -> Optional[bytes]:
    """macOS reads HEIC itself, so the Mac app needs no decoder of its own."""
    if sys.platform != "darwin":
        return None
    try:
        import Foundation
        import objc
        import Quartz
    except ImportError:
        return None
    with objc.autorelease_pool():
        source = Quartz.CGImageSourceCreateWithData(Foundation.NSData.dataWithBytes_length_(data, len(data)), None)
        picture = source and Quartz.CGImageSourceCreateThumbnailAtIndex(source, 0, {
            Quartz.kCGImageSourceCreateThumbnailFromImageAlways: True, Quartz.kCGImageSourceCreateThumbnailWithTransform: True,
            Quartz.kCGImageSourceThumbnailMaxPixelSize: 8000})
        if not picture:
            return None
        out = Foundation.NSMutableData.data()
        target = Quartz.CGImageDestinationCreateWithData(out, "public.jpeg", 1, None)
        Quartz.CGImageDestinationAddImage(target, picture, {Quartz.kCGImageDestinationLossyCompressionQuality: 0.92})
        return bytes(out) if Quartz.CGImageDestinationFinalize(target) else None


def _heic_to_jpeg(data: bytes) -> bytes:
    if found := _heic_from_macos(data):
        return found
    try:
        import pillow_heif

        img = pillow_heif.open_heif(data).to_pillow()
    except Exception as exc:  # noqa: BLE001 - not installed, or a damaged or unusual file
        raise ReportError("Couldn't open that HEIC photo. Save it as JPEG or PNG and try again.") from exc
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92)
    return buf.getvalue()


def for_ai(data: bytes, content_type: str, filename: str) -> tuple[bytes, str, str]:
    """What's sent when the person chooses an outside AI, which won't take a HEIC."""
    if file_type(data, content_type, filename) != "image/heic":
        return data, content_type, filename
    return _heic_to_jpeg(data), "image/jpeg", os.path.splitext(filename)[0] + ".jpg"


def _open_images(data: bytes, kind: str) -> list[Any]:
    """The pages of an image file, upright: phone photos are often stored sideways, and a scanner's TIFF can hold many pages."""
    from PIL import Image, ImageOps, ImageSequence

    try:
        img = Image.open(io.BytesIO(_heic_to_jpeg(data) if kind == "image/heic" else data))
        return [ImageOps.exif_transpose(frame.convert("RGB")) for frame in ImageSequence.Iterator(img)][:MAX_PAGES]
    except ReportError:
        raise
    except Exception as exc:  # noqa: BLE001 - unusual and damaged files
        raise ReportError("Couldn't open that image. Save it as JPEG or PNG and try again.") from exc


def _reading_turn(boxes: list[tuple[float, ...]]) -> int:
    """How far a page's text is turned, from which way its letters run: a landscape report stored sideways runs up the page."""
    votes = {0: 0, 90: 0, 180: 0, 270: 0}
    for a, b in zip(boxes, boxes[1:]):
        across, up = b[0] - a[0], b[1] - a[1]
        if abs(across) > abs(up):
            votes[0 if across > 0 else 180] += 1
        elif up:
            votes[90 if up > 0 else 270] += 1
    return max(votes, key=lambda turn: votes[turn])


def _pdf_words(page: Any) -> tuple[list[Word], float]:
    """The words in a page's text layer, placed so they read from left to right, and how tall the page is that way up."""
    import pypdfium2.raw as pdfium

    text = page.get_textpage()
    left, bottom, right, top = page.get_bbox()
    letters = []
    for i in range(text.count_chars()):
        ch = chr(pdfium.FPDFText_GetUnicode(text.raw, i) or 32)
        # The letter's whole width: a tight box leaves a gap around a ".".
        letters.append((ch, *text.get_charbox(i, loose=True), pdfium.FPDFText_GetFontSize(text.raw, i)) if not ch.isspace() and ord(ch) >= 32 else (" ",))
    text.close()
    turn = _reading_turn([found[1:5] for found in letters if len(found) > 1])

    def place(l: float, b: float, r: float, t: float) -> tuple[float, float, float, float]:
        if turn == 90:
            return b - bottom, l - left, t - bottom, r - left
        if turn == 180:
            return right - r, b - bottom, right - l, t - bottom
        if turn == 270:
            return top - t, right - r, top - b, right - l
        return l - left, top - t, r - left, top - b

    words: list[Word] = []
    current: list[tuple[str, float, float, float, float]] = []

    def finish() -> None:
        if current:
            words.append(Word("".join(c[0] for c in current), min(c[1] for c in current), min(c[2] for c in current),
                              max(c[3] for c in current), max(c[4] for c in current), None))
            current.clear()

    size = 0.0
    for found in letters:
        if len(found) == 1:
            finish()
            continue
        ch, font = found[0], found[5]
        l, t, r, b = place(*found[1:5])
        if current:
            _, _, last_top, last_right, last_bottom = current[-1]
            # Letters far apart are two words, with or without a space between them. So is a change of size: a lab's "01" after a test's name.
            if l - last_right > font * 0.3 or abs((t + b) - (last_top + last_bottom)) > font * 0.6 or abs(font - size) > size * 0.1:
                finish()
        current.append((ch, l, t, r, b))
        size = font
    finish()
    return words, (right - left if turn in (90, 270) else top - bottom)


def _has_big_picture(page: Any) -> bool:
    import pypdfium2.raw as pdfium

    width, height = page.get_size()
    for found in page.get_objects(filter=[pdfium.FPDF_PAGEOBJ_IMAGE], max_depth=2):
        l, b, r, t = found.get_bounds()
        if (r - l) * (t - b) > width * height * 0.5:
            return True
    return False


class _Page(NamedTuple):
    lines: list[Line]
    preview: Optional[dict[str, Any]]
    scan: bool              # it needed OCR
    unread: bool = False    # it needed OCR and didn't get it


def _ocr_page(image: Any, number: int, used: Optional[str]) -> _Page:
    prepared, angle = _prepare(image, used)
    preview = _preview(image, angle)
    try:
        words = ocr.read_words(prepared)
    except OcrError as exc:
        log.warning("Couldn't read a page: %s", exc)
        return _Page([], preview, True, True)
    return _Page(ocr.lay_out(words, number, prepared.height, UNSURE_BELOW.get(used or "", 0.0), read_by_ocr=True), preview, True)


def _render(page: Any, width: float) -> Any:
    across, down = page.get_size()
    scale = min(width / max(across, 1), LONGEST_SIDE * 2 / max(across, down, 1))
    return page.render(scale=scale).to_pil().convert("RGB")


def _read_pdf(data: bytes, used: Optional[str]) -> tuple[list[_Page], int]:
    import pypdfium2

    try:
        with _pdfium:
            doc = pypdfium2.PdfDocument(data)
            total = len(doc)
    except pypdfium2.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise ReportError("That PDF is protected with a password. Save a copy without the password, then try again.") from exc
        raise ReportError("Couldn't open that PDF.") from exc

    def one(number: int) -> _Page:
        with _pdfium:
            page = doc[number]
            words, height = _pdf_words(page)
            chars = sum(len(w.text) for w in words)
            scan = chars < MIN_TEXT or (chars < STAMP_TEXT and _has_big_picture(page))
            picture = _render(page, PREVIEW_WIDTH) if not scan or not used else None
            large = _render(page, 2500) if scan and used else None
            page.close()
        if large is not None:
            return _ocr_page(large, number, used)
        if scan:
            return _Page([], _preview(picture), True, True)
        return _Page(ocr.lay_out(words, number, height), _preview(picture), False)

    try:
        with ThreadPoolExecutor(max_workers=OCR_WORKERS) as pool:
            return list(pool.map(one, range(min(total, MAX_PAGES)))), total
    except pypdfium2.PdfiumError as exc:
        raise ReportError("Couldn't read that PDF.") from exc
    finally:
        with _pdfium:
            doc.close()


def _read_pages(data: bytes, content_type: str, filename: str) -> tuple[list[_Page], int, Optional[str]]:
    if len(data) > MAX_BYTES:
        raise ReportError("That file is larger than 15 MB.")
    kind = file_type(data, content_type, filename)
    if not kind:
        raise ReportError("Upload a PDF or a photo (JPEG, PNG or HEIC).")
    used = ocr.engine()
    if kind == "application/pdf":
        pages, total = _read_pdf(data, used)
        return pages, total, used
    images = _open_images(data, kind)
    _require_ocr()
    with ThreadPoolExecutor(max_workers=OCR_WORKERS) as pool:
        return list(pool.map(lambda i: _ocr_page(images[i], i, used), range(len(images)))), len(images), used


def _require_ocr() -> None:
    if not ocr_available():
        raise ReportError("Reading photos and scans needs a reader on the computer running Syntropy Health, such as a free program "
                          f"called Tesseract. {install_advice()} PDFs from a lab's website work without it.")


def page_previews(data: bytes, content_type: str, filename: str) -> list[dict[str, Any]]:
    """A kept file's pages as images, for where a PDF or a HEIC can't be shown: the phone apps, and the Mac and Windows windows."""
    kind = file_type(data, content_type, filename)
    if kind != "application/pdf":
        return [_preview(img) for img in _open_images(data, kind)] if kind else []
    import pypdfium2

    try:
        with _pdfium:
            doc = pypdfium2.PdfDocument(data)
            pictures = []
            for number in range(min(len(doc), MAX_PAGES)):
                page = doc[number]
                pictures.append(_render(page, PREVIEW_WIDTH))
                page.close()
            doc.close()
    except pypdfium2.PdfiumError as exc:
        raise ReportError("Couldn't open that PDF.") from exc
    return [_preview(p) for p in pictures]


# ---------------------------------------------------------------------------
# Finding results in the text
# ---------------------------------------------------------------------------

MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b|\b(\d{4})-(\d{2})-(\d{2})\b"
                  r"|\b([A-Za-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})\b|\b(\d{1,2})-([A-Za-z]{3})-(\d{4})\b")
TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp][Mm])?\b")
PHONE = re.compile(r"\(?\b\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b")
ZIP = re.compile(r"\b\d{5}-\d{4}\b")
NUMBER = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$|^-?\d+(?:\.\d+)?$")
COMPARED = re.compile(r"^(?:<|>|<=|>=|≤|≥)=?\d*\.?\d+$")
LAB_CODE = re.compile(r"^[0Oo°][0-9lI]{1,2}$")                # Labcorp's "01" site code after the test name
ATTACHED_CODE = re.compile(r"[0O°][1-9lI]$")                  # OCR runs it into the name: "MCV01", "MCVOI"
STUCK_FLAG = re.compile(r"^(\d+(?:\.\d+)?)(H|L|HH|LL)$")      # "105H"
FLAGS = {"H": "H", "HH": "H", "HIGH": "H", "H*": "H", "L": "L", "LL": "L", "LOW": "L", "L*": "L",
         "A": None, "ABN": None, "ABNORMAL": None, "CRIT": None, "*": None}
QUALITATIVE = {"negative", "positive", "reactive", "nonreactive", "non-reactive", "detected", "undetected", "equivocal", "indeterminate",
               "normal", "abnormal", "trace", "none", "clear", "cloudy", "hazy", "yellow", "amber", "straw", "pending"}
# Only a result after "not", "non" or "no": a "Not Provided" isn't one.
AFTER_NOT = {"negative", "positive", "reactive", "detected", "growth", "seen", "found", "isolated"}
# Only a result in a column of their own: they begin plenty of test names.
IN_COLUMN = {"few", "rare", "moderate", "many", "occasional", "small", "large"}
RANGE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)")
RANGE_CELL = re.compile(r"^-?\d+(?:\.\d+)?\s*(?:-|–|to)\s*\d")
BELOW = re.compile(r"(?:<=?|≤|less than)\s*(?:or\s*=\s*)?(\d*\.?\d+)", re.I)
ABOVE = re.compile(r"(?:>=?|≥|greater than)\s*(?:or\s*=\s*)?(\d*\.?\d+)", re.I)
UNITS = {u.lower(): u for u in ("mg/dL", "g/dL", "ng/mL", "pg/mL", "ng/dL", "ug/dL", "mcg/dL", "U/L", "IU/L", "mmol/L",
                                "umol/L", "nmol/L", "pmol/L", "mEq/L", "mIU/L", "uIU/mL", "mIU/mL", "mg/L", "g/L", "fL", "pg", "K/uL", "M/uL",
                                "x10E3/uL", "x10E6/uL", "Thousand/uL", "Million/uL", "cells/uL", "mL/min/1.73m2", "mL/min/1.73", "mm/hr", "U/mL")}
UNIT_WORDS = {"%", "fl", "pg", "sec", "seconds", "ratio", "mm/hr", "index", "iu", "u"}
# What OCR confuses in a unit: l, I, i, 1 and |. "mmo1/1" and "mg/dI" are mmol/L and mg/dL.
_ONE_STROKE = str.maketrans("1i|", "lll")
UNITS_READ = {u.lower().translate(_ONE_STROKE): u for u in UNITS.values()}
# A unit run into the number before it: "27.0-33.0pg", "5.4%".
STUCK_UNIT = re.compile(r"(?<=\d)(?=%|(?:pg|fL|fl)\b|(?:mg|g|ng|pg|ug|mcg|U|IU|mmol|umol|nmol|pmol|mEq|mIU|uIU|K|M)/)")
UNIT = re.compile(r"^\(?[a-zµμ%*0-9^.]*[a-zµμ][a-zµμ0-9^.*]*/[a-zµμ0-9^.*/]+\)?$", re.I)
NOT_TESTS = re.compile(r"^(page|phone|fax|dob|date|age|sex|gender|patient|name|account|acct|specimen|collected|received|"
                       r"reported|resulted|printed|npi|mrn|id|client|physician|doctor|ordering|ordered|referring|provider|address|suite|ste|"
                       r"street|city|zip|time|final|report|control|requisition|lab director|director|result|reference|test|tests|units|"
                       r"flag|comment|note|please|your|fasting|clia|medical)\b", re.I)
REF_LINE = re.compile(r"^\s*(?:reference (?:range|interval)|ref\.? range)\s*:?\s*(.+)$", re.I)
LABS = [(re.compile(p, re.I), name) for p, name in (
    (r"quest\s+diagnostics", "Quest Diagnostics"), (r"lab\s*corp|laboratory corporation of america", "Labcorp"),
    (r"bio-?reference", "BioReference"), (r"sonora quest", "Sonora Quest"), (r"\barup\b", "ARUP Laboratories"),
    (r"mayo clinic lab", "Mayo Clinic Laboratories"), (r"kaiser permanente", "Kaiser Permanente"),
)]


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


@lru_cache(maxsize=1)
def _known_keys() -> frozenset[str]:
    return frozenset(_key(n) for t in lab_catalog.common_labs() for n in (t["name"], *t.get("aliases", [])))


def _known(name: str) -> bool:
    names = _known_keys()
    k = _key(name)
    return k in names or _key(re.sub(r"\(.*?\)", "", name)) in names


def _date(m: re.Match) -> Optional[str]:
    g = m.groups()
    try:
        if g[0]:
            y = int(g[2]) + (2000 if len(g[2]) == 2 else 0)
            d = date(y, int(g[0]), int(g[1]))
        elif g[3]:
            d = date(int(g[3]), int(g[4]), int(g[5]))
        elif g[6]:
            d = date(int(g[8]), MONTHS[g[6][:3].lower()], int(g[7]))
        else:
            d = date(int(g[11]), MONTHS[g[10].lower()], int(g[9]))
    except (KeyError, ValueError):
        return None
    return d.isoformat() if 1990 <= d.year <= date.today().year + 1 else None


def collected_date(text: str) -> Optional[str]:
    """The date next to a "collected" label: on the same line, or in the same column of the next line."""
    lines = text.splitlines()
    for label in (r"collect", r"drawn", r"specimen date|date of service"):
        for i, line in enumerate(lines):
            at = re.search(label, line, re.I)
            if not at:
                continue
            after = [m for m in DATE.finditer(line) if m.start() >= at.start()]
            found = next((d for d in map(_date, after) if d), None)
            if found:
                return found
            if i + 1 < len(lines):
                below = sorted(DATE.finditer(lines[i + 1]), key=lambda m: abs(m.start() - at.start()))
                found = next((d for d in map(_date, below) if d), None)
                if found:
                    return found
    return None


def _num(s: str) -> float | int:
    s = s.replace(",", "")
    return float(s) if "." in s else int(s)


def _word(token: str) -> str:
    return token.lower().strip(".,")


def _says_result(tokens: list[str], i: int) -> bool:
    token = tokens[i]
    if NUMBER.match(token) or COMPARED.match(token) or STUCK_FLAG.match(token):
        return True
    if _word(token) in ("not", "non", "no"):
        return i + 1 < len(tokens) and _word(tokens[i + 1]) in AFTER_NOT
    return _word(token) in QUALITATIVE


def _clean(line: str) -> str:
    """Without what looks like a result and isn't: dates, times, phone numbers and zip codes. Spacing stays, for the columns."""
    line = line.replace("|", " ")
    for pattern in (DATE, TIME, PHONE, ZIP):
        line = pattern.sub(lambda m: " " * len(m.group()), line)
    return STUCK_UNIT.sub(" ", line)


def _without_code(name: str) -> str:
    """A test's name without the lab's site code after it, spaced off or run in."""
    words = name.split()
    if len(words) > 1 and LAB_CODE.match(words[-1]):
        name = " ".join(words[:-1])
    m = ATTACHED_CODE.search(name)
    return name[:m.start()] if m and not _known(name) and _known(name[:m.start()]) else name


def _read_unit(token: str) -> tuple[Optional[str], bool]:
    """A unit as it's normally written, and whether that took a guess at what OCR misread."""
    t = re.sub(r"^TU/", "IU/", re.sub(r"^9/", "g/", token.strip("()")))
    if t.lower() in UNITS:
        return UNITS[t.lower()], False
    if t.lower().translate(_ONE_STROKE) in UNITS_READ:
        return UNITS_READ[t.lower().translate(_ONE_STROKE)], False
    if UNIT.match(t) and len(t) <= 6:
        guess = _closest_unit(t)
        if guess:
            return guess, True
    return t, False


def _closest_unit(token: str) -> Optional[str]:
    """One letter misread, and only one: "g/aL" is g/dL, not g/L or mg/L."""
    read = token.lower().translate(_ONE_STROKE)
    close = [u for u in UNITS_READ if len(u) == len(read) and sum(a != b for a, b in zip(u, read)) == 1]
    return UNITS_READ[close[0]] if len(close) == 1 else None


def _is_unit(token: str) -> bool:
    t = token.rstrip(".,;:")
    return bool(UNIT.match(t)) or t.lower() in UNIT_WORDS


def _segments(line: str) -> list[str]:
    """A printed line can hold two results side by side, on a report laid out in columns. Another begins at a gap, after a result,
    at a name with a result of its own after it."""
    cells = list(re.finditer(r"\S+(?: \S+)*", line))
    starts = [0]
    seen = False
    for n, cell in enumerate(cells):
        tokens = cell.group().split()
        if seen and n and re.match(r"[A-Za-z]", tokens[0]) and not _is_unit(tokens[0]) and _word(tokens[0]) not in (*QUALITATIVE, "not", "non", "no", "high", "low") \
                and tokens[0].upper() not in FLAGS:
            after = tokens[1:] + (cells[n + 1].group().split() if n + 1 < len(cells) else [])
            if any(_says_result(after, i) for i in range(len(after))):
                starts.append(cell.start())
                seen = False
        if any(_says_result(tokens, i) for i in range(len(tokens))) or RANGE.search(cell.group()):
            seen = True
    return [line[a:b].strip() for a, b in zip(starts, [*starts[1:], len(line)])]


def _name_and_rest(line: str) -> tuple[str, list[str], bool]:
    """The test's name, what follows it, and whether a gap in the print separates them. When the second column starts with a result,
    the whole first column is the name: "CA 125", not "CA" with a result of 125."""
    cells = re.split(r"\s{2,}", line.strip())
    if len(cells) >= 2 and re.match(r"[A-Za-z]", cells[0]) and not RANGE_CELL.match(cells[1]):
        second = cells[1].split()
        first_cell = cells[0].split()
        code_only = LAB_CODE.match(cells[1]) and len(cells) > 2
        looks = _says_result(second, 0) or _word(second[0]) in IN_COLUMN or code_only
        # A number at the end of the first column is part of the name when the second column has the result: unless what's before it is a
        # test already, as in "Glucose 105   98", where the second column is the previous result.
        holds_result = (_says_result(first_cell, len(first_cell) - 1) and not _known(cells[0]) and not LAB_CODE.match(first_cell[-1])
                        and (_known(" ".join(first_cell[:-1])) or not _says_result(second, 0)))
        if looks and not holds_result:
            return cells[0], " ".join(cells[1:]).split(), True
    tokens = line.split()
    i = next((n for n in range(1, len(tokens)) if _says_result(tokens, n)), len(tokens))
    return " ".join(tokens[:i]), tokens[i:], False


def _parse_line(line: str) -> tuple[Optional[dict[str, Any]], list[str]]:
    """One result, and the printed words each part came from (name words, the value, then what followed it)."""
    tokens = line.split()
    if len(tokens) < 2:
        return None, []
    raw_name, rest, in_columns = _name_and_rest(line)
    if raw_name.rstrip().endswith(":") and not _known(raw_name):       # "Prediabetes: 5.7 - 6.4", "Total Volume: Not Provided"
        return None, []
    name = _without_code(raw_name.strip(" .:,-"))
    while rest and LAB_CODE.match(rest[0]) and len(rest) > 1:
        rest = rest[1:]
    if not name or not rest or len(name) > 60 or len(name.split()) > 8 or not re.match(r"[A-Za-z]", name) or NOT_TESTS.match(name):
        return None, []
    known = _known(name)
    printed = rest

    value: Any = None
    value_text: Optional[str] = None
    flag: Optional[str] = None
    first, rest = rest[0], rest[1:]
    stuck = STUCK_FLAG.match(first)
    if stuck:
        value, flag = _num(stuck.group(1)), stuck.group(2)[0]
    elif NUMBER.match(first):
        value = _num(first)
    elif COMPARED.match(first):
        value_text = first
    else:
        words = [first]
        while rest and _word(words[-1]) in ("not", "non", "no") and _word(rest[0]) in AFTER_NOT:
            words.append(rest.pop(0))                                  # "Not Detected", "Non Reactive", "No growth"
        value_text = " ".join(words).strip(".,").capitalize()
    if rest and rest[0].upper() in FLAGS:
        flag = FLAGS[rest[0].upper()] or flag
        rest = rest[1:]

    tail = " ".join(rest)
    ref_low = ref_high = None
    ref_text = None
    rng = RANGE.search(tail)
    if rng and _num(rng.group(1)) <= _num(rng.group(2)):
        ref_low, ref_high, ref_text = _num(rng.group(1)), _num(rng.group(2)), rng.group(0)
    elif value_text and rest and (_word(rest[0]) in QUALITATIVE or _word(rest[0]) in ("not", "non", "no")):
        ref_text = " ".join(rest[:2] if _word(rest[0]) in ("not", "non", "no") else rest[:1])   # "Negative", "Not Detected"
    elif m := BELOW.search(tail):
        ref_high, ref_text = _num(m.group(1)), m.group(0)
    elif m := ABOVE.search(tail):
        ref_low, ref_text = _num(m.group(1)), m.group(0)
    unit = next((t.strip("()") for t in (re.sub(r"^9/", "g/", t.rstrip(".,;:")) for t in rest)
                 if (UNIT.match(t) and not QUALITATIVE & set(t.lower().split("/"))) or t.lower() in UNIT_WORDS), None)
    unit, guessed = _read_unit(unit) if unit else (None, False)
    if not unit:        # read too badly to look like one, and where one goes: "KJUL"
        unit = next((u for u in (_closest_unit(t) for t in rest[:1] if t.isalpha() and 3 <= len(t) <= 6) if u), None)
        guessed = bool(unit)

    if not known:
        # Unknown names need more evidence, so addresses and page numbers don't become results.
        said = (value_text or "").split()
        worded = bool(said) and not COMPARED.match(said[0])
        if worded and not in_columns and (_word(said[0]) not in (*QUALITATIVE, "not", "non", "no") or len(tokens) > 6):
            return None, []
        # "<0.012" in a column of its own is a result whatever its name, even when the rest of its line didn't come through.
        if not worded and ref_text is None and unit is None and not (in_columns and value_text):
            return None, []
        # More words than a unit has: a sentence with a number in it ("value of 6.5% or greater indicates...").
        if sum(t.isalpha() and t.upper() not in FLAGS and not _is_unit(t) for t in rest) >= 4:
            return None, []
    row = {"test": name, "loinc": None, "value": value, "value_text": value_text, "unit": unit,
           "ref_low": ref_low, "ref_high": ref_high, "ref_text": ref_text, "flag": flag, "date": None, "unsure": ["unit"] if guessed else []}
    return row, printed


def parse_line(line: str) -> Optional[dict[str, Any]]:
    return _parse_line(_clean(line))[0]


def _restore_point(row: dict[str, Any]) -> bool:
    """OCR's commonest mistake on a photo is losing a decimal point: "136" for 13.6. When the printed range puts exactly one place for
    it, that's what the value becomes, and it's marked to check."""
    value, low, high = row["value"], row["ref_low"], row["ref_high"]
    if not isinstance(value, int) or value < 10 or low is None or high is None or low <= value <= high:
        return False
    fits = [value / 10 ** n for n in (1, 2, 3) if value >= 10 ** (n - 1) and low <= value / 10 ** n <= high]
    if len(fits) != 1:
        return False
    row["value"] = round(fits[0], 3)
    return True


def _unsure(row: dict[str, Any], printed: list[str], doubtful: frozenset[str], line_read_by_ocr: bool) -> list[str]:
    """Which parts of a row to check first against the page: words the reader wasn't sure of, a printed flag that doesn't fit the
    printed range, and a value so far from its range that a decimal point may have gone missing."""
    out = list(row["unsure"])
    value, low, high = row["value"], row["ref_low"], row["ref_high"]
    if doubtful:
        if doubtful & set(row["test"].split()):
            out.append("test")
        if printed and printed[0] in doubtful:
            out.append("value")
        if row["unit"] and "unit" not in out and doubtful & {t.strip("(),.;:") for t in printed if _read_unit(t.strip("(),.;:"))[0] == row["unit"]}:
            out.append("unit")
        if any(t in doubtful for t in printed[1:] if any(c.isdigit() for c in t)):
            out.append("range")
    if line_read_by_ocr and "test" not in out and len(_key(row["test"])) <= 5 and not _known(row["test"]):     # a short code misread is hard to spot
        out.append("test")
    if value is not None and "value" not in out:
        off_flag = (row["flag"] == "H" and high is not None and value <= high) or (row["flag"] == "L" and low is not None and value >= low)
        far = (high is not None and high > 0 and value > high * 10) or (low is not None and low > 0 and value < low / 10)
        if off_flag or far:
            out.append("value")
    return out


def parse_lines(lines: list[Line]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    seen: set[Any] = set()
    last: Optional[dict[str, Any]] = None
    for line in lines:
        cleaned = _clean(line.text)
        note = REF_LINE.match(cleaned)
        if note:
            if last and last["ref_low"] is None and last["ref_high"] is None and not last["value_text"]:
                said = note.group(1)
                rng = RANGE.search(said)
                below, above = BELOW.search(said), ABOVE.search(said)
                if rng:
                    last["ref_low"], last["ref_high"], last["ref_text"] = _num(rng.group(1)), _num(rng.group(2)), rng.group(0)
                elif below:
                    last["ref_high"], last["ref_text"] = _num(below.group(1)), below.group(0)
                elif above:
                    last["ref_low"], last["ref_text"] = _num(above.group(1)), above.group(0)
            continue
        for segment in _segments(cleaned):
            row, printed = _parse_line(segment)
            if not row:
                continue
            key = (_key(row["test"]), row["value"], row["value_text"])
            if key in seen:               # reports often repeat out-of-range results in a summary
                continue
            seen.add(key)
            restored = line.ocr and _restore_point(row)
            row["unsure"] = _unsure(row, printed, line.doubtful, line.ocr)
            if restored and "value" not in row["unsure"]:
                row["unsure"].append("value")
            if line.bottom > line.top:
                row["where"] = {"page": line.page, "top": round(line.top, 4), "bottom": round(line.bottom, 4)}
            results.append(row)
            last = row
    text = "\n".join(line.text for line in lines)
    lab = next((name for pattern, name in LABS if pattern.search(text)), None)
    collected = collected_date(text)
    # Several reports scanned into one file: a page that says its own date keeps it.
    for number in {line.page for line in lines} if len({line.page for line in lines}) > 1 else ():
        own = collected_date("\n".join(line.text for line in lines if line.page == number))
        if own and own != collected:
            for r in results:
                if r.get("where", {}).get("page") == number:
                    r["date"] = own
    return {"collected": collected, "lab": lab, "results": results}


def parse_text(text: str) -> dict[str, Any]:
    return parse_lines([Line(t, 0, 0.0, 0.0, frozenset(), False) for t in text.splitlines()])


def _correct_ocr_names(results: list[dict[str, Any]]) -> None:
    """Replaces a misread test name ("HEMOGLOBIN A1¢") with the common test it is clearly meant to be, so the result
    joins that test's trend. The person sees the corrected name when reviewing, marked to check."""
    canonical: dict[str, str] = {}
    for t in lab_catalog.common_labs():
        for n in (t["name"], *t.get("aliases", [])):
            canonical.setdefault(_key(n), t["name"])
    for r in results:
        k = _key(r["test"])
        if len(k) >= 5 and not lab_catalog.loinc_codes(r["test"]):
            # Letters misread, not letters different: "BUN/Creatinine Ratio" is close to "Albumin/creatinine ratio", and another test.
            close = [c for c in difflib.get_close_matches(k, canonical, n=3, cutoff=0.9) if abs(len(c) - len(k)) <= 1]
            if close:
                r["test"] = canonical[close[0]]
                r.setdefault("unsure", [])
                if "test" not in r["unsure"]:
                    r["unsure"].append("test")


def _fill_missing_units(results: list[dict[str, Any]]) -> None:
    """OCR drops a lone "%" or reads it as "8". For a test whose usual unit is known, that goes in the box, marked to check."""
    for r in results:
        if r["unit"] or r["value"] is None:
            continue
        unit = re.sub(r"\{[^}]*\}", "", (lab_catalog.matched_test(r["test"]) or {}).get("unit") or "")
        if unit:
            r["unit"] = unit
            if "unit" not in r["unsure"]:
                r["unsure"].append("unit")


def read(data: bytes, content_type: str, filename: str) -> dict[str, Any]:
    pages, total, used = _read_pages(data, content_type, filename)
    unread = [i + 1 for i, p in enumerate(pages) if p.unread]
    if len(unread) == len(pages) and not used:
        _require_ocr()
    if len(unread) == len(pages):
        raise ReportError("Couldn't read any page of that file. Try a sharper copy, or enter the results by hand.")
    out = parse_lines([line for p in pages for line in p.lines])
    scanned = any(p.scan and not p.unread for p in pages)
    if scanned:
        _correct_ocr_names(out["results"])
        _fill_missing_units(out["results"])
    notes = []
    if unread:
        notes.append(f"Page {', '.join(map(str, unread))} couldn't be read" + ("." if used else f": it's a scan, and this computer has no reader for those. {install_advice()}"))
    if total > len(pages):
        notes.append(f"This file has {total} pages. The first {len(pages)} were read.")
    return {**out, "read_as": "ocr" if scanned else "text", "engine": used if scanned else None, "page_count": total,
            "pages": [p.preview for p in pages if p.preview], "notes": notes}
