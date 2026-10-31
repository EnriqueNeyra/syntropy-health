#!/usr/bin/env python3
"""Checks that the packaged app reads a photographed lab report with nothing installed: starts it on a data folder of its own (SYNTROPY_DATA_DIR),
sends it a picture of one and a scanned PDF of the same page, and looks at what comes back.
    python scripts/check_app_ocr.py "desktop/dist/Syntropy Health.app/Contents/MacOS/Syntropy Health" vision"""

from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from PIL import Image, ImageDraw, ImageFont

CSRF = {"X-Requested-With": "syntropy"}
LINES = ("Quest Diagnostics", "Collected: 09/12/2026", "", "GLUCOSE          105 H          65-99 mg/dL", "CREATININE       0.82           0.50-1.03 mg/dL",
         "SODIUM           140            135-146 mmol/L", "TSH              2.10           0.40-4.50 mIU/L")


def page() -> Image.Image:
    img = Image.new("RGB", (1700, 1100), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=34)
    for i, line in enumerate(LINES):
        draw.text((80, 80 + i * 60), line, fill="black", font=font)
    return img.rotate(1.5, expand=True, fillcolor=(235, 230, 220), resample=Image.BICUBIC)


def main() -> int:
    program, engine = sys.argv[1], sys.argv[2]
    data = os.environ.get("SYNTROPY_DATA_DIR") or tempfile.mkdtemp(prefix="ocr-check-")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "SYNTROPY_DATA_DIR": data, "SYNTROPY_SCHEDULER": "false", "SYNTROPY_MDNS": "0"}
    server = subprocess.Popen([program, "serve", "--port", str(port)], env=env)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            try:
                if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(1)
        else:
            raise SystemExit("The app's server didn't start")
        c = httpx.Client(base_url=url, headers=CSRF, timeout=120)
        r = c.post("/api/setup", json={"password": "correct horse battery", "profile_name": "Check", "accept_terms": True, "timezone": "UTC"})
        assert r.status_code == 200, r.text
        reader = c.get("/api/labs/reader").json()
        print("reader:", reader)
        assert reader["ocr"] and reader["engine"] == engine, reader
        jpeg, scan = io.BytesIO(), io.BytesIO()
        page().save(jpeg, "JPEG", quality=85)
        page().save(scan, "PDF", resolution=200)
        for name, body, kind in (("photo.jpg", jpeg.getvalue(), "image/jpeg"), ("scan.pdf", scan.getvalue(), "application/pdf")):
            r = c.post("/api/labs/extract", files={"file": (name, body, kind)})
            assert r.status_code == 200, r.text
            out = r.json()
            rows = {x["test"].upper(): x for x in out["results"]}
            print(name, out["read_as"], out["engine"], {k: v["value"] for k, v in rows.items()})
            assert out["engine"] == engine and out["pages"], out["engine"]
            assert rows["GLUCOSE"]["value"] == 105 and rows["CREATININE"]["value"] == 0.82 and rows["TSH"]["value"] == 2.1, rows
    finally:
        server.terminate()
        try:
            server.wait(15)
        except subprocess.TimeoutExpired:
            server.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
