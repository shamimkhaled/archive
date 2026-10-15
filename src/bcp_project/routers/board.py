"""Board-member meeting routes."""
from __future__ import annotations

import base64
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import viewable_doc_ids
from ..brand import BRAND
from ..ui import local_dt
from ..deps import (
    MEETING_UPLOAD_DIR,
    UPLOAD_DIR,
    get_current_user,
    get_db,
    templates,
)
from ..models import BoardBriefing, BoardMeeting, MeetingAttendance, MeetingDocument, MeetingInvitation, User
from ..services.briefing import STATE_APPROVED
from ..services.minutes import list_items as list_minutes_items
from .meeting_common import (
    _load_meeting_document_or_404,
    _meeting_workspace_extras,
    _require_invited_or_404,
    _stream_meeting_document,
)
from ..services.board_packages import meeting_packages_view

logger = logging.getLogger("bcp_project.routers.board")
router = APIRouter()

@router.get("/board/meetings", response_class=HTMLResponse)
async def board_meetings_list(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    statement = (
        select(BoardMeeting)
        .join(MeetingInvitation, MeetingInvitation.meeting_id == BoardMeeting.id)
        .where(MeetingInvitation.username == current_user.username)
        .order_by(BoardMeeting.id.desc())
    )
    result = await db.execute(statement)
    meetings = result.scalars().all()

    return templates.TemplateResponse(
        request,
        "board_meetings_list.html",
        {"user": current_user, "meetings": meetings},
    )


@router.get("/board/meetings/{meeting_id}", response_class=HTMLResponse)
async def board_meeting_detail(
    request: Request,
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
) -> Any:
    meeting = await _require_invited_or_404(meeting_id, current_user, db)

    doc_statement = (
        select(MeetingDocument)
        .where(MeetingDocument.meeting_id == meeting_id)
        .order_by(MeetingDocument.uploaded_at.desc())
    )
    doc_result = await db.execute(doc_statement)
    documents = doc_result.scalars().all()

    attendance_statement = select(MeetingAttendance).where(
        MeetingAttendance.meeting_id == meeting_id,
        MeetingAttendance.username == current_user.username,
    )
    attendance_result = await db.execute(attendance_statement)
    my_attendance = attendance_result.scalar_one_or_none()
    extras = await _meeting_workspace_extras(db, meeting)
    archive_ids = [d.archive_doc_id for d in documents if d.archive_doc_id]
    briefed = set()
    if archive_ids:
        briefed = set(
            (
                await db.execute(
                    select(BoardBriefing.doc_id).where(
                        BoardBriefing.doc_id.in_(archive_ids),
                        BoardBriefing.state == STATE_APPROVED,
                        BoardBriefing.package_id.is_(None),
                    )
                )
            ).scalars()
        )

    minutes_doc_id = meeting.minutes_doc_id if meeting.minutes_state == "approved" else None
    minutes_viewable = bool(minutes_doc_id) and minutes_doc_id in await viewable_doc_ids(db, current_user, [minutes_doc_id])
    decisions = (await list_minutes_items(db, meeting_id))["decisions"] if minutes_viewable else []

    return templates.TemplateResponse(
        request,
        "board_meeting_detail.html",
        {
            "user": current_user,
            "meeting": meeting,
            "documents": documents,
            "briefed_doc_ids": briefed,
            "packages": await meeting_packages_view(db, meeting_id, approved_only=True),
            "my_attendance": my_attendance,
            "status": status,
            "minutes_doc_id": minutes_doc_id,
            "minutes_viewable": minutes_viewable,
            "decisions": decisions,
            **extras,
        },
    )


@router.post("/board/meetings/{meeting_id}/attendance")
async def sign_meeting_attendance(
    meeting_id: int,
    signature: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    meeting = await _require_invited_or_404(meeting_id, current_user, db)

    def back(code: str) -> RedirectResponse:
        return RedirectResponse(
            url=f"/board/meetings/{meeting_id}?status={code}#attendance", status_code=status.HTTP_303_SEE_OTHER
        )

    if not meeting.attendance_open:
        return back("attendance_locked")

    existing_statement = select(MeetingAttendance).where(
        MeetingAttendance.meeting_id == meeting_id,
        MeetingAttendance.username == current_user.username,
    )
    existing_result = await db.execute(existing_statement)
    if existing_result.scalar_one_or_none() is not None:
        return back("already_signed")

    try:
        _, _, b64data = signature.partition(",")
        image_bytes = base64.b64decode(b64data, validate=True)
    except (ValueError, TypeError):
        image_bytes = b""
    if not image_bytes.startswith(b"\x89PNG"):
        return back("signature_invalid")

    sig_dir = MEETING_UPLOAD_DIR / str(meeting_id) / "signatures"
    sig_dir.mkdir(parents=True, exist_ok=True)
    destination = sig_dir / f"{uuid.uuid4().hex}_{current_user.username}.png"
    destination.write_bytes(image_bytes)

    db.add(MeetingAttendance(
        meeting_id=meeting_id,
        username=current_user.username,
        signature_file=str(destination.relative_to(UPLOAD_DIR)),
    ))
    await db.commit()

    return RedirectResponse(url=f"/board/meetings/{meeting_id}?status=signed", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/board/meetings/{meeting_id}/documents/{document_id}/file")
async def board_meeting_document_file(
    request: Request,
    meeting_id: int,
    document_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await _require_invited_or_404(meeting_id, current_user, db)
    document = await _load_meeting_document_or_404(meeting_id, document_id, db)
    return await _stream_meeting_document(request, db, document, meeting_id, current_user.username)


@router.get("/board/meetings/{meeting_id}/documents/{document_id}/view", response_class=HTMLResponse)
async def board_meeting_document_view(
    request: Request,
    meeting_id: int,
    document_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    meeting = await _require_invited_or_404(meeting_id, current_user, db)
    document = await _load_meeting_document_or_404(meeting_id, document_id, db)

    seal_text = BRAND.seal(current_user.username)
    return templates.TemplateResponse(
        request,
        "viewer.html",
        {
            "user": current_user,
            "page_title": document.title,
            "page_subtitle": f"{meeting.title} · added {local_dt(document.uploaded_at)}",
            "file_url": f"/board/meetings/{meeting_id}/documents/{document_id}/file",
            "back_url": f"/board/meetings/{meeting_id}",
            "back_label": "Back to meeting",
            "seal_text": seal_text,
            "hide_app_chrome": True,
        },
    )
