"""In-app notification routes."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import get_current_user, get_db, templates
from ..models import NotificationEvent, User

router = APIRouter()

@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    rows = (
        await db.execute(
            select(NotificationEvent)
            .where(NotificationEvent.username == current_user.username)
            .order_by(NotificationEvent.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {"user": current_user, "items": rows, "page_title": "Notifications"},
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


@router.get("/api/notifications/unread-count")
async def unread_notification_count(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    count = await db.scalar(
        select(func.count(NotificationEvent.id)).where(
            NotificationEvent.username == current_user.username,
            NotificationEvent.read_at.is_(None),
        )
    )
    return {"unread": int(count or 0)}


@router.post("/api/notifications/read")
async def mark_notifications_read(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Mark all unread notifications read, or only ``{"ids": [...]}`` when a JSON body names them."""
    ids: list = []
    if "application/json" in (request.headers.get("content-type") or ""):
        try:
            payload = await request.json()
            ids = [int(i) for i in (payload or {}).get("ids") or []]
        except (ValueError, TypeError, AttributeError):
            ids = []
    query = select(NotificationEvent).where(
        NotificationEvent.username == current_user.username,
        NotificationEvent.read_at.is_(None),
    )
    if ids:
        query = query.where(NotificationEvent.id.in_(ids))
    result = await db.execute(query)
    now = datetime.utcnow()
    for row in result.scalars().all():
        row.read_at = now
    await db.commit()
    return {"status": "ok"}
