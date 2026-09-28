"""Pull pending documents from DocumentSourcePort into the archive ingest path."""

from __future__ import annotations

import logging
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy import select

from ..cache import bump_search_cache_version
from ..db import get_session
from ..deps import UPLOAD_DIR
from ..jobs.handlers import JOB_CHUNK_INDEX, run_chunk_index
from ..jobs.queue import enqueue_job, job_queue_enabled
from ..models import DocumentRecord
from ..ports.document_source import get_document_source
from ..ports.events import publish_event
from ..ports.registry import get_registry
from ..qdrant_store import build_summary_embedding_text, embed_texts, make_qdrant_indexer
from ..summary_extractor import extract_document_summary
from ..aws_utils import store_pdf

logger = logging.getLogger("bcp_project.services.connector_sync")


def _suggest_doc_id(filename: str) -> str:
    stem = Path(filename).stem.replace(" ", "-")[:40]
    return f"INBOX-{date.today().isoformat()}-{stem}-{uuid.uuid4().hex[:6]}".upper()


async def pull_document_source(*, limit: int = 20, uploaded_by: str = "connector") -> Dict[str, Any]:
    """List + ingest pending inbox/SFTP PDFs. Returns summary counts."""
    registry = get_registry()
    source_reg = registry.get("document_source")
    source = source_reg.factory()

    try:
        pending = source_reg.circuit.call(lambda: source.list_pending(limit=limit))
    except Exception as exc:
        logger.exception("document_source list_pending failed")
        return {"ok": False, "error": str(exc)[:200], "imported": 0, "skipped": 0}

    imported = 0
    skipped = 0
    errors: List[str] = []

    for item in pending:
        item_id = str(item.get("id") or "")
        name = str(item.get("name") or item_id)
        if not item_id.lower().endswith(".pdf") and not name.lower().endswith(".pdf"):
            skipped += 1
            continue
        try:
            pdf_bytes = source_reg.circuit.call(lambda: source.fetch_bytes(item_id))
            doc_id = _suggest_doc_id(name)
            async with get_session() as db:
                exists = await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == doc_id))
                if exists.scalar_one_or_none() is not None:
                    skipped += 1
                    continue

                safe_name = Path(name).name.replace("..", "")
                filename = f"{uuid.uuid4().hex}_{safe_name}"
                destination = UPLOAD_DIR / filename
                destination.write_bytes(pdf_bytes)
                pdf_path = str(destination.resolve())

                from ..pdf_parser import parse_pdf

                pages = parse_pdf(pdf_path)
                if not pages:
                    errors.append(f"{item_id}: no text")
                    skipped += 1
                    continue
                document_text = "\n\n".join(page.text for page in pages)
                if len(document_text) > 60000:
                    document_text = document_text[:60000] + "\n\n[Truncated for summarization]"
                summary = extract_document_summary(document_text)
                remote_location = store_pdf(pdf_path, filename)

                qdrant = make_qdrant_indexer()
                qdrant.create_collections()
                summary_payload = {
                    "doc_id": doc_id,
                    "doc_type": "Imported",
                    "doc_date": date.today().isoformat(),
                    "searchable_keywords": summary.searchable_keywords,
                    "major_projects": [
                        p if isinstance(p, dict) else p.dict() for p in summary.major_projects
                    ],
                    "core_info": summary.core_info or {},
                    "key_personnel": [p if isinstance(p, dict) else p.dict() for p in summary.key_personnel],
                    "finance_and_admin": list(summary.finance_and_admin or []),
                }
                summary_text = build_summary_embedding_text(summary_payload)
                summary_vector = embed_texts([summary_text])[0]
                qdrant.upload_summary(summary_id=doc_id, payload=summary_payload, vector=summary_vector)

                page_payloads = [
                    {"text": page.text, "metadata": dict(page.metadata or {})} for page in pages
                ]
                if job_queue_enabled():
                    job_id = await enqueue_job(
                        JOB_CHUNK_INDEX,
                        {"doc_id": doc_id, "page_payloads": page_payloads},
                    )
                    if not job_id:
                        run_chunk_index(doc_id, page_payloads)
                else:
                    run_chunk_index(doc_id, page_payloads)

                db.add(
                    DocumentRecord(
                        doc_id=doc_id,
                        doc_date=date.today(),
                        doc_type="Imported",
                        keywords=list(summary.searchable_keywords or []),
                        summary_json=summary_payload,
                        file_location=remote_location,
                        uploaded_by=uploaded_by,
                    )
                )
                await db.commit()
                await bump_search_cache_version()

            source_reg.circuit.call(lambda: source.mark_processed(item_id))
            publish_event(
                "document.indexed",
                {
                    "doc_id": doc_id,
                    "doc_type": "Imported",
                    "source": item.get("source") or "document_source",
                    "uploaded_by": uploaded_by,
                },
            )
            imported += 1
        except Exception as exc:
            logger.exception("Failed importing %s", item_id)
            errors.append(f"{item_id}: {str(exc)[:120]}")
            skipped += 1

    return {"ok": True, "imported": imported, "skipped": skipped, "errors": errors[:10]}
