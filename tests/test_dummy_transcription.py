import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcp_project.dummy_transcription import (
    DUMMY_SEGMENTS,
    expected_dummy_count,
    sync_dummy_transcript,
    transcription_payload,
)


def test_dummy_count_grows_with_elapsed_time():
    started = datetime(2026, 9, 16, 10, 0, 0)
    assert expected_dummy_count(started, started) == 1
    assert expected_dummy_count(started, started + timedelta(seconds=4)) == 1
    assert expected_dummy_count(started, started + timedelta(seconds=5)) == 2
    assert expected_dummy_count(started, started + timedelta(seconds=50)) == len(DUMMY_SEGMENTS)


def test_sync_does_not_shrink_or_invent_after_cap():
    started = datetime(2026, 9, 16, 10, 0, 0)
    first = sync_dummy_transcript([], started, now=started)
    assert first[0]["speaker"] == "Chair"
    later = sync_dummy_transcript(first, started, now=started + timedelta(seconds=20))
    assert len(later) >= len(first)
    assert later[0]["text"] == first[0]["text"]
    capped = sync_dummy_transcript(later, started, now=started + timedelta(hours=2))
    assert len(capped) == len(DUMMY_SEGMENTS)


def test_payload_hides_segments_for_invitees():
    lines = sync_dummy_transcript([], datetime(2026, 9, 16, 10, 0, 0), now=datetime(2026, 9, 16, 10, 0, 0))
    hidden = transcription_payload("live", lines, include_segments=False)
    shown = transcription_payload("live", lines, include_segments=True)
    assert hidden["live"] is True
    assert hidden["dummy"] is True
    assert hidden["segments"] == []
    assert hidden["segment_count"] == 1
    assert shown["segments"][0]["text"]
