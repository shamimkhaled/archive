"""Split long documents into page-aligned windows for map-reduce model calls.

Windows never cut a page in two unless that single page exceeds the window, in which case
the page is split into parts that keep its page number, so every map output stays citable.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

Page = Tuple[int, str]


def _split_long_page(number: int, text: str, max_chars: int) -> List[Page]:
    parts: List[Page] = []
    rest = text
    while len(rest) > max_chars:
        cut = rest.rfind("\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = rest.rfind(" ", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars
        parts.append((number, rest[:cut].strip()))
        rest = rest[cut:]
    if rest.strip():
        parts.append((number, rest.strip()))
    return parts


def page_windows(pages: Iterable[Page], max_chars: int) -> List[List[Page]]:
    """Group consecutive pages into windows of at most ``max_chars`` characters of page text."""
    windows: List[List[Page]] = []
    current: List[Page] = []
    size = 0
    for number, text in pages:
        text = (text or "").strip()
        if not text:
            continue
        for piece in _split_long_page(number, text, max_chars) if len(text) > max_chars else [(number, text)]:
            if current and size + len(piece[1]) > max_chars:
                windows.append(current)
                current, size = [], 0
            current.append(piece)
            size += len(piece[1])
    if current:
        windows.append(current)
    return windows


def render_window(window: Sequence[Page]) -> str:
    """Window text with page markers the model can cite: ``[Page 3]``."""
    return "\n\n".join(f"[Page {number}]\n{text}" for number, text in window)


def sample_windows(windows: List[List[Page]], limit: int) -> List[List[Page]]:
    """Evenly spaced windows when a document needs more calls than ``limit`` allows."""
    if limit <= 0 or len(windows) <= limit:
        return windows
    step = len(windows) / limit
    return [windows[int(i * step)] for i in range(limit)]
