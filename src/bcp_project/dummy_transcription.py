"""Simulated board-meeting captions for the optional AI transcription demo."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

TRANSCRIPTION_OFF = "off"
TRANSCRIPTION_LIVE = "live"
TRANSCRIPTION_STOPPED = "stopped"

DUMMY_INTERVAL_SECONDS = 5

DUMMY_SEGMENTS = [
    {
        "speaker": "Chair",
        "text": "Good afternoon. I call this sitting of the Board of Sonali Bank PLC to order.",
    },
    {
        "speaker": "Secretary",
        "text": "The agenda and papers were circulated. Attendance check-in is available for members in the room.",
    },
    {
        "speaker": "Chair",
        "text": "Item one: confirmation of the previous minutes. Are there any comments?",
    },
    {
        "speaker": "Member",
        "text": "No objection. The minutes may be taken as read and confirmed.",
    },
    {
        "speaker": "Chair",
        "text": "Thank you. Item two: financial performance. Management may proceed.",
    },
    {
        "speaker": "Management",
        "text": "Quarterly operating profit is in line with the pack. Credit quality remains under close watch.",
    },
    {
        "speaker": "Member",
        "text": "Please note the NPL movement in the confidential annex before we take a decision.",
    },
    {
        "speaker": "Chair",
        "text": "Noted. We will return to that annex after the related-party paper.",
    },
    {
        "speaker": "Secretary",
        "text": "Action: circulate the revised annex to members after this sitting.",
    },
    {
        "speaker": "Chair",
        "text": "If there is no other business, we may adjourn. Thank you.",
    },
]


def expected_dummy_count(started_at: Optional[datetime], now: Optional[datetime] = None) -> int:
    if started_at is None:
        return 0
    clock = now or datetime.utcnow()
    elapsed = max(0.0, (clock - started_at).total_seconds())
    count = 1 + int(elapsed // DUMMY_INTERVAL_SECONDS)
    return min(len(DUMMY_SEGMENTS), count)


def sync_dummy_transcript(
    segments: Optional[List[Dict[str, Any]]],
    started_at: Optional[datetime],
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Grow the dummy transcript to match elapsed time. Does not invent lines after stop."""
    clock = now or datetime.utcnow()
    want = expected_dummy_count(started_at, clock)
    out: List[Dict[str, Any]] = list(segments or [])
    while len(out) < want:
        index = len(out)
        line = DUMMY_SEGMENTS[index]
        stamp = started_at + timedelta(seconds=index * DUMMY_INTERVAL_SECONDS) if started_at else clock
        out.append(
            {
                "id": index + 1,
                "speaker": line["speaker"],
                "text": line["text"],
                "at": stamp.replace(microsecond=0).isoformat() + "Z",
                "dummy": True,
            }
        )
    return out


def transcription_payload(
    status: str,
    segments: Optional[List[Dict[str, Any]]],
    *,
    started_at: Optional[datetime] = None,
    stopped_at: Optional[datetime] = None,
    started_by: Optional[str] = None,
    include_segments: bool = True,
) -> Dict[str, Any]:
    lines = list(segments or [])
    return {
        "status": status or TRANSCRIPTION_OFF,
        "live": status == TRANSCRIPTION_LIVE,
        "dummy": True,
        "started_at": started_at.isoformat() + "Z" if started_at else None,
        "stopped_at": stopped_at.isoformat() + "Z" if stopped_at else None,
        "started_by": started_by,
        "segment_count": len(lines),
        "segments": lines if include_segments else [],
    }
