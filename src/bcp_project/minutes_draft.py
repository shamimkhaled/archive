"""Phase C — draft meeting minutes from a finished transcript (LLM or template fallback)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import load_environment

load_environment()

logger = logging.getLogger("bcp_project.minutes_draft")

MINUTES_OFF = "off"
MINUTES_PENDING = "pending"
MINUTES_GENERATING = "generating"
MINUTES_READY = "ready"
MINUTES_FAILED = "failed"


def meeting_sensitivity() -> str:
    """Board transcripts are confidential unless MEETING_SENSITIVITY says otherwise."""
    import os

    from .models import parse_sensitivity

    return parse_sensitivity(os.getenv("MEETING_SENSITIVITY") or "confidential")


def _strip_code_fence(text: str) -> str:
    text = re.sub(r"```(?:markdown|md)?\s*", "", text, flags=re.IGNORECASE)
    text = text.replace("```", "")
    return text.strip()


def transcript_to_plain(segments: Optional[List[Dict[str, Any]]]) -> str:
    lines: List[str] = []
    for seg in segments or []:
        speaker = (seg.get("speaker") or "Speaker").strip()
        text = (seg.get("text") or "").strip()
        if not text or speaker == "System":
            continue
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


def _template_minutes(
    *,
    title: str,
    scheduled_at: Optional[datetime],
    location: Optional[str],
    agenda: str,
    transcript: str,
) -> str:
    when = scheduled_at.strftime("%d %b %Y, %H:%M UTC") if scheduled_at else "—"
    agenda_block = (agenda or "").strip() or "_No agenda recorded._"
    transcript_block = transcript.strip() or "_No transcript segments available._"
    return (
        f"# Draft minutes — {title}\n\n"
        f"**Date/time:** {when}  \n"
        f"**Venue:** {location or '—'}  \n"
        f"**Status:** Draft (auto-generated; edit before circulation)\n\n"
        f"## Agenda\n\n{agenda_block}\n\n"
        f"## Discussion points\n\n- _Summarise the points discussed under each agenda item after review._\n\n"
        f"### Transcript\n\n{transcript_block}\n\n"
        f"## Decisions\n\n- _Add decisions after review._\n\n"
        f"## Actions\n\n| Action | Owner | Responsible division | Due |\n|---|---|---|---|\n"
        f"| _Add actions after review_ |  |  |  |\n\n"
        f"---\n_Generated without an LLM (template fallback). Review and edit before approval._\n"
    )


def _llm_minutes(
    *,
    title: str,
    scheduled_at: Optional[datetime],
    location: Optional[str],
    agenda: str,
    transcript: str,
    sensitivity: Optional[str] = None,
) -> Optional[str]:
    from . import ai
    from .ai.guard import with_rules, wrap_untrusted

    when = scheduled_at.strftime("%Y-%m-%d %H:%M UTC") if scheduled_at else "unknown"
    system = with_rules(
        "You are a board secretary drafting formal minutes for Sonali Bank PLC.\n"
        "Write clear draft minutes in Markdown from the transcript you are given.\n"
        "Use these sections in order: Attendance note, Agenda items discussed, "
        "Discussion points (the key points raised under each agenda item, with the speaker where stated), "
        "Decisions, Actions, Any other business.\n"
        "Write Actions as a table with columns: Action | Owner | Responsible division | Due. "
        "Fill Owner, Responsible division, and Due only where the transcript states them.\n"
        "Do not invent attendees, decisions, or actions that are not supported by the transcript.\n"
        "If something is unclear, write '_To be confirmed_'.\n"
        "Return Markdown only — no code fences, no preamble."
    )
    prompt = (
        f"Meeting title: {title}\n"
        f"Scheduled: {when}\n"
        f"Location: {location or '—'}\n"
        f"Agenda:\n{wrap_untrusted(agenda or '(none)', 'agenda')}\n\n"
        f"Transcript:\n{wrap_untrusted(transcript or '(empty)', 'transcript')}\n"
    )
    try:
        raw = ai.chat(
            "minutes",
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            sensitivity=sensitivity if sensitivity is not None else meeting_sensitivity(),
            temperature=0.2,
            max_tokens=2500,
        ).strip()
        return _strip_code_fence(raw) or None
    except ai.EgressBlocked as exc:
        logger.warning("Minutes LLM skipped by egress policy: %s", exc)
        return None
    except Exception:
        logger.exception("Minutes LLM call failed")
        return None


def generate_minutes_markdown(
    *,
    title: str,
    scheduled_at: Optional[datetime] = None,
    location: Optional[str] = None,
    agenda: str = "",
    segments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return {body_md, source: llm|template}."""
    transcript = transcript_to_plain(segments)
    body = _llm_minutes(
        title=title,
        scheduled_at=scheduled_at,
        location=location,
        agenda=agenda or "",
        transcript=transcript,
    )
    if body:
        return {"body_md": body, "source": "llm"}
    return {
        "body_md": _template_minutes(
            title=title,
            scheduled_at=scheduled_at,
            location=location,
            agenda=agenda or "",
            transcript=transcript,
        ),
        "source": "template",
    }


def minutes_payload(
    status: str,
    body: Optional[str] = None,
    *,
    generated_at: Optional[datetime] = None,
    source: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "status": status or MINUTES_OFF,
        "ready": (status or "") == MINUTES_READY,
        "generating": (status or "") in {MINUTES_PENDING, MINUTES_GENERATING},
        "body_md": body or "",
        "source": source,
        "generated_at": generated_at.isoformat() + "Z" if generated_at else None,
    }
