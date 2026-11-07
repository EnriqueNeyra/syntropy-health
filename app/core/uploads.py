"""What a request body may be, checked before anything reads it, and compressed uploads from the iPhone app.

Every route has a limit, including those anyone can reach without signing in: the body is read and parsed before a
route looks at it, so a password field of a gigabyte has already done its damage by the time it's refused.

A page of health samples is a few megabytes of repetitive JSON that compresses about tenfold, which matters most away
from home (over Tailscale or a phone connection). The app compresses only when the server's pairing probe lists the
encoding, so older servers keep receiving plain JSON.

The larger limits are for someone signed in, or a paired phone, and that's checked before the body is read: FastAPI
parses JSON before a route's dependencies say who's asking, and 64 MB of JSON can be well over a gigabyte once parsed.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zlib
from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

UPLOAD_ENCODINGS = ("gzip", "deflate")
# Where compressed bodies are accepted: the ingest endpoints, nothing else.
PATH_PREFIX = "/api/ingest/"
# Larger than any batch the ingest endpoints accept, so an honest upload is never refused, while a small compressed
# body can't expand into gigabytes.
MAX_INFLATED = 64 * 1024 * 1024
MAX_COMPRESSED = 32 * 1024 * 1024
MB = 1024 * 1024
# How much a compressed body may grow: real batches of samples shrink about tenfold, and even ones made of the same few
# values sixty. A body built to inflate (a few kilobytes of gzip to 64 MB) stops here, and a small one may always reach
# INFLATED_ANYWAY, so an honest batch is never refused for compressing well.
MAX_RATIO = 100
INFLATED_ANYWAY = 8 * MB
# Far more than any form or setting needs.
MAX_BODY = 1 * MB
# Routes that take more. A person's conversations with an AI, kept and sent whole; the rest are files.
LARGER = (("/api/ai/", 16 * MB), ("/mcp", 4 * MB))
# Of those, the ones whose route checks who's asking only once the body is parsed: until a session says who, MAX_BODY.
# (/mcp checks its token before it reads the body.)
SIGNED_IN_FOR_MORE = ("/api/ai/",)
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
# Before setup there's nobody to be signed in: a restore asks its own question.
RESTORE = "/api/restore/check"
# Files, and only from someone signed in, asked before the first byte is read: anyone else would otherwise have the
# server write them to its disk. Well above real files: a heavy Apple Health export is a couple of gigabytes.
UPLOADS = (("/api/imports/apple-health", 16 * 1024 * MB), ("/api/imports/archive", 16 * 1024 * MB), ("/api/imports/fhir", 201 * MB),
           ("/api/labs/extract", 16 * MB), ("/api/labs/report", 16 * MB), (RESTORE, 64 * 1024 * MB))
# Room to leave on the disk when deciding whether a file fits.
DISK_MARGIN = 256 * MB

TOO_LARGE = b'{"detail":"That\'s too large for Syntropy Health to accept."}'

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class TooLarge(Exception):
    pass


def body_limit(path: str) -> int:
    if path.startswith(PATH_PREFIX):
        return MAX_INFLATED
    return next((limit for prefix, limit in (*UPLOADS, *LARGER) if path.startswith(prefix)), MAX_BODY)


def _no_room_for(size: int) -> bool:
    from app.core import config
    for folder in (config.data_dir(), tempfile.gettempdir()):
        try:
            if shutil.disk_usage(folder).free < size + DISK_MARGIN:
                return True
        except OSError:
            continue
    return False


def inflate(body: bytes, encoding: str, limit: int = MAX_INFLATED) -> bytes:
    """Decompresses a gzip or deflate body, refusing one that would grow past ``limit``. "deflate" is meant to be
    zlib-wrapped, but raw deflate is common enough to accept too."""
    attempts = [31] if encoding == "gzip" else [15, -15]
    for i, wbits in enumerate(attempts):
        d = zlib.decompressobj(wbits)
        try:
            out = d.decompress(body, limit + 1)
        except zlib.error:
            if i + 1 < len(attempts):
                continue
            raise
        if len(out) > limit or d.unconsumed_tail:
            raise TooLarge()
        out += d.flush()
        if len(out) > limit:
            raise TooLarge()
        if not d.eof:
            raise zlib.error("incomplete compressed body")
        return out
    raise zlib.error("unreadable compressed body")


class DecompressUploads:
    """ASGI middleware: limits the size of every request body, and turns a gzip or deflate one on the ingest endpoints
    back into plain JSON."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from app.core import auth

        headers = scope.get("headers") or []
        path = scope.get("path", "")
        method = scope.get("method", "")
        if any(path.startswith(prefix) for prefix, _ in UPLOADS) and method == "POST":
            refused = await self._refused_upload(scope, headers)
            if refused:
                return await _reply(send, refused[0], refused[1])
        limit = body_limit(path)
        length = next((v for k, v in headers if k == b"content-length"), b"")
        if length.isdigit() and int(length) > limit:       # too large whoever sends it: said so first, unread
            return await _reply(send, 413, TOO_LARGE)
        if path.startswith(PATH_PREFIX) and method not in SAFE_METHODS:
            # Samples from a paired phone (or the web app): nobody else's body is read at all, let alone inflated.
            refused, known = await self._caller(scope, auth._device_or_user)
            if refused:
                return await _reply(send, refused[0], refused[1])
            if not known:
                limit = MAX_BODY
        elif path.startswith(SIGNED_IN_FOR_MORE) and method not in SAFE_METHODS:
            refused, known = await self._caller(scope, auth._signed_in)
            if refused or not known:
                limit = MAX_BODY        # the route says why it's refused, having read no more than any public one
        if not path.startswith(PATH_PREFIX):
            return await self._plain(scope, receive, send, headers, limit)
        encoding = next((v.decode("latin-1").strip().lower() for k, v in headers if k == b"content-encoding"), "")
        if not encoding or encoding == "identity":
            return await self._plain(scope, receive, send, headers, limit)
        if encoding not in UPLOAD_ENCODINGS:
            return await _reply(send, 415, b'{"detail":"Unsupported Content-Encoding."}')

        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > min(MAX_COMPRESSED, limit):
                return await _reply(send, 413, b'{"detail":"Upload too large."}')
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        try:
            body = inflate(b"".join(chunks), encoding, min(limit, max(INFLATED_ANYWAY, size * MAX_RATIO)))
        except TooLarge:
            return await _reply(send, 413, b'{"detail":"Upload too large."}')
        except zlib.error:
            return await _reply(send, 400, b'{"detail":"The compressed upload could not be read."}')

        plain_headers = [(k, v) for k, v in headers if k not in (b"content-encoding", b"content-length")]
        plain_headers.append((b"content-length", str(len(body)).encode()))
        delivered = False

        async def replay() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app({**scope, "headers": plain_headers}, replay, send)


    async def _refused_upload(self, scope: Scope, headers: list) -> Optional[tuple[int, bytes]]:
        """Why not to read this file at all: nobody's signed in (or it can't fit). A server unable to say goes on, and
        the route answers as usual."""
        from app.core import auth

        if getattr(getattr(scope.get("app"), "state", None), "problem", None):
            return None
        try:
            if scope.get("path", "").startswith(RESTORE):
                from app.api.backups import _may_restore
                await _may_restore(Request(scope))
            else:
                await run_in_threadpool(auth._signed_in, Request(scope))
        except HTTPException as exc:
            return exc.status_code, json.dumps({"detail": exc.detail}).encode()
        length = next((v for k, v in headers if k == b"content-length"), b"")
        if length.isdigit() and await run_in_threadpool(_no_room_for, int(length)):
            return 507, b'{"detail":"There isn\'t enough room on this computer\'s disk for that file. Free up some space and try again."}'
        return None

    async def _caller(self, scope: Scope, check: Callable[[Request], Any]) -> tuple[Optional[tuple[int, bytes]], bool]:
        """Whether whoever sent this may send it, asked before its body is read: (why not, whether that could be said). A
        server that can't say (its database won't open) goes on: the route answers, having read no more than MAX_BODY."""
        if getattr(getattr(scope.get("app"), "state", None), "problem", None):
            return None, False
        try:
            await run_in_threadpool(check, Request(scope))
        except HTTPException as exc:
            return (exc.status_code, json.dumps({"detail": exc.detail}).encode()), True
        return None, True

    async def _plain(self, scope: Scope, receive: Receive, send: Send, headers: list, limit: int) -> None:
        """An uncompressed body gets its limit whoever sent it: it's parsed before the sign-in or device token is checked,
        so without one anyone on the network could make the server read any amount of it."""
        length = next((v for k, v in headers if k == b"content-length"), None)
        if length is not None and length.isdigit() and int(length) > limit:
            return await _reply(send, 413, TOO_LARGE)
        size = 0
        started = refused = False

        async def counted() -> dict[str, Any]:
            nonlocal size, refused
            message = await receive()
            size += len(message.get("body", b""))
            if size > limit:          # a body sent without its length: stop reading it, and answer for the route
                if not started and not refused:
                    await _reply(send, 413, TOO_LARGE)
                refused = True
                return {"type": "http.disconnect"}
            return message

        async def sending(message: dict[str, Any]) -> None:
            nonlocal started
            if not refused:
                started = True
                await send(message)

        await self.app(scope, counted, sending)


async def _reply(send: Send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
