"""Phase A transcription helpers (audio paths + payload)."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcp_project.asr import stamp_segments, whisper_available
from bcp_project.dummy_transcription import (
    TRANSCRIPTION_LIVE,
    TRANSCRIPTION_PROCESSING,
    transcription_payload,
)
from bcp_project.meeting_audio import (
    MAX_CHUNK_BYTES,
    chunk_path,
    clear_meeting_audio,
    list_chunk_paths,
    meeting_chunks_dir,
    save_chunk,
)


def test_save_and_list_chunks(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    clear_meeting_audio(9)
    assert list_chunk_paths(9) == []
    p0 = save_chunk(9, 0, b"webm-bytes-0")
    p1 = save_chunk(9, 1, b"webm-bytes-1")
    assert p0.exists() and p1.exists()
    paths = list_chunk_paths(9)
    assert [p.name for p in paths] == ["000000.webm", "000001.webm"]
    assert chunk_path(9, 0) == meeting_chunks_dir(9) / "000000.webm"


def test_chunk_rejects_oversize(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    clear_meeting_audio(3)
    try:
        save_chunk(3, 0, b"x" * (MAX_CHUNK_BYTES + 1))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_transcription_payload_phase_flags():
    lines = [{"id": 1, "speaker": "Transcript", "text": "Hello", "dummy": False}]
    payload = transcription_payload(
        TRANSCRIPTION_PROCESSING,
        lines,
        started_at=datetime(2026, 9, 20, 10, 0, 0),
        include_segments=True,
        dummy=False,
        chunk_count=4,
    )
    assert payload["processing"] is True
    assert payload["live"] is False
    assert payload["dummy"] is False
    assert payload["phase"] == "c"
    assert payload["chunk_count"] == 4
    assert payload["segments"][0]["text"] == "Hello"

    live = transcription_payload(TRANSCRIPTION_LIVE, [], dummy=False, chunk_count=2)
    assert live["live"] is True
    assert live["processing"] is False


def test_stamp_segments_adds_iso_times():
    started = datetime(2026, 9, 20, 12, 0, 0)
    stamped = stamp_segments(
        [{"speaker": "Transcript", "text": "One", "t0": 5, "t1": 8}],
        started,
    )
    assert stamped[0]["at"] == "2026-09-20T12:00:05Z"
    assert stamped[0]["dummy"] is False


def test_whisper_available_is_bool():
    assert isinstance(whisper_available(), bool)
