import base64
import logging
import os
from datetime import datetime
from typing import Literal, Optional

import resend

from .brand import BRAND
from .config import load_environment, resolve_resend_api_key

load_environment()

logger = logging.getLogger("bcp_project.notifications")
logging.basicConfig(level=logging.INFO)

RESEND_API_KEY = resolve_resend_api_key()
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM", os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev"))
RESEND_FROM_NAME = os.getenv("RESEND_FROM_NAME", BRAND.email_from_name)

EmailKind = Literal["invitation", "reminder_48h", "reminder_24h", "agenda_updated"]

_SUBJECT_BY_KIND = {
    "invitation": "Board Meeting Invitation: {title}",
    "reminder_48h": "Reminder (48 hours): {title}",
    "reminder_24h": "Reminder (24 hours): {title}",
    "agenda_updated": "Agenda updated: {title}",
}

_INTRO_BY_KIND = {
    "invitation": "You're invited to attend the following board meeting.",
    "reminder_48h": "This is a reminder that the following board meeting is in about 48 hours.",
    "reminder_24h": "This is a reminder that the following board meeting is in about 24 hours.",
    "agenda_updated": "The agenda for the following board meeting has been updated.",
}


def _build_email_html(
    kind: EmailKind,
    meeting_title: str,
    scheduled_at: datetime,
    location: str,
    agenda: str,
    google_calendar_link: str,
) -> str:
    intro = _INTRO_BY_KIND[kind]
    agenda_html = (
        f'<p style="white-space:pre-wrap;margin:0.5em 0;">{agenda}</p>' if agenda.strip() else "<p style=\"color:#5a6d66;\">No agenda has been posted yet.</p>"
    )
    return f"""
    <div style="font-family: Inter, system-ui, sans-serif; color:#12122e; max-width:560px; margin:0 auto;">
        <div style="background:{BRAND.navy}; color:#ffffff; padding:1.25rem 1.5rem; border-radius:14px 14px 0 0;">
            <strong style="font-size:1.1rem;">{BRAND.product_name}</strong><br/>
            <span style="opacity:0.85;">{BRAND.org_name}</span>
        </div>
        <div style="border:1px solid #d8dae8; border-top:none; border-radius:0 0 14px 14px; padding:1.5rem;">
            <p>{intro}</p>
            <h2 style="margin:0.25em 0;">{meeting_title}</h2>
            <p style="margin:0.25em 0;"><strong>When:</strong> {scheduled_at.strftime('%Y-%m-%d %H:%M')}</p>
            {f'<p style="margin:0.25em 0;"><strong>Location:</strong> {location}</p>' if location else ''}
            <h3 style="margin-top:1.25em;">Agenda</h3>
            {agenda_html}
            <p style="margin-top:1.5em;">
                <a href="{google_calendar_link}" style="background:{BRAND.navy}; color:#ffffff; text-decoration:none; padding:0.7em 1.1em; border-radius:10px; font-weight:700;">
                    Add to Google Calendar
                </a>
            </p>
            <p style="color:#5a6d66; font-size:0.85em; margin-top:1.5em;">
                A calendar invite (.ics) is attached to this email for Outlook, Apple Calendar, and other calendar apps.
            </p>
        </div>
    </div>
    """


def send_meeting_email(
    kind: EmailKind,
    to_username: str,
    to_email: Optional[str],
    meeting_title: str,
    scheduled_at: datetime,
    location: str,
    agenda: str,
    ics_content: str,
    google_calendar_link: str,
) -> bool:
    """Send a board meeting email (invitation or reminder) via Resend.

    Never raises: a failed or skipped send returns False and logs, so one
    bad recipient/API hiccup can't break meeting creation or the reminder
    sweep for everyone else.
    """
    if not to_email:
        logger.warning("Skipping %s email for %s: no email on file", kind, to_username)
        return False

    if not RESEND_API_KEY:
        logger.warning("Skipping %s email for %s: RESEND_API_KEY is not configured", kind, to_email)
        return False

    subject = _SUBJECT_BY_KIND[kind].format(title=meeting_title)
    html = _build_email_html(kind, meeting_title, scheduled_at, location, agenda, google_calendar_link)
    ics_b64 = base64.b64encode(ics_content.encode("utf-8")).decode("ascii")

    try:
        resend.api_key = RESEND_API_KEY
        resend.Emails.send({
            "from": f"{RESEND_FROM_NAME} <{RESEND_FROM_EMAIL}>",
            "to": [to_email],
            "subject": subject,
            "html": html,
            "attachments": [{
                "filename": "meeting.ics",
                "content": ics_b64,
            }],
        })
        logger.info("Sent %s email to=%s subject=%r", kind, to_email, subject)
        return True
    except Exception:
        logger.exception("Failed to send %s email to=%s", kind, to_email)
        return False
