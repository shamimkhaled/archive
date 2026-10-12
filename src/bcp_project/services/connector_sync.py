"""Pull pending documents from DocumentSourcePort into the archive ingest path."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy import select

from ..db import get_session
from ..intake import IMAGE_TYPES, PDF, IntakeRejected, validate_upload
from ..jobs.outbox import notify_outbox
from ..models import DocumentRecord, parse_sensitivity
from ..ports.registry import get_registry
from .document_versions import intake_new_document
from .ingest import run_document_ingest

logger = logging.getLogger("bcp_project.services.connector_sync")

CONNECTOR_SENSITIVITY = parse_sensitivity(os.getenv("CONNECTOR_SENSITIVITY") or "internal")
ACCEPTED_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff")


def _suggest_doc_id(filename: str) -> str:
    stem = Path(filename).stem.replace(" ", "-")[:40]
    return f"INBOX-{date.today().isoformat()}-{stem}-{uuid.uuid4().hex[:6]}".upper()


async def pull_document_source(*, limit: int = 20, uploaded_by: str = "connector") -> Dict[str, Any]:
    """List + ingest pending inbox/SFTP files through the same intake as manual uploads."""
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
        if not any(s.lower().endswith(ACCEPTED_SUFFIXES) for s in (item_id, name)):
            skipped += 1
            continue
        try:
            data = source_reg.circuit.call(lambda: source.fetch_bytes(item_id))
            try:
                upload = validate_upload(data)
            except IntakeRejected as exc:
                errors.append(f"{item_id}: {exc}")
                skipped += 1
                continue
            if upload.content_type not in IMAGE_TYPES and upload.content_type != PDF:
                skipped += 1
                continue
            doc_id = _suggest_doc_id(name)
            async with get_session() as db:
                exists = await db.execute(select(DocumentRecord.id).where(DocumentRecord.doc_id == doc_id))
                if exists.scalar_one_or_none() is not None:
                    skipped += 1
                    continue
                result = await intake_new_document(
                    db,
                    upload=upload,
                    doc_id=doc_id,
                    doc_date=date.today(),
                    doc_type="Imported",
                    keywords=[],
                    sensitivity=CONNECTOR_SENSITIVITY,
                    uploaded_by=uploaded_by,
                    metadata={"title": Path(name).stem[:300]},
                )
                await db.commit()
            if result.job_id:
                notify_outbox()
            else:
                await run_document_ingest(doc_id, version=1)
            source_reg.circuit.call(lambda: source.mark_processed(item_id))
            imported += 1
        except Exception as exc:
            logger.exception("Failed importing %s", item_id)
            errors.append(f"{item_id}: {str(exc)[:120]}")
            skipped += 1

    return {"ok": True, "imported": imported, "skipped": skipped, "errors": errors[:10]}
