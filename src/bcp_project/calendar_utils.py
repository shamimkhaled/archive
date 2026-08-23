import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode

from .brand import BRAND


def _escape_ics_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold_line(line: str) -> str:
    # RFC 5545 requires folding lines longer than 75 octets.
    if len(line) <= 75:
        return line
    parts = [line[:75]]
    rest = line[75:]
    while rest:
        parts.append(" " + rest[:74])
        rest = rest[74:]
    return "\r\n".join(parts)


def build_meeting_ics(
    meeting_uid: str,
    title: str,
    scheduled_at: datetime,
    location: str,
    description: str,
    duration_minutes: int = 60,
    reminder_minutes_before: int = 30,
) -> str:
    """Build a minimal RFC 5545 .ics invite for a board meeting."""
    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    dtstart = scheduled_at.strftime("%Y%m%dT%H%M%SZ")
    dtend = (scheduled_at + timedelta(minutes=duration_minutes)).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//{BRAND.product_name}//Board Meetings//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{meeting_uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        f"SUMMARY:{_escape_ics_text(title)}",
        f"LOCATION:{_escape_ics_text(location)}",
        f"DESCRIPTION:{_escape_ics_text(description)}",
        "STATUS:CONFIRMED",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "DESCRIPTION:Board meeting reminder",
        f"TRIGGER:-PT{reminder_minutes_before}M",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(_fold_line(line) for line in lines) + "\r\n"


def new_meeting_uid(meeting_id: int) -> str:
    return f"board-meeting-{meeting_id}-{uuid.uuid4().hex}@bcp-project"


def build_google_calendar_link(
    title: str,
    scheduled_at: datetime,
    location: str,
    description: str,
    duration_minutes: int = 60,
) -> str:
    """Build a one-click 'Add to Google Calendar' link for a board meeting."""
    dtstart = scheduled_at.strftime("%Y%m%dT%H%M%SZ")
    dtend = (scheduled_at + timedelta(minutes=duration_minutes)).strftime("%Y%m%dT%H%M%SZ")
    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{dtstart}/{dtend}",
        "details": description,
        "location": location,
    }
    return f"https://calendar.google.com/calendar/render?{urlencode(params)}"
