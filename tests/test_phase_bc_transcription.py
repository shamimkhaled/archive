"""Phase B/C transcription helpers (minutes draft + live payload)."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcp_project.dummy_transcription import TRANSCRIPTION_LIVE, transcription_payload
from bcp_project.minutes_draft import (
    MINUTES_READY,
    generate_minutes_markdown,
    minutes_payload,
    transcript_to_plain,
)
from bcp_project.jobs.handlers import JOB_MEETING_CHUNK_STT, JOB_MEETING_MINUTES, dispatch_job


def test_transcript_to_plain_skips_system():
    plain = transcript_to_plain(
        [
            {"speaker": "System", "text": "ignore"},
            {"speaker": "Chair", "text": "We approve the paper."},
        ]
    )
    assert "ignore" not in plain
    assert "Chair: We approve the paper." in plain


def test_generate_minutes_template_fallback(monkeypatch):
    monkeypatch.setattr(
        "bcp_project.minutes_draft._llm_minutes",
        lambda **kwargs: None,
    )
    draft = generate_minutes_markdown(
        title="Board sitting",
        scheduled_at=datetime(2026, 9, 20, 10, 0, 0),
        location="Head Office",
        agenda="1. Minutes\n2. Finance",
        segments=[{"speaker": "Chair", "text": "The minutes are confirmed."}],
    )
    assert draft["source"] == "template"
    assert "Board sitting" in draft["body_md"]
    assert "The minutes are confirmed." in draft["body_md"]
    assert "Draft minutes" in draft["body_md"]


def test_minutes_payload_flags():
    payload = minutes_payload(
        MINUTES_READY,
        "# Hello",
        generated_at=datetime(2026, 9, 20, 11, 0, 0),
        source="template",
    )
    assert payload["ready"] is True
    assert payload["generating"] is False
    assert payload["body_md"] == "# Hello"
    assert payload["source"] == "template"


def test_transcription_payload_includes_minutes():
    payload = transcription_payload(
        TRANSCRIPTION_LIVE,
        [],
        minutes={"status": "pending", "ready": False},
    )
    assert payload["phase"] == "c"
    assert payload["minutes"]["status"] == "pending"


def test_dispatch_unknown_still_raises():
    import asyncio

    try:
        asyncio.run(dispatch_job({"type": "nope", "payload": {}}))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "Unknown job type" in str(exc)


def test_job_type_constants():
    assert JOB_MEETING_CHUNK_STT == "meeting_chunk_stt"
    assert JOB_MEETING_MINUTES == "meeting_minutes"
