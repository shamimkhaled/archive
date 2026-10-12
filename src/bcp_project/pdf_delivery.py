"""Watermarked PDF delivery: stamp off the event loop, cache briefly per viewer, serve byte ranges.

PDF viewers fetch large files in ranges. Each viewer's stamped copy is kept for a few minutes
(keyed by document, source, and username, so one person's seal is never served to another),
and range requests are answered from it instead of re-stamping the whole file.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Awaitable, Callable, Dict, Hashable, Iterator, Optional, Tuple

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from .pdf_watermark import stamp_pdf_bytes

CHUNK_BYTES = 256 * 1024
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class StampCache:
    def __init__(self, max_bytes: int, ttl_seconds: float):
        self.max_bytes, self.ttl = max_bytes, ttl_seconds
        self._items: "OrderedDict[Hashable, Tuple[float, bytes]]" = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> Optional[bytes]:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            if item[0] < time.monotonic():
                self._drop(key)
                return None
            self._items.move_to_end(key)
            return item[1]

    def put(self, key: Hashable, data: bytes) -> None:
        if len(data) > self.max_bytes:
            return
        with self._lock:
            if key in self._items:
                self._drop(key)
            self._items[key] = (time.monotonic() + self.ttl, data)
            self._size += len(data)
            while self._size > self.max_bytes and self._items:
                self._drop(next(iter(self._items)))

    def _drop(self, key: Hashable) -> None:
        _, data = self._items.pop(key)
        self._size -= len(data)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._size = 0


_cache = StampCache(
    max_bytes=int(float(os.getenv("PDF_STAMP_CACHE_MB") or 128) * 1024 * 1024),
    ttl_seconds=float(os.getenv("PDF_STAMP_CACHE_SECONDS") or 300),
)


def stamp_cache() -> StampCache:
    return _cache


async def stamped_pdf(key: Hashable, load: Callable[[], Awaitable[bytes]], seal_text: str) -> bytes:
    cached = _cache.get(key)
    if cached is not None:
        return cached
    data = await load()
    stamped = await asyncio.to_thread(stamp_pdf_bytes, data, seal_text)
    _cache.put(key, stamped)
    return stamped


def parse_range(header: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    """Single byte range -> (start, end inclusive). None: no/unsupported header. Raises ValueError if unsatisfiable."""
    match = _RANGE.match((header or "").strip())
    if not match or (not match.group(1) and not match.group(2)):
        return None
    first, last = match.group(1), match.group(2)
    if not first:
        length = int(last)
        if length == 0:
            raise ValueError("empty suffix range")
        return max(0, size - length), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise ValueError("range not satisfiable")
    return start, end


def is_initial_request(request: Request) -> bool:
    """True for a full fetch or the first range; later ranges of the same view are not re-audited."""
    try:
        parsed = parse_range(request.headers.get("range"), 1 << 62)
    except ValueError:
        return False
    return parsed is None or parsed[0] == 0


def _chunks(data: bytes) -> Iterator[bytes]:
    view = memoryview(data)
    for start in range(0, len(view), CHUNK_BYTES):
        yield bytes(view[start : start + CHUNK_BYTES])


def pdf_response(
    request: Request, data: bytes, *, filename: str, disposition: str = "inline", headers: Optional[Dict[str, str]] = None
) -> Response:
    base = {
        "Content-Disposition": f'{disposition}; filename="{filename}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow",
        "Accept-Ranges": "bytes",
        **(headers or {}),
    }
    size = len(data)
    try:
        span = parse_range(request.headers.get("range"), size)
    except ValueError:
        return Response(status_code=416, headers={**base, "Content-Range": f"bytes */{size}"})
    if span is None:
        return StreamingResponse(_chunks(data), media_type="application/pdf", headers={**base, "Content-Length": str(size)})
    start, end = span
    return Response(
        content=data[start : end + 1],
        status_code=206,
        media_type="application/pdf",
        headers={**base, "Content-Range": f"bytes {start}-{end}/{size}"},
    )
