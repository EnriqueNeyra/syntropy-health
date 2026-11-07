"""
The encrypted form of a backup: a small header, then the zip in authenticated chunks, so a database of many gigabytes
never has to fit in memory.

    "SYNTROPY-BACKUP\\n"  4-byte header length  header JSON  frames...

    frame: 4-byte length (the top bit marks the last frame), then AES-256-GCM ciphertext and tag

The passphrase goes through scrypt; HKDF with a random salt per file makes that file's key, so the counter in each frame's
nonce never repeats under one key, including for scheduled backups, which reuse one scrypt result. The nonce also carries
the last-frame mark and the header is authenticated with every frame, so chunks can't be swapped or cut off unnoticed.
The header holds no health information.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
from dataclasses import dataclass
from typing import Any, BinaryIO, Iterator, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"SYNTROPY-BACKUP\n"
CHUNK = 1024 * 1024
MIN_PASSPHRASE = 10
_SCRYPT = {"n": 2**17, "r": 8, "p": 1}      # about 128 MB and a third of a second
_LAST = 0x80000000
# What a file may ask for before it has proved anything: it mustn't be able to make this computer run out of memory.
_MAX_N, _MAX_R, _MAX_P, _MAX_HEADER, _MAX_FRAME = 2**22, 32, 16, 64 * 1024, 64 * 1024 * 1024


class BackupFileError(ValueError):
    """What's wrong with this file, in words for the person."""


class WrongPassphrase(BackupFileError):
    def __init__(self) -> None:
        super().__init__("That passphrase doesn't open this backup. Check it and try again.")


class Damaged(BackupFileError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("This backup file is damaged or incomplete, so it can't be relied on. Try another copy or an older "
                         f"backup.{f' ({detail})' if detail else ''}")


@dataclass(frozen=True)
class SealKey:
    """A passphrase after scrypt: it opens every file made with these settings, without the passphrase being kept."""
    material: bytes
    kdf: dict[str, Any]


def _scrypt(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(passphrase.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=128 * n * r * p + 1024 * 1024)


def new_seal_key(passphrase: str) -> SealKey:
    salt = secrets.token_bytes(16)
    return SealKey(_scrypt(passphrase, salt, **_SCRYPT), {"name": "scrypt", **_SCRYPT, "salt": base64.b64encode(salt).decode()})


def _check_value(material: bytes) -> str:
    return base64.b64encode(hmac.new(material, b"syntropy-backup-passphrase-check", hashlib.sha256).digest()[:16]).decode()


def _file_key(material: bytes, salt: bytes) -> AESGCM:
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"syntropy-backup-v1").derive(material)
    return AESGCM(derived)


def _nonce(counter: int, last: bool) -> bytes:
    return struct.pack(">Q", counter) + b"\0\0\0" + (b"\1" if last else b"\0")


class Sealer:
    """A write-only file for ``zipfile``: what's written comes out encrypted in ``out``. ``close`` writes the last frame."""

    def __init__(self, out: BinaryIO, key: SealKey) -> None:
        salt = secrets.token_bytes(32)
        header = json.dumps({"format": 1, "cipher": "AES-256-GCM", "chunk": CHUNK, "kdf": key.kdf,
                             "file_salt": base64.b64encode(salt).decode(), "check": _check_value(key.material)},
                            separators=(",", ":")).encode()
        out.write(MAGIC + struct.pack(">I", len(header)) + header)
        self._out, self._aad, self._gcm = out, MAGIC + header, _file_key(key.material, salt)
        self._buf, self._counter, self._closed = bytearray(), 0, False

    def write(self, data: bytes) -> int:
        self._buf += data
        while len(self._buf) >= CHUNK:
            self._frame(bytes(self._buf[:CHUNK]), False)
            del self._buf[:CHUNK]
        return len(data)

    def flush(self) -> None:
        self._out.flush()

    def _frame(self, plain: bytes, last: bool) -> None:
        sealed = self._gcm.encrypt(_nonce(self._counter, last), plain, self._aad)
        self._out.write(struct.pack(">I", len(sealed) | (_LAST if last else 0)) + sealed)
        self._counter += 1

    def close(self) -> None:
        if not self._closed:
            self._frame(bytes(self._buf), True)
            self._buf.clear()
            self._closed = True


def _read_exactly(fh: BinaryIO, size: int) -> bytes:
    data = fh.read(size)
    if len(data) != size:
        raise Damaged("it ends early")
    return data


def _read_header(fh: BinaryIO) -> tuple[bytes, dict[str, Any]]:
    if fh.read(len(MAGIC)) != MAGIC:
        raise BackupFileError("This isn't a Syntropy Health backup.")
    size = struct.unpack(">I", _read_exactly(fh, 4))[0]
    if size > _MAX_HEADER:
        raise Damaged("its header is the wrong size")
    raw = _read_exactly(fh, size)
    try:
        header = json.loads(raw)
    except ValueError as exc:
        raise Damaged("its header can't be read") from exc
    if not isinstance(header, dict) or header.get("format") != 1:
        raise BackupFileError("This backup was made by a newer version of Syntropy Health. Install the latest version to open it.")
    return raw, header


def _material(header: dict[str, Any], passphrase: Optional[str], key: Optional[SealKey]) -> bytes:
    if key is not None:
        return key.material
    if not passphrase:
        raise WrongPassphrase()
    kdf = header.get("kdf") or {}
    try:
        n, r, p, salt = int(kdf["n"]), int(kdf["r"]), int(kdf["p"]), base64.b64decode(kdf["salt"])
    except (KeyError, TypeError, ValueError) as exc:
        raise Damaged("its header can't be read") from exc
    if kdf.get("name") != "scrypt" or n > _MAX_N or r > _MAX_R or p > _MAX_P or n < 2 or n & (n - 1):
        raise Damaged("its header asks for something unreasonable")
    return _scrypt(passphrase, salt, n, r, p)


def unseal(fh: BinaryIO, passphrase: Optional[str] = None, key: Optional[SealKey] = None) -> Iterator[bytes]:
    """The zip's bytes, a chunk at a time, each authenticated before it's returned. Raises WrongPassphrase before the
    first chunk, and Damaged the moment anything doesn't add up, including a file cut short."""
    raw, header = _read_header(fh)
    material = _material(header, passphrase, key)
    if not hmac.compare_digest(_check_value(material), str(header.get("check"))):
        raise WrongPassphrase()
    try:
        gcm = _file_key(material, base64.b64decode(header["file_salt"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise Damaged("its header can't be read") from exc
    aad = MAGIC + raw
    counter = 0

    def frames() -> Iterator[bytes]:
        nonlocal counter
        while True:
            word = struct.unpack(">I", _read_exactly(fh, 4))[0]
            last, size = bool(word & _LAST), word & ~_LAST
            if size > _MAX_FRAME:
                raise Damaged("a section is the wrong size")
            try:
                plain = gcm.decrypt(_nonce(counter, last), _read_exactly(fh, size), aad)
            except InvalidTag as exc:
                raise Damaged("a section doesn't match its checksum") from exc
            counter += 1
            if last:
                if fh.read(1):
                    raise Damaged("there's extra data after the end")
                yield plain
                return
            yield plain
    return frames()


def is_sealed(first_bytes: bytes) -> bool:
    return first_bytes.startswith(MAGIC)


def check_value(key: SealKey) -> str:
    """What a file sealed with this key says in its header to show which passphrase opens it. A passphrase run through
    scrypt again, with a new salt, as every backup made by hand is, gives another."""
    return _check_value(key.material)


def sealed_with(path: Any) -> Optional[str]:
    """The check value in a sealed backup's header (see check_value), read without opening anything. None for any other file."""
    try:
        with open(path, "rb") as fh:
            _, header = _read_header(fh)
    except (OSError, BackupFileError):
        return None
    check = header.get("check")
    return check if isinstance(check, str) else None
