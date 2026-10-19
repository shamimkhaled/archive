"""Optional local speech-to-text for Phase A (faster-whisper or openai-whisper)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bcp_project.asr")


def _model_name() -> str:
    # Prefer the routed model so worker env (AI_STT_MODEL) and WHISPER_MODEL stay in sync.
    return (
        (os.getenv("AI_STT_MODEL") or os.getenv("WHISPER_MODEL") or "small").strip() or "small"
    )


def _device() -> str:
    return (os.getenv("WHISPER_DEVICE") or "cpu").strip() or "cpu"


def _remote_stt() -> bool:
    from .ai.routing import stt_route

    return stt_route().provider not in {"local_whisper", "none"}


def whisper_available() -> bool:
    if _remote_stt():
        return True
    try:
        import faster_whisper  # noqa: F401

        return True
    except ImportError:
        try:
            import whisper  # noqa: F401

            return True
        except ImportError:
            return False


def stt_status() -> Dict[str, Any]:
    """Configuration check for the admin page and /readyz. Loads no model."""
    from .ai.providers import provider_configured
    from .ai.routing import stt_route

    route = stt_route()
    issues: List[str] = []
    detail = route.model
    if route.provider == "none":
        issues.append("AI_STT_PROVIDER=none (speech-to-text disabled)")
    elif route.provider == "local_whisper":
        if not whisper_available():
            issues.append("faster-whisper (or openai-whisper) is not installed on this process")
        detail = f"{route.model} on {_device()}"
    elif route.provider == "local":
        if not provider_configured("local", "stt"):
            issues.append("LOCAL_STT_BASE_URL (or LOCAL_LLM_BASE_URL) is not set")
    elif not provider_configured(route.provider, "stt"):
        issues.append(f"{route.provider} credentials are not configured")
    return {
        "provider": route.provider,
        "model": route.model,
        "enabled": route.provider != "none",
        "detail": detail,
        "ready": route.provider != "none" and not issues,
        "issues": issues,
    }


def _language(language: Optional[str]) -> Optional[str]:
    return (language or os.getenv("WHISPER_LANGUAGE") or "").strip() or None


@lru_cache(maxsize=2)
def _faster_whisper_model(name: str, device: str, compute: str):
    from faster_whisper import WhisperModel

    return WhisperModel(name, device=device, compute_type=compute)


def _segments_from_faster_whisper(path: Path, language: Optional[str] = None, model_name: Optional[str] = None) -> List[Dict[str, Any]]:
    compute = (os.getenv("WHISPER_COMPUTE_TYPE") or "int8").strip() or "int8"
    model = _faster_whisper_model(model_name or _model_name(), _device(), compute)
    language = _language(language)
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


def _segments_from_openai_whisper(path: Path, language: Optional[str] = None, model_name: Optional[str] = None) -> List[Dict[str, Any]]:
    import whisper

    model = whisper.load_model(model_name or _model_name(), device=_device())
    language = _language(language)
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
    """Return transcript segments for a local audio file, using the AI_STT_PROVIDER route."""
    if not path.exists() or path.stat().st_size == 0:
        return [
            {
                "id": 1,
                "speaker": "System",
                "text": "No audio was recorded for this sitting.",
                "dummy": False,
            }
        ]
    if not _remote_stt():
        return transcribe_local(path)

    from . import ai
    from .minutes_draft import meeting_sensitivity

    language = (os.getenv("WHISPER_LANGUAGE") or "").strip() or None
    try:
        segments = ai.transcribe(str(path), language=language, sensitivity=meeting_sensitivity())
    except Exception as exc:
        logger.exception("Remote speech-to-text failed for %s", path)
        return [{"id": 1, "speaker": "System", "text": f"Speech recognition failed: {exc}", "dummy": False}]
    for seg in segments:
        seg.setdefault("dummy", False)
        if seg.get("speaker") == "Speaker":
            seg["speaker"] = "Transcript"
    return segments


def transcribe_local(path: Path, *, language: Optional[str] = None, model: Optional[str] = None) -> List[Dict[str, Any]]:
    """faster-whisper, then openai-whisper, on this machine. ``language`` and ``model`` override
    WHISPER_LANGUAGE and WHISPER_MODEL."""
    name = model or _model_name()
    try:
        import faster_whisper  # noqa: F401

        logger.info("Transcribing %s with faster-whisper model=%s", path.name, name)
        return _segments_from_faster_whisper(path, language, name)
    except ImportError:
        pass
    except Exception:
        logger.exception("faster-whisper failed for %s", path)

    try:
        import whisper  # noqa: F401

        logger.info("Transcribing %s with openai-whisper model=%s", path.name, name)
        return _segments_from_openai_whisper(path, language, name)
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
