"""Minutes review workflow: draft -> in_review -> approved, with a lock and four-eyes approval.

``minutes_status`` tracks generation (pending/generating/ready); ``minutes_state`` tracks review.
Only approved minutes reach the archive: approval renders a PDF, stores it write-once as
document ``MIN-{meeting_id}`` (a new version on each re-approval) and indexes the approved
text page by page. Drafts are never searchable.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import is_production
from ..minutes_draft import MINUTES_GENERATING, MINUTES_PENDING, MINUTES_READY, meeting_sensitivity, transcript_to_plain
from ..models import BoardMeeting, DocumentRecord, MeetingAction, MeetingDecision

logger = logging.getLogger("bcp_project.services.minutes")

MINUTES_DRAFT = "draft"
MINUTES_IN_REVIEW = "in_review"
MINUTES_APPROVED = "approved"
MINUTES_STATES = (MINUTES_DRAFT, MINUTES_IN_REVIEW, MINUTES_APPROVED)

SOURCE_EDITED = "edited"
SOURCE_MANUAL = "manual"
SOURCE_AI = "ai"
MINUTES_DOC_TYPE = "Board Minutes"
ACTION_STATUSES = ("open", "in_progress", "done", "cancelled")
MAX_EXTRACTED_ITEMS = 25


class MinutesWorkflowError(Exception):
    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def four_eyes_required() -> bool:
    """Approver must differ from the submitter and the last editor. On by default in production."""
    raw = (os.getenv("MINUTES_FOUR_EYES") or "").strip().lower()
    if not raw:
        return is_production()
    return raw in {"1", "true", "yes", "on"}


def minutes_state(meeting: BoardMeeting) -> str:
    state = (meeting.minutes_state or "").strip()
    return state if state in MINUTES_STATES else MINUTES_DRAFT


def require_draft(meeting: BoardMeeting, what: str = "Minutes") -> None:
    state = minutes_state(meeting)
    if state != MINUTES_DRAFT:
        label = "in review" if state == MINUTES_IN_REVIEW else state
        raise MinutesWorkflowError(f"{what} cannot change while the minutes are {label}")


def save_edit(meeting: BoardMeeting, body_md: str, username: str, *, now: Optional[datetime] = None) -> bool:
    """Store a person's edit. Returns True when the text changed (source becomes ``edited``)."""
    require_draft(meeting)
    body = (body_md or "").strip()
    changed = body != (meeting.minutes_draft or "").strip()
    meeting.minutes_draft = body
    meeting.minutes_status = MINUTES_READY
    meeting.minutes_generated_at = meeting.minutes_generated_at or (now or datetime.utcnow())
    if changed:
        meeting.minutes_source = SOURCE_EDITED
        meeting.minutes_edited_by = username
        meeting.minutes_edited_at = now or datetime.utcnow()
    return changed


def submit_for_review(meeting: BoardMeeting, username: str, *, now: Optional[datetime] = None) -> None:
    require_draft(meeting)
    if (meeting.minutes_status or "") in {MINUTES_PENDING, MINUTES_GENERATING}:
        raise MinutesWorkflowError("Minutes are still being generated")
    if not (meeting.minutes_draft or "").strip():
        raise MinutesWorkflowError("There are no minutes to submit", status_code=400)
    meeting.minutes_state = MINUTES_IN_REVIEW
    meeting.minutes_submitted_by = username
    meeting.minutes_submitted_at = now or datetime.utcnow()
    meeting.minutes_review_note = None


def reject(meeting: BoardMeeting, username: str, note: str) -> None:
    if minutes_state(meeting) != MINUTES_IN_REVIEW:
        raise MinutesWorkflowError("Only minutes in review can be returned")
    note = (note or "").strip()
    if len(note) < 3:
        raise MinutesWorkflowError("Say what needs to change", status_code=400)
    meeting.minutes_state = MINUTES_DRAFT
    meeting.minutes_review_note = f"Returned by {username}: {note}"[:2000]


def approval_blocker(meeting: BoardMeeting, username: str) -> Optional[str]:
    """Why ``username`` cannot approve now, or None. Shown in the UI before they try."""
    if minutes_state(meeting) != MINUTES_IN_REVIEW:
        return "Minutes must be submitted for review first"
    if four_eyes_required():
        if username == meeting.minutes_submitted_by:
            return "Four-eyes rule: you submitted these minutes, so someone else must approve them"
        if username == meeting.minutes_edited_by:
            return "Four-eyes rule: you last edited these minutes, so someone else must approve them"
    return None


def body_sha256(body: str) -> str:
    return hashlib.sha256((body or "").strip().encode("utf-8")).hexdigest()


def approve(meeting: BoardMeeting, username: str, *, now: Optional[datetime] = None) -> str:
    """Lock the minutes. Returns the SHA-256 of the approved text."""
    blocker = approval_blocker(meeting, username)
    if blocker:
        raise MinutesWorkflowError(blocker, status_code=403 if blocker.startswith("Four-eyes") else 409)
    digest = body_sha256(meeting.minutes_draft or "")
    meeting.minutes_state = MINUTES_APPROVED
    meeting.minutes_approved_by = username
    meeting.minutes_approved_at = now or datetime.utcnow()
    meeting.minutes_sha256 = digest
    meeting.minutes_review_note = None
    return digest


def amend(meeting: BoardMeeting, username: str, reason: str) -> int:
    """Reopen approved minutes as the next version. The archived copy stays searchable
    until the amended minutes are approved and indexed."""
    if minutes_state(meeting) != MINUTES_APPROVED:
        raise MinutesWorkflowError("Only approved minutes can be amended")
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise MinutesWorkflowError("Give a reason for the amendment", status_code=400)
    meeting.minutes_state = MINUTES_DRAFT
    meeting.minutes_version = int(meeting.minutes_version or 1) + 1
    meeting.minutes_review_note = f"Amendment opened by {username}: {reason}"[:2000]
    meeting.minutes_submitted_by = None
    meeting.minutes_submitted_at = None
    return meeting.minutes_version


def workflow_view(meeting: BoardMeeting, username: Optional[str] = None) -> Dict[str, Any]:
    state = minutes_state(meeting)

    def iso(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() + "Z" if value else None

    return {
        "state": state,
        "version": int(meeting.minutes_version or 1),
        "locked": state != MINUTES_DRAFT,
        "four_eyes": four_eyes_required(),
        "edited_by": meeting.minutes_edited_by,
        "edited_at": iso(meeting.minutes_edited_at),
        "submitted_by": meeting.minutes_submitted_by,
        "submitted_at": iso(meeting.minutes_submitted_at),
        "approved_by": meeting.minutes_approved_by,
        "approved_at": iso(meeting.minutes_approved_at),
        "review_note": meeting.minutes_review_note,
        "sha256": meeting.minutes_sha256,
        "doc_id": meeting.minutes_doc_id,
        "approve_blocker": approval_blocker(meeting, username) if username else None,
    }


async def archive_approved_minutes(db: AsyncSession, meeting: BoardMeeting, *, approved_by: str):
    """Store the approved minutes as a write-once PDF version and stage indexing of the approved text.

    The version is marked reviewed, so ingest skips OCR and indexes exactly the pages saved here.
    Caller commits, then calls ``dispatch_after_commit`` with the returned IntakeResult.
    """
    from ..intake import validate_upload
    from ..minutes_pdf import render_minutes_pdf
    from ..ocr import PageText
    from .document_versions import intake_new_document, intake_new_version
    from .ingest import VERSION_REVIEWED, save_pages

    approved_at = meeting.minutes_approved_at or datetime.utcnow()
    pdf, page_texts = await asyncio.to_thread(
        lambda: render_minutes_pdf(
            title=meeting.title,
            body_md=meeting.minutes_draft or "",
            meeting_date=meeting.scheduled_at,
            location=meeting.location,
            version=int(meeting.minutes_version or 1),
            approved_by=approved_by,
            approved_at=approved_at,
            sha256=meeting.minutes_sha256 or body_sha256(meeting.minutes_draft or ""),
        )
    )
    upload = validate_upload(pdf)
    doc_id = meeting.minutes_doc_id or f"MIN-{meeting.id}"
    document = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == doc_id))).scalar_one_or_none()
    note = f"Minutes v{int(meeting.minutes_version or 1)} approved by {approved_by}"
    if document is None:
        result = await intake_new_document(
            db,
            upload=upload,
            doc_id=doc_id,
            doc_date=(meeting.scheduled_at or approved_at).date(),
            doc_type=MINUTES_DOC_TYPE,
            keywords=["board minutes"],
            sensitivity=meeting_sensitivity(),
            uploaded_by=approved_by,
            metadata={
                "title": f"Minutes — {meeting.title}",
                "subject": meeting.title,
                "category": MINUTES_DOC_TYPE,
                "reference_number": doc_id,
            },
            meeting_id=meeting.id,
        )
        result.version.note = note
    else:
        result = await intake_new_version(db, document=document, upload=upload, uploaded_by=approved_by, note=note)
    version = result.version
    await save_pages(
        db,
        doc_id,
        version.version,
        [PageText(page_number=n, text=text, confidence=None, source="approved") for n, text in enumerate(page_texts, 1)],
    )
    version.status = VERSION_REVIEWED
    version.reviewed_by = approved_by
    version.reviewed_at = approved_at
    result.document.page_count = len(page_texts)
    meeting.minutes_doc_id = doc_id
    return result


# --- Decisions and actions ------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold()).strip()


def parse_due_date(value: Any) -> Optional[date]:
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _grounded(evidence: str, transcript_norm: str) -> bool:
    quote = _norm(evidence).strip(" \"'“”‘’….")
    return len(quote) >= 8 and quote in transcript_norm


def extract_decisions_and_actions(
    body_md: str, segments: Optional[Sequence[Dict[str, Any]]], *, sensitivity: Optional[str] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """Ask the model for decisions and actions, each with a verbatim transcript quote.

    Items whose quote is not found in the transcript are dropped, so nothing is invented.
    Returns empty lists when the model is unavailable or blocked by egress policy.
    """
    from .. import ai

    transcript = transcript_to_plain(list(segments or []))
    if not transcript.strip() or not (body_md or "").strip():
        return {"decisions": [], "actions": []}
    system = (
        "You extract board decisions and action items for Sonali Bank PLC minutes. "
        "Return JSON: {\"decisions\": [{\"text\", \"agenda_item\", \"evidence\"}], "
        "\"actions\": [{\"description\", \"owner\", \"due_date\" (YYYY-MM-DD or null), \"evidence\", "
        "\"decision_index\" (0-based index into decisions, or null)}]}. "
        "\"evidence\" must be an exact quote copied from the transcript that supports the item. "
        "Only include items the transcript states. Keep the language of the transcript (Bangla or English)."
    )
    user = f"Draft minutes:\n{body_md[:12000]}\n\nTranscript:\n{transcript[:24000]}"
    try:
        data = ai.chat_json("minutes", system, user, sensitivity=sensitivity or meeting_sensitivity(), max_tokens=2000)
    except ai.EgressBlocked as exc:
        logger.info("Decision extraction skipped by egress policy: %s", exc)
        return {"decisions": [], "actions": []}
    except Exception:
        logger.exception("Decision extraction failed")
        return {"decisions": [], "actions": []}

    transcript_norm = _norm(transcript)
    decisions: List[Dict[str, Any]] = []
    index_map: Dict[int, int] = {}
    for i, item in enumerate(list(data.get("decisions") or [])[:MAX_EXTRACTED_ITEMS]):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        if text and _grounded(evidence, transcript_norm):
            index_map[i] = len(decisions)
            decisions.append(
                {"text": text, "agenda_item": (str(item.get("agenda_item") or "").strip() or None), "evidence": evidence}
            )
    actions: List[Dict[str, Any]] = []
    for item in list(data.get("actions") or [])[:MAX_EXTRACTED_ITEMS]:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        if not description or not _grounded(evidence, transcript_norm):
            continue
        link = item.get("decision_index")
        actions.append(
            {
                "description": description,
                "owner": (str(item.get("owner") or "").strip() or None),
                "due_date": parse_due_date(item.get("due_date")),
                "evidence": evidence,
                "decision_index": index_map.get(link) if isinstance(link, int) else None,
            }
        )
    return {"decisions": decisions, "actions": actions}


async def replace_ai_items(
    db: AsyncSession, meeting_id: int, extracted: Dict[str, List[Dict[str, Any]]], *, actor: str = "minutes-ai"
) -> Dict[str, int]:
    """Swap previous AI-sourced items for a fresh extraction. Manual items are never touched."""
    ai_decisions = select(MeetingDecision.id).where(
        MeetingDecision.meeting_id == meeting_id, MeetingDecision.source == SOURCE_AI
    )
    await db.execute(
        delete(MeetingAction).where(MeetingAction.meeting_id == meeting_id, MeetingAction.source == SOURCE_AI)
    )
    await db.execute(
        update(MeetingAction)
        .where(MeetingAction.meeting_id == meeting_id, MeetingAction.decision_id.in_(ai_decisions))
        .values(decision_id=None)
    )
    await db.execute(delete(MeetingDecision).where(MeetingDecision.id.in_(ai_decisions)))
    start = int(
        await db.scalar(
            select(MeetingDecision.seq).where(MeetingDecision.meeting_id == meeting_id).order_by(MeetingDecision.seq.desc()).limit(1)
        )
        or 0
    )
    rows: List[MeetingDecision] = []
    for n, item in enumerate(extracted.get("decisions") or [], 1):
        row = MeetingDecision(
            meeting_id=meeting_id,
            seq=start + n,
            agenda_item=(item.get("agenda_item") or None),
            text=item["text"],
            evidence=item.get("evidence"),
            source=SOURCE_AI,
            created_by=actor,
        )
        db.add(row)
        rows.append(row)
    await db.flush()
    for item in extracted.get("actions") or []:
        link = item.get("decision_index")
        db.add(
            MeetingAction(
                meeting_id=meeting_id,
                decision_id=rows[link].id if isinstance(link, int) and 0 <= link < len(rows) else None,
                description=item["description"],
                owner=item.get("owner"),
                due_date=item.get("due_date"),
                evidence=item.get("evidence"),
                source=SOURCE_AI,
                created_by=actor,
            )
        )
    await db.flush()
    return {"decisions": len(rows), "actions": len(extracted.get("actions") or [])}


async def list_items(db: AsyncSession, meeting_id: int) -> Dict[str, List[Any]]:
    decisions = list(
        (
            await db.execute(
                select(MeetingDecision).where(MeetingDecision.meeting_id == meeting_id).order_by(MeetingDecision.seq, MeetingDecision.id)
            )
        ).scalars()
    )
    actions = list(
        (
            await db.execute(
                select(MeetingAction).where(MeetingAction.meeting_id == meeting_id).order_by(MeetingAction.id)
            )
        ).scalars()
    )
    return {"decisions": decisions, "actions": actions}


def decision_payload(row: MeetingDecision) -> Dict[str, Any]:
    return {
        "id": row.id,
        "seq": row.seq,
        "agenda_item": row.agenda_item,
        "text": row.text,
        "evidence": row.evidence,
        "source": row.source,
        "created_by": row.created_by,
    }


def action_payload(row: MeetingAction) -> Dict[str, Any]:
    return {
        "id": row.id,
        "decision_id": row.decision_id,
        "description": row.description,
        "owner": row.owner,
        "due_date": row.due_date.isoformat() if row.due_date else None,
        "status": row.status,
        "evidence": row.evidence,
        "source": row.source,
        "completed_at": row.completed_at.isoformat() + "Z" if row.completed_at else None,
    }


def set_action_status(row: MeetingAction, status: str, username: str, *, now: Optional[datetime] = None) -> None:
    """Allowed even after approval: tracking progress does not change what the board decided."""
    status = (status or "").strip()
    if status not in ACTION_STATUSES:
        raise MinutesWorkflowError(f"Status must be one of: {', '.join(ACTION_STATUSES)}", status_code=400)
    row.status = status
    row.updated_by = username
    row.updated_at = now or datetime.utcnow()
    row.completed_at = row.updated_at if status == "done" else None
