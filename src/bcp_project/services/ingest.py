"""Asynchronous document ingest: an explicit, resumable state machine.

received -> queued -> parsing -> summarizing -> indexing -> indexed
                         |            \\-------------\\--> failed
                         \\-> needs_review -(reviewer approves)-> summarizing
                                         \\-(reviewer rejects)-> rejected

Each upload is a ``document_versions`` row with the SHA-256 of the stored
original. The original is stored (write-once) before the job is staged, so a
failed run can be retried without re-uploading. Search always reflects the
latest approved version; a newer version under review does not hide it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import ai
from ..models import DocumentPage, DocumentRecord, DocumentVersion

logger = logging.getLogger("bcp_project.services.ingest")

INGEST_RECEIVED = "received"
INGEST_QUEUED = "queued"
INGEST_PARSING = "parsing"
INGEST_SUMMARIZING = "summarizing"
INGEST_INDEXING = "indexing"
INGEST_INDEXED = "indexed"
INGEST_FAILED = "failed"
INGEST_NEEDS_REVIEW = "needs_review"
INGEST_REJECTED = "rejected"

INGEST_STATES = (
    INGEST_RECEIVED,
    INGEST_QUEUED,
    INGEST_PARSING,
    INGEST_NEEDS_REVIEW,
    INGEST_SUMMARIZING,
    INGEST_INDEXING,
    INGEST_INDEXED,
    INGEST_FAILED,
    INGEST_REJECTED,
)
# needs_review waits on a person, so pollers stop there too.
TERMINAL_STATES = frozenset({INGEST_INDEXED, INGEST_FAILED, INGEST_NEEDS_REVIEW, INGEST_REJECTED})

# Progress shown to the uploader while polling the status endpoint.
STATE_PROGRESS = {
    INGEST_RECEIVED: 10,
    INGEST_QUEUED: 20,
    INGEST_PARSING: 35,
    INGEST_NEEDS_REVIEW: 45,
    INGEST_SUMMARIZING: 60,
    INGEST_INDEXING: 82,
    INGEST_INDEXED: 100,
    INGEST_FAILED: 0,
    INGEST_REJECTED: 0,
}

VERSION_PROCESSING = "processing"
VERSION_PENDING_REVIEW = "pending_review"
VERSION_REVIEWED = "reviewed"
VERSION_APPROVED = "approved"
VERSION_SUPERSEDED = "superseded"
VERSION_REJECTED = "rejected"

JOB_DOCUMENT_INGEST = "document_ingest"
MAX_SUMMARY_CHARS = 60000


def build_summary_payload(doc_id: str, doc_type: str, doc_date: str, summary: Any) -> Dict[str, Any]:
    return {
        "doc_id": doc_id,
        "doc_type": doc_type,
        "doc_date": doc_date,
        "searchable_keywords": list(summary.searchable_keywords or []),
        "major_projects": [p if isinstance(p, dict) else p.dict() for p in summary.major_projects],
        "core_info": summary.core_info or {},
        "key_personnel": [p if isinstance(p, dict) else p.dict() for p in summary.key_personnel],
        "finance_and_admin": list(summary.finance_and_admin or []),
    }


async def set_ingest_status(db: AsyncSession, document: DocumentRecord, status: str, *, error: Optional[str] = None) -> None:
    document.ingest_status = status
    document.ingest_error = error
    document.ingest_updated_at = datetime.utcnow()
    await db.commit()


def ingest_status_payload(document: DocumentRecord, job: Optional[Any] = None) -> Dict[str, Any]:
    status = document.ingest_status or INGEST_INDEXED
    payload: Dict[str, Any] = {
        "doc_id": document.doc_id,
        "status": status,
        "progress": STATE_PROGRESS.get(status, 0),
        "done": status in TERMINAL_STATES,
        "error": document.ingest_error if status in {INGEST_FAILED, INGEST_REJECTED} else None,
        "page_count": document.page_count,
        "ocr_confidence": document.ocr_confidence,
        "verification_status": document.verification_status,
        "review_url": f"/review/{document.doc_id}" if status == INGEST_NEEDS_REVIEW else None,
        "updated_at": document.ingest_updated_at.isoformat() if document.ingest_updated_at else None,
    }
    if job is not None:
        payload["job"] = {
            "id": job.id,
            "status": job.status,
            "attempts": job.attempts,
            "last_error": job.last_error,
        }
    return payload


def summarize_with_provenance(text: str, sensitivity: Optional[str]) -> Tuple[Any, Dict[str, Any]]:
    """Run the summary model; when policy blocks it, return an empty summary instead of failing."""
    from ..summary_extractor import DocumentSummary, extract_document_summary

    try:
        summary = extract_document_summary(text, sensitivity=sensitivity)
    except ai.EgressBlocked as exc:
        logger.warning("Summary skipped by egress policy: %s", exc)
        return DocumentSummary(), {"summary": "skipped_egress_policy", "policy": ai.egress_policy()}
    call = ai.last_call() or {}
    return summary, {
        "summary": "ai",
        "provider": call.get("provider"),
        "model": call.get("model"),
        "route": call.get("source"),
        "at": datetime.utcnow().isoformat() + "Z",
    }


def summarize_pages_with_provenance(pages: List[Tuple[int, str]], sensitivity: Optional[str]) -> Tuple[Any, Dict[str, Any]]:
    """Map-reduce over page windows so long papers are summarized whole, not truncated.

    Each window of at most MAX_SUMMARY_CHARS is summarized on its own (map), then the
    partial summaries are merged (reduce). ``SUMMARY_MAX_WINDOWS`` bounds the calls per
    document; beyond it, evenly spaced windows are used and provenance says so.
    """
    from concurrent.futures import ThreadPoolExecutor

    from ..long_text import page_windows, render_window, sample_windows
    from ..summary_extractor import merge_summaries

    windows = page_windows(pages, MAX_SUMMARY_CHARS)
    if len(windows) <= 1:
        return summarize_with_provenance("\n\n".join(text for _, text in pages), sensitivity)
    used = sample_windows(windows, int(os.getenv("SUMMARY_MAX_WINDOWS") or 12))
    workers = max(1, int(os.getenv("SUMMARY_MAP_CONCURRENCY") or 3))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda w: summarize_with_provenance(render_window(w), sensitivity), used))
    blocked = next((prov for _, prov in results if prov.get("summary") != "ai"), None)
    if blocked is not None:
        return results[0][0], blocked
    provenance = {**results[-1][1], "map_reduce": {"windows": len(windows), "summarized": len(used)}}
    return merge_summaries([summary for summary, _ in results]), provenance


def ai_provenance_label(summary_json: Optional[Dict[str, Any]]) -> str:
    """Short UI label for who produced the summary, e.g. ``AI summary: local / qwen2.5:14b``."""
    info = (summary_json or {}).get("_ai") or {}
    if info.get("summary") == "skipped_egress_policy":
        return "AI summary withheld by egress policy"
    if info.get("provider"):
        return f"AI summary: {info['provider']} / {info.get('model') or '?'}"
    return ""


def index_summary_vector(qdrant: Any, doc_id: str, payload: Dict[str, Any], sensitivity: Optional[str]) -> bool:
    """Embed and upsert the summary point. False when egress policy keeps it out of the vector index."""
    from ..qdrant_store import build_summary_embedding_text, embed_texts

    try:
        vector = embed_texts([build_summary_embedding_text(payload)], sensitivity=sensitivity)[0]
    except ai.EgressBlocked as exc:
        logger.warning("Summary vector skipped for %s: %s", doc_id, exc)
        return False
    qdrant.upload_summary(doc_id, dict(payload), vector)
    return True


async def version_row(db: AsyncSession, document: DocumentRecord, version: Optional[int] = None) -> DocumentVersion:
    """The requested version, else the newest; documents from before versioning get a v1 row."""
    stmt = select(DocumentVersion).where(DocumentVersion.doc_id == document.doc_id)
    if version is not None:
        stmt = stmt.where(DocumentVersion.version == version)
    row = (await db.execute(stmt.order_by(DocumentVersion.version.desc()).limit(1))).scalar_one_or_none()
    if row is None:
        if version not in (None, 1):
            raise ValueError(f"{document.doc_id} has no version {version}")
        row = DocumentVersion(
            doc_id=document.doc_id,
            version=1,
            file_location=document.file_location,
            sha256=document.sha256,
            content_type=document.content_type or "application/pdf",
            status=VERSION_PROCESSING,
            created_by=document.uploaded_by,
            created_at=document.created_at or datetime.utcnow(),
        )
        db.add(row)
        await db.flush()
    return row


async def save_pages(db: AsyncSession, doc_id: str, version: int, pages: List[Any]) -> None:
    await db.execute(
        delete(DocumentPage).where(DocumentPage.doc_id == doc_id, DocumentPage.version == version)
    )
    for page in pages:
        db.add(
            DocumentPage(
                doc_id=doc_id,
                version=version,
                page_number=page.page_number,
                text=page.text or "",
                ocr_confidence=page.confidence,
                source=page.source,
            )
        )
    await db.flush()


async def load_pages(db: AsyncSession, doc_id: str, version: int) -> List[DocumentPage]:
    return list(
        (
            await db.execute(
                select(DocumentPage)
                .where(DocumentPage.doc_id == doc_id, DocumentPage.version == version)
                .order_by(DocumentPage.page_number)
            )
        ).scalars()
    )


async def _extract(db: AsyncSession, document: DocumentRecord, ver: DocumentVersion) -> Dict[str, Any]:
    """Parse/OCR the version, store page text and suggestions. Returns the confidence summary."""
    from ..deps import _load_stored_pdf_bytes
    from ..ocr import extract_pages, summarize_confidence
    from .metadata_suggest import suggest_metadata

    source = ver.original_location or ver.file_location
    data = await _load_stored_pdf_bytes(source, resource_id=document.doc_id)
    pages = await asyncio.to_thread(extract_pages, data, ver.content_type or "application/pdf")
    if not pages:
        raise ValueError("The document has no pages")
    await save_pages(db, document.doc_id, ver.version, pages)
    confidence = summarize_confidence(pages)
    document.page_count = len(pages)
    document.ocr_confidence = confidence["mean"]
    text = "\n\n".join(p.text for p in pages)
    document.suggested_metadata = await asyncio.to_thread(
        lambda: suggest_metadata(text, sensitivity=document.sensitivity)
    )
    return confidence


async def run_document_ingest(doc_id: str, *, version: Optional[int] = None, session_factory=None) -> str:
    """Worker entry point. Raises on failure so the job is retried, then dead-lettered.

    parsing -> (needs_review, wait for a person) -> summarizing -> indexing -> indexed.
    A version that a reviewer approved skips parsing and indexes the corrected text.
    """
    from ..cache import bump_search_cache_version
    from ..db import get_session
    from ..ports.events import publish_event

    factory = session_factory or get_session
    async with factory() as db:
        document = (
            await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == doc_id))
        ).scalar_one_or_none()
        if document is None:
            raise ValueError(f"document_ingest: {doc_id} not found")
        ver = await version_row(db, document, version)
        if ver.status in {VERSION_APPROVED, VERSION_SUPERSEDED, VERSION_REJECTED}:
            logger.info("Ingest for %s v%s already %s; skipping", doc_id, ver.version, ver.status)
            return document.ingest_status
        if ver.status == VERSION_PENDING_REVIEW:
            return INGEST_NEEDS_REVIEW

        pk = document.id
        try:
            if ver.status != VERSION_REVIEWED:
                await set_ingest_status(db, document, INGEST_PARSING)
                confidence = await _extract(db, document, ver)
                if confidence["needs_review"]:
                    ver.status = VERSION_PENDING_REVIEW
                    if document.current_version == ver.version:
                        document.verification_status = "pending"
                    await set_ingest_status(db, document, INGEST_NEEDS_REVIEW)
                    publish_event(
                        "document.needs_review",
                        {"doc_id": doc_id, "version": ver.version, "confidence": confidence["mean"]},
                    )
                    logger.info("%s v%s needs review (OCR confidence %s)", doc_id, ver.version, confidence)
                    return INGEST_NEEDS_REVIEW
            pages = await load_pages(db, doc_id, ver.version)
            if not any((p.text or "").strip() for p in pages):
                raise ValueError("Could not extract text from the document")
            await index_version(db, document, ver, pages)
        except Exception as exc:
            await db.rollback()
            document = await db.get(DocumentRecord, pk, populate_existing=True)
            if document is not None:
                await set_ingest_status(db, document, INGEST_FAILED, error=f"{type(exc).__name__}: {exc}"[:1000])
            raise

    await bump_search_cache_version()
    publish_event(
        "document.indexed",
        {
            "doc_id": doc_id,
            "version": ver.version,
            "doc_type": document.doc_type,
            "uploaded_by": document.uploaded_by,
            "source": "ingest",
        },
    )
    logger.info("Ingest complete for %s v%s (%s pages)", doc_id, ver.version, document.page_count)
    return INGEST_INDEXED


async def index_version(db: AsyncSession, document: DocumentRecord, ver: DocumentVersion, pages: List[DocumentPage]) -> None:
    """Summarize and index ``ver``, then make it the searchable version of the document."""
    from ..jobs.handlers import run_chunk_index
    from ..qdrant_store import make_qdrant_indexer
    from ..search_acl import search_payload

    doc_id = document.doc_id
    await set_ingest_status(db, document, INGEST_SUMMARIZING)
    sensitivity = document.sensitivity
    summary, provenance = await asyncio.to_thread(
        summarize_pages_with_provenance, [(p.page_number, p.text or "") for p in pages], sensitivity
    )
    payload = build_summary_payload(doc_id, document.doc_type, document.doc_date.isoformat(), summary)

    await set_ingest_status(db, document, INGEST_INDEXING)
    acl = {**search_payload(document), "version": ver.version}
    qdrant = make_qdrant_indexer()
    await asyncio.to_thread(qdrant.create_collections)
    await asyncio.to_thread(qdrant.delete_document_chunks, doc_id)
    if await asyncio.to_thread(index_summary_vector, qdrant, doc_id, {**payload, **acl}, sensitivity):
        page_payloads = [
            {
                "text": p.text,
                "metadata": {
                    **acl,
                    "page_number": p.page_number,
                    "text_source": p.source,
                    **({"ocr_confidence": p.ocr_confidence} if p.ocr_confidence is not None else {}),
                },
            }
            for p in pages
            if (p.text or "").strip()
        ]
        await asyncio.to_thread(run_chunk_index, doc_id, page_payloads, sensitivity=sensitivity)
        provenance["vectors"] = "indexed"
    else:
        provenance["vectors"] = "skipped_egress_policy"

    await db.execute(
        update(DocumentVersion)
        .where(
            DocumentVersion.doc_id == doc_id,
            DocumentVersion.version != ver.version,
            DocumentVersion.status == VERSION_APPROVED,
        )
        .values(status=VERSION_SUPERSEDED)
    )
    ver.status = VERSION_APPROVED
    document.current_version = ver.version
    document.file_location = ver.file_location
    document.sha256 = ver.sha256
    document.content_type = ver.content_type
    document.page_count = len(pages)
    if document.verification_status in {"pending", "rejected"}:
        document.verification_status = "approved"
    document.summary_json = {**payload, "_ai": provenance}
    if not document.keywords:
        document.keywords = list(payload["searchable_keywords"])
    await set_ingest_status(db, document, INGEST_INDEXED)
