"""Dashboard and appearance pages."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_db, get_current_user, get_optional_current_user, templates
from ..models import BoardMeeting, DocumentRecord, MeetingStatus, Role, User

router = APIRouter()

@router.get("/", response_class=HTMLResponse)
async def read_root(
    request: Request,
    current_user: Optional[User] = Depends(get_optional_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    if current_user is None:
        return templates.TemplateResponse(request, "login.html")
    can_upload = current_user.role in (Role.admin, Role.uploader)
    can_search = current_user.role in (Role.admin, Role.board_secretary, Role.board_member)

    document_count = await db.scalar(select(func.count()).select_from(DocumentRecord))

    next_meeting = None
    if current_user.role in (Role.admin, Role.board_secretary):
        statement = (
            select(BoardMeeting)
            .where(BoardMeeting.scheduled_at >= datetime.utcnow(), BoardMeeting.status == MeetingStatus.scheduled)
            .order_by(BoardMeeting.scheduled_at.asc())
            .limit(1)
        )
        result = await db.execute(statement)
        next_meeting = result.scalar_one_or_none()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": current_user,
            "can_upload": can_upload,
            "can_search": can_search,
            "document_count": document_count or 0,
            "next_meeting": next_meeting,
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
