"""In-app notification routes."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_current_user, get_db, templates
from ..models import NotificationEvent, User

router = APIRouter()

@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Any:
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {"user": current_user},
    )


@router.get("/api/notifications")
async def list_notifications(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    result = await db.execute(
        select(NotificationEvent)
        .where(NotificationEvent.username == current_user.username)
        .order_by(NotificationEvent.created_at.desc())
        .limit(50)
    )
    rows = result.scalars().all()
    unread = sum(1 for r in rows if r.read_at is None)
    return {
        "unread": unread,
        "count": len(rows),
        "results": [
            {
                "id": r.id,
                "kind": r.kind,
                "title": r.title,
                "body": r.body,
                "meeting_id": r.meeting_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "read": r.read_at is not None,
            }
            for r in rows
        ],
    }


@router.post("/api/notifications/read")
async def mark_notifications_read(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    result = await db.execute(
        select(NotificationEvent).where(
            NotificationEvent.username == current_user.username,
            NotificationEvent.read_at.is_(None),
        )
    )
    now = datetime.utcnow()
    for row in result.scalars().all():
        row.read_at = now
    await db.commit()
    return {"status": "ok"}
