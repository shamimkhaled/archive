"""Dashboard and appearance pages."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_db, get_current_user, get_optional_current_user, templates
from ..models import (
    AccessRequestStatus,
    BoardMeeting,
    DocumentAccessRequest,
    DocumentRecord,
    MeetingInvitation,
    MeetingStatus,
    Role,
    User,
)
from ..ports.identity import oidc_enabled
from ..ui import display_timezone

router = APIRouter()

@router.get("/", response_class=HTMLResponse)
async def read_root(
    request: Request,
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    if current_user is None:
        return templates.TemplateResponse(request, "login.html", {"oidc_enabled": oidc_enabled()})
    role = current_user.role
    can_upload = role in (Role.admin, Role.uploader)
    can_search = role in (Role.admin, Role.board_secretary, Role.board_member)
    organizer = role in (Role.admin, Role.board_secretary)

    document_count = await db.scalar(select(func.count()).select_from(DocumentRecord))

    # Meeting times are typed in Dhaka wall-clock time; keep today's meetings visible until the day is over.
    today_start = datetime.now(display_timezone()).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    upcoming = select(BoardMeeting).where(
        BoardMeeting.scheduled_at >= today_start, BoardMeeting.status == MeetingStatus.scheduled
    )
    if not organizer:
        upcoming = upcoming.join(MeetingInvitation, MeetingInvitation.meeting_id == BoardMeeting.id).where(
            MeetingInvitation.username == current_user.username
        )
    next_meetings = list(
        (await db.execute(upcoming.order_by(BoardMeeting.scheduled_at.asc()).limit(3))).scalars()
    )

    async def count(statement) -> int:
        return int(await db.scalar(statement) or 0)

    todo = []
    if organizer:
        todo.append({
            "count": await count(select(func.count()).select_from(DocumentRecord).where(DocumentRecord.ingest_status == "needs_review")),
            "label": "documents waiting for a check",
            "href": "/review",
        })
        todo.append({
            "count": await count(select(func.count()).select_from(DocumentRecord).where(DocumentRecord.metadata_check_pending.is_(True))),
            "label": "documents with details filled in by the AI to confirm",
            "href": "/review?list=details",
        })
        todo.append({
            "count": await count(
                select(func.count()).select_from(DocumentAccessRequest).where(DocumentAccessRequest.status == AccessRequestStatus.pending)
            ),
            "label": "access requests to answer",
            "href": "/admin/access-requests",
        })
        todo.append({
            "count": await count(select(func.count()).select_from(BoardMeeting).where(BoardMeeting.minutes_state == "in_review")),
            "label": "sets of minutes waiting for approval",
            "href": "/meetings",
        })
    if role == Role.uploader:
        mine = select(func.count()).select_from(DocumentRecord).where(DocumentRecord.uploaded_by == current_user.username)
        todo.append({"count": await count(mine.where(DocumentRecord.ingest_status == "needs_review")), "label": "of your uploads waiting for a check", "href": "/uploads/mine?show=check"})
        todo.append({"count": await count(mine.where(DocumentRecord.ingest_status == "failed")), "label": "of your uploads that didn't work", "href": "/uploads/mine"})
    if role == Role.board_member:
        todo.append({
            "count": await count(
                select(func.count()).select_from(DocumentAccessRequest).where(
                    DocumentAccessRequest.requester_username == current_user.username,
                    DocumentAccessRequest.status == AccessRequestStatus.pending,
                )
            ),
            "label": "of your access requests still waiting",
            "href": "/access-requests",
        })

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": current_user,
            "can_upload": can_upload,
            "can_search": can_search,
            "is_organizer": organizer,
            "document_count": document_count or 0,
            "next_meeting": next_meetings[0] if next_meetings else None,
            "next_meetings": next_meetings,
            "todo": todo,
        },
    )


@router.get("/appearance", response_class=HTMLResponse)
async def appearance_page(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Any:
    return templates.TemplateResponse(
        request,
        "appearance.html",
        {"user": current_user},
    )
