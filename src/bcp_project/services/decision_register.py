"""Decision register and historical references, filtered per reader at request time.

The register lists decisions from approved minutes only. A decision is visible exactly when its
minutes document (``MIN-{meeting_id}``) is: privileged roles see all, everyone else needs an
active grant, and minutes still awaiting OCR verification are hidden from all. Historical
references come from the same permission-aware search the archive uses, keeping only papers
the reader can open.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import unverified_doc_ids, viewable_doc_ids
from ..models import BoardMeeting, DocumentRecord, MeetingAction, MeetingDecision, User
from .minutes import MINUTES_APPROVED

logger = logging.getLogger("bcp_project.services.decision_register")

_WORD = re.compile(r"[\w\u0980-\u09FF\u200c\u200d]+", re.UNICODE)
_STOPWORDS = frozenset(
    "the and for with that this from are was were will shall has have had its into upon under over about "
    "board bank meeting paper proposal approved approve decision sonali plc ltd limited".split()
)
MAX_REGISTER_ROWS = 500
REFERENCE_TIMEOUT_SECONDS = 6.0


def _tokens(text: str) -> List[str]:
    return [t for t in (w.casefold() for w in _WORD.findall(text or "")) if len(t) >= 3 and t not in _STOPWORDS]


async def visible_minutes_doc_ids(db: AsyncSession, user: User, doc_ids: Iterable[str]) -> Set[str]:
    ids = {d for d in doc_ids if d}
    if not ids:
        return set()
    visible = await viewable_doc_ids(db, user, ids)
    return visible - await unverified_doc_ids(db, visible)


async def decision_register(
    db: AsyncSession, user: User, *, q: Optional[str] = None, limit: int = MAX_REGISTER_ROWS
) -> List[Dict[str, Any]]:
    rows = (
        await db.execute(
            select(MeetingDecision, BoardMeeting)
            .join(BoardMeeting, BoardMeeting.id == MeetingDecision.meeting_id)
            .where(BoardMeeting.minutes_state == MINUTES_APPROVED, BoardMeeting.minutes_doc_id.is_not(None))
            .order_by(BoardMeeting.scheduled_at.desc(), MeetingDecision.seq)
        )
    ).all()
    visible = await visible_minutes_doc_ids(db, user, {meeting.minutes_doc_id for _, meeting in rows})
    rows = [(d, m) for d, m in rows if m.minutes_doc_id in visible]
    if q and q.strip():
        needle = q.strip().casefold()
        wanted = set(_tokens(q))
        rows = [
            (d, m)
            for d, m in rows
            if needle in " ".join([d.text, d.agenda_item or "", m.title]).casefold()
            or (wanted and wanted <= set(_tokens(" ".join([d.text, d.agenda_item or "", m.title]))))
        ]
    rows = rows[:limit]

    actions: Dict[int, List[MeetingAction]] = {}
    decision_ids = [d.id for d, _ in rows]
    if decision_ids:
        for action in (
            await db.execute(
                select(MeetingAction).where(MeetingAction.decision_id.in_(decision_ids)).order_by(MeetingAction.id)
            )
        ).scalars():
            actions.setdefault(action.decision_id, []).append(action)

    return [
        {
            "decision_id": d.id,
            "meeting_id": m.id,
            "meeting_title": m.title,
            "meeting_date": m.scheduled_at.date().isoformat() if m.scheduled_at else None,
            "seq": d.seq,
            "agenda_item": d.agenda_item,
            "text": d.text,
            "minutes_doc_id": m.minutes_doc_id,
            "minutes_version": m.minutes_version,
            "minutes_url": f"/view/{m.minutes_doc_id}",
            "actions": [
                {
                    "description": a.description,
                    "owner": a.owner,
                    "division": a.division,
                    "due_date": a.due_date.isoformat() if a.due_date else None,
                    "status": a.status,
                }
                for a in actions.get(d.id, [])
            ],
        }
        for d, m in rows
    ]


async def related_decisions(db: AsyncSession, user: User, text: str, *, limit: int = 5) -> List[Dict[str, Any]]:
    """Prior decisions sharing the most terms with ``text``, from minutes the reader may see."""
    query = Counter(_tokens(text))
    if not query:
        return []
    scored = []
    for entry in await decision_register(db, user):
        terms = set(_tokens(" ".join([entry["text"], entry["agenda_item"] or ""])))
        score = sum(count for term, count in query.items() if term in terms)
        if score >= min(2, len(query)):
            scored.append((score, entry))
    scored.sort(key=lambda item: (item[0], item[1]["meeting_date"] or ""), reverse=True)
    return [entry for _, entry in scored[:limit]]


async def historical_references(
    db: AsyncSession, user: User, document: DocumentRecord, query: str, *, limit: int = 5
) -> List[Dict[str, Any]]:
    """Earlier papers on the same subject that this reader can open; never the paper itself."""
    from ..policy import principal_from_user
    from .document_query import search_documents_for_principal

    q = " ".join((query or document.title or document.subject or "").split())[:300]
    if not q:
        return []
    try:
        result = await asyncio.wait_for(
            search_documents_for_principal(
                db, principal_from_user(user), q, limit=limit + 5, tool_name="board_briefing_references"
            ),
            timeout=REFERENCE_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("Historical references unavailable for %s", document.doc_id, exc_info=True)
        return []
    seen: Set[str] = {document.doc_id}
    references: List[Dict[str, Any]] = []
    for row in result.get("results") or []:
        doc_id = row.get("doc_id")
        if not row.get("can_view") or not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        references.append(
            {
                "doc_id": doc_id,
                "title": row.get("title") or doc_id,
                "doc_type": row.get("doc_type"),
                "page": row.get("page"),
                "snippet": row.get("snippet"),
                "view_url": row.get("view_url") or f"/view/{doc_id}",
            }
        )
        if len(references) >= limit:
            break
    return references
