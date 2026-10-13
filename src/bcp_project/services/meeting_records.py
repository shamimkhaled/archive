"""Linked, hash-sealed records of a meeting (requirement 3.2: archive audio/video, transcript,
draft minutes, and approved minutes as linked records).

Every artifact is stored write-once and gets a ``MeetingRecord`` row with the SHA-256 of the
stored bytes. The transcript and the approved minutes are also archive documents
(``TRN-<id>`` and ``MIN-<id>``) linked by ``meeting_id``, so they are searchable.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..intake import object_key, sha256_file, sha256_hex, store_file_write_once, store_write_once
from ..models import BoardMeeting, MeetingRecord

logger = logging.getLogger("bcp_project.services.meeting_records")

KIND_RECORDING = "recording"
KIND_TRANSCRIPT = "transcript"
KIND_MINUTES_DRAFT = "minutes_draft"
KIND_MINUTES_APPROVED = "minutes_approved"
KIND_LABELS = {
    KIND_RECORDING: "Recording",
    KIND_TRANSCRIPT: "Transcript",
    KIND_MINUTES_DRAFT: "Draft minutes",
    KIND_MINUTES_APPROVED: "Approved minutes",
}
TRANSCRIPT_DOC_TYPE = "Meeting Transcript"

RECORDING_TYPES = {
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
}


def _key(meeting_id: int, kind: str, version: int, sha256: str, extension: str) -> str:
    return object_key(f"meetings/{kind}", f"MTG-{meeting_id}", version, sha256, extension)


async def _next_version(db: AsyncSession, meeting_id: int, kind: str) -> int:
    current = await db.scalar(
        select(func.max(MeetingRecord.version)).where(MeetingRecord.meeting_id == meeting_id, MeetingRecord.kind == kind)
    )
    return int(current or 0) + 1


async def _find(db: AsyncSession, meeting_id: int, kind: str, *, sha256: Optional[str] = None, filename: Optional[str] = None):
    stmt = select(MeetingRecord).where(MeetingRecord.meeting_id == meeting_id, MeetingRecord.kind == kind)
    if sha256:
        stmt = stmt.where(MeetingRecord.sha256 == sha256)
    if filename:
        stmt = stmt.where(MeetingRecord.filename == filename)
    return (await db.execute(stmt.limit(1))).scalar_one_or_none()


async def seal_bytes(
    db: AsyncSession,
    meeting_id: int,
    kind: str,
    data: bytes,
    *,
    content_type: str,
    extension: str,
    created_by: str,
    filename: Optional[str] = None,
) -> MeetingRecord:
    """Store ``data`` write-once and record it. Identical bytes already sealed return the existing row."""
    digest = sha256_hex(data)
    existing = await _find(db, meeting_id, kind, sha256=digest)
    if existing is not None:
        return existing
    version = await _next_version(db, meeting_id, kind)
    location = await asyncio.to_thread(
        store_write_once, data, _key(meeting_id, kind, version, digest, extension), content_type=content_type, sha256=digest
    )
    row = MeetingRecord(
        meeting_id=meeting_id,
        kind=kind,
        version=version,
        file_location=location,
        sha256=digest,
        size_bytes=len(data),
        content_type=content_type,
        filename=filename,
        created_by=created_by,
        created_at=datetime.utcnow(),
    )
    db.add(row)
    await db.flush()
    return row


async def seal_recording(db: AsyncSession, meeting_id: int, path: Path, *, created_by: str, filename: Optional[str] = None):
    """Seal the original recording file (uploaded file or merged live session), streamed from disk."""
    digest = await asyncio.to_thread(sha256_file, path)
    existing = await _find(db, meeting_id, KIND_RECORDING, sha256=digest)
    if existing is not None:
        return existing
    extension = path.suffix.lower() or ".bin"
    content_type = RECORDING_TYPES.get(extension, "application/octet-stream")
    version = await _next_version(db, meeting_id, KIND_RECORDING)
    location = await asyncio.to_thread(
        store_file_write_once, path, _key(meeting_id, KIND_RECORDING, version, digest, extension), content_type=content_type, sha256=digest
    )
    row = MeetingRecord(
        meeting_id=meeting_id,
        kind=KIND_RECORDING,
        version=version,
        file_location=location,
        sha256=digest,
        size_bytes=path.stat().st_size,
        content_type=content_type,
        filename=filename or path.name,
        created_by=created_by,
        created_at=datetime.utcnow(),
    )
    db.add(row)
    await db.flush()
    return row


async def seal_minutes_draft(db: AsyncSession, meeting: BoardMeeting, *, submitted_by: str) -> Optional[MeetingRecord]:
    body = meeting.minutes_draft or ""
    if not body.strip():
        return None
    return await seal_bytes(
        db,
        meeting.id,
        KIND_MINUTES_DRAFT,
        body.encode("utf-8"),
        content_type="text/markdown; charset=utf-8",
        extension=".md",
        created_by=submitted_by,
        filename=f"minutes-draft-v{int(meeting.minutes_version or 1)}.md",
    )


async def link_archive_document(
    db: AsyncSession, meeting_id: int, kind: str, intake_result: Any, *, created_by: str, filename: Optional[str] = None
) -> MeetingRecord:
    """Record an archive document version (transcript or approved minutes) as a meeting record."""
    version = intake_result.version
    row = MeetingRecord(
        meeting_id=meeting_id,
        kind=kind,
        version=await _next_version(db, meeting_id, kind),
        file_location=version.file_location,
        sha256=version.sha256,
        size_bytes=getattr(version, "size_bytes", None),
        content_type="application/pdf",
        filename=filename,
        archive_doc_id=intake_result.document.doc_id,
        created_by=created_by,
        created_at=datetime.utcnow(),
    )
    db.add(row)
    await db.flush()
    return row


def transcript_markdown(meeting: BoardMeeting) -> str:
    lines: List[str] = [f"# Transcript — {meeting.title}", ""]
    for seg in meeting.transcription_json or []:
        text = (seg.get("text") or "").strip()
        speaker = (seg.get("speaker") or "Speaker").strip()
        if not text or speaker == "System" or seg.get("dummy"):
            continue
        t0 = float(seg.get("t0") or 0)
        lines.append(f"[{int(t0 // 60):02d}:{int(t0 % 60):02d}] **{speaker}:** {text}")
        lines.append("")
    return "\n".join(lines).strip()


async def archive_transcript(db: AsyncSession, meeting: BoardMeeting, *, approved_by: str):
    """Archive the transcript as document ``TRN-<id>`` (a new version when it changed).

    Returns ``(IntakeResult, MeetingRecord)``, or None when there is no transcript or it is
    unchanged since the last archived version.
    """
    from ..intake import validate_upload
    from ..minutes_draft import meeting_sensitivity
    from ..minutes_pdf import render_minutes_pdf
    from ..models import DocumentRecord
    from ..ocr import PageText
    from .document_versions import intake_new_document, intake_new_version
    from .ingest import VERSION_REVIEWED, save_pages

    body = transcript_markdown(meeting)
    if body.count("\n") < 2:
        return None
    text_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    filename = f"transcript-{text_sha[:16]}.pdf"
    if await _find(db, meeting.id, KIND_TRANSCRIPT, filename=filename) is not None:
        return None

    approved_at = meeting.minutes_approved_at or datetime.utcnow()
    pdf, page_texts = await asyncio.to_thread(
        lambda: render_minutes_pdf(
            title=meeting.title,
            body_md=body,
            meeting_date=meeting.scheduled_at,
            location=meeting.location,
            version=int(meeting.minutes_version or 1),
            approved_by=approved_by,
            approved_at=approved_at,
            sha256=text_sha,
            label="Meeting transcript",
        )
    )
    upload = validate_upload(pdf)
    doc_id = f"TRN-{meeting.id}"
    document = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == doc_id))).scalar_one_or_none()
    note = f"Transcript archived with minutes v{int(meeting.minutes_version or 1)} by {approved_by}"
    if document is None:
        result = await intake_new_document(
            db,
            upload=upload,
            doc_id=doc_id,
            doc_date=(meeting.scheduled_at or approved_at).date(),
            doc_type=TRANSCRIPT_DOC_TYPE,
            keywords=["meeting transcript"],
            sensitivity=meeting_sensitivity(),
            uploaded_by=approved_by,
            metadata={
                "title": f"Transcript — {meeting.title}",
                "subject": meeting.title,
                "category": TRANSCRIPT_DOC_TYPE,
                "reference_number": doc_id,
            },
            meeting_id=meeting.id,
        )
    else:
        result = await intake_new_version(db, document=document, upload=upload, uploaded_by=approved_by, note=note)
    result.version.note = note
    await save_pages(
        db,
        doc_id,
        result.version.version,
        [PageText(page_number=n, text=text, confidence=None, source="approved") for n, text in enumerate(page_texts, 1)],
    )
    result.version.status = VERSION_REVIEWED
    result.version.reviewed_by = approved_by
    result.version.reviewed_at = approved_at
    result.document.page_count = len(page_texts)
    record = await link_archive_document(db, meeting.id, KIND_TRANSCRIPT, result, created_by=approved_by, filename=filename)
    return result, record


async def list_records(db: AsyncSession, meeting_id: int) -> List[MeetingRecord]:
    rows = await db.execute(
        select(MeetingRecord)
        .where(MeetingRecord.meeting_id == meeting_id)
        .order_by(MeetingRecord.kind, MeetingRecord.version)
    )
    return list(rows.scalars())


def record_payload(row: MeetingRecord) -> Dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "label": KIND_LABELS.get(row.kind, row.kind),
        "version": row.version,
        "sha256": row.sha256,
        "size_bytes": row.size_bytes,
        "content_type": row.content_type,
        "filename": row.filename,
        "archive_doc_id": row.archive_doc_id,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "download_url": f"/meetings/{row.meeting_id}/records/{row.id}/file",
        "view_url": f"/view/{row.archive_doc_id}" if row.archive_doc_id else None,
    }
