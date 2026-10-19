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
    client_session_path,
    list_chunk_paths,
    materialize_chunks_through,
    meeting_chunks_dir,
    merge_chunks,
    save_chunk,
    save_client_session,
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


def test_materialize_and_merge_concat_mediarecorder_fragments(tmp_path, monkeypatch):
    """Later MediaRecorder blobs lack a WebM header; STT must concat 0..N before decode."""
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    clear_meeting_audio(11)
    # Minimal EBML-looking header only on the first slice (real MediaRecorder pattern).
    header = b"\x1a\x45\xdf\xa3" + b"HEADER"
    save_chunk(11, 0, header + b"-cluster-0")
    save_chunk(11, 1, b"-cluster-1-no-header")
    save_chunk(11, 2, b"-cluster-2-no-header")

    playable = materialize_chunks_through(11, 2)
    assert playable is not None and playable.exists()
    # Without ffmpeg the playable file is the concatenated WebM prefix.
    if playable.suffix == ".webm":
        assert playable.read_bytes().startswith(header)
        assert b"-cluster-2-no-header" in playable.read_bytes()

    merged = merge_chunks(11)
    assert merged is not None and merged.exists()
    webm = tmp_path / "meetings" / "11" / "audio" / "session.webm"
    assert webm.exists()
    assert webm.read_bytes().startswith(header)
    assert b"-cluster-1-no-header" in webm.read_bytes()


def test_merge_prefers_client_assembled_session(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    clear_meeting_audio(12)
    save_chunk(12, 0, b"\x1a\x45\xdf\xa3-short")
    save_chunk(12, 1, b"-frag")
    full = b"\x1a\x45\xdf\xa3" + b"CLIENT-FULL-RECORDING-BYTES"
    save_client_session(12, full)
    assert client_session_path(12).read_bytes() == full
    merged = merge_chunks(12)
    assert merged is not None
    webm = tmp_path / "meetings" / "12" / "audio" / "session.webm"
    assert webm.read_bytes() == full


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


def test_stt_status_reports_provider_without_loading_model():
    from bcp_project.asr import stt_status

    status = stt_status()
    assert status["provider"]
    assert "ready" in status and "issues" in status
    assert status["enabled"] == (status["provider"] != "none")
