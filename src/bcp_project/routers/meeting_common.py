"""Shared meeting helpers used by meetings and board routers."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import write_audit
from ..brand import BRAND
from ..deps import _client_ip, _load_stored_pdf_bytes
from ..pdf_delivery import is_initial_request, pdf_response, stamped_pdf
from ..dummy_transcription import (
    TRANSCRIPTION_OFF,
    transcription_payload,
)
from ..meeting_audio import list_chunk_paths
from ..minutes_draft import MINUTES_OFF, minutes_payload
from ..models import BoardMeeting, MeetingDocument, MeetingInvitation, User

logger = logging.getLogger("bcp_project.routers.meeting_common")


async def _load_meeting_or_404(meeting_id: int, db: AsyncSession) -> BoardMeeting:
    statement = select(BoardMeeting).where(BoardMeeting.id == meeting_id)
    result = await db.execute(statement)
    meeting = result.scalar_one_or_none()
    if meeting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    return meeting


def _meeting_transcription_status(meeting: BoardMeeting) -> str:
    return (meeting.transcription_status or TRANSCRIPTION_OFF).strip() or TRANSCRIPTION_OFF


def _minutes_view(meeting: BoardMeeting, username: Optional[str] = None) -> Dict[str, Any]:
    from ..services.minutes import workflow_view

    payload = minutes_payload(
        (meeting.minutes_status or MINUTES_OFF).strip() or MINUTES_OFF,
        meeting.minutes_draft,
        generated_at=meeting.minutes_generated_at,
        source=meeting.minutes_source,
    )
    payload["workflow"] = workflow_view(meeting, username)
    return payload


def _transcription_view(meeting: BoardMeeting, *, include_segments: bool) -> Dict[str, Any]:
    segments = list(meeting.transcription_json or [])
    is_dummy = any(bool(line.get("dummy")) for line in segments) if segments else False
    minutes = _minutes_view(meeting)
    if not include_segments:
        # Invitees get progress only; minutes text is read from the archive once approved.
        minutes = {k: v for k, v in minutes.items() if k not in {"body_md", "workflow"}}
        minutes["state"] = meeting.minutes_state or "draft"
        minutes["doc_id"] = meeting.minutes_doc_id if meeting.minutes_state == "approved" else None
    return transcription_payload(
        _meeting_transcription_status(meeting),
        segments,
        started_at=meeting.transcription_started_at,
        stopped_at=meeting.transcription_stopped_at,
        started_by=meeting.transcription_started_by,
        include_segments=include_segments,
        dummy=is_dummy,
        chunk_count=len(list_chunk_paths(meeting.id)),
        phase="c",
        minutes=minutes,
    )


async def _require_invited_or_404(meeting_id: int, user: User, db: AsyncSession) -> BoardMeeting:
    meeting = await _load_meeting_or_404(meeting_id, db)
    statement = select(MeetingInvitation).where(
        MeetingInvitation.meeting_id == meeting_id,
        MeetingInvitation.username == user.username,
    )
    result = await db.execute(statement)
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    return meeting


async def _load_meeting_document_or_404(meeting_id: int, document_id: int, db: AsyncSession) -> MeetingDocument:
    statement = select(MeetingDocument).where(
        MeetingDocument.id == document_id,
        MeetingDocument.meeting_id == meeting_id,
    )
    result = await db.execute(statement)
    document = result.scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting document not found")
    return document


def _meeting_document_filename(document: MeetingDocument) -> str:
    location = (document.file_location or "").strip()
    basename = location.rsplit("/", 1)[-1] if location else ""
    suffix = Path(basename).suffix or ".pdf"
    safe_title = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in document.title.strip())
    safe_title = safe_title.strip("_") or f"meeting-document-{document.id}"
    return f"{safe_title}{suffix}"


async def _stream_meeting_document(
    request: Request,
    db: AsyncSession,
    document: MeetingDocument,
    meeting_id: int,
    username: str,
) -> Response:
    """Watermarked, range-capable stream of a meeting paper; audited once per view."""
    key = ("meeting_doc", document.id, document.file_location, username)
    try:
        stamped = await stamped_pdf(
            key,
            lambda: _load_stored_pdf_bytes(document.file_location, resource_id=f"meeting_document:{document.id}"),
            BRAND.seal_meeting_doc(username),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Watermark stamping failed for meeting document %s", document.id)
        raise HTTPException(status_code=502, detail="Could not prepare secure meeting document stream") from exc
    if is_initial_request(request):
        await write_audit(
            db,
            username=username,
            action="view_file",
            resource_type="meeting_document",
            resource_id=str(document.id),
            detail=f"meeting_id={meeting_id}",
            ip_address=_client_ip(request),
            commit=True,
        )
    return pdf_response(request, stamped, filename=_meeting_document_filename(document))


def _validate_pdf_upload(upload: UploadFile) -> None:
    content_type = (upload.content_type or "").lower()
    name = (upload.filename or "").lower()
    if content_type not in ("application/pdf", "application/x-pdf", "") and not name.endswith(".pdf"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are allowed")
    if content_type == "" and not name.endswith(".pdf"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are allowed")


def _parse_agenda_items(agenda: str) -> List[str]:
    items: List[str] = []
    for raw in (agenda or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        items.append(line)
        if len(items) >= 16:
            break
    return items


async def _related_board_meetings(
    db: AsyncSession,
    meeting: BoardMeeting,
    *,
    limit: int = 5,
) -> List[BoardMeeting]:
    statement = (
        select(BoardMeeting)
        .where(BoardMeeting.id != meeting.id)
        .order_by(BoardMeeting.scheduled_at.desc())
        .limit(limit)
    )
    result = await db.execute(statement)
    return list(result.scalars().all())


async def _meeting_workspace_extras(db: AsyncSession, meeting: BoardMeeting) -> Dict[str, Any]:
    agenda_items = _parse_agenda_items(meeting.agenda)
    related = await _related_board_meetings(db, meeting)
    topics = [item[:80] for item in agenda_items[:8]]
    intel_queries = agenda_items[:3] or [meeting.title]
    return {
        "agenda_items": agenda_items,
        "related_meetings": related,
        "meeting_topics": topics,
        "intel_queries": intel_queries,
    }
