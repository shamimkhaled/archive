"""Create documents and versions from validated uploads, and stage their ingest."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..intake import PDF, ValidatedUpload, image_to_pdf, object_key, sha256_hex, store_write_once
from ..jobs.outbox import stage_job
from ..jobs.queue import job_queue_enabled
from ..models import DocumentRecord, DocumentVersion
from .ingest import INGEST_QUEUED, JOB_DOCUMENT_INGEST, VERSION_PROCESSING

logger = logging.getLogger("bcp_project.services.document_versions")

METADATA_FIELDS = ("title", "reference_number", "department", "subject", "category")


@dataclass
class IntakeResult:
    document: DocumentRecord
    version: DocumentVersion
    job_id: Optional[str]
    duplicate_of: List[Dict[str, Any]]


async def next_version_number(db: AsyncSession, doc_id: str) -> int:
    current = await db.scalar(select(func.max(DocumentVersion.version)).where(DocumentVersion.doc_id == doc_id))
    return int(current or 0) + 1


async def find_duplicates(db: AsyncSession, sha256: str, *, exclude_doc: Optional[str] = None) -> List[Dict[str, Any]]:
    stmt = select(DocumentVersion.doc_id, DocumentVersion.version).where(DocumentVersion.sha256 == sha256)
    if exclude_doc:
        stmt = stmt.where(DocumentVersion.doc_id != exclude_doc)
    return [{"doc_id": d, "version": v} for d, v in (await db.execute(stmt.limit(5))).all()]


async def store_version_files(doc_id: str, version: int, upload: ValidatedUpload) -> Dict[str, Optional[str]]:
    """Write-once original, plus a PDF rendition for image uploads (what the viewer opens)."""
    original = await asyncio.to_thread(
        store_write_once,
        upload.data,
        object_key("originals", doc_id, version, upload.sha256, upload.extension),
        content_type=upload.content_type,
        sha256=upload.sha256,
    )
    if not upload.is_image:
        return {"file_location": original, "original_location": None}
    pdf = await asyncio.to_thread(image_to_pdf, upload.data)
    pdf_hash = sha256_hex(pdf)
    rendition = await asyncio.to_thread(
        store_write_once,
        pdf,
        object_key("renditions", doc_id, version, pdf_hash, ".pdf"),
        content_type=PDF,
        sha256=pdf_hash,
    )
    return {"file_location": rendition, "original_location": original}


def _stage(db: AsyncSession, doc_id: str, version: int) -> Optional[str]:
    if not job_queue_enabled():
        return None
    return stage_job(
        db,
        JOB_DOCUMENT_INGEST,
        {"doc_id": doc_id, "version": version},
        resource_type="document",
        resource_id=doc_id,
    )


async def intake_new_document(
    db: AsyncSession,
    *,
    upload: ValidatedUpload,
    doc_id: str,
    doc_date: date,
    doc_type: str,
    keywords: List[str],
    sensitivity: str,
    uploaded_by: str,
    metadata: Optional[Dict[str, Optional[str]]] = None,
    meeting_id: Optional[int] = None,
    stage: bool = True,
) -> IntakeResult:
    """Store the original, create the document and its v1, and stage ingest. Caller commits.

    ``stage=False`` is for callers that run the ingest themselves in-request.
    """
    files = await store_version_files(doc_id, 1, upload)
    fields = {k: (v or None) for k, v in (metadata or {}).items() if k in METADATA_FIELDS}
    document = DocumentRecord(
        doc_id=doc_id,
        doc_date=doc_date,
        doc_type=doc_type,
        keywords=keywords,
        summary_json=None,
        file_location=files["file_location"],
        uploaded_by=uploaded_by,
        sensitivity=sensitivity,
        ingest_status=INGEST_QUEUED,
        ingest_updated_at=datetime.utcnow(),
        current_version=1,
        sha256=upload.sha256,
        content_type=upload.content_type,
        meeting_id=meeting_id,
        **fields,
    )
    version = DocumentVersion(
        doc_id=doc_id,
        version=1,
        sha256=upload.sha256,
        file_location=files["file_location"],
        original_location=files["original_location"],
        content_type=upload.content_type,
        size_bytes=len(upload.data),
        status=VERSION_PROCESSING,
        created_by=uploaded_by,
        created_at=datetime.utcnow(),
    )
    duplicates = await find_duplicates(db, upload.sha256)
    db.add(document)
    db.add(version)
    await db.flush()
    return IntakeResult(document, version, _stage(db, doc_id, 1) if stage else None, duplicates)


async def intake_new_version(
    db: AsyncSession,
    *,
    document: DocumentRecord,
    upload: ValidatedUpload,
    uploaded_by: str,
    note: Optional[str] = None,
) -> IntakeResult:
    """Add version N+1. The current version stays searchable until N+1 is indexed. Caller commits."""
    same = await db.scalar(
        select(DocumentVersion.version).where(
            DocumentVersion.doc_id == document.doc_id, DocumentVersion.sha256 == upload.sha256
        )
    )
    if same is not None:
        raise ValueError(f"This file is identical to version {same}")
    number = await next_version_number(db, document.doc_id)
    files = await store_version_files(document.doc_id, number, upload)
    version = DocumentVersion(
        doc_id=document.doc_id,
        version=number,
        sha256=upload.sha256,
        file_location=files["file_location"],
        original_location=files["original_location"],
        content_type=upload.content_type,
        size_bytes=len(upload.data),
        status=VERSION_PROCESSING,
        note=note,
        created_by=uploaded_by,
        created_at=datetime.utcnow(),
    )
    db.add(version)
    document.ingest_status = INGEST_QUEUED
    document.ingest_error = None
    document.ingest_updated_at = datetime.utcnow()
    await db.flush()
    duplicates = await find_duplicates(db, upload.sha256, exclude_doc=document.doc_id)
    return IntakeResult(document, version, _stage(db, document.doc_id, number), duplicates)


async def list_versions(db: AsyncSession, doc_id: str) -> List[DocumentVersion]:
    return list(
        (
            await db.execute(
                select(DocumentVersion).where(DocumentVersion.doc_id == doc_id).order_by(DocumentVersion.version.desc())
            )
        ).scalars()
    )


def version_payload(row: DocumentVersion, *, current: int) -> Dict[str, Any]:
    return {
        "version": row.version,
        "status": row.status,
        "is_current": row.version == current,
        "sha256": row.sha256,
        "content_type": row.content_type,
        "size_bytes": row.size_bytes,
        "note": row.note,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "view_url": f"/view/{row.doc_id}/versions/{row.version}/file",
    }
