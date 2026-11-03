"""
Files that hold health data for a little while and don't need to outlast this process: a large upload on its way in, the
copy an import reads, a person's archive being put together for download.

Each is encrypted as it's written, with a key made for that file and kept only in memory. So a crash, a power cut or a copy of
the disk leaves nothing readable, there's no key to look after, and what's left behind can simply be deleted at the next start.

AES-256-GCM, a chunk at a time, numbered: the zip files these usually are get read out of order, so any chunk has to open by
itself, and one moved or changed fails its check. A chunk's number is its nonce, which is safe because a key encrypts only
one file and a file is only written once, start to finish.
"""

from __future__ import annotations

import io
import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CHUNK = 256 * 1024
_TAG = 16

_kept: dict[str, tuple[bytes, int]] = {}      # by path: what it takes to read a file back, for as long as this process lasts
_kept_lock = threading.Lock()


class Damaged(OSError):
    """A chunk doesn't check out. The disk's fault, or someone changing the file."""


class PrivateFile(io.RawIOBase):
    """Written once, start to finish, then read in any order, like a temporary file. ``streaming`` is for ZipFile, which then
    writes as it goes rather than coming back to fill in sizes."""

    def __init__(self, backing: Optional[BinaryIO] = None, key: Optional[bytes] = None, size: Optional[int] = None, streaming: bool = False):
        super().__init__()
        self._fh = backing
        self._key = key or secrets.token_bytes(32)
        self._cipher = AESGCM(self._key)
        self._pending = bytearray()
        self._written = 0 if size is None else size
        self._finished = size is not None
        self._streaming = streaming
        self._chunks = 0
        self._position = 0
        self._cached: tuple[int, bytes] = (-1, b"")
        self._rolled = True         # for Starlette: not an in-memory file, so it's read in a worker thread

    def _backing(self) -> BinaryIO:
        if self._fh is None:
            self._fh = tempfile.TemporaryFile()
        return self._fh

    # ------------------------------------------------------------------ writing

    def writable(self) -> bool:
        return not self._finished

    def write(self, data: Any) -> int:
        if self._finished:
            raise OSError("A private file is written once, then read.")
        view = bytes(data)
        self._pending += view
        self._written += len(view)
        while len(self._pending) >= CHUNK:
            self._seal(bytes(self._pending[:CHUNK]))
            del self._pending[:CHUNK]
        return len(view)

    def _seal(self, plain: bytes) -> None:
        self._backing().write(self._cipher.encrypt(self._chunks.to_bytes(12, "big"), plain, None))
        self._chunks += 1

    def finish(self) -> None:
        """Nothing more will be written. Reading does this by itself."""
        if self._finished:
            return
        if self._pending or not self._chunks:
            self._seal(bytes(self._pending))
            self._pending.clear()
        self._backing().flush()
        self._finished = True

    # ------------------------------------------------------------------ reading

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return not self._streaming or self._finished

    def tell(self) -> int:
        return self._position if self._finished else self._written

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if self._streaming and not self._finished:
            raise OSError("It can't be read until it's been written.")
        self.finish()
        base = {os.SEEK_SET: 0, os.SEEK_CUR: self._position, os.SEEK_END: self._written}[whence]
        self._position = max(0, base + offset)
        return self._position

    def _chunk(self, index: int) -> bytes:
        if self._cached[0] == index:
            return self._cached[1]
        fh = self._backing()
        fh.seek(index * (CHUNK + _TAG))
        sealed = fh.read(CHUNK + _TAG)
        try:
            plain = self._cipher.decrypt(index.to_bytes(12, "big"), sealed, None)
        except InvalidTag as exc:
            raise Damaged("A temporary file was changed or damaged on the disk.") from exc
        self._cached = (index, plain)
        return plain

    def read(self, size: int = -1) -> bytes:
        self.finish()
        left = self._written - self._position
        wanted = left if size is None or size < 0 else min(size, left)
        if wanted <= 0:
            return b""
        parts = []
        while wanted > 0:
            index, inner = divmod(self._position, CHUNK)
            piece = self._chunk(index)[inner:inner + wanted]
            if not piece:
                raise Damaged("A temporary file is shorter than it should be.")
            parts.append(piece)
            self._position += len(piece)
            wanted -= len(piece)
        return b"".join(parts)

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    @property
    def size(self) -> int:
        return self._written

    def close(self) -> None:
        if not self.closed and self._fh is not None:
            self._fh.close()
        self._cached = (-1, b"")
        super().close()


class Spool(PrivateFile):
    """Stands in for the SpooledTemporaryFile that Starlette keeps a large upload in. A real one is plain, in the system's
    temporary folder, until the request ends."""

    def __init__(self, max_size: int = 0, **_: Any):
        super().__init__()


def protect_upload_spool() -> None:
    """There's no setting for this. tests/test_private_files.py fails if a newer Starlette stops using the name."""
    from starlette import formparsers

    formparsers.SpooledTemporaryFile = Spool  # type: ignore[attr-defined,assignment,misc]


def create(path: Path, *, streaming: bool = False) -> PrivateFile:
    """A file at this path, private from the moment it exists, readable by this process later with ``open_read``."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    return PrivateFile(os.fdopen(fd, "r+b"), streaming=streaming)


def keep(path: Path, file: PrivateFile) -> None:
    """It's been written, and will be read back through ``open_read``. Closes it."""
    file.finish()
    with _kept_lock:
        _kept[str(path)] = (file._key, file._written)
    file.close()


def open_read(path: Path) -> BinaryIO:
    """A private file that's been kept, or an ordinary one, which is what tests and the command line hand an import."""
    with _kept_lock:
        known = _kept.get(str(path))
    if not known:
        return open(path, "rb")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    return PrivateFile(os.fdopen(fd, "rb"), known[0], known[1])     # type: ignore[return-value]


def forget(path: Path) -> None:
    with _kept_lock:
        _kept.pop(str(path), None)


def stream(file: PrivateFile, block: int = CHUNK) -> Iterator[bytes]:
    """For a response: from the start, closing it at the end."""
    try:
        file.seek(0)
        while data := file.read(block):
            yield data
    finally:
        file.close()
