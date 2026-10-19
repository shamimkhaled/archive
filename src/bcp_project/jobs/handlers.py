"""Background job handlers (chunk indexing, meeting transcription, live STT, minutes)."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from ..asr import stamp_segments, transcribe_audio_file, whisper_available
from ..chunker import chunk_documents
from ..db import get_session
from ..dummy_transcription import TRANSCRIPTION_LIVE, TRANSCRIPTION_PROCESSING, TRANSCRIPTION_STOPPED
from ..meeting_audio import (
    chunk_path,
    list_chunk_paths,
    materialize_chunks_through,
    merge_chunks,
    normalize_recording,
    recording_path,
    session_audio_path,
)
from ..minutes_draft import (
    MINUTES_FAILED,
    MINUTES_GENERATING,
    MINUTES_READY,
    generate_minutes_markdown,
    meeting_sensitivity,
)
from ..models import BoardMeeting
from ..services.minutes import MINUTES_DRAFT, extract_decisions_and_actions, minutes_state, replace_ai_items
from ..ports.adapters import get_llm_client, get_vector_store
from ..tx_live import publish_caption

logger = logging.getLogger("bcp_project.jobs.handlers")

JOB_CHUNK_INDEX = "chunk_index"
JOB_MEETING_TRANSCRIBE = "meeting_transcribe"
JOB_MEETING_CHUNK_STT = "meeting_chunk_stt"
JOB_MEETING_MINUTES = "meeting_minutes"
JOB_AUDIT_EXPORT = "audit_export"
JOB_CONNECTOR_PULL = "connector_pull"
JOB_WEBHOOK_DISPATCH = "webhook_dispatch"
JOB_DOCUMENT_INGEST = "document_ingest"
JOB_BOARD_BRIEFING = "board_briefing"
JOB_SIEM_FORWARD = "siem_forward"

# Approximate MediaRecorder timeslice used by the client (seconds).
CHUNK_DURATION_HINT = 5.0


def run_chunk_index(doc_id: str, page_payloads: List[Dict[str, Any]], *, sensitivity: str | None = None) -> int:
    """Embed + upsert child chunks. Used by worker and in-process fallback."""
    pages = []
    for payload in page_payloads:
        text = (payload or {}).get("text") or ""
        if not text.strip():
            continue
        pages.append(
            type("Page", (), {"text": text, "metadata": dict((payload or {}).get("metadata") or {})})()
        )

    if not pages:
        logger.info("No page text to chunk for %s", doc_id)
        return 0

    store = get_vector_store()
    store.create_collections()
    child_documents = chunk_documents(pages, parent_doc_id=doc_id)
    if not child_documents:
        return 0

    chunk_records = store.prepare_chunk_records(child_documents, parent_doc_id=doc_id)
    texts = [record["text"] for record in chunk_records]
    llm = get_llm_client()
    vectors: List[List[float]] = []
    batch_size = 24
    for i in range(0, len(texts), batch_size):
        vectors.extend(llm.embed_texts(texts[i : i + batch_size], sensitivity=sensitivity))
    store.upload_chunks(chunks=chunk_records, vectors=vectors)
    logger.info("Chunk index complete for %s (%s chunks)", doc_id, len(chunk_records))
    return len(chunk_records)


def _is_system_segment(seg: Dict[str, Any]) -> bool:
    return (seg.get("speaker") or "") == "System"


async def run_meeting_chunk_stt(meeting_id: int, seq: int) -> int:
    """Phase B: transcribe live captions for chunks ``0..seq``.

    Later MediaRecorder WebM blobs are not standalone files; we rebuild a playable
    prefix, remux to WAV when possible, and replace live segments with absolute times.
    """
    mid = int(meeting_id)
    sequence = int(seq)
    if not whisper_available():
        return 0

    path = await asyncio.to_thread(materialize_chunks_through, mid, sequence)
    if path is None or not path.exists() or path.stat().st_size == 0:
        return 0

    part = await asyncio.to_thread(transcribe_audio_file, path)
    usable = [dict(seg) for seg in part if not _is_system_segment(seg) and (seg.get("text") or "").strip()]
    if not usable:
        return 0

    published = 0
    async with get_session() as session:
        result = await session.execute(select(BoardMeeting).where(BoardMeeting.id == mid))
        meeting = result.scalar_one_or_none()
        if meeting is None:
            return 0
        status = (meeting.transcription_status or "").strip()
        if status not in {TRANSCRIPTION_LIVE, TRANSCRIPTION_PROCESSING}:
            return 0

        # Keep only non-live, non-system rows; live captions are rebuilt from the full prefix.
        kept = [
            s
            for s in list(meeting.transcription_json or [])
            if not _is_system_segment(s) and not s.get("live")
        ]
        next_id = max([int(s.get("id") or 0) for s in kept] + [0]) + 1
        started = meeting.transcription_started_at
        new_rows: List[Dict[str, Any]] = []
        for seg in usable:
            item = dict(seg)
            item["t0"] = round(float(item.get("t0") or 0), 2)
            item["t1"] = round(float(item.get("t1") or item["t0"]), 2)
            item["id"] = next_id
            item["seq"] = sequence
            item["live"] = status == TRANSCRIPTION_LIVE
            item["dummy"] = False
            if started is not None:
                from datetime import timedelta

                stamp = started + timedelta(seconds=item["t0"])
                item["at"] = stamp.replace(microsecond=0).isoformat() + "Z"
            next_id += 1
            new_rows.append(item)

        meeting.transcription_json = kept + new_rows
        flag_modified(meeting, "transcription_json")
        await session.commit()

    for row in new_rows:
        await publish_caption(
            mid,
            {"type": "segment", "segment": row, "status": TRANSCRIPTION_LIVE},
        )
        published += 1
    logger.info("Meeting %s live STT seq=%s published=%s path=%s", mid, sequence, published, path.name)
    return published


async def run_meeting_minutes(meeting_id: int) -> bool:
    """Phase C: draft minutes from the finished transcript."""
    mid = int(meeting_id)
    async with get_session() as session:
        result = await session.execute(select(BoardMeeting).where(BoardMeeting.id == mid))
        meeting = result.scalar_one_or_none()
        if meeting is None:
            logger.error("meeting_minutes: meeting %s not found", mid)
            return False
        if minutes_state(meeting) != MINUTES_DRAFT:
            # Minutes under review or approved are locked; a late job must not overwrite them.
            logger.info("meeting_minutes: meeting %s minutes are %s; not regenerating", mid, meeting.minutes_state)
            if meeting.minutes_status != MINUTES_READY:
                meeting.minutes_status = MINUTES_READY
                await session.commit()
            return False
        meeting.minutes_status = MINUTES_GENERATING
        await session.commit()

        segments = list(meeting.transcription_json or [])
        try:
            draft = generate_minutes_markdown(
                title=meeting.title,
                scheduled_at=meeting.scheduled_at,
                location=meeting.location,
                agenda=meeting.agenda or "",
                segments=segments,
            )
            meeting.minutes_draft = draft["body_md"]
            meeting.minutes_source = draft["source"]
            meeting.minutes_generated_at = datetime.utcnow()
            meeting.minutes_edited_by = None
            meeting.minutes_edited_at = None
            meeting.minutes_status = MINUTES_READY
            if draft["source"] == "llm":
                extracted = await asyncio.to_thread(
                    extract_decisions_and_actions, draft["body_md"], segments, sensitivity=meeting_sensitivity()
                )
                counts = await replace_ai_items(session, mid, extracted)
                logger.info("Meeting %s: extracted %s decisions, %s actions", mid, counts["decisions"], counts["actions"])
            await session.commit()
            await publish_caption(
                mid,
                {
                    "type": "minutes",
                    "status": MINUTES_READY,
                    "source": draft["source"],
                },
            )
            logger.info("Meeting %s minutes ready (source=%s)", mid, draft["source"])
            return True
        except Exception:
            logger.exception("meeting_minutes failed for %s", mid)
            meeting.minutes_status = MINUTES_FAILED
            await session.commit()
            return False


async def _schedule_minutes(meeting_id: int) -> None:
    from ..jobs.queue import enqueue_job, job_queue_enabled

    if job_queue_enabled():
        job_id = await enqueue_job(JOB_MEETING_MINUTES, {"meeting_id": int(meeting_id)})
        if job_id:
            return
    await run_meeting_minutes(int(meeting_id))


def _diarize_segments(mid: int, audio_path: Path, segments: List[Dict[str, Any]], speaker_map: Dict[str, str]):
    """Returns (segments, diarization_status). Failure keeps the undiarized transcript."""
    from ..diarization import DIARIZATION_DONE, DIARIZATION_EMPTY, DIARIZATION_FAILED, assign_speakers, diarize

    try:
        turns = diarize(audio_path)
    except Exception:
        logger.exception("Diarization failed for meeting %s", mid)
        return segments, DIARIZATION_FAILED
    if not turns:
        return segments, DIARIZATION_EMPTY
    return assign_speakers(segments, turns, speaker_map), DIARIZATION_DONE


async def run_meeting_transcribe(meeting_id: int, source: str | None = None) -> int:
    """Transcribe an uploaded recording, or the merged live chunks; write segments onto the meeting."""
    from ..diarization import DIARIZATION_OFF, diarization_enabled

    mid = int(meeting_id)
    chunks: List[Path] = []
    recording = recording_path(mid) if source != "live" else None
    if recording is not None:
        original = recording
        audio_path = await asyncio.to_thread(normalize_recording, mid, recording)
    else:
        chunks = list_chunk_paths(mid)
        merged = await asyncio.to_thread(merge_chunks, mid)
        audio_path = merged or (session_audio_path(mid) if session_audio_path(mid).exists() else None)
        # Seal the MediaRecorder WebM (or merged webm), not necessarily the WAV used for STT.
        webm = session_audio_path(mid)
        original = webm if webm.exists() else audio_path

    if audio_path is not None:
        segments_raw = await asyncio.to_thread(transcribe_audio_file, audio_path)
    elif chunks:
        # Last-resort: rebuild a playable prefix of every chunk and transcribe once.
        playable = await asyncio.to_thread(materialize_chunks_through, mid, len(chunks) - 1)
        if playable is not None:
            segments_raw = await asyncio.to_thread(transcribe_audio_file, playable)
            original = original or playable
        else:
            segments_raw = [
                {
                    "id": 1,
                    "speaker": "System",
                    "text": "Audio chunks could not be decoded. Install ffmpeg and try recording again.",
                    "dummy": False,
                }
            ]
    else:
        segments_raw = [
            {
                "id": 1,
                "speaker": "System",
                "text": "No audio chunks were uploaded. Keep this tab open while recording, then stop.",
                "dummy": False,
            }
        ]

    diarization_status = DIARIZATION_OFF
    if audio_path is not None and diarization_enabled():
        async with get_session() as session:
            row = await session.get(BoardMeeting, mid)
            speaker_map = dict((row.speaker_map if row else None) or {})
        segments_raw, diarization_status = await asyncio.to_thread(
            _diarize_segments, mid, audio_path, segments_raw, speaker_map
        )

    async with get_session() as session:
        result = await session.execute(select(BoardMeeting).where(BoardMeeting.id == mid))
        meeting = result.scalar_one_or_none()
        if meeting is None:
            logger.error("meeting_transcribe: meeting %s not found", mid)
            return 0
        stamped = stamp_segments(segments_raw, meeting.transcription_started_at)
        for row in stamped:
            row["live"] = False
        meeting.transcription_json = stamped
        meeting.diarization_status = diarization_status
        if (meeting.transcription_status or "") == TRANSCRIPTION_PROCESSING:
            meeting.transcription_status = TRANSCRIPTION_STOPPED
        await session.commit()
        if original is not None and original.exists():
            from ..services.meeting_records import seal_recording

            try:
                await seal_recording(
                    session,
                    mid,
                    original,
                    created_by=meeting.transcription_started_by or "system",
                    filename=meeting.recording_filename if recording is not None else f"live-session{original.suffix}",
                )
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("Sealing the recording for meeting %s failed; the transcript is kept", mid)
        logger.info(
            "Meeting %s transcription complete (%s segments, %s chunks)",
            mid,
            len(stamped),
            len(chunks),
        )
        await publish_caption(
            mid,
            {
                "type": "status",
                "status": TRANSCRIPTION_STOPPED,
                "segment_count": len(stamped),
            },
        )

    await _schedule_minutes(mid)
    return len(stamped)


async def run_audit_export(since_id: int = 0, limit: int = 500) -> Dict[str, Any]:
    """Export audit_logs rows as NDJSON for SIEM pickup (append-only file)."""
    import json
    from pathlib import Path

    from ..models import AuditLog
    from ..services.audit_chain import audit_row_payload

    export_dir = Path(os.getenv("AUDIT_EXPORT_DIR", "audit_exports"))
    export_dir.mkdir(parents=True, exist_ok=True)
    out_path = export_dir / f"audit_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.ndjson"
    exported = 0
    last_id = since_id
    async with get_session() as session:
        stmt = (
            select(AuditLog)
            .where(AuditLog.id > int(since_id))
            .order_by(AuditLog.id.asc())
            .limit(int(limit))
        )
        rows = (await session.execute(stmt)).scalars().all()
        with out_path.open("w", encoding="utf-8") as fh:
            for row in rows:
                payload = {**audit_row_payload(row), "prev_hash": row.prev_hash, "row_hash": row.row_hash}
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
                exported += 1
                last_id = row.id
    logger.info("Audit export wrote %s rows to %s (last_id=%s)", exported, out_path, last_id)
    return {"path": str(out_path), "exported": exported, "last_id": last_id}


async def run_audit_maintenance(*, batch: int = 1000, max_batches: int = 20) -> Dict[str, int]:
    """Seal new audit rows into the hash chain, then forward sealed rows to the SIEM if enabled."""
    from ..services.audit_chain import seal_pending
    from ..services.siem import forward_audit, siem_enabled

    sealed = sent = 0
    for _ in range(max_batches):
        async with get_session() as session:
            count = await seal_pending(session, limit=batch)
        sealed += count
        if count < batch:
            break
    if siem_enabled():
        for _ in range(max_batches):
            async with get_session() as session:
                result = await forward_audit(session, limit=batch)
            sent += result["sent"]
            if result["sent"] < batch:
                break
    if sealed or sent:
        logger.info("Audit maintenance: sealed %s rows, forwarded %s to SIEM", sealed, sent)
    return {"sealed": sealed, "forwarded": sent}


async def dispatch_job(job: Dict[str, Any]) -> None:
    job_type = job.get("type")
    payload = job.get("payload") or {}
    if job_type == JOB_CHUNK_INDEX:
        doc_id = str(payload.get("doc_id") or "")
        page_payloads = payload.get("page_payloads") or []
        if not doc_id:
            raise ValueError("chunk_index job missing doc_id")
        if payload.get("sensitivity"):
            run_chunk_index(doc_id, page_payloads, sensitivity=str(payload["sensitivity"]))
        else:
            run_chunk_index(doc_id, page_payloads)
        return
    if job_type == JOB_DOCUMENT_INGEST:
        from ..services.ingest import run_document_ingest

        doc_id = str(payload.get("doc_id") or "")
        if not doc_id:
            raise ValueError("document_ingest job missing doc_id")
        version = payload.get("version")
        await run_document_ingest(doc_id, version=int(version) if version is not None else None)
        return
    if job_type == JOB_BOARD_BRIEFING:
        from ..services.briefing import run_board_briefing

        briefing_id = payload.get("briefing_id")
        if briefing_id is None:
            raise ValueError("board_briefing job missing briefing_id")
        await run_board_briefing(int(briefing_id))
        return
    if job_type == JOB_MEETING_TRANSCRIBE:
        meeting_id = payload.get("meeting_id")
        if meeting_id is None:
            raise ValueError("meeting_transcribe job missing meeting_id")
        await run_meeting_transcribe(int(meeting_id), source=payload.get("source"))
        return
    if job_type == JOB_MEETING_CHUNK_STT:
        meeting_id = payload.get("meeting_id")
        seq = payload.get("seq")
        if meeting_id is None or seq is None:
            raise ValueError("meeting_chunk_stt job missing meeting_id/seq")
        await run_meeting_chunk_stt(int(meeting_id), int(seq))
        return
    if job_type == JOB_MEETING_MINUTES:
        meeting_id = payload.get("meeting_id")
        if meeting_id is None:
            raise ValueError("meeting_minutes job missing meeting_id")
        await run_meeting_minutes(int(meeting_id))
        return
    if job_type == JOB_AUDIT_EXPORT:
        await run_audit_export(
            since_id=int(payload.get("since_id") or 0),
            limit=int(payload.get("limit") or 500),
        )
        return
    if job_type == JOB_SIEM_FORWARD:
        await run_audit_maintenance(batch=int(payload.get("limit") or 1000))
        return
    if job_type == JOB_CONNECTOR_PULL:
        from ..services.connector_sync import pull_document_source

        await pull_document_source(
            limit=int(payload.get("limit") or 20),
            uploaded_by=str(payload.get("uploaded_by") or "connector"),
        )
        return
    if job_type == JOB_WEBHOOK_DISPATCH:
        from ..ports.events import publish_event

        event_type = str(payload.get("event_type") or "")
        body = payload.get("payload") or {}
        if not event_type:
            raise ValueError("webhook_dispatch missing event_type")
        ok = publish_event(event_type, body if isinstance(body, dict) else {})
        if not ok:
            raise RuntimeError(f"webhook_dispatch failed for {event_type}")
        return
    raise ValueError(f"Unknown job type: {job_type}")
