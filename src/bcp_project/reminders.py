import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .calendar_utils import build_google_calendar_link, build_meeting_ics, new_meeting_uid
from .models import BoardMeeting, MeetingInvitation, MeetingStatus, User
from .notify import notify_meeting_email

logger = logging.getLogger("bcp_project.reminders")


async def send_due_reminders(db: AsyncSession) -> None:
    """Send 48h/24h reminder emails for upcoming meetings that have crossed
    their reminder window and haven't been sent one yet. Idempotent: each
    reminder is only sent once per invitee, tracked by its *_sent_at column.
    """
    now = datetime.utcnow()

    meetings_result = await db.execute(
        select(BoardMeeting).where(
            BoardMeeting.status == MeetingStatus.scheduled,
            BoardMeeting.scheduled_at > now,
            BoardMeeting.notifications_enabled.is_(True),
        )
    )
    meetings = meetings_result.scalars().all()

    for meeting in meetings:
        due_48h = now >= meeting.scheduled_at - timedelta(hours=48)
        due_24h = now >= meeting.scheduled_at - timedelta(hours=24)
        if not (due_48h or due_24h):
            continue

        invites_result = await db.execute(
            select(MeetingInvitation, User)
            .join(User, User.username == MeetingInvitation.username)
            .where(MeetingInvitation.meeting_id == meeting.id)
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

        for invite, user in invites_result.all():
            if due_48h and invite.reminder_48h_sent_at is None:
                sent = await notify_meeting_email(
                    db,
                    kind="reminder_48h",
                    user=user,
                    meeting_title=meeting.title,
                    scheduled_at=meeting.scheduled_at,
                    location=meeting.location or "",
                    agenda=meeting.agenda or "",
                    ics_content=ics_content,
                    google_calendar_link=google_calendar_link,
                    meeting_id=meeting.id,
                    meeting_notifications_enabled=meeting.notifications_enabled,
                )
                if sent:
                    invite.reminder_48h_sent_at = now

            if due_24h and invite.reminder_24h_sent_at is None:
                sent = await notify_meeting_email(
                    db,
                    kind="reminder_24h",
                    user=user,
                    meeting_title=meeting.title,
                    scheduled_at=meeting.scheduled_at,
                    location=meeting.location or "",
                    agenda=meeting.agenda or "",
                    ics_content=ics_content,
                    google_calendar_link=google_calendar_link,
                    meeting_id=meeting.id,
                    meeting_notifications_enabled=meeting.notifications_enabled,
                )
                if sent:
                    invite.reminder_24h_sent_at = now

    await db.commit()
    logger.info("Reminder sweep complete: checked %d upcoming meeting(s)", len(meetings))
