"""Board-paper briefings: eight cited headings, map-reduce over long papers, and a review lifecycle.

A briefing belongs to one document version. Generation (``status``: pending -> generating ->
ready | failed) reads the paper in page-aligned windows, asks the model for points with a page
and a verbatim quote, drops every point whose quote is not on the paper, and merges the windows.
Review (``state``: ai_draft -> management_reviewed -> approved) is separate: only an AI draft can
be edited or regenerated, and approval hashes the content so later changes are detectable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import ai
from ..ai.guard import with_rules, wrap_untrusted
from ..config import is_production
from ..long_text import page_windows, render_window, sample_windows
from ..models import BoardBriefing, DocumentRecord, DocumentVersion
from ..summary_extractor import DocumentSummary

logger = logging.getLogger("bcp_project.services.briefing")

BRIEFING_HEADINGS: Tuple[Tuple[str, str], ...] = (
    ("subject", "Subject"),
    ("background", "Background"),
    ("proposal", "Proposal"),
    ("financial_implications", "Financial implications"),
    ("risks", "Risks"),
    ("regulatory_considerations", "Regulatory considerations"),
    ("management_recommendation", "Management recommendation"),
    ("decision_required", "Decision required"),
)
HEADING_KEYS = tuple(key for key, _ in BRIEFING_HEADINGS)
HEADING_LABELS = dict(BRIEFING_HEADINGS)

STATE_AI_DRAFT = "ai_draft"
STATE_REVIEWED = "management_reviewed"
STATE_APPROVED = "approved"
BRIEFING_STATES = (STATE_AI_DRAFT, STATE_REVIEWED, STATE_APPROVED)
STATE_LABELS = {
    STATE_AI_DRAFT: "AI draft — not reviewed by management",
    STATE_REVIEWED: "Reviewed by management — awaiting approval",
    STATE_APPROVED: "Approved briefing",
}

STATUS_PENDING = "pending"
STATUS_GENERATING = "generating"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

SOURCE_AI = "ai"
SOURCE_EDITED = "edited"

JOB_BOARD_BRIEFING = "board_briefing"
MAX_POINTS_PER_HEADING = 6
MAX_EDITED_POINTS = 20
MIN_QUOTE_CHARS = 8


class BriefingWorkflowError(Exception):
    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


class BriefingPoint(BaseModel):
    text: str
    page: Optional[int] = None
    quote: Optional[str] = None
    source: str = SOURCE_AI


class BriefingSection(BaseModel):
    points: List[BriefingPoint] = Field(default_factory=list)
    not_found: bool = True


class BoardPaperSummary(DocumentSummary):
    """``DocumentSummary`` plus the eight Board headings. Every point cites a page; an empty
    heading is marked ``not_found`` rather than filled in."""

    subject: BriefingSection = Field(default_factory=BriefingSection)
    background: BriefingSection = Field(default_factory=BriefingSection)
    proposal: BriefingSection = Field(default_factory=BriefingSection)
    financial_implications: BriefingSection = Field(default_factory=BriefingSection)
    risks: BriefingSection = Field(default_factory=BriefingSection)
    regulatory_considerations: BriefingSection = Field(default_factory=BriefingSection)
    management_recommendation: BriefingSection = Field(default_factory=BriefingSection)
    decision_required: BriefingSection = Field(default_factory=BriefingSection)

    def section(self, key: str) -> BriefingSection:
        return getattr(self, key)

    def sections(self) -> List[Tuple[str, str, BriefingSection]]:
        return [(key, label, getattr(self, key)) for key, label in BRIEFING_HEADINGS]

    def headline_text(self, keys: Sequence[str] = ("subject", "proposal", "decision_required")) -> str:
        return " ".join(point.text for key in keys for point in self.section(key).points)


def load_summary(content: Optional[Dict[str, Any]]) -> BoardPaperSummary:
    return BoardPaperSummary.model_validate(content or {})


def content_sha256(content: Optional[Dict[str, Any]]) -> str:
    canonical = json.dumps(content or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- Citation grounding --------------------------------------------------------------

_SPACE = re.compile(r"\s+")
_QUOTE_CHARS = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " "})
_ELLIPSIS = re.compile(r"\.{3,}|\u2026")


def _norm(text: Optional[str]) -> str:
    text = unicodedata.normalize("NFC", text or "").translate(_QUOTE_CHARS)
    return _SPACE.sub(" ", text).strip().casefold()


def _quote_on(fragments: List[str], page_text: str) -> bool:
    return all(fragment in page_text for fragment in fragments)


def ground_point(point: BriefingPoint, page_norms: Dict[int, str]) -> Optional[BriefingPoint]:
    """Keep a point only if its quote is on the paper; fix the page when the quote is elsewhere."""
    fragments = [f.strip() for f in _ELLIPSIS.split(_norm(point.quote)) if len(f.strip()) >= MIN_QUOTE_CHARS]
    if not fragments or not point.text.strip():
        return None
    if point.page in page_norms and _quote_on(fragments, page_norms[point.page]):
        return point
    for number in sorted(page_norms):
        if _quote_on(fragments, page_norms[number]):
            return point.model_copy(update={"page": number})
    return None


def _coerce_points(raw: Any, window_pages: Sequence[int]) -> List[BriefingPoint]:
    points: List[BriefingPoint] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        quote = str(item.get("quote") or "").strip()
        try:
            page = int(item.get("page"))
        except (TypeError, ValueError):
            page = window_pages[0] if len(window_pages) == 1 else None
        if text and quote:
            points.append(BriefingPoint(text=text[:600], page=page, quote=quote[:500]))
    return points


# --- Map-reduce generation ---------------------------------------------------------------

_KEYS_TEXT = ", ".join(HEADING_KEYS)

_MAP_SYSTEM = f"""You prepare Board briefings for Sonali Bank PLC from Board papers.
You receive one part of a paper. Each page starts with a [Page N] marker.
Return a JSON object with exactly these keys: {_KEYS_TEXT}.
Each key maps to a list of points: {{"text": "...", "page": N, "quote": "..."}}.
Rules:
- Use only the text you are given. Never infer, estimate, or add outside knowledge.
- "page" is the number from the [Page N] marker of the page the evidence is on.
- "quote" is copied exactly from that page: 5 to 40 consecutive words, same spelling and language.
- "text" is one short point in the paper's own language (English or Bangla).
- If this part says nothing for a heading, return an empty list for that heading.
- At most 4 points per heading."""

_REDUCE_SYSTEM = f"""You merge notes taken from different parts of one Board paper into a single briefing.
For each heading you receive numbered candidate points. Return a JSON object with exactly these keys:
{_KEYS_TEXT}. Each key maps to a list of {{"text": "...", "ids": [candidate numbers]}}.
Rules:
- Merge duplicates and keep the most decision-relevant points, at most {MAX_POINTS_PER_HEADING} per heading.
- Never add a fact that is not in the candidates you merge.
- Only use ids listed under the same heading. Keep the paper's language."""


def window_chars() -> int:
    return max(4000, int(os.getenv("BRIEFING_WINDOW_CHARS") or 24000))


def max_windows() -> int:
    return max(1, int(os.getenv("BRIEFING_MAX_WINDOWS") or 16))


def _dedupe(points: List[BriefingPoint]) -> List[BriefingPoint]:
    seen, out = set(), []
    for point in points:
        key = _norm(point.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(point)
    return out


def _deterministic_reduce(candidates: Dict[str, List[BriefingPoint]]) -> Dict[str, List[BriefingPoint]]:
    return {key: _dedupe(points)[:MAX_POINTS_PER_HEADING] for key, points in candidates.items()}


def _llm_reduce(
    candidates: Dict[str, List[BriefingPoint]], *, sensitivity: Optional[str]
) -> Optional[Dict[str, List[BriefingPoint]]]:
    blocks = []
    for key in HEADING_KEYS:
        lines = [f"  {i}. {p.text} (p. {p.page})" for i, p in enumerate(candidates[key], 1)]
        blocks.append(f"{key}:\n" + ("\n".join(lines) if lines else "  (none)"))
    try:
        data = ai.chat_json(
            "board_brief",
            with_rules(_REDUCE_SYSTEM),
            wrap_untrusted("\n\n".join(blocks), "briefing_candidates"),
            sensitivity=sensitivity,
            max_tokens=2000,
        )
    except ai.EgressBlocked:
        raise
    except Exception:
        logger.warning("Briefing reduce step failed; using the deterministic merge", exc_info=True)
        return None
    merged: Dict[str, List[BriefingPoint]] = {}
    for key in HEADING_KEYS:
        pool = candidates[key]
        items = data.get(key) if isinstance(data, dict) else None
        if not isinstance(items, list):
            merged[key] = _dedupe(pool)[:MAX_POINTS_PER_HEADING]
            continue
        points: List[BriefingPoint] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            ids = [i for i in (item.get("ids") or []) if isinstance(i, int) and 1 <= i <= len(pool)]
            if not ids:
                continue
            cited = pool[ids[0] - 1]
            text = str(item.get("text") or "").strip() or cited.text
            points.append(cited.model_copy(update={"text": text[:600]}))
        merged[key] = _dedupe(points)[:MAX_POINTS_PER_HEADING] if points or not pool else _dedupe(pool)[:MAX_POINTS_PER_HEADING]
    return merged


def generate_briefing(
    pages: Sequence[Tuple[int, str]], *, sensitivity: Optional[str], title: str = ""
) -> Tuple[BoardPaperSummary, Dict[str, Any]]:
    """Map each window to cited points, ground them against the page text, then reduce.

    Raises ``ai.EgressBlocked`` when policy forbids sending the paper to the configured model.
    """
    page_norms = {number: _norm(text) for number, text in pages if (text or "").strip()}
    if not page_norms:
        raise BriefingWorkflowError("The paper has no extractable text.", status_code=422)
    windows = page_windows([(n, t) for n, t in pages if (t or "").strip()], window_chars())
    used = sample_windows(windows, max_windows())
    label = f" ({title})" if title else ""

    def run(item: Tuple[int, List[Tuple[int, str]]]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        index, window = item
        user = f"Board paper{label}, part {index} of {len(used)}:\n\n{wrap_untrusted(render_window(window), 'board_paper')}"
        try:
            data = ai.chat_json("board_brief", with_rules(_MAP_SYSTEM), user, sensitivity=sensitivity, max_tokens=1800)
        except ai.EgressBlocked:
            raise
        except Exception:
            logger.warning("Briefing map step failed for part %s", index, exc_info=True)
            return None, None
        return (data if isinstance(data, dict) else {}), ai.last_call()

    workers = max(1, int(os.getenv("BRIEFING_MAP_CONCURRENCY") or 3))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, enumerate(used, 1)))
    succeeded = [(data, call) for data, call in results if data is not None]
    if not succeeded:
        raise RuntimeError("The model did not return a usable briefing for any part of the paper.")

    candidates: Dict[str, List[BriefingPoint]] = {key: [] for key in HEADING_KEYS}
    proposed = dropped = 0
    for (data, _), window in zip(results, used):
        if data is None:
            continue
        window_pages = [number for number, _ in window]
        for key in HEADING_KEYS:
            for point in _coerce_points(data.get(key), window_pages):
                proposed += 1
                grounded = ground_point(point, page_norms)
                if grounded is None:
                    dropped += 1
                    continue
                candidates[key].append(grounded)

    reducer = "single"
    merged = candidates
    if len(used) > 1:
        merged = _llm_reduce(candidates, sensitivity=sensitivity) if any(candidates.values()) else None
        reducer = "llm" if merged is not None else "deterministic"
        if merged is None:
            merged = _deterministic_reduce(candidates)
    else:
        merged = _deterministic_reduce(candidates)

    summary = BoardPaperSummary()
    for key in HEADING_KEYS:
        points = sorted(merged[key], key=lambda p: (p.page or 0))
        setattr(summary, key, BriefingSection(points=points, not_found=not points))

    call = next((c for _, c in reversed(succeeded) if c), None) or ai.last_call() or {}
    provenance = {
        "provider": call.get("provider"),
        "model": call.get("model"),
        "route": call.get("source"),
        "at": datetime.utcnow().isoformat() + "Z",
        "map_reduce": {
            "windows": len(windows),
            "used": len(used),
            "failed": len(results) - len(succeeded),
            "reducer": reducer,
        },
        "citations": {"proposed": proposed, "dropped_ungrounded": dropped},
    }
    return summary, provenance


# --- Editing -------------------------------------------------------------------------------

_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_CITE = re.compile(r"\[\s*(?:p|pp|page|পৃ|পৃষ্ঠা)\.?\s*([0-9০-৯]{1,4})\s*\]\s*$", re.IGNORECASE)
_NOT_FOUND = re.compile(r"^\[?\s*(?:not found|পাওয়া যায়নি)\s*\]?$", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def section_to_text(section: BriefingSection) -> str:
    if not section.points:
        return "[not found]"
    return "\n".join(f"- {p.text} [p. {p.page}]" for p in section.points)


def parse_section_text(text: str, existing: BriefingSection, page_count: Optional[int]) -> BriefingSection:
    """One point per line ending in ``[p. N]``; ``[not found]`` (or nothing) marks the heading empty.

    A line that matches an existing point keeps that point's quote, so unchanged AI points stay
    traceable to their evidence.
    """
    lines = [_BULLET.sub("", line).strip() for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines or (len(lines) == 1 and _NOT_FOUND.match(lines[0])):
        return BriefingSection(points=[], not_found=True)
    if len(lines) > MAX_EDITED_POINTS:
        raise BriefingWorkflowError(f"At most {MAX_EDITED_POINTS} points per heading.", status_code=400)
    known = {(_norm(p.text), p.page): p for p in existing.points}
    points: List[BriefingPoint] = []
    for line in lines:
        match = _CITE.search(line)
        if not match:
            raise BriefingWorkflowError(f"Every point needs a page reference such as [p. 3]: “{line[:80]}”", status_code=400)
        page = int(match.group(1).translate(_BN_DIGITS))
        if page < 1 or (page_count and page > page_count):
            raise BriefingWorkflowError(f"Page {page} is not in this paper.", status_code=400)
        body = line[: match.start()].strip()
        if not body:
            raise BriefingWorkflowError("A point cannot be only a page reference.", status_code=400)
        points.append(known.get((_norm(body), page)) or BriefingPoint(text=body[:600], page=page, source=SOURCE_EDITED))
    return BriefingSection(points=points, not_found=False)


# --- Workflow --------------------------------------------------------------------------------


def four_eyes_required() -> bool:
    """Approver must differ from the reviewer and the last editor. On by default in production."""
    raw = (os.getenv("BRIEFING_FOUR_EYES") or "").strip().lower()
    if not raw:
        return is_production()
    return raw in {"1", "true", "yes", "on"}


def briefing_state(briefing: BoardBriefing) -> str:
    state = (briefing.state or "").strip()
    return state if state in BRIEFING_STATES else STATE_AI_DRAFT


def require_editable(briefing: BoardBriefing) -> None:
    if briefing_state(briefing) != STATE_AI_DRAFT:
        raise BriefingWorkflowError("Only an AI draft can be changed. Return or reopen the briefing first.")
    if briefing.status != STATUS_READY:
        raise BriefingWorkflowError("The briefing is not ready yet.")


def save_section(
    briefing: BoardBriefing,
    key: str,
    text: str,
    username: str,
    *,
    page_count: Optional[int] = None,
    now: Optional[datetime] = None,
) -> bool:
    if key not in HEADING_KEYS:
        raise BriefingWorkflowError("Unknown heading.", status_code=400)
    require_editable(briefing)
    summary = load_summary(briefing.content)
    section = parse_section_text(text, summary.section(key), page_count)
    if section == summary.section(key):
        return False
    setattr(summary, key, section)
    briefing.content = summary.model_dump()
    briefing.source = SOURCE_EDITED
    briefing.edited_by = username
    briefing.edited_at = now or datetime.utcnow()
    return True


def mark_reviewed(briefing: BoardBriefing, username: str, *, now: Optional[datetime] = None) -> None:
    require_editable(briefing)
    briefing.state = STATE_REVIEWED
    briefing.reviewed_by = username
    briefing.reviewed_at = now or datetime.utcnow()
    briefing.review_note = None


def approval_blocker(briefing: BoardBriefing, username: str) -> Optional[str]:
    if briefing_state(briefing) != STATE_REVIEWED:
        return "Only a briefing reviewed by management can be approved."
    if four_eyes_required() and username in {briefing.reviewed_by, briefing.edited_by}:
        return "Four-eyes rule: the approver must be someone other than the reviewer and the last editor."
    return None


def approve(briefing: BoardBriefing, username: str, *, now: Optional[datetime] = None) -> str:
    blocker = approval_blocker(briefing, username)
    if blocker:
        state_ok = briefing_state(briefing) == STATE_REVIEWED
        raise BriefingWorkflowError(blocker, status_code=403 if state_ok else 409)
    digest = content_sha256(briefing.content)
    briefing.state = STATE_APPROVED
    briefing.approved_by = username
    briefing.approved_at = now or datetime.utcnow()
    briefing.sha256 = digest
    return digest


def return_to_draft(briefing: BoardBriefing, username: str, note: str) -> None:
    note = (note or "").strip()
    if briefing_state(briefing) != STATE_REVIEWED:
        raise BriefingWorkflowError("Only a reviewed briefing can be returned.")
    if not note:
        raise BriefingWorkflowError("Say what needs to change.", status_code=400)
    briefing.state = STATE_AI_DRAFT
    briefing.review_note = f"Returned by {username}: {note}"[:2000]
    briefing.reviewed_by = None
    briefing.reviewed_at = None


def reopen(briefing: BoardBriefing, username: str, reason: str) -> None:
    reason = (reason or "").strip()
    if briefing_state(briefing) != STATE_APPROVED:
        raise BriefingWorkflowError("Only an approved briefing can be reopened.")
    if not reason:
        raise BriefingWorkflowError("A reason is required to reopen an approved briefing.", status_code=400)
    briefing.state = STATE_AI_DRAFT
    briefing.review_note = f"Reopened by {username}: {reason}"[:2000]
    briefing.reviewed_by = briefing.reviewed_at = None
    briefing.approved_by = briefing.approved_at = None
    briefing.sha256 = None


def briefing_view(briefing: Optional[BoardBriefing]) -> Optional[Dict[str, Any]]:
    if briefing is None:
        return None

    def iso(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() + "Z" if value else None

    state = briefing_state(briefing)
    summary = load_summary(briefing.content) if briefing.content else None
    return {
        "id": briefing.id,
        "doc_id": briefing.doc_id,
        "version": briefing.version,
        "state": state,
        "state_label": STATE_LABELS[state],
        "status": briefing.status,
        "error": briefing.error,
        "source": briefing.source,
        "provenance": briefing.provenance or {},
        "sections": [
            {
                "key": key,
                "label": label,
                "not_found": section.not_found,
                "points": [p.model_dump() for p in section.points],
                "edit_text": section_to_text(section),
            }
            for key, label, section in (summary.sections() if summary else [])
        ],
        "requested_by": briefing.requested_by,
        "generated_at": iso(briefing.generated_at),
        "edited_by": briefing.edited_by,
        "edited_at": iso(briefing.edited_at),
        "reviewed_by": briefing.reviewed_by,
        "reviewed_at": iso(briefing.reviewed_at),
        "approved_by": briefing.approved_by,
        "approved_at": iso(briefing.approved_at),
        "review_note": briefing.review_note,
        "sha256": briefing.sha256,
    }


# --- Persistence -------------------------------------------------------------------------------


async def current_version_number(db: AsyncSession, document: DocumentRecord) -> int:
    """The searchable (approved) version; documents from before versioning count as v1."""
    from .ingest import VERSION_APPROVED

    number = (
        await db.execute(
            select(DocumentVersion.version)
            .where(DocumentVersion.doc_id == document.doc_id, DocumentVersion.status == VERSION_APPROVED)
            .order_by(DocumentVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return int(number or 1)


async def get_briefing(db: AsyncSession, doc_id: str, version: int, *, for_update: bool = False) -> Optional[BoardBriefing]:
    stmt = select(BoardBriefing).where(BoardBriefing.doc_id == doc_id, BoardBriefing.version == version)
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def latest_briefing(db: AsyncSession, doc_id: str, *, approved_only: bool = False) -> Optional[BoardBriefing]:
    stmt = select(BoardBriefing).where(BoardBriefing.doc_id == doc_id)
    if approved_only:
        stmt = stmt.where(BoardBriefing.state == STATE_APPROVED)
    return (await db.execute(stmt.order_by(BoardBriefing.version.desc()).limit(1))).scalar_one_or_none()


async def load_paper_pages(db: AsyncSession, document: DocumentRecord, version: int) -> List[Tuple[int, str]]:
    """Stored page text for the version; papers archived before page storage are parsed on the fly."""
    from .ingest import load_pages

    pages = await load_pages(db, document.doc_id, version)
    if pages:
        return [(p.page_number, p.text or "") for p in pages]
    from ..deps import _load_stored_pdf_bytes
    from ..ocr import extract_pages

    ver = (
        await db.execute(
            select(DocumentVersion).where(DocumentVersion.doc_id == document.doc_id, DocumentVersion.version == version)
        )
    ).scalar_one_or_none()
    source = (ver.original_location or ver.file_location) if ver else document.file_location
    content_type = (ver.content_type if ver else None) or "application/pdf"
    data = await _load_stored_pdf_bytes(source, resource_id=document.doc_id)
    extracted = await asyncio.to_thread(extract_pages, data, content_type)
    return [(p.page_number, p.text or "") for p in extracted]


async def request_briefing(db: AsyncSession, document: DocumentRecord, username: str) -> BoardBriefing:
    """Create or reset the briefing row for the current version; the caller commits and schedules it."""
    version = await current_version_number(db, document)
    row = await get_briefing(db, document.doc_id, version, for_update=True)
    if row is None:
        row = BoardBriefing(doc_id=document.doc_id, version=version, requested_by=username)
        db.add(row)
    else:
        if briefing_state(row) != STATE_AI_DRAFT:
            raise BriefingWorkflowError("This briefing is under review or approved. Return or reopen it to regenerate.")
        if row.status == STATUS_GENERATING:
            raise BriefingWorkflowError("A briefing is already being generated.")
        row.requested_by = username
    row.state = STATE_AI_DRAFT
    row.status = STATUS_PENDING
    row.error = None
    await db.flush()
    return row


async def run_board_briefing(briefing_id: int, *, session_factory=None) -> bool:
    """Worker entry point. Failures are recorded on the row; an egress block is not retried."""
    from ..db import get_session

    factory = session_factory or get_session
    async with factory() as db:
        briefing = await db.get(BoardBriefing, int(briefing_id))
        if briefing is None:
            return False
        if briefing_state(briefing) != STATE_AI_DRAFT:
            logger.info("Briefing %s is %s; not regenerating", briefing_id, briefing.state)
            return False
        document = (
            await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == briefing.doc_id))
        ).scalar_one_or_none()
        if document is None:
            briefing.status = STATUS_FAILED
            briefing.error = "The paper no longer exists."
            await db.commit()
            return False
        briefing.status = STATUS_GENERATING
        await db.commit()
        try:
            pages = await load_paper_pages(db, document, briefing.version)
            summary, provenance = await asyncio.to_thread(
                generate_briefing, pages, sensitivity=document.sensitivity, title=document.title or document.doc_id
            )
        except ai.EgressBlocked as exc:
            logger.warning("Briefing %s blocked by egress policy: %s", briefing_id, exc)
            briefing.status = STATUS_FAILED
            briefing.error = (
                "AI policy does not allow this paper's sensitivity to be sent to the configured model. "
                "Route the board_brief task to a local model to brief restricted papers."
            )
            await db.commit()
            return False
        except Exception as exc:
            logger.exception("Briefing %s failed", briefing_id)
            briefing.status = STATUS_FAILED
            briefing.error = str(exc)[:500] or exc.__class__.__name__
            await db.commit()
            return False

        try:
            base = DocumentSummary.model_validate(
                {k: v for k, v in (document.summary_json or {}).items() if k in DocumentSummary.model_fields}
            )
        except ValueError:
            base = DocumentSummary()
        for field in DocumentSummary.model_fields:
            setattr(summary, field, getattr(base, field))
        briefing.content = summary.model_dump()
        briefing.provenance = provenance
        briefing.status = STATUS_READY
        briefing.source = SOURCE_AI
        briefing.error = None
        briefing.generated_at = datetime.utcnow()
        briefing.edited_by = None
        briefing.edited_at = None
        await db.commit()
        logger.info(
            "Briefing %s ready for %s v%s (%s windows)",
            briefing_id, briefing.doc_id, briefing.version, provenance["map_reduce"]["used"],
        )
        return True
