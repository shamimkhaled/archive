"""Shared meeting helpers used by meetings and board routers."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from fastapi import HTTPException, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..dummy_transcription import (
    TRANSCRIPTION_OFF,
    transcription_payload,
)
from ..meeting_audio import list_chunk_paths
from ..minutes_draft import MINUTES_OFF, minutes_payload
from ..models import BoardMeeting, MeetingDocument, MeetingInvitation, User


async def _load_meeting_or_404(meeting_id: int, db: AsyncSession) -> BoardMeeting:
    statement = select(BoardMeeting).where(BoardMeeting.id == meeting_id)
    result = await db.execute(statement)
    meeting = result.scalar_one_or_none()
    if meeting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    return meeting


def _meeting_transcription_status(meeting: BoardMeeting) -> str:
    return (meeting.transcription_status or TRANSCRIPTION_OFF).strip() or TRANSCRIPTION_OFF


def _minutes_view(meeting: BoardMeeting) -> Dict[str, Any]:
    return minutes_payload(
        (meeting.minutes_status or MINUTES_OFF).strip() or MINUTES_OFF,
        meeting.minutes_draft,
        generated_at=meeting.minutes_generated_at,
        source=meeting.minutes_source,
    )


def _transcription_view(meeting: BoardMeeting, *, include_segments: bool) -> Dict[str, Any]:
    segments = list(meeting.transcription_json or [])
    is_dummy = any(bool(line.get("dummy")) for line in segments) if segments else False
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
        minutes=_minutes_view(meeting),
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


def _serve_meeting_document_file(document: MeetingDocument, stamped_pdf: bytes) -> Response:
    filename = _meeting_document_filename(document)
    return Response(
        content=stamped_pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


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
