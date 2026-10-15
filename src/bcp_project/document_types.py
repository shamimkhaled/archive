"""Document type catalog for upload and filters.

Types are not a fixed enum in the database — `documents.doc_type` is free text.
This module provides suggested defaults (including Bank Statement) and merges
any types already used in the archive so new types appear automatically after
the first upload.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence

# Suggested defaults for Sonali Bank archive uploads.
DEFAULT_DOCUMENT_TYPES: Sequence[str] = (
    "Meeting Minutes",
    "Board Paper",
    "Agreement",
    "Bank Statement",
    "Budget",
    "Audit Report",
    "Circular",
    "Office Order",
    "Memo",
    "Policy",
    "Letter",
    "Other",
)


# Types the Board receives as papers; the viewer offers organizers a structured briefing for them.
BOARD_PAPER_TYPES: Sequence[str] = ("Board Paper", "Board Memo", "Memo")


def is_board_paper_type(value: str) -> bool:
    return normalize_document_type(value).casefold() in {t.casefold() for t in BOARD_PAPER_TYPES}


def normalize_document_type(value: str) -> str:
    """Trim and collapse whitespace; keep user casing for display."""
    cleaned = " ".join((value or "").split())
    return cleaned[:64] if cleaned else ""


def merge_document_types(
    *groups: Iterable[str],
    defaults: Sequence[str] = DEFAULT_DOCUMENT_TYPES,
) -> List[str]:
    """Union defaults + any extra labels, case-insensitive de-dupe, stable order."""
    seen = set()
    ordered: List[str] = []

    def _add(label: str) -> None:
        normalized = normalize_document_type(label)
        if not normalized:
            return
        key = normalized.casefold()
        if key in seen:
            return
        seen.add(key)
        ordered.append(normalized)

    for label in defaults:
        _add(label)
    for group in groups:
        for label in group:
            _add(label)
    return ordered
