"""Reads lab results from a report on this machine, without AI or any network call: the text layer of a PDF from a
lab's website, or Tesseract OCR for photos and scans of paper reports. The person reviews every row before saving."""

from __future__ import annotations

import difflib
import io
import re
import shutil
import subprocess
from datetime import date
from functools import lru_cache
from typing import Any, Optional

from app.store import lab_catalog

MAX_BYTES = 15 * 1024 * 1024
MAX_PAGES = 30
OCR_TIMEOUT = 90


class ReportError(Exception):
    pass


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None


# ---------------------------------------------------------------------------
# Getting text out of the file
# ---------------------------------------------------------------------------

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
        rows = list(small.rotate(angle, expand=False, resample=Image.BILINEAR).resize((1, small.height), Image.BOX).getdata())
        mean = sum(rows) / len(rows)
        return sum((r - mean) ** 2 for r in rows)

    best = max((a / 2 for a in range(-20, 21)), key=unevenness)                # -10° to 10° in 0.5° steps
    return max((best + a / 10 for a in range(-4, 5)), key=unevenness)


def _ocr(image: Any) -> str:
    from PIL import Image, ImageOps

    img = ImageOps.grayscale(ImageOps.exif_transpose(image))   # phone photos are often stored sideways
    # Tesseract reads best at print resolution: enlarge small images, shrink huge ones so it stays quick.
    if img.width < 1800 or img.width > 4000:
        scale = (1800 if img.width < 1800 else 4000) / img.width
        img = img.resize((round(img.width * scale), round(img.height * scale)))
    img = _flatten(img)
    if abs(angle := _skew(img)) >= 0.3:
        img = img.rotate(angle, expand=True, fillcolor=255, resample=Image.BICUBIC)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    try:
        proc = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "6", "-l", "eng", "-c", "preserve_interword_spaces=1"],
                              input=buf.getvalue(), capture_output=True, timeout=OCR_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ReportError("Reading the photo took too long. Try a smaller or clearer photo.") from exc
    if proc.returncode != 0:
        raise ReportError("Couldn't read that image. Try a JPEG or PNG photo.")
    return proc.stdout.decode("utf-8", "replace")


def _require_ocr() -> None:
    if not ocr_available():
        raise ReportError("Reading photos and scans needs Tesseract on the computer running Syntropy Health "
                          "(the Docker image includes it; on a Mac run: brew install tesseract).")


def _open_image(data: bytes) -> Any:
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception as exc:  # noqa: BLE001 - HEIC and damaged files
        raise ReportError("Couldn't open that image. Save it as JPEG or PNG and try again.") from exc


def _pdf_text(data: bytes) -> tuple[str, list[Any]]:
    """The PDF's text with columns kept in place, plus the page images for a scan with no text layer."""
    from pypdf import PdfReader

    try:
        pages = PdfReader(io.BytesIO(data)).pages[:MAX_PAGES]
        text = "\n".join((p.extract_text(extraction_mode="layout") or "") for p in pages)
    except Exception as exc:  # noqa: BLE001
        raise ReportError("Couldn't open that PDF.") from exc
    if len(re.sub(r"\s", "", text)) >= 40:
        return text, []
    images = []
    for page in pages:
        try:
            images += [f.image for f in page.images]
        except Exception:  # noqa: BLE001 - an image format Pillow can't decode; skip it
            continue
    return "", images


def report_text(data: bytes, content_type: str, filename: str) -> tuple[str, str]:
    """(text, "text" | "ocr")."""
    if len(data) > MAX_BYTES:
        raise ReportError("That file is larger than 15 MB.")
    is_pdf = content_type == "application/pdf" or filename.lower().endswith(".pdf")
    if not is_pdf and not content_type.startswith("image/"):
        raise ReportError("Upload a PDF or a photo (JPEG or PNG).")
    if is_pdf:
        text, images = _pdf_text(data)
        if text:
            return text, "text"
        if not images:
            raise ReportError("That PDF has no readable text or images.")
        _require_ocr()
        return "\n".join(_ocr(img) for img in images), "ocr"
    _require_ocr()
    return _ocr(_open_image(data)), "ocr"


# ---------------------------------------------------------------------------
# Finding results in the text
# ---------------------------------------------------------------------------

MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b|\b(\d{4})-(\d{2})-(\d{2})\b"
                  r"|\b([A-Za-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})\b|\b(\d{1,2})-([A-Za-z]{3})-(\d{4})\b")
TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp][Mm])?\b")
NUMBER = re.compile(r"^\d{1,3}(?:,\d{3})+(?:\.\d+)?$|^\d+(?:\.\d+)?$")
COMPARED = re.compile(r"^(?:<|>|<=|>=|≤|≥)=?\d+(?:\.\d+)?$")
LAB_CODE = re.compile(r"^0\d{1,2}$")                          # Labcorp's "01" site code after the test name
STUCK_FLAG = re.compile(r"^(\d+(?:\.\d+)?)(H|L|HH|LL)$")      # "105H"
FLAGS = {"H": "H", "HH": "H", "HIGH": "H", "H*": "H", "L": "L", "LL": "L", "LOW": "L", "L*": "L",
         "A": None, "ABN": None, "ABNORMAL": None, "CRIT": None, "*": None}
QUALITATIVE = {"negative", "positive", "reactive", "nonreactive", "non-reactive", "detected", "undetected", "not",
               "normal", "abnormal", "trace", "none", "clear", "cloudy", "hazy", "yellow", "amber", "straw", "pending"}
ZIP = re.compile(r"\b\d{5}-\d{4}\b")
RANGE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)")
BELOW = re.compile(r"(?:<=?|≤|less than)\s*(?:or\s*=\s*)?(\d+(?:\.\d+)?)", re.I)
ABOVE = re.compile(r"(?:>=?|≥|greater than)\s*(?:or\s*=\s*)?(\d+(?:\.\d+)?)", re.I)
UNITS = {u.lower(): u for u in ("mg/dL", "g/dL", "ng/mL", "pg/mL", "ng/dL", "ug/dL", "mcg/dL", "U/L", "IU/L", "mmol/L",
                                "umol/L", "nmol/L", "pmol/L", "mEq/L", "mIU/L", "uIU/mL", "mIU/mL", "mg/L", "g/L")}
UNIT_WORDS = {"%", "fl", "pg", "sec", "seconds", "ratio", "mm/hr", "index", "iu", "u"}
UNIT = re.compile(r"^\(?[a-zµμ%*0-9^.]*[a-zµμ][a-zµμ0-9^.*]*/[a-zµμ0-9^.*/]+\)?$", re.I)
NOT_TESTS = re.compile(r"^(page|phone|fax|dob|date|age|sex|gender|patient|name|account|acct|specimen|collected|received|"
                       r"reported|printed|npi|id|client|physician|doctor|ordering|provider|address|suite|ste|street|city|"
                       r"zip|time|final|report|control|requisition|lab director|director|result|reference|test|tests|units|"
                       r"flag|comment|note|clia|medical)\b", re.I)
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


def parse_line(line: str) -> Optional[dict[str, Any]]:
    line = TIME.sub(" ", DATE.sub(" ", line.replace("|", " ")))
    tokens = line.split()
    if len(tokens) < 2:
        return None
    # The test name is everything before the first result-looking token.
    i = 0
    while i < len(tokens) and not (NUMBER.match(tokens[i]) or COMPARED.match(tokens[i]) or STUCK_FLAG.match(tokens[i])
                                   or tokens[i].lower().strip(".,") in QUALITATIVE):
        i += 1
    name = " ".join(tokens[:i]).strip(" .:,-")
    rest = tokens[i:]
    while rest and LAB_CODE.match(rest[0]) and len(rest) > 1:
        rest = rest[1:]
    if not name or not rest or len(name) > 60 or i > 8 or not re.match(r"[A-Za-z]", name) or NOT_TESTS.match(name):
        return None
    known = _known(name)

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
        while rest and words[-1].lower() in ("not", "non") and rest[0].lower().strip(".,") in QUALITATIVE:
            words.append(rest.pop(0))                                  # "Not Detected", "Non Reactive"
        value_text = " ".join(words).strip(".,").capitalize()
    if rest and rest[0].upper() in FLAGS:
        flag = FLAGS[rest[0].upper()] or flag
        rest = rest[1:]

    tail = ZIP.sub(" ", " ".join(rest))
    ref_low = ref_high = None
    ref_text = None
    rng = RANGE.search(tail)
    if rng and _num(rng.group(1)) <= _num(rng.group(2)):
        ref_low, ref_high, ref_text = _num(rng.group(1)), _num(rng.group(2)), rng.group(0)
    elif value_text and rest and rest[0].lower().strip(".,") in QUALITATIVE:
        ref_text = " ".join(rest[:2] if rest[0].lower() in ("not", "non") else rest[:1])   # "Negative", "Not Detected"
    elif m := BELOW.search(tail):
        ref_high, ref_text = _num(m.group(1)), m.group(0)
    elif m := ABOVE.search(tail):
        ref_low, ref_text = _num(m.group(1)), m.group(0)
    unit = next((t.strip("()") for t in (t.rstrip(".,;:") for t in rest)
                 if (UNIT.match(t) and not QUALITATIVE & set(t.lower().split("/"))) or t.lower() in UNIT_WORDS), None)
    unit = UNITS.get(unit.lower(), unit) if unit else None                   # OCR often reads "mg/dL" as "mg/dl"

    if not known:
        # Unknown names need more evidence, so addresses and page numbers don't become results.
        if value_text is not None and (value_text.split()[0].lower() not in QUALITATIVE or len(tokens) > 6):
            return None
        if value is not None and ref_text is None and unit is None:
            return None
    return {"test": name, "loinc": None, "value": value, "value_text": value_text, "unit": unit,
            "ref_low": ref_low, "ref_high": ref_high, "ref_text": ref_text, "flag": flag, "date": None}


def parse_text(text: str) -> dict[str, Any]:
    results, seen = [], set()
    for line in text.splitlines():
        row = parse_line(line)
        if not row:
            continue
        key = (_key(row["test"]), row["value"], row["value_text"])
        if key not in seen:               # reports often repeat out-of-range results in a summary
            seen.add(key)
            results.append(row)
    lab = next((name for pattern, name in LABS if pattern.search(text)), None)
    return {"collected": collected_date(text), "lab": lab, "results": results}


def _correct_ocr_names(results: list[dict[str, Any]]) -> None:
    """Replaces a misread test name ("HEMOGLOBIN A1¢") with the common test it is clearly meant to be, so the result
    joins that test's trend. The person sees the corrected name when reviewing."""
    canonical: dict[str, str] = {}
    for t in lab_catalog.common_labs():
        for n in (t["name"], *t.get("aliases", [])):
            canonical.setdefault(_key(n), t["name"])
    for r in results:
        k = _key(r["test"])
        if len(k) >= 5 and not lab_catalog.loinc_codes(r["test"]):
            close = difflib.get_close_matches(k, canonical, n=1, cutoff=0.9)
            if close:
                r["test"] = canonical[close[0]]


def read(data: bytes, content_type: str, filename: str) -> dict[str, Any]:
    text, read_as = report_text(data, content_type, filename)
    out = parse_text(text)
    if read_as == "ocr":
        _correct_ocr_names(out["results"])
    return {**out, "read_as": read_as}
