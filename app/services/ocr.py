"""Reading the words on a page image on this computer, with nothing sent anywhere: Apple's Vision on a Mac, the OCR built into
Windows there, and Tesseract anywhere it's installed. Each gives the words and where they are, so that what's on one printed line
stays on one line, whichever engine read it and whatever columns the page has."""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from functools import lru_cache
from typing import Any, NamedTuple, Optional

from app.core import config

log = logging.getLogger("syntropy.ocr")

OCR_TIMEOUT = 90
# Readers sometimes return a Cyrillic or Greek letter that looks like a Latin one ("СВС"), which would never match a test's name.
LOOK_ALIKES = str.maketrans("АВСЕНІКМОРТХаеорсухΑΒΕΗΙΚΜΝΟΡΤΥΧЗ", "ABCEHIKMOPTXaeopcyxABEHIKMNOPTYX3")


class OcrError(Exception):
    pass


class Word(NamedTuple):
    text: str
    left: float             # in the units of the page they came from, y growing downward
    top: float
    right: float
    bottom: float
    conf: Optional[float]   # 0 to 1, or None from an engine that doesn't say


class Line(NamedTuple):
    text: str                       # with its words spaced as they are on the page, so columns can be told apart
    page: int
    top: float                      # as fractions of the page's height
    bottom: float
    doubtful: frozenset[str]        # words the engine wasn't sure of
    ocr: bool = True                # read from an image, not a text layer


# ---------------------------------------------------------------------------
# Which engine this computer has
# ---------------------------------------------------------------------------

def tesseract_path() -> Optional[str]:
    """Also in the usual install folders: apps started from the Dock or Start menu get a short PATH, which would miss a
    Tesseract just installed by following install_advice."""
    from app.services.ai_agents import _search_dirs

    folders = _search_dirs()
    if sys.platform == "win32":
        folders += [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Tesseract-OCR"),
                    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR")]
    return shutil.which("tesseract", path=os.pathsep.join(folders))


@lru_cache(maxsize=1)
def _vision_ready() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import Vision

        return bool(Vision.VNRecognizeTextRequest)
    except Exception as exc:  # noqa: BLE001 - a source install without pyobjc-framework-Vision, or a very old macOS
        log.info("Apple's Vision isn't available: %s", exc)
        return False


def _powershell() -> str:
    """Windows PowerShell, not the newer pwsh, which can't reach the system's own WinRT classes."""
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


_WINDOWS_HAS_OCR = """
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
if ([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages.Count -gt 0) { 'yes' }
"""

# Reads the image at the path in SYNTROPY_OCR_IMAGE, and prints each word with its box. Run through Windows PowerShell because
# that reaches the OCR built into Windows 10 and 11 with nothing added to the app, and because it can be stopped if it hangs.
_WINDOWS_READ = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await($operation, $type) {
  $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
  $task.Wait() | Out-Null
  $task.Result
}
[void][Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
[void][Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
[void][Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
$english = [Windows.Globalization.Language]::new('en-US')
$engine = if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($english)) { [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($english) }
          else { [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($null -eq $engine) { exit 3 }
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:SYNTROPY_OCR_IMAGE)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $result.Lines) {
  foreach ($word in $line.Words) {
    $box = $word.BoundingRect
    "$($box.X)`t$($box.Y)`t$($box.Width)`t$($box.Height)`t$($word.Text)"
  }
}
"""


def _windows(script: str, env: Optional[dict[str, str]] = None, timeout: float = OCR_TIMEOUT) -> subprocess.CompletedProcess:
    # From standard input, so the script needs no file of its own and no change to the computer's script policy. PowerShell reads
    # it as typed at a prompt, where a block spanning lines waits for an empty line: without one at the end, everything from the
    # first such block on was never run, and the page came back with no words and no error.
    return subprocess.run([_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "-"],
                          input=(script.rstrip("\n") + "\n\n").encode("utf-8"), capture_output=True, timeout=timeout, check=False,
                          env={**os.environ, **(env or {})}, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


_windows_ok = False


def _windows_ready() -> bool:
    """Only a yes is kept: someone told to add a language can do that without restarting the app."""
    global _windows_ok
    if sys.platform != "win32" or _windows_ok:
        return _windows_ok
    try:
        _windows_ok = _windows(_WINDOWS_HAS_OCR, timeout=20).stdout.decode("utf-8", "replace").strip().endswith("yes")
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("Windows' OCR isn't available: %s", exc)
    return _windows_ok


def engine() -> Optional[str]:
    """What reads photos and scans on this computer: "vision", "windows", "tesseract", or None."""
    if _vision_ready():
        return "vision"
    if _windows_ready():
        return "windows"
    return "tesseract" if tesseract_path() else None


def available() -> bool:
    return engine() is not None


def install_advice() -> str:
    """Only for when no engine works: the Mac and Windows apps normally have one built in."""
    if config.env("SYNTROPY_INSTALL") == "docker":
        return "The Docker image normally includes it: pull the latest image."
    if sys.platform == "darwin":
        return ("On a Mac, run pip install pyobjc-framework-Vision for the reader built into macOS, or install Homebrew from brew.sh "
                "and run brew install tesseract in Terminal. There's no need to restart Syntropy Health after installing Tesseract.")
    if sys.platform == "win32":
        return ("Windows has a reader built in that needs an English language pack: Settings, Time & language, Language & region, "
                "Add a language. Or open PowerShell and run winget install UB-Mannheim.TesseractOCR. There's no need to restart "
                "Syntropy Health.")
    return "On Linux, run sudo apt install tesseract-ocr, or your system's equivalent."


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _png(img: Any) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _vision_pass(img: Any, revision: Optional[int] = None, scale: float = 1.0, across: float = 0.0, down: float = 0.0) -> list[Word]:
    """One look at an image. `scale`, `across` and `down` say how it sits in the page, so its words come back in the page's units."""
    import Foundation
    import objc
    import Vision

    words: list[Word] = []
    width, height = img.size
    data = _png(img)
    with objc.autorelease_pool():
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(Foundation.NSData.dataWithBytes_length_(data, len(data)), None)
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        # Correction turns what's printed into likely English, and lab results are neither English nor to be guessed at.
        request.setUsesLanguageCorrection_(False)
        request.setRecognitionLanguages_(["en-US"])
        if revision:
            request.setRevision_(revision)
        done, error = handler.performRequests_error_([request], None)
        if not done:
            raise OcrError(f"Vision: {error}")
        for seen in request.results() or []:
            top = seen.topCandidates_(1)
            if not top:
                continue
            text, conf = str(top[0].string()), float(top[0].confidence())
            whole = seen.boundingBox()
            for m in re.finditer(r"\S+", text):
                box, _ = top[0].boundingBoxForRange_error_(Foundation.NSMakeRange(m.start(), m.end() - m.start()), None)
                rect = box.boundingBox() if box is not None else None
                if rect is None:       # not told where this word is: its share of the line
                    x = whole.origin.x + whole.size.width * m.start() / max(len(text), 1)
                    w = whole.size.width * (m.end() - m.start()) / max(len(text), 1)
                    y, h = whole.origin.y, whole.size.height
                else:
                    x, y, w, h = rect.origin.x, rect.origin.y, rect.size.width, rect.size.height
                words.append(Word(m.group(), x * width * scale + across, (1 - y - h) * height * scale + down,
                                  (x + w) * width * scale + across, (1 - y) * height * scale + down, conf))
    return words


def _vision_words(img: Any) -> list[Word]:
    """Several looks at the page, because any one of them can miss a lone short word, or read a range upside down ("15-150" as
    "091-91"): the whole page, smaller, in bands and in side-by-side strips (where a short word is larger against what's around it),
    and with the previous version of the recognizer, which reads units better. Then they vote."""
    from PIL import Image

    passes = [_vision_pass(img), _vision_pass(img, revision=2)]
    half = img.resize((img.width // 2, img.height // 2), Image.LANCZOS)
    passes.append(_vision_pass(half, scale=img.width / half.width))
    bands: list[Word] = []
    step = img.height / 5
    for i in range(5):
        top, bottom = max(0, int(i * step - step * 0.15)), min(img.height, int((i + 1) * step + step * 0.15))
        bands += _vision_pass(img.crop((0, top, img.width, bottom)), down=top)
    passes.append(bands)
    strips: list[Word] = []
    for left, right in ((0.0, 0.6), (0.4, 1.0)):
        strips += _vision_pass(img.crop((int(img.width * left), 0, int(img.width * right), img.height)), across=int(img.width * left))
    passes.append(strips)
    return _vote(passes)


def _overlaps(a: Any, b: Any) -> bool:
    across = min(a.right, b.right) - max(a.left, b.left)
    down = min(a.bottom, b.bottom) - max(a.top, b.top)
    return across > 0.5 * min(a.right - a.left, b.right - b.left) and down > 0.5 * min(a.bottom - a.top, b.bottom - b.top)


class _Spot:
    """One place on the page, and what each look read there."""

    def __init__(self, word: Word) -> None:
        self.left, self.top, self.right, self.bottom = word.left, word.top, word.right, word.bottom
        self.readings: dict[int, list[Word]] = {}

    def add(self, look: int, word: Word) -> None:
        """Another word from one look is the same place read as two words. Unless they overlap: two bands saw it, which isn't two votes."""
        said = self.readings.setdefault(look, [])
        if not any(_overlaps(w, word) for w in said):
            said.append(word)


def _vote(passes: list[list[Word]]) -> list[Word]:
    """Each place on the page gets the reading most looks agree on. A word few of them agree on is marked doubtful."""
    if not passes or not any(passes):
        return []
    heights = sorted(w.bottom - w.top for words in passes for w in words)
    bucket = max(heights[len(heights) // 2], 1.0)
    grid: dict[int, list[_Spot]] = {}
    spots: list[_Spot] = []
    for look, words in enumerate(passes):
        for word in sorted(words, key=lambda w: w.left):
            row = int((word.top + word.bottom) / 2 / bucket)
            near = [s for r in (row - 1, row, row + 1) for s in grid.get(r, ()) if _overlaps(s, word)]
            if near:
                near[0].add(look, word)
            else:
                spot = _Spot(word)
                spot.add(look, word)
                grid.setdefault(row, []).append(spot)
                spots.append(spot)
    out = []
    for spot in spots:
        readings = [" ".join(w.text for w in said) for said in spot.readings.values()]
        counts: dict[str, int] = {}
        for reading in readings:
            counts[reading] = counts.get(reading, 0) + 1
        # The most votes, and of equals the earliest look's: readings keep the order of the looks.
        best = max(counts, key=lambda r: (counts[r], -readings.index(r)))
        out.append(Word(best, spot.left, spot.top, spot.right, spot.bottom, 1.0 if counts[best] >= 2 or len(passes) == 1 else 0.3))
    return out


def _tesseract_words(img: Any) -> list[Word]:
    try:
        proc = subprocess.run([tesseract_path() or "tesseract", "stdin", "stdout", "--psm", "6", "-l", "eng", "-c", "preserve_interword_spaces=1", "tsv"],
                              input=_png(img), capture_output=True, timeout=OCR_TIMEOUT, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))     # no console flash on Windows
    except subprocess.TimeoutExpired as exc:
        raise OcrError("Reading the page took too long.") from exc
    if proc.returncode != 0:
        raise OcrError(f"Tesseract: {proc.stderr.decode('utf-8', 'replace')[:200]}")
    words: list[Word] = []
    for row in csv.DictReader(io.StringIO(proc.stdout.decode("utf-8", "replace")), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (row.get("text") or "").strip()
        if text and row.get("level") == "5":
            left, top, w, h = (float(row[k]) for k in ("left", "top", "width", "height"))
            words.append(Word(text, left, top, left + w, top + h, max(float(row.get("conf") or 0), 0) / 100))
    return words


def _windows_words(img: Any) -> list[Word]:
    fd, path = tempfile.mkstemp(suffix=".png")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(_png(img))
        try:
            proc = _windows(_WINDOWS_READ, {"SYNTROPY_OCR_IMAGE": path})
        except subprocess.TimeoutExpired as exc:
            raise OcrError("Reading the page took too long.") from exc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if proc.returncode != 0:
        raise OcrError(f"Windows OCR: {proc.stderr.decode('utf-8', 'replace')[:200]}")
    words: list[Word] = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) == 5 and parts[4].strip():
            x, y, w, h = (float(p.replace(",", ".")) for p in parts[:4])     # a comma where the computer's language writes one
            words.append(Word(parts[4].strip(), x, y, x + w, y + h, None))
    return words


def read_words(img: Any) -> list[Word]:
    """The words on a page image. Raises OcrError when the engine fails."""
    used = engine()
    if used == "vision":
        return _vision_words(img)
    if used == "windows":
        return _windows_words(img)
    if used == "tesseract":
        return _tesseract_words(img)
    raise OcrError("No reader on this computer.")


# ---------------------------------------------------------------------------
# From words to printed lines
# ---------------------------------------------------------------------------

def _printed_lines(words: list[Word]) -> list[list[Word]]:
    """Follows each printed line from left to right, one word to the next, so a page photographed at a slight angle stays on its
    lines where a fixed height across the page would drift onto the next."""
    if not words:
        return []
    height = statistics.median(w.bottom - w.top for w in words)
    lines: list[list[Word]] = []
    for word in sorted(words, key=lambda w: w.left):
        middle = (word.top + word.bottom) / 2
        best, nearest = None, height * 0.55
        for line in lines:
            last = line[-1]
            if last.right > word.left + height * 0.5:       # this word is on top of it: another line
                continue
            off = abs((last.top + last.bottom) / 2 - middle)
            if off < nearest:
                best, nearest = line, off
        if best is None:
            lines.append([word])
        else:
            best.append(word)
    return sorted(lines, key=lambda line: statistics.fmean((w.top + w.bottom) / 2 for w in line))


def lay_out(words: list[Word], page: int, page_height: float, unsure_below: float = 0.0, read_by_ocr: bool = False) -> list[Line]:
    """Printed lines as text, each word placed by where it is on the page: two or more spaces mean a gap in the print."""
    words = [w._replace(text=w.text.translate(LOOK_ALIKES)) for w in words if w.right > w.left and w.bottom > w.top]
    if not words:
        return []
    char = statistics.median((w.right - w.left) / max(len(w.text), 1) for w in words)
    out = []
    for line in _printed_lines(words):
        text = ""
        for word in line:
            column = max(round(word.left / char), len(text) + 1 if text else 0)
            text = text.ljust(column) + word.text
        out.append(Line(text, page, min(w.top for w in line) / page_height, max(w.bottom for w in line) / page_height,
                        frozenset(t for w in line if w.conf is not None and w.conf < unsure_below for t in w.text.split()), read_by_ocr))
    return out
