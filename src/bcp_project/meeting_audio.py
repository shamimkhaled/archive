"""Local audio paths for meeting transcription: live chunks (WebM) and uploaded recordings."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("bcp_project.meeting_audio")

MAX_CHUNK_BYTES = 8 * 1024 * 1024  # 8 MiB per chunk
MAX_CHUNKS = 2400  # ~3h at 5s slices


def _meeting_upload_root() -> Path:
    upload_dir = Path(os.getenv("UPLOAD_DIR", "uploaded_pdfs"))
    root = upload_dir / "meetings"
    root.mkdir(parents=True, exist_ok=True)
    return root


def meeting_audio_dir(meeting_id: int) -> Path:
    path = _meeting_upload_root() / str(meeting_id) / "audio"
    path.mkdir(parents=True, exist_ok=True)
    return path


def meeting_chunks_dir(meeting_id: int) -> Path:
    path = meeting_audio_dir(meeting_id) / "chunks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def clear_meeting_audio(meeting_id: int) -> None:
    root = meeting_audio_dir(meeting_id)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    meeting_chunks_dir(meeting_id)


def chunk_path(meeting_id: int, seq: int) -> Path:
    return meeting_chunks_dir(meeting_id) / f"{int(seq):06d}.webm"


def list_chunk_paths(meeting_id: int) -> List[Path]:
    chunks = meeting_chunks_dir(meeting_id)
    return sorted(chunks.glob("*.webm"))


def save_chunk(meeting_id: int, seq: int, data: bytes) -> Path:
    if seq < 0 or seq >= MAX_CHUNKS:
        raise ValueError("Invalid chunk sequence")
    if len(data) > MAX_CHUNK_BYTES:
        raise ValueError("Chunk too large")
    if not data:
        raise ValueError("Empty chunk")
    path = chunk_path(meeting_id, seq)
    path.write_bytes(data)
    return path


def session_audio_path(meeting_id: int) -> Path:
    return meeting_audio_dir(meeting_id) / "session.webm"


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def merge_chunks(meeting_id: int) -> Optional[Path]:
    """Merge WebM chunks into session.webm. Returns path or None if no audio."""
    chunks = list_chunk_paths(meeting_id)
    if not chunks:
        return None
    out = session_audio_path(meeting_id)
    if len(chunks) == 1:
        shutil.copyfile(chunks[0], out)
        return out

    if _ffmpeg_available():
        list_file = meeting_audio_dir(meeting_id) / "concat.txt"
        lines = []
        for chunk in chunks:
            escaped = str(chunk.resolve()).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(list_file),
                    "-c",
                    "copy",
                    str(out),
                ],
                check=True,
                capture_output=True,
                timeout=600,
            )
            return out
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("ffmpeg merge failed for meeting %s: %s", meeting_id, exc)

    with out.open("wb") as dest:
        for chunk in chunks:
            dest.write(chunk.read_bytes())
    return out


# --- Recorded-file upload (a meeting recorded elsewhere, transcribed after the fact) ---

RECORDING_STEM = "recording"
NORMALIZED_NAME = "recording.16k.wav"


def recording_max_bytes() -> int:
    return int(float(os.getenv("MEETING_RECORDING_MAX_MB") or 500) * 1024 * 1024)


def sniff_recording(head: bytes) -> Optional[str]:
    """File extension for a supported audio/video container, judged by magic bytes only."""
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav"
    if head[:4] == b"OggS":
        return ".ogg"
    if head[:4] == b"fLaC":
        return ".flac"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    if head[4:8] == b"ftyp":
        return ".m4a" if head[8:12] in {b"M4A ", b"M4B "} else ".mp4"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return ".mp3"
    if head[:6] == b"#!AMR\n":
        return ".amr"
    return None


def recording_path(meeting_id: int) -> Optional[Path]:
    for path in sorted(meeting_audio_dir(meeting_id).glob(f"{RECORDING_STEM}.*")):
        if path.name != NORMALIZED_NAME:
            return path
    return None


def clear_recording(meeting_id: int) -> None:
    for path in meeting_audio_dir(meeting_id).glob(f"{RECORDING_STEM}.*"):
        path.unlink(missing_ok=True)


def normalize_recording(meeting_id: int, source: Path) -> Path:
    """16 kHz mono WAV for speech models and diarization; the original is used if ffmpeg is missing."""
    if not _ffmpeg_available():
        return source
    out = meeting_audio_dir(meeting_id) / NORMALIZED_NAME
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)],
            check=True,
            capture_output=True,
            timeout=int(os.getenv("MEETING_RECORDING_FFMPEG_TIMEOUT") or 1800),
        )
        return out
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("ffmpeg could not normalize the recording for meeting %s: %s", meeting_id, exc)
        return source
