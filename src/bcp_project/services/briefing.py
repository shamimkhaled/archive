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
from dataclasses import dataclass
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
    ("decision_required", "Required Board decision"),
)
HEADING_KEYS = tuple(key for key, _ in BRIEFING_HEADINGS)
HEADING_LABELS = dict(BRIEFING_HEADINGS)
# Headings that carry risk for the Board; highlighted and gathered into the key-risks strip.
ATTENTION_KEYS = ("risks", "financial_implications", "regulatory_considerations")

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
    # Which paper the page belongs to; None on a single-paper briefing.
    doc_id: Optional[str] = None


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


def _coerce_points(raw: Any, window_pages: Sequence[int], doc_id: Optional[str] = None) -> List[BriefingPoint]:
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
            points.append(BriefingPoint(text=text[:600], page=page, quote=quote[:500], doc_id=doc_id))
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

_PACKAGE_MAP_SYSTEM = f"""You prepare Board briefings for Sonali Bank PLC from a package of Board papers:
a memo (M) with its annexures (A1, A2, ...) and supporting papers (S1, ...).
You receive one part of ONE paper in the package. Each page starts with a [LABEL · Page N] marker.
Return a JSON object with exactly these keys: {_KEYS_TEXT}.
Each key maps to a list of points: {{"text": "...", "page": N, "quote": "..."}}.
Rules:
- Use only the text you are given. Never infer, estimate, or add outside knowledge.
- "page" is the number N from the marker of the page the evidence is on.
- "quote" is copied exactly from that page: 5 to 40 consecutive words, same spelling and language.
- "text" is one short point in the paper's own language (English or Bangla).
- Risks, financial effects and regulatory issues in annexures matter as much as those in the memo.
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


@dataclass
class Paper:
    """One paper to brief. ``doc_id``/``label`` are set for package members (M, A1, S1 ...)."""

    pages: Sequence[Tuple[int, str]]
    doc_id: Optional[str] = None
    label: str = ""
    title: str = ""
    role: str = "memo"


def _dedupe(points: List[BriefingPoint]) -> List[BriefingPoint]:
    seen, out = set(), []
    for point in points:
        key = _norm(point.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(point)
    return out


def _cite_label(point: BriefingPoint, labels: Dict[Optional[str], str]) -> str:
    label = labels.get(point.doc_id) or ""
    return f"{label} p. {point.page}" if label else f"p. {point.page}"


def _deterministic_reduce(candidates: Dict[str, List[BriefingPoint]]) -> Dict[str, List[BriefingPoint]]:
    return {key: _dedupe(points)[:MAX_POINTS_PER_HEADING] for key, points in candidates.items()}


def _llm_reduce(
    candidates: Dict[str, List[BriefingPoint]],
    *,
    sensitivity: Optional[str],
    labels: Optional[Dict[Optional[str], str]] = None,
) -> Optional[Dict[str, List[BriefingPoint]]]:
    labels = labels or {}
    blocks = []
    for key in HEADING_KEYS:
        lines = [f"  {i}. {p.text} ({_cite_label(p, labels)})" for i, p in enumerate(candidates[key], 1)]
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


def _allocate_windows(counts: Sequence[int], budget: int) -> List[int]:
    """Window budget per paper: the memo (index 0) gets at least half, every other paper at least one."""
    if len(counts) == 1:
        return [min(counts[0], budget)]
    budget = max(budget, 2 * (len(counts) - 1))
    alloc = [0] * len(counts)
    alloc[0] = min(counts[0], max(1, budget // 2))
    remaining = budget - alloc[0]
    while remaining > 0:
        progressed = False
        for i in range(1, len(counts)):
            if remaining and alloc[i] < counts[i]:
                alloc[i] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    alloc[0] = min(counts[0], alloc[0] + remaining)
    return alloc


def _generate(papers: Sequence[Paper], *, sensitivity: Optional[str]) -> Tuple[BoardPaperSummary, Dict[str, Any]]:
    """Map every paper's windows to cited points, ground each against its own paper, then reduce.

    Windows never mix papers, so a point's ``doc_id`` is the paper its window came from and its
    quote must be on that paper; a quote that only appears in another paper is dropped.
    """
    package = len(papers) > 1 or any(p.label for p in papers)
    norms: Dict[Optional[str], Dict[int, str]] = {}
    per_paper: List[List[List[Tuple[int, str]]]] = []
    for paper in papers:
        texts = [(n, t) for n, t in paper.pages if (t or "").strip()]
        norms[paper.doc_id] = {number: _norm(text) for number, text in texts}
        per_paper.append(page_windows(texts, window_chars()))
    if not any(norms.values()):
        raise BriefingWorkflowError("The paper has no extractable text.", status_code=422)

    alloc = _allocate_windows([len(w) for w in per_paper], max_windows())
    jobs: List[Tuple[Paper, List[Tuple[int, str]]]] = []
    paper_stats = []
    for paper, windows, quota in zip(papers, per_paper, alloc):
        chosen = sample_windows(windows, quota) if windows else []
        jobs.extend((paper, window) for window in chosen)
        paper_stats.append(
            {"doc_id": paper.doc_id, "label": paper.label, "windows": len(windows), "used": len(chosen)}
        )
    total_windows = sum(len(w) for w in per_paper)
    system = _PACKAGE_MAP_SYSTEM if package else _MAP_SYSTEM

    def run(item: Tuple[int, Tuple[Paper, List[Tuple[int, str]]]]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        index, (paper, window) = item
        name = f" ({paper.title})" if paper.title else ""
        if package:
            head = f"Paper {paper.label}{name} — {paper.role}, part {index} of {len(jobs)}:"
        else:
            head = f"Board paper{name}, part {index} of {len(jobs)}:"
        user = f"{head}\n\n{wrap_untrusted(render_window(window, paper.label if package else ''), 'board_paper')}"
        try:
            data = ai.chat_json("board_brief", with_rules(system), user, sensitivity=sensitivity, max_tokens=1800)
        except ai.EgressBlocked:
            raise
        except Exception:
            logger.warning("Briefing map step failed for part %s", index, exc_info=True)
            return None, None
        return (data if isinstance(data, dict) else {}), ai.last_call()

    workers = max(1, int(os.getenv("BRIEFING_MAP_CONCURRENCY") or 3))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, enumerate(jobs, 1)))
    succeeded = [(data, call) for data, call in results if data is not None]
    if not succeeded:
        raise RuntimeError("The model did not return a usable briefing for any part of the paper.")

    candidates: Dict[str, List[BriefingPoint]] = {key: [] for key in HEADING_KEYS}
    proposed = dropped = 0
    for (data, _), (paper, window) in zip(results, jobs):
        if data is None:
            continue
        window_pages = [number for number, _ in window]
        for key in HEADING_KEYS:
            for point in _coerce_points(data.get(key), window_pages, paper.doc_id if package else None):
                proposed += 1
                grounded = ground_point(point, norms[paper.doc_id])
                if grounded is None:
                    dropped += 1
                    continue
                candidates[key].append(grounded)

    labels = {paper.doc_id: paper.label for paper in papers} if package else {}
    reducer = "single"
    if len(jobs) > 1:
        merged = _llm_reduce(candidates, sensitivity=sensitivity, labels=labels) if any(candidates.values()) else None
        reducer = "llm" if merged is not None else "deterministic"
        if merged is None:
            merged = _deterministic_reduce(candidates)
    else:
        merged = _deterministic_reduce(candidates)

    order = {paper.doc_id: i for i, paper in enumerate(papers)}
    summary = BoardPaperSummary()
    for key in HEADING_KEYS:
        points = sorted(merged[key], key=lambda p: (order.get(p.doc_id, 0), p.page or 0))
        setattr(summary, key, BriefingSection(points=points, not_found=not points))

    call = next((c for _, c in reversed(succeeded) if c), None) or ai.last_call() or {}
    provenance = {
        "provider": call.get("provider"),
        "model": call.get("model"),
        "route": call.get("source"),
        "at": datetime.utcnow().isoformat() + "Z",
        "map_reduce": {
            "windows": total_windows,
            "used": len(jobs),
            "failed": len(results) - len(succeeded),
            "reducer": reducer,
        },
        "citations": {"proposed": proposed, "dropped_ungrounded": dropped},
    }
    if package:
        provenance["papers"] = paper_stats
    return summary, provenance


def generate_briefing(
    pages: Sequence[Tuple[int, str]], *, sensitivity: Optional[str], title: str = ""
) -> Tuple[BoardPaperSummary, Dict[str, Any]]:
    """Brief one paper. Raises ``ai.EgressBlocked`` when policy forbids sending it to the model."""
    return _generate([Paper(pages=pages, title=title)], sensitivity=sensitivity)


def generate_package_briefing(
    papers: Sequence[Paper], *, sensitivity: Optional[str]
) -> Tuple[BoardPaperSummary, Dict[str, Any]]:
    """Brief a memo with its annexures; ``papers[0]`` is the memo. Every point names its paper."""
    if not papers:
        raise BriefingWorkflowError("The package has no papers.", status_code=422)
    return _generate(papers, sensitivity=sensitivity)


# --- Editing -------------------------------------------------------------------------------

_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_CITE = re.compile(
    r"\[\s*(?:(?-i:(M|[AS]\d{1,2}))\s*[·.,:-]?\s*)?(?:p|pp|page|পৃ|পৃষ্ঠা)\.?\s*([0-9০-৯]{1,4})\s*\]\s*$",
    re.IGNORECASE,
)
_NOT_FOUND = re.compile(r"^\[?\s*(?:not found|পাওয়া যায়নি)\s*\]?$", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")
MEMO_LABEL = "M"


def member_labels(members: Optional[Sequence[Dict[str, Any]]]) -> Dict[str, str]:
    return {str(m["doc_id"]): str(m.get("label") or "") for m in members or [] if m.get("doc_id")}


def _cite(point: BriefingPoint, labels: Optional[Dict[str, str]]) -> str:
    label = (labels or {}).get(point.doc_id or "")
    return f"[{label} p. {point.page}]" if label else f"[p. {point.page}]"


def section_to_text(section: BriefingSection, labels: Optional[Dict[str, str]] = None) -> str:
    if not section.points:
        return "[not found]"
    return "\n".join(f"- {p.text} {_cite(p, labels)}" for p in section.points)


def parse_section_text(
    text: str,
    existing: BriefingSection,
    page_count: Optional[int],
    *,
    members: Optional[Sequence[Dict[str, Any]]] = None,
) -> BriefingSection:
    """One point per line ending in ``[p. N]`` (``[A1 p. N]`` in a package; no label means the memo).

    ``[not found]`` (or nothing) marks the heading empty. A line that matches an existing point
    keeps that point's quote, so unchanged AI points stay traceable to their evidence.
    """
    lines = [_BULLET.sub("", line).strip() for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines or (len(lines) == 1 and _NOT_FOUND.match(lines[0])):
        return BriefingSection(points=[], not_found=True)
    if len(lines) > MAX_EDITED_POINTS:
        raise BriefingWorkflowError(f"At most {MAX_EDITED_POINTS} points per heading.", status_code=400)
    by_label = {
        str(m.get("label")): (str(m["doc_id"]), m.get("page_count")) for m in members or [] if m.get("doc_id")
    }
    known = {(_norm(p.text), p.doc_id, p.page): p for p in existing.points}
    points: List[BriefingPoint] = []
    for line in lines:
        match = _CITE.search(line)
        if not match:
            example = "[A1 p. 3]" if by_label else "[p. 3]"
            raise BriefingWorkflowError(f"Every point needs a page reference such as {example}: “{line[:80]}”", status_code=400)
        label = match.group(1)
        doc_id: Optional[str] = None
        limit = page_count
        if by_label:
            label = label or MEMO_LABEL
            if label not in by_label:
                raise BriefingWorkflowError(f"There is no paper {label} in this package.", status_code=400)
            doc_id, limit = by_label[label]
        elif label and label != MEMO_LABEL:
            raise BriefingWorkflowError("This briefing covers one paper; cite pages as [p. 3].", status_code=400)
        page = int(match.group(2).translate(_BN_DIGITS))
        if page < 1 or (limit and page > limit):
            where = f"paper {label}" if by_label else "this paper"
            raise BriefingWorkflowError(f"Page {page} is not in {where}.", status_code=400)
        body = line[: match.start()].strip()
        if not body:
            raise BriefingWorkflowError("A point cannot be only a page reference.", status_code=400)
        points.append(
            known.get((_norm(body), doc_id, page))
            or BriefingPoint(text=body[:600], page=page, source=SOURCE_EDITED, doc_id=doc_id)
        )
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
    section = parse_section_text(text, summary.section(key), page_count, members=briefing.members if getattr(briefing, "package_id", None) else None)
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
    members = list(getattr(briefing, "members", None) or []) if getattr(briefing, "package_id", None) else []
    labels = member_labels(members)
    return {
        "id": briefing.id,
        "doc_id": briefing.doc_id,
        "version": briefing.version,
        "package_id": getattr(briefing, "package_id", None),
        "package_revision": getattr(briefing, "package_revision", None),
        "members": members,
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
                "points": [{**p.model_dump(), "label": labels.get(p.doc_id or "")} for p in section.points],
                "edit_text": section_to_text(section, labels),
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
    stmt = select(BoardBriefing).where(
        BoardBriefing.doc_id == doc_id, BoardBriefing.version == version, BoardBriefing.package_id.is_(None)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def latest_briefing(db: AsyncSession, doc_id: str, *, approved_only: bool = False) -> Optional[BoardBriefing]:
    stmt = select(BoardBriefing).where(BoardBriefing.doc_id == doc_id, BoardBriefing.package_id.is_(None))
    if approved_only:
        stmt = stmt.where(BoardBriefing.state == STATE_APPROVED)
    return (await db.execute(stmt.order_by(BoardBriefing.version.desc()).limit(1))).scalar_one_or_none()


async def get_package_briefing(
    db: AsyncSession, package_id: int, revision: int, *, for_update: bool = False
) -> Optional[BoardBriefing]:
    stmt = select(BoardBriefing).where(
        BoardBriefing.package_id == package_id, BoardBriefing.package_revision == revision
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def latest_package_briefing(
    db: AsyncSession, package_id: int, *, approved_only: bool = False
) -> Optional[BoardBriefing]:
    stmt = select(BoardBriefing).where(BoardBriefing.package_id == package_id)
    if approved_only:
        stmt = stmt.where(BoardBriefing.state == STATE_APPROVED)
    return (await db.execute(stmt.order_by(BoardBriefing.package_revision.desc()).limit(1))).scalar_one_or_none()


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
        _reset_for_generation(row, username)
    row.state = STATE_AI_DRAFT
    row.status = STATUS_PENDING
    row.error = None
    await db.flush()
    return row


def _reset_for_generation(row: BoardBriefing, username: str) -> None:
    if briefing_state(row) != STATE_AI_DRAFT:
        raise BriefingWorkflowError("This briefing is under review or approved. Return or reopen it to regenerate.")
    if row.status == STATUS_GENERATING:
        raise BriefingWorkflowError("A briefing is already being generated.")
    row.requested_by = username


async def request_package_briefing(
    db: AsyncSession, package: Any, members: List[Dict[str, Any]], username: str
) -> BoardBriefing:
    """Create or reset the briefing for the package's current revision; ``members`` is the snapshot."""
    memo = next((m for m in members if m.get("role") == "memo"), None)
    if memo is None:
        raise BriefingWorkflowError("Add the memo to the package first.", status_code=400)
    row = await get_package_briefing(db, package.id, package.revision, for_update=True)
    if row is None:
        row = BoardBriefing(
            doc_id=memo["doc_id"],
            version=int(memo.get("version") or 1),
            package_id=package.id,
            package_revision=package.revision,
            requested_by=username,
        )
        db.add(row)
    else:
        _reset_for_generation(row, username)
        row.doc_id = memo["doc_id"]
        row.version = int(memo.get("version") or 1)
    row.members = members
    row.state = STATE_AI_DRAFT
    row.status = STATUS_PENDING
    row.error = None
    await db.flush()
    return row


async def _package_papers(db: AsyncSession, briefing: BoardBriefing) -> Tuple[List[Paper], List[DocumentRecord]]:
    members = sorted(briefing.members or [], key=lambda m: (m.get("role") != "memo", m.get("seq") or 0))
    ids = [str(m["doc_id"]) for m in members]
    docs = {
        d.doc_id: d
        for d in (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id.in_(ids)))).scalars().all()
    }
    missing = [doc_id for doc_id in ids if doc_id not in docs]
    if missing:
        raise BriefingWorkflowError(f"Paper {', '.join(missing)} in this package no longer exists.", status_code=404)
    papers = []
    for member in members:
        document = docs[str(member["doc_id"])]
        pages = await load_paper_pages(db, document, int(member.get("version") or 1))
        papers.append(
            Paper(
                pages=pages,
                doc_id=document.doc_id,
                label=str(member.get("label") or ""),
                title=document.title or document.doc_id,
                role=str(member.get("role") or "annexure"),
            )
        )
    return papers, [docs[i] for i in ids]


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
            if briefing.package_id:
                papers, member_docs = await _package_papers(db, briefing)
                sensitivity = ai.max_sensitivity(d.sensitivity for d in member_docs)
                summary, provenance = await asyncio.to_thread(generate_package_briefing, papers, sensitivity=sensitivity)
            else:
                pages = await load_paper_pages(db, document, briefing.version)
                summary, provenance = await asyncio.to_thread(
                    generate_briefing, pages, sensitivity=document.sensitivity, title=document.title or document.doc_id
                )
        except ai.EgressBlocked as exc:
            logger.warning("Briefing %s blocked by egress policy: %s", briefing_id, exc)
            briefing.status = STATUS_FAILED
            subject = "a paper in this package" if briefing.package_id else "this paper"
            briefing.error = (
                f"AI policy does not allow the sensitivity of {subject} to be sent to the configured model. "
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
