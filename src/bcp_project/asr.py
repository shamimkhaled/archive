"""Optional local speech-to-text for Phase A (faster-whisper or openai-whisper)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("bcp_project.asr")


def _model_name() -> str:
    return (os.getenv("WHISPER_MODEL") or "base").strip() or "base"


def _device() -> str:
    return (os.getenv("WHISPER_DEVICE") or "cpu").strip() or "cpu"


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except ImportError:
        try:
            import whisper  # noqa: F401

            return True
        except ImportError:
            return False


def _segments_from_faster_whisper(path: Path) -> List[Dict[str, Any]]:
    from faster_whisper import WhisperModel

    compute = (os.getenv("WHISPER_COMPUTE_TYPE") or "int8").strip() or "int8"
    model = WhisperModel(_model_name(), device=_device(), compute_type=compute)
    language = (os.getenv("WHISPER_LANGUAGE") or "").strip() or None
    segments_iter, _info = model.transcribe(
        str(path),
        language=language,
        vad_filter=True,
        beam_size=1,
    )
    out: List[Dict[str, Any]] = []
    for index, seg in enumerate(segments_iter, start=1):
        text = (seg.text or "").strip()
        if not text:
            continue
        out.append(
            {
                "id": index,
                "speaker": "Transcript",
                "text": text,
                "t0": round(float(seg.start or 0), 2),
                "t1": round(float(seg.end or 0), 2),
                "dummy": False,
            }
        )
    return out


def _segments_from_openai_whisper(path: Path) -> List[Dict[str, Any]]:
    import whisper

    model = whisper.load_model(_model_name(), device=_device())
    language = (os.getenv("WHISPER_LANGUAGE") or "").strip() or None
    kwargs = {"fp16": False}
    if language:
        kwargs["language"] = language
    result = model.transcribe(str(path), **kwargs)
    out: List[Dict[str, Any]] = []
    for index, seg in enumerate(result.get("segments") or [], start=1):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        out.append(
            {
                "id": index,
                "speaker": "Transcript",
                "text": text,
                "t0": round(float(seg.get("start") or 0), 2),
                "t1": round(float(seg.get("end") or 0), 2),
                "dummy": False,
            }
        )
    if not out and (result.get("text") or "").strip():
        out.append(
            {
                "id": 1,
                "speaker": "Transcript",
                "text": result["text"].strip(),
                "t0": 0.0,
                "t1": 0.0,
                "dummy": False,
            }
        )
    return out


def transcribe_audio_file(path: Path) -> List[Dict[str, Any]]:
    """Return transcript segments for a local audio file."""
    if not path.exists() or path.stat().st_size == 0:
        return [
            {
                "id": 1,
                "speaker": "System",
                "text": "No audio was recorded for this sitting.",
                "dummy": False,
            }
        ]

    try:
        import faster_whisper  # noqa: F401

        logger.info("Transcribing %s with faster-whisper model=%s", path.name, _model_name())
        return _segments_from_faster_whisper(path)
    except ImportError:
        pass
    except Exception:
        logger.exception("faster-whisper failed for %s", path)

    try:
        import whisper  # noqa: F401

        logger.info("Transcribing %s with openai-whisper model=%s", path.name, _model_name())
        return _segments_from_openai_whisper(path)
    except ImportError:
        return [
            {
                "id": 1,
                "speaker": "System",
                "text": (
                    "Audio was saved, but no speech model is installed on the worker. "
                    "Run: pip install faster-whisper  (and install ffmpeg). "
                    "Then re-stop or re-run the meeting_transcribe job."
                ),
                "dummy": False,
            }
        ]
    except Exception as exc:
        logger.exception("openai-whisper failed for %s", path)
        return [
            {
                "id": 1,
                "speaker": "System",
                "text": f"Speech recognition failed: {exc}",
                "dummy": False,
            }
        ]


def stamp_segments(segments: List[Dict[str, Any]], started_at: datetime | None) -> List[Dict[str, Any]]:
    """Attach ISO timestamps from meeting start + segment offsets."""
    base = started_at or datetime.utcnow()
    stamped: List[Dict[str, Any]] = []
    for index, seg in enumerate(segments, start=1):
        item = dict(seg)
        item["id"] = index
        t0 = float(item.get("t0") or 0)
        stamp = base + timedelta(seconds=t0)
        item["at"] = stamp.replace(microsecond=0).isoformat() + "Z"
        item["dummy"] = False
        stamped.append(item)
    return stamped
