"""Helpers for email + in-app meeting notifications."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from .models import NotificationEvent, User
from .notifications import EmailKind, send_meeting_email

logger = logging.getLogger("bcp_project.notify")


async def record_notification(
    db: AsyncSession,
    *,
    username: str,
    kind: str,
    title: str,
    body: str = "",
    meeting_id: Optional[int] = None,
) -> None:
    db.add(
        NotificationEvent(
            username=username,
            kind=kind,
            title=title,
            body=body,
            meeting_id=meeting_id,
            created_at=datetime.utcnow(),
        )
    )


def user_allows_email(user: User) -> bool:
    return bool(getattr(user, "notifications_enabled", True))


async def notify_meeting_email(
    db: AsyncSession,
    *,
    kind: EmailKind,
    user: User,
    meeting_title: str,
    scheduled_at: datetime,
    location: str,
    agenda: str,
    ics_content: str,
    google_calendar_link: str,
    meeting_id: int,
    meeting_notifications_enabled: bool = True,
) -> bool:
    """Send email if meeting+user allow it, and always create an in-app event when appropriate."""
    if not meeting_notifications_enabled:
        logger.info("Skipping %s for %s: meeting notifications disabled", kind, user.username)
        return False

    labels = {
        "invitation": "Meeting invitation",
        "reminder_48h": "Meeting reminder (48h)",
        "reminder_24h": "Meeting reminder (24h)",
        "agenda_updated": "Agenda updated",
    }
    await record_notification(
        db,
        username=user.username,
        kind=kind,
        title=f"{labels.get(kind, 'Meeting update')}: {meeting_title}",
        body=f"{scheduled_at.strftime('%Y-%m-%d %H:%M')}"
        + (f" · {location}" if location else ""),
        meeting_id=meeting_id,
    )

    if not user_allows_email(user):
        logger.info("Skipping email %s for %s: user notifications disabled", kind, user.username)
        return False

    return send_meeting_email(
        kind,
        user.username,
        user.email,
        meeting_title,
        scheduled_at,
        location,
        agenda,
        ics_content,
        google_calendar_link,
    )
