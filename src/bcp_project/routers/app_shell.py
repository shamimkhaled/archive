"""React PWA shell: /app static hosting, the session/feature-flag endpoint, and page payloads that had no JSON form."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import assert_can_view, is_archive_download_allowed, write_audit
from ..brand import BRAND
from ..deps import _client_ip, _load_document_or_404, get_current_user, get_db, require_role
from ..models import BoardBriefing, DocumentRecord, MeetingDocument, Role, User
from ..services.briefing import STATE_APPROVED
from ..services.ingest import ai_provenance_label
from ..spa import enabled_screens, serve_spa
from .meeting_common import _load_meeting_or_404, _minutes_view, _parse_agenda_items, _require_invited_or_404

router = APIRouter()

ORGANIZERS = (Role.admin, Role.board_secretary)


@router.get("/api/app/session")
async def app_session(current_user: User = Depends(get_current_user)) -> Dict[str, Any]:
    role = current_user.role
    return {
        "username": current_user.username,
        "role": role.value,
        "permissions": {
            "search": role in (Role.admin, Role.board_secretary, Role.board_member),
            "review": role in (Role.admin, Role.board_secretary, Role.uploader),
            "organize_meetings": role in ORGANIZERS,
            "admin": role == Role.admin,
        },
        "screens": sorted(enabled_screens()),
    }


@router.get("/api/app/documents/{doc_id}")
async def app_document(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """What the viewer page renders around the PDF; audited as a view like /view/{doc_id}."""
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    document = await _load_document_or_404(doc_id, db)
    await assert_can_view(current_user, doc_id, db)
    has_briefing = await db.scalar(select(BoardBriefing.id).where(BoardBriefing.doc_id == doc_id).limit(1)) is not None
    await write_audit(
        db,
        username=current_user.username,
        action="view",
        resource_type="document",
        resource_id=doc_id,
        detail="viewer_page spa",
        ip_address=_client_ip(request),
        commit=True,
    )
    can_download = is_archive_download_allowed(current_user)
    return {
        "doc_id": doc_id,
        "title": document.title,
        "doc_type": document.doc_type,
        "doc_date": document.doc_date.isoformat() if document.doc_date else None,
        "department": document.department,
        "reference_number": document.reference_number,
        "page_count": document.page_count,
        "current_version": document.current_version,
        "provenance": ai_provenance_label(document.summary_json),
        "seal_text": BRAND.seal(current_user.username),
        "file_url": f"/view/{doc_id}/file",
        "download_url": f"/download/{doc_id}" if can_download else None,
        "can_see_versions": current_user.role in ORGANIZERS,
        "has_briefing": has_briefing,
    }


@router.get("/api/app/meetings/{meeting_id}")
async def app_meeting(
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Organizers get the minutes workflow; invitees get the meeting, its papers, and approved minutes only."""
    organizer = current_user.role in ORGANIZERS
    if organizer:
        meeting = await _load_meeting_or_404(meeting_id, db)
    else:
        meeting = await _require_invited_or_404(meeting_id, current_user, db)

    documents = (
        await db.execute(
            select(MeetingDocument).where(MeetingDocument.meeting_id == meeting_id).order_by(MeetingDocument.uploaded_at.desc())
        )
    ).scalars().all()
    archive_ids = [d.archive_doc_id for d in documents if d.archive_doc_id]
    archive_status: Dict[str, str] = {}
    briefed: set = set()
    if archive_ids:
        archive_status = dict(
            (await db.execute(select(DocumentRecord.doc_id, DocumentRecord.ingest_status).where(DocumentRecord.doc_id.in_(archive_ids)))).all()
        )
        briefed = set(
            (
                await db.execute(
                    select(BoardBriefing.doc_id).where(BoardBriefing.doc_id.in_(archive_ids), BoardBriefing.state == STATE_APPROVED)
                )
            ).scalars()
        )
    file_base = "/meetings" if organizer else "/board/meetings"

    if organizer:
        minutes = _minutes_view(meeting, current_user.username)
    else:
        approved = meeting.minutes_state == "approved"
        minutes = {"state": meeting.minutes_state or "draft", "doc_id": meeting.minutes_doc_id if approved else None}

    return {
        "id": meeting.id,
        "title": meeting.title,
        "scheduled_at": meeting.scheduled_at.isoformat() if meeting.scheduled_at else None,
        "location": meeting.location,
        "status": meeting.status.value if hasattr(meeting.status, "value") else str(meeting.status),
        "attendance_open": bool(meeting.attendance_open),
        "agenda_items": _parse_agenda_items(meeting.agenda),
        "is_organizer": organizer,
        "classic_url": f"/meetings/{meeting.id}?classic=1" if organizer else f"/board/meetings/{meeting.id}",
        "documents": [
            {
                "id": d.id,
                "title": d.title,
                "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None,
                "archive_doc_id": d.archive_doc_id,
                "archive_status": archive_status.get(d.archive_doc_id or ""),
                "briefed": (d.archive_doc_id or "") in briefed,
                "file_url": f"{file_base}/{meeting.id}/documents/{d.id}/file",
            }
            for d in documents
        ],
        "minutes": minutes,
    }


@router.get("/app", include_in_schema=False)
async def app_root() -> Response:
    return serve_spa("")


@router.get("/app/{path:path}", include_in_schema=False)
async def app_files(path: str) -> Response:
    return serve_spa(path)
