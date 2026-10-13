"""Optional speaker diarization: who spoke when, matched onto transcript segments.

``DIARIZATION_PROVIDER``:
- ``none`` (default): segments keep the generic "Transcript" speaker.
- ``pyannote``: runs ``pyannote.audio`` on the worker (``DIARIZATION_MODEL``, ``HF_TOKEN``).
- ``http``: POSTs the audio to ``DIARIZATION_URL``, a service on bank hardware that returns
  ``{"turns": [{"start": s, "end": s, "speaker": "SPEAKER_00"}]}``.

Labels such as ``SPEAKER_00`` are shown as "Speaker 1" until the secretary names them in the
speaker map; the raw label stays on each segment so renaming is reversible.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bcp_project.diarization")

DIARIZATION_OFF = "off"
DIARIZATION_DONE = "done"
DIARIZATION_FAILED = "failed"
DIARIZATION_EMPTY = "empty"

_pipeline = None
_pipeline_lock = threading.Lock()


def diarization_provider() -> str:
    provider = (os.getenv("DIARIZATION_PROVIDER") or "none").strip().lower()
    return provider if provider in {"none", "pyannote", "http"} else "none"


def diarization_enabled() -> bool:
    return diarization_provider() != "none"


def diarization_status() -> Dict[str, Any]:
    """Configuration check for the admin page and /readyz. Loads no model and imports nothing:
    diarization runs on the meeting worker, not the web process that answers these."""
    provider = diarization_provider()
    issues: List[str] = []
    detail: Optional[str] = None
    if provider == "pyannote":
        detail = os.getenv("DIARIZATION_MODEL") or "pyannote/speaker-diarization-3.1"
        local_copy = detail.startswith(("/", "."))
        if not local_copy and not (os.getenv("HF_TOKEN") or "").strip():
            issues.append("HF_TOKEN is not set; the gated pyannote model cannot be downloaded")
    elif provider == "http":
        detail = (os.getenv("DIARIZATION_URL") or "").strip() or None
        if not detail:
            issues.append("DIARIZATION_URL is not set")
    return {
        "provider": provider,
        "enabled": provider != "none",
        "detail": detail,
        "ready": provider != "none" and not issues,
        "issues": issues,
    }


def _turn(start: Any, end: Any, speaker: Any) -> Optional[Dict[str, Any]]:
    try:
        s, e = float(start), float(end)
    except (TypeError, ValueError):
        return None
    label = str(speaker or "").strip()
    if e <= s or not label:
        return None
    return {"start": round(s, 2), "end": round(e, 2), "speaker": label}


def _pyannote_turns(path: Path) -> List[Dict[str, Any]]:
    global _pipeline
    from pyannote.audio import Pipeline  # optional dependency, installed only where used

    with _pipeline_lock:
        if _pipeline is None:
            model = os.getenv("DIARIZATION_MODEL") or "pyannote/speaker-diarization-3.1"
            _pipeline = Pipeline.from_pretrained(model, use_auth_token=os.getenv("HF_TOKEN") or None)
    annotation = _pipeline(str(path))
    turns = []
    for segment, _, speaker in annotation.itertracks(yield_label=True):
        item = _turn(segment.start, segment.end, speaker)
        if item:
            turns.append(item)
    return turns


def _http_turns(path: Path) -> List[Dict[str, Any]]:
    import httpx

    url = (os.getenv("DIARIZATION_URL") or "").strip()
    if not url:
        raise RuntimeError("DIARIZATION_PROVIDER=http needs DIARIZATION_URL")
    headers = {}
    token = (os.getenv("DIARIZATION_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    timeout = float(os.getenv("DIARIZATION_TIMEOUT") or 600)
    with path.open("rb") as fh:
        response = httpx.post(url, files={"file": (path.name, fh)}, headers=headers, timeout=timeout)
    response.raise_for_status()
    turns = []
    for row in (response.json() or {}).get("turns") or []:
        if isinstance(row, dict):
            item = _turn(row.get("start"), row.get("end"), row.get("speaker"))
            if item:
                turns.append(item)
    return turns


def diarize(path: Path) -> List[Dict[str, Any]]:
    """Speaker turns for ``path``. Raises on provider failure; returns [] when disabled."""
    provider = diarization_provider()
    if provider == "pyannote":
        return _pyannote_turns(path)
    if provider == "http":
        return _http_turns(path)
    return []


def display_name(label: str, speaker_map: Optional[Dict[str, str]], order: Dict[str, int]) -> str:
    named = (speaker_map or {}).get(label)
    if named and named.strip():
        return named.strip()
    return f"Speaker {order.get(label, len(order)) + 1}"


def _speaker_order(turns: List[Dict[str, Any]]) -> Dict[str, int]:
    order: Dict[str, int] = {}
    for turn in sorted(turns, key=lambda t: t["start"]):
        order.setdefault(turn["speaker"], len(order))
    return order


def assign_speakers(
    segments: List[Dict[str, Any]], turns: List[Dict[str, Any]], speaker_map: Optional[Dict[str, str]] = None
) -> List[Dict[str, Any]]:
    """Give each timed segment the speaker whose turns overlap it most."""
    if not turns:
        return segments
    order = _speaker_order(turns)
    out = []
    for seg in segments:
        item = dict(seg)
        if (item.get("speaker") or "") != "System" and item.get("t1") is not None:
            t0, t1 = float(item.get("t0") or 0), float(item.get("t1") or 0)
            overlap: Dict[str, float] = {}
            for turn in turns:
                shared = min(t1, turn["end"]) - max(t0, turn["start"])
                if shared > 0:
                    overlap[turn["speaker"]] = overlap.get(turn["speaker"], 0.0) + shared
            if overlap:
                label = max(overlap, key=overlap.get)
                item["speaker_label"] = label
                item["speaker"] = display_name(label, speaker_map, order)
        out.append(item)
    return out


def speaker_labels(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Distinct labels in first-spoken order, with the name currently shown."""
    seen: Dict[str, Dict[str, Any]] = {}
    for seg in segments:
        label = seg.get("speaker_label")
        if label and label not in seen:
            seen[label] = {"label": label, "name": seg.get("speaker") or label, "segments": 0}
        if label:
            seen[label]["segments"] += 1
    return list(seen.values())


def apply_speaker_map(segments: List[Dict[str, Any]], speaker_map: Dict[str, str]) -> List[Dict[str, Any]]:
    """Rename speakers by label. Unnamed labels fall back to "Speaker N"."""
    order: Dict[str, int] = {}
    for seg in segments:
        label = seg.get("speaker_label")
        if label:
            order.setdefault(label, len(order))
    out = []
    for seg in segments:
        item = dict(seg)
        label = item.get("speaker_label")
        if label:
            item["speaker"] = display_name(label, speaker_map, order)
        out.append(item)
    return out


def clean_speaker_map(raw: Dict[str, Any], labels: List[str]) -> Dict[str, str]:
    known = set(labels)
    return {str(k): str(v).strip()[:80] for k, v in (raw or {}).items() if k in known and str(v or "").strip()}
