"""Local audio paths for meeting transcription: live chunks (WebM) and uploaded recordings.

Browser ``MediaRecorder`` with a timeslice writes the EBML/WebM header only in the first
blob. Later blobs are Cluster fragments and are not valid files on their own — PyAV /
faster-whisper fail with ``Invalid data found when processing input``. We always rebuild a
playable prefix (binary concat of chunks ``0..N``) and prefer a 16 kHz WAV remux before STT.
"""

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


def session_wav_path(meeting_id: int) -> Path:
    return meeting_audio_dir(meeting_id) / "session.16k.wav"


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _binary_concat(paths: List[Path], out: Path) -> Path:
    """MediaRecorder-safe merge: later WebM slices need the first blob's header bytes."""
    with out.open("wb") as dest:
        for path in paths:
            dest.write(path.read_bytes())
    return out


def remux_to_wav(source: Path, out: Path, *, timeout: int = 600) -> Optional[Path]:
    """Decode any container to 16 kHz mono WAV for Whisper. None if ffmpeg fails.

    MediaRecorder concatenations often lack clean duration/cues; ignore corrupt packets
    and regenerate timestamps so later Clusters are not dropped.
    """
    if not _ffmpeg_available() or not source.exists() or source.stat().st_size == 0:
        return None
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-fflags",
                "+genpts+discardcorrupt",
                "-err_detect",
                "ignore_err",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(out),
            ],
            check=True,
            capture_output=True,
            timeout=timeout,
        )
        if out.exists() and out.stat().st_size > 0:
            return out
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("ffmpeg remux to wav failed for %s: %s", source, exc)
    return None


def client_session_path(meeting_id: int) -> Path:
    """Browser-assembled MediaRecorder blob (full ``new Blob(parts)``), preferred over concat."""
    return meeting_audio_dir(meeting_id) / "session.client.webm"


def save_client_session(meeting_id: int, data: bytes) -> Path:
    """Persist the browser's assembled recording for final STT."""
    if not data:
        raise ValueError("Empty session")
    limit = recording_max_bytes()
    if len(data) > limit:
        raise ValueError("Session too large")
    path = client_session_path(meeting_id)
    path.write_bytes(data)
    # Also mirror as session.webm so seals / fallbacks see a complete file.
    session_audio_path(meeting_id).write_bytes(data)
    return path


def materialize_chunks_through(meeting_id: int, seq: int) -> Optional[Path]:
    """Build a playable WebM of chunks ``0..seq`` (inclusive) for live STT."""
    paths = [chunk_path(meeting_id, i) for i in range(int(seq) + 1)]
    existing = [p for p in paths if p.exists() and p.stat().st_size > 0]
    if not existing:
        return None
    staging = meeting_audio_dir(meeting_id) / f"live-through-{int(seq):06d}.webm"
    _binary_concat(existing, staging)
    wav = meeting_audio_dir(meeting_id) / f"live-through-{int(seq):06d}.16k.wav"
    remuxed = remux_to_wav(staging, wav, timeout=120)
    return remuxed or staging


def merge_chunks(meeting_id: int) -> Optional[Path]:
    """Merge WebM chunks into session audio. Prefer WAV remux for speech models.

    Prefers the browser-assembled ``session.client.webm`` when present (same bytes the
    browser would play). Never uses ``ffmpeg -c copy`` on concatenated fragments — that
    truncates to the first Cluster (~5s) and was producing garbage Whisper output.

    Returns a path Whisper can open (``session.16k.wav`` when ffmpeg works, else ``session.webm``).
    """
    webm = session_audio_path(meeting_id)
    client = client_session_path(meeting_id)
    if client.exists() and client.stat().st_size > 0:
        if client.resolve() != webm.resolve():
            shutil.copyfile(client, webm)
        logger.info(
            "Meeting %s using client-assembled session (%s bytes)",
            meeting_id,
            client.stat().st_size,
        )
    else:
        chunks = list_chunk_paths(meeting_id)
        if not chunks:
            return None
        if len(chunks) == 1:
            shutil.copyfile(chunks[0], webm)
        else:
            # ffmpeg concat demuxer fails on MediaRecorder fragment blobs; binary concat.
            # Do not re-mux with -c copy — that drops later Clusters.
            _binary_concat(chunks, webm)

    wav = remux_to_wav(webm, session_wav_path(meeting_id), timeout=600)
    if wav is not None:
        logger.info("Meeting %s session remuxed to %s", meeting_id, wav.name)
        return wav
    logger.warning("Meeting %s WAV remux failed; Whisper will open %s", meeting_id, webm.name)
    return webm


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
