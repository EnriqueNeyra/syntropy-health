"""Compressed uploads from the iPhone app.

A page of health samples is a few megabytes of repetitive JSON that compresses about tenfold, which matters most away
from home (over Tailscale or a phone connection). The app compresses only when the server's pairing probe lists the
encoding, so older servers keep receiving plain JSON.
"""

from __future__ import annotations

import zlib
from typing import Any, Awaitable, Callable

UPLOAD_ENCODINGS = ("gzip", "deflate")
# Where compressed bodies are accepted: the ingest endpoints, nothing else.
PATH_PREFIX = "/api/ingest/"
# Larger than any batch the ingest endpoints accept, so an honest upload is never refused, while a small compressed
# body can't expand into gigabytes.
MAX_INFLATED = 64 * 1024 * 1024
MAX_COMPRESSED = 32 * 1024 * 1024

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class TooLarge(Exception):
    pass


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
    """ASGI middleware: turns a gzip or deflate request body on the ingest endpoints back into plain JSON."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(PATH_PREFIX):
            return await self.app(scope, receive, send)
        headers = scope.get("headers") or []
        encoding = next((v.decode("latin-1").strip().lower() for k, v in headers if k == b"content-encoding"), "")
        if not encoding or encoding == "identity":
            return await self._plain(scope, receive, send, headers)
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
            if size > MAX_COMPRESSED:
                return await _reply(send, 413, b'{"detail":"Upload too large."}')
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        try:
            body = inflate(b"".join(chunks), encoding)
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


    async def _plain(self, scope: Scope, receive: Receive, send: Send, headers: list) -> None:
        """An uncompressed upload gets the same cap as an inflated one: the body is parsed before the device token is
        checked, so without it anyone on the network could make the server read any amount of JSON."""
        length = next((v for k, v in headers if k == b"content-length"), None)
        if length is not None and length.isdigit() and int(length) > MAX_INFLATED:
            return await _reply(send, 413, b'{"detail":"Upload too large."}')
        size = 0

        async def counted() -> dict[str, Any]:
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > MAX_INFLATED:          # a body sent without its length: stop reading it
                return {"type": "http.disconnect"}
            return message

        await self.app(scope, counted, send)


async def _reply(send: Send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
