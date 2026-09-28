"""Organizer meeting management routes."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from ..access_control import write_audit
from ..auth import decode_access_token
from ..aws_utils import store_pdf
from ..brand import BRAND
from ..calendar_utils import build_google_calendar_link, build_meeting_ics, new_meeting_uid
from ..deps import (
    MEETING_INVITEE_ROLES,
    MEETING_UPLOAD_DIR,
    UPLOAD_DIR,
    _client_ip,
    _load_stored_pdf_bytes,
    get_current_user,
    get_db,
    require_meeting_organizer,
    role_display_label,
    templates,
)
from ..dummy_transcription import (
    TRANSCRIPTION_LIVE,
    TRANSCRIPTION_PROCESSING,
)
from ..jobs.handlers import (
    JOB_MEETING_CHUNK_STT,
    JOB_MEETING_MINUTES,
    JOB_MEETING_TRANSCRIBE,
    run_meeting_chunk_stt,
    run_meeting_minutes,
    run_meeting_transcribe,
)
from ..jobs.queue import enqueue_job, job_queue_enabled
from ..meeting_audio import clear_meeting_audio, list_chunk_paths, save_chunk
from ..minutes_draft import (
    MINUTES_OFF,
    MINUTES_PENDING,
    MINUTES_READY,
)
from ..models import (
    BoardMeeting,
    MeetingAttendance,
    MeetingDocument,
    MeetingInvitation,
    MeetingStatus,
    Role,
    User,
)
from ..notify import notify_meeting_email
from ..pdf_watermark import stamp_pdf_bytes
from ..tx_live import iter_captions
from .meeting_common import (
    _load_meeting_document_or_404,
    _load_meeting_or_404,
    _meeting_transcription_status,
    _meeting_workspace_extras,
    _minutes_view,
    _serve_meeting_document_file,
    _transcription_view,
    _validate_pdf_upload,
)

logger = logging.getLogger("bcp_project.routers.meetings")
router = APIRouter()

@router.get("/meetings", response_class=HTMLResponse)
async def list_meetings(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
) -> Any:
    require_meeting_organizer(current_user)

    statement = select(BoardMeeting).order_by(BoardMeeting.id.desc())
    result = await db.execute(statement)
    meetings = result.scalars().all()

    return templates.TemplateResponse(
        request,
        "meetings_list.html",
        {"user": current_user, "meetings": meetings, "status": status},
    )


@router.get("/meetings/new", response_class=HTMLResponse)
async def new_meeting_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_meeting_organizer(current_user)

    statement = (
        select(User)
        .where(User.role.in_(MEETING_INVITEE_ROLES), User.is_active.is_(True))
        .order_by(User.role, User.username)
    )
    result = await db.execute(statement)
    invite_candidates = result.scalars().all()

    invitees_by_role = []
    for role in MEETING_INVITEE_ROLES:
        members = [u for u in invite_candidates if u.role == role]
        if members:
            invitees_by_role.append(
                {"role": role, "label": role_display_label(role), "users": members}
            )

    return templates.TemplateResponse(
        request,
        "meeting_form.html",
        {
            "user": current_user,
            "invitees_by_role": invitees_by_role,
            "invite_candidates": invite_candidates,
        },
    )


@router.post("/meetings")
async def create_meeting(
    title: str = Form(..., min_length=3, max_length=200),
    scheduled_at: str = Form(...),
    location: str = Form(""),
    agenda: str = Form(""),
    invitees: List[str] = Form([]),
    notifications_enabled: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)

    try:
        meeting_datetime = datetime.fromisoformat(scheduled_at)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid meeting date/time")

    notify = notifications_enabled is not None
    meeting = BoardMeeting(
        title=title,
        scheduled_at=meeting_datetime,
        location=location.strip() or None,
        agenda=agenda,
        status=MeetingStatus.scheduled,
        created_by=current_user.username,
        notifications_enabled=notify,
    )
    db.add(meeting)
    await db.flush()

    invitee_usernames = {name.strip() for name in invitees if name and name.strip()}
    if invitee_usernames:
        statement = select(User).where(
            User.username.in_(invitee_usernames),
            User.role.in_(MEETING_INVITEE_ROLES),
            User.is_active.is_(True),
        )
        result = await db.execute(statement)
        invitee_users = result.scalars().all()

        ics_content = build_meeting_ics(
            meeting_uid=new_meeting_uid(meeting.id),
            title=meeting.title,
            scheduled_at=meeting.scheduled_at,
            location=meeting.location or "",
            description=meeting.agenda or "Board meeting",
        )
        google_calendar_link = build_google_calendar_link(
            title=meeting.title,
            scheduled_at=meeting.scheduled_at,
            location=meeting.location or "",
            description=meeting.agenda or "Board meeting",
        )
        for member in invitee_users:
            invitation = MeetingInvitation(meeting_id=meeting.id, username=member.username)
            db.add(invitation)
            if notify:
                sent = await notify_meeting_email(
                    db,
                    kind="invitation",
                    user=member,
                    meeting_title=meeting.title,
                    scheduled_at=meeting.scheduled_at,
                    location=meeting.location or "",
                    agenda=meeting.agenda or "",
                    ics_content=ics_content,
                    google_calendar_link=google_calendar_link,
                    meeting_id=meeting.id,
                    meeting_notifications_enabled=True,
                )
                if sent:
                    invitation.invitation_email_sent_at = datetime.utcnow()

    await db.commit()

    return RedirectResponse(url=f"/meetings/{meeting.id}?status=created", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/meetings/{meeting_id}", response_class=HTMLResponse)
async def meeting_detail(
    request: Request,
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
) -> Any:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)

    doc_statement = (
        select(MeetingDocument)
        .where(MeetingDocument.meeting_id == meeting_id)
        .order_by(MeetingDocument.uploaded_at.desc())
    )
    doc_result = await db.execute(doc_statement)
    documents = doc_result.scalars().all()

    invite_statement = (
        select(MeetingInvitation, User)
        .outerjoin(User, User.username == MeetingInvitation.username)
        .where(MeetingInvitation.meeting_id == meeting_id)
        .order_by(MeetingInvitation.username)
    )
    invite_result = await db.execute(invite_statement)
    invitations = []
    for invite, invitee_user in invite_result.all():
        invitations.append(
            {
                "username": invite.username,
                "invited_at": invite.invited_at,
                "role_label": role_display_label(invitee_user.role) if invitee_user else "User",
            }
        )

    attendance_statement = select(MeetingAttendance).where(MeetingAttendance.meeting_id == meeting_id)
    attendance_result = await db.execute(attendance_statement)
    attendance = {row.username: row for row in attendance_result.scalars().all()}
    extras = await _meeting_workspace_extras(db, meeting)
    transcript_segments = list(meeting.transcription_json or [])

    return templates.TemplateResponse(
        request,
        "meeting_detail.html",
        {
            "user": current_user,
            "meeting": meeting,
            "documents": documents,
            "invitations": invitations,
            "attendance": attendance,
            "status": status,
            "transcript_segments": transcript_segments,
            "audio_chunk_count": len(list_chunk_paths(meeting_id)),
            "minutes_view": _minutes_view(meeting),
            **extras,
        },
    )


@router.post("/meetings/{meeting_id}/attendance/open")
async def open_meeting_attendance(
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)

    meeting.attendance_open = True
    meeting.attendance_opened_at = datetime.utcnow()
    meeting.attendance_closed_at = None
    await db.commit()

    return RedirectResponse(url=f"/meetings/{meeting_id}?status=attendance_opened", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/meetings/{meeting_id}/attendance/close")
async def close_meeting_attendance(
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)

    meeting.attendance_open = False
    meeting.attendance_closed_at = datetime.utcnow()
    await db.commit()

    return RedirectResponse(url=f"/meetings/{meeting_id}?status=attendance_closed", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/meetings/{meeting_id}/transcription/start")
async def start_meeting_transcription(
    request: Request,
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)
    current = _meeting_transcription_status(meeting)
    if current == TRANSCRIPTION_LIVE:
        return RedirectResponse(
            url=f"/meetings/{meeting_id}?status=transcription_already_live#transcription",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if current == TRANSCRIPTION_PROCESSING:
        return RedirectResponse(
            url=f"/meetings/{meeting_id}?status=transcription_processing#transcription",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    clear_meeting_audio(meeting_id)
    meeting.transcription_status = TRANSCRIPTION_LIVE
    meeting.transcription_started_at = datetime.utcnow()
    meeting.transcription_stopped_at = None
    meeting.transcription_started_by = current_user.username
    meeting.transcription_json = []
    flag_modified(meeting, "transcription_json")
    meeting.minutes_status = MINUTES_OFF
    meeting.minutes_draft = None
    meeting.minutes_source = None
    meeting.minutes_generated_at = None
    await write_audit(
        db,
        username=current_user.username,
        action="transcription_start",
        resource_type="meeting",
        resource_id=str(meeting_id),
        detail="phase_b_live_captions",
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    return RedirectResponse(
        url=f"/meetings/{meeting_id}?status=transcription_started#transcription",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/api/meetings/{meeting_id}/transcription/chunks")
async def upload_transcription_chunk(
    meeting_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    seq: int = Form(...),
    chunk: UploadFile = File(...),
) -> JSONResponse:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)
    if _meeting_transcription_status(meeting) != TRANSCRIPTION_LIVE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Transcription is not recording")
    data = await chunk.read()
    try:
        path = save_chunk(meeting_id, int(seq), data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Phase B: enqueue per-chunk live STT (falls back to in-process).
    live_queued = False
    if job_queue_enabled():
        job_id = await enqueue_job(
            JOB_MEETING_CHUNK_STT,
            {"meeting_id": meeting_id, "seq": int(seq)},
            job_id=f"tx-chunk-{meeting_id}-{int(seq)}",
        )
        live_queued = bool(job_id)
    if not live_queued:
        background_tasks.add_task(run_meeting_chunk_stt, meeting_id, int(seq))

    return JSONResponse(
        {
            "ok": True,
            "seq": int(seq),
            "bytes": len(data),
            "chunk_count": len(list_chunk_paths(meeting_id)),
            "path": path.name,
            "live_stt": True,
        }
    )


@router.post("/meetings/{meeting_id}/transcription/stop")
async def stop_meeting_transcription(
    request: Request,
    meeting_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)
    if _meeting_transcription_status(meeting) != TRANSCRIPTION_LIVE:
        return RedirectResponse(
            url=f"/meetings/{meeting_id}?status=transcription_not_live#transcription",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    existing = [
        row
        for row in list(meeting.transcription_json or [])
        if (row.get("speaker") or "") != "System" and (row.get("text") or "").strip()
    ]
    meeting.transcription_status = TRANSCRIPTION_PROCESSING
    meeting.transcription_stopped_at = datetime.utcnow()
    if existing:
        meeting.transcription_json = existing + [
            {
                "id": len(existing) + 1,
                "speaker": "System",
                "text": "Finalizing transcript and drafting minutes…",
                "dummy": False,
            }
        ]
    else:
        meeting.transcription_json = [
            {
                "id": 1,
                "speaker": "System",
                "text": "Transcribing recorded audio… This may take a minute.",
                "dummy": False,
            }
        ]
    flag_modified(meeting, "transcription_json")
    meeting.minutes_status = MINUTES_PENDING
    await write_audit(
        db,
        username=current_user.username,
        action="transcription_stop",
        resource_type="meeting",
        resource_id=str(meeting_id),
        detail=f"phase_b_chunks={len(list_chunk_paths(meeting_id))}",
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()

    queued = False
    if job_queue_enabled():
        job_id = await enqueue_job(JOB_MEETING_TRANSCRIBE, {"meeting_id": meeting_id})
        queued = bool(job_id)
        if job_id:
            logger.info("Queued meeting_transcribe job %s for meeting %s", job_id, meeting_id)
    if not queued:
        background_tasks.add_task(run_meeting_transcribe, meeting_id)
        logger.info("Scheduled in-process meeting_transcribe for meeting %s", meeting_id)

    return RedirectResponse(
        url=f"/meetings/{meeting_id}?status=transcription_processing#transcription",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/api/meetings/{meeting_id}/transcription")
async def meeting_transcription_api(
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    meeting = await _load_meeting_or_404(meeting_id, db)
    is_organizer = current_user.role in (Role.admin, Role.board_secretary)
    if not is_organizer:
        invite = await db.execute(
            select(MeetingInvitation.id).where(
                MeetingInvitation.meeting_id == meeting_id,
                MeetingInvitation.username == current_user.username,
            )
        )
        if invite.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
        return JSONResponse(_transcription_view(meeting, include_segments=False))

    return JSONResponse(_transcription_view(meeting, include_segments=True))


@router.websocket("/ws/meetings/{meeting_id}/transcription")
async def meeting_transcription_ws(websocket: WebSocket, meeting_id: int) -> None:
    """Phase B: live caption stream for organizers (cookie auth)."""
    await websocket.accept()
    token = websocket.cookies.get("access_token")
    payload = decode_access_token(token) if token else None
    if not payload or not payload.get("sub"):
        await websocket.close(code=4401)
        return
    role = str(payload.get("role") or "")
    if role not in {Role.admin.value, Role.board_secretary.value}:
        await websocket.close(code=4403)
        return

    from ..db import get_session

    try:
        async with get_session() as session:
            meeting = await _load_meeting_or_404(meeting_id, session)
            snapshot = _transcription_view(meeting, include_segments=True)
    except HTTPException:
        await websocket.close(code=4404)
        return

    await websocket.send_json({"type": "snapshot", **snapshot})
    try:
        async for event in iter_captions(meeting_id):
            await websocket.send_json(event)
            if event.get("type") == "status" and event.get("status") == "stopped":
                break
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("transcription ws failed for meeting %s", meeting_id)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


@router.get("/api/meetings/{meeting_id}/minutes")
async def meeting_minutes_api(
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)
    return JSONResponse(_minutes_view(meeting))


@router.post("/meetings/{meeting_id}/minutes")
async def save_meeting_minutes(
    request: Request,
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    body_md: str = Form(...),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)
    meeting.minutes_draft = (body_md or "").strip()
    meeting.minutes_status = MINUTES_READY
    meeting.minutes_source = meeting.minutes_source or "edited"
    meeting.minutes_generated_at = meeting.minutes_generated_at or datetime.utcnow()
    await write_audit(
        db,
        username=current_user.username,
        action="minutes_save",
        resource_type="meeting",
        resource_id=str(meeting_id),
        detail=f"chars={len(meeting.minutes_draft or '')}",
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    return RedirectResponse(
        url=f"/meetings/{meeting_id}?status=minutes_saved#minutes",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/meetings/{meeting_id}/minutes/regenerate")
async def regenerate_meeting_minutes(
    request: Request,
    meeting_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)
    if not list(meeting.transcription_json or []):
        return RedirectResponse(
            url=f"/meetings/{meeting_id}?status=minutes_no_transcript#minutes",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    meeting.minutes_status = MINUTES_PENDING
    await write_audit(
        db,
        username=current_user.username,
        action="minutes_regenerate",
        resource_type="meeting",
        resource_id=str(meeting_id),
        detail="phase_c",
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()

    queued = False
    if job_queue_enabled():
        job_id = await enqueue_job(JOB_MEETING_MINUTES, {"meeting_id": meeting_id})
        queued = bool(job_id)
    if not queued:
        background_tasks.add_task(run_meeting_minutes, meeting_id)

    return RedirectResponse(
        url=f"/meetings/{meeting_id}?status=minutes_generating#minutes",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/meetings/{meeting_id}/attendance/print", response_class=HTMLResponse)
async def print_meeting_attendance(
    request: Request,
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)

    invite_statement = (
        select(MeetingInvitation)
        .where(MeetingInvitation.meeting_id == meeting_id)
        .order_by(MeetingInvitation.username)
    )
    invite_result = await db.execute(invite_statement)
    invitations = invite_result.scalars().all()

    attendance_statement = select(MeetingAttendance).where(MeetingAttendance.meeting_id == meeting_id)
    attendance_result = await db.execute(attendance_statement)
    attendance = {row.username: row for row in attendance_result.scalars().all()}

    return templates.TemplateResponse(
        request,
        "attendance_print.html",
        {
            "meeting": meeting,
            "invitations": invitations,
            "attendance": attendance,
            "generated_at": datetime.utcnow(),
            "generated_by": current_user.username,
        },
    )


@router.get("/meetings/{meeting_id}/attendance/{username}/signature")
async def meeting_attendance_signature(
    meeting_id: int,
    username: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    require_meeting_organizer(current_user)
    await _load_meeting_or_404(meeting_id, db)

    statement = select(MeetingAttendance).where(
        MeetingAttendance.meeting_id == meeting_id,
        MeetingAttendance.username == username,
    )
    result = await db.execute(statement)
    attendance = result.scalar_one_or_none()
    if attendance is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Signature not found")

    path = UPLOAD_DIR / attendance.signature_file
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Signature file not found on disk")

    return Response(
        content=path.read_bytes(),
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/meetings/{meeting_id}/agenda")
async def update_meeting_agenda(
    meeting_id: int,
    agenda: str = Form(...),
    notify_invitees: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)

    meeting.agenda = agenda

    if notify_invitees is not None and meeting.notifications_enabled:
        invites_result = await db.execute(
            select(MeetingInvitation, User)
            .join(User, User.username == MeetingInvitation.username)
            .where(MeetingInvitation.meeting_id == meeting_id)
        )
        ics_content = build_meeting_ics(
            meeting_uid=new_meeting_uid(meeting.id),
            title=meeting.title,
            scheduled_at=meeting.scheduled_at,
            location=meeting.location or "",
            description=agenda or "Board meeting",
        )
        google_calendar_link = build_google_calendar_link(
            title=meeting.title,
            scheduled_at=meeting.scheduled_at,
            location=meeting.location or "",
            description=agenda or "Board meeting",
        )
        for _invite, member in invites_result.all():
            await notify_meeting_email(
                db,
                kind="agenda_updated",
                user=member,
                meeting_title=meeting.title,
                scheduled_at=meeting.scheduled_at,
                location=meeting.location or "",
                agenda=agenda or "",
                ics_content=ics_content,
                google_calendar_link=google_calendar_link,
                meeting_id=meeting.id,
                meeting_notifications_enabled=True,
            )

    await db.commit()

    return RedirectResponse(url=f"/meetings/{meeting_id}?status=agenda_updated", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/meetings/{meeting_id}/notifications")
async def update_meeting_notifications(
    meeting_id: int,
    notifications_enabled: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)
    meeting.notifications_enabled = notifications_enabled is not None
    await db.commit()
    return RedirectResponse(
        url=f"/meetings/{meeting_id}?status=notifications_updated",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/meetings/{meeting_id}/notifications/resend")
async def resend_meeting_invitations(
    meeting_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)
    if not meeting.notifications_enabled:
        return RedirectResponse(
            url=f"/meetings/{meeting_id}?status=notifications_disabled",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    invites_result = await db.execute(
        select(MeetingInvitation, User)
        .join(User, User.username == MeetingInvitation.username)
        .where(MeetingInvitation.meeting_id == meeting_id)
    )
    ics_content = build_meeting_ics(
        meeting_uid=new_meeting_uid(meeting.id),
        title=meeting.title,
        scheduled_at=meeting.scheduled_at,
        location=meeting.location or "",
        description=meeting.agenda or "Board meeting",
    )
    google_calendar_link = build_google_calendar_link(
        title=meeting.title,
        scheduled_at=meeting.scheduled_at,
        location=meeting.location or "",
        description=meeting.agenda or "Board meeting",
    )
    sent_count = 0
    for invite, member in invites_result.all():
        sent = await notify_meeting_email(
            db,
            kind="invitation",
            user=member,
            meeting_title=meeting.title,
            scheduled_at=meeting.scheduled_at,
            location=meeting.location or "",
            agenda=meeting.agenda or "",
            ics_content=ics_content,
            google_calendar_link=google_calendar_link,
            meeting_id=meeting.id,
            meeting_notifications_enabled=True,
        )
        if sent:
            invite.invitation_email_sent_at = datetime.utcnow()
            sent_count += 1
    await db.commit()
    return RedirectResponse(
        url=f"/meetings/{meeting_id}?status=invites_resent&sent={sent_count}",
        status_code=status.HTTP_303_SEE_OTHER,
    )

@router.post("/meetings/{meeting_id}/documents")
async def upload_meeting_document(
    meeting_id: int,
    title: str = Form(..., min_length=1, max_length=255),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_meeting_organizer(current_user)
    await _load_meeting_or_404(meeting_id, db)
    _validate_pdf_upload(file)

    meeting_dir = MEETING_UPLOAD_DIR / str(meeting_id)
    meeting_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "document.pdf").name.replace("..", "")
    filename = f"{uuid.uuid4().hex}_{safe_name}"
    destination = meeting_dir / filename
    file_bytes = await file.read()
    destination.write_bytes(file_bytes)

    storage_key = f"meetings/{meeting_id}/{filename}"
    try:
        remote_location = await asyncio.to_thread(store_pdf, str(destination.resolve()), storage_key)
    except Exception as exc:
        logger.exception("Failed to store meeting document for meeting %s", meeting_id)
        raise HTTPException(status_code=502, detail="Failed to store meeting document") from exc

    document = MeetingDocument(
        meeting_id=meeting_id,
        title=title,
        file_location=remote_location,
        uploaded_by=current_user.username,
    )
    db.add(document)
    await db.commit()

    return RedirectResponse(url=f"/meetings/{meeting_id}?status=doc_uploaded", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/meetings/{meeting_id}/documents/{document_id}/file")
async def meeting_document_file(
    request: Request,
    meeting_id: int,
    document_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    require_meeting_organizer(current_user)
    document = await _load_meeting_document_or_404(meeting_id, document_id, db)
    pdf_bytes = await _load_stored_pdf_bytes(
        document.file_location,
        resource_id=f"meeting_document:{document.id}",
    )
    seal_text = BRAND.seal_meeting_doc(current_user.username)
    try:
        stamped = stamp_pdf_bytes(pdf_bytes, seal_text)
    except Exception as exc:
        logger.exception("Watermark stamping failed for meeting document %s", document.id)
        raise HTTPException(status_code=502, detail="Could not prepare secure meeting document stream") from exc
    await write_audit(
        db,
        username=current_user.username,
        action="view_file",
        resource_type="meeting_document",
        resource_id=str(document.id),
        detail=f"meeting_id={meeting_id}",
        ip_address=_client_ip(request),
        commit=True,
    )
    return _serve_meeting_document_file(document, stamped)


@router.get("/meetings/{meeting_id}/documents/{document_id}/view", response_class=HTMLResponse)
async def meeting_document_view(
    request: Request,
    meeting_id: int,
    document_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_meeting_organizer(current_user)
    meeting = await _load_meeting_or_404(meeting_id, db)
    document = await _load_meeting_document_or_404(meeting_id, document_id, db)

    seal_text = BRAND.seal(current_user.username)
    return templates.TemplateResponse(
        request,
        "viewer.html",
        {
            "user": current_user,
            "page_title": document.title,
            "page_subtitle": f"{meeting.title} · Uploaded {document.uploaded_at.strftime('%Y-%m-%d %H:%M')}",
            "file_url": f"/meetings/{meeting_id}/documents/{document_id}/file",
            "back_url": f"/meetings/{meeting_id}",
            "back_label": "Back to meeting",
            "seal_text": seal_text,
            "hide_app_chrome": True,
        },
    )
