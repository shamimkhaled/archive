#!/usr/bin/env python3
"""Rebuild Qdrant page-text chunks from stored PDFs (OCR / extracted text)."""

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select

from bcp_project.db import get_session
from bcp_project.main_api import _load_stored_pdf_bytes, reindex_chunks_from_pdf_bytes
from bcp_project.models import DocumentRecord
from bcp_project.qdrant_store import make_qdrant_indexer, reset_qdrant_indexer


async def reindex(doc_id: Optional[str] = None) -> None:
    async with get_session() as db:
        statement = select(DocumentRecord.doc_id, DocumentRecord.file_location).order_by(
            DocumentRecord.created_at.desc()
        )
        if doc_id:
            statement = statement.where(DocumentRecord.doc_id == doc_id)
        rows = (await db.execute(statement)).all()

    if not rows:
        print("No documents found.")
        return

    indexer = make_qdrant_indexer()
    indexer.create_collections()

    ok = 0
    failed = 0
    for record_id, location in rows:
        last_exc: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                pdf_bytes = await _load_stored_pdf_bytes(location, resource_id=record_id)
                pages = await asyncio.to_thread(reindex_chunks_from_pdf_bytes, record_id, pdf_bytes)
                print(f"ok  {record_id}  pages={pages}")
                last_exc = None
                ok += 1
                break
            except Exception as exc:
                last_exc = exc
                print(f"retry  {record_id}  attempt {attempt}/3  {type(exc).__name__}: {exc}")
                reset_qdrant_indexer()
                await asyncio.sleep(1.5 * attempt)
        if last_exc is not None:
            print(f"fail  {record_id}  {type(last_exc).__name__}: {last_exc}")
            failed += 1
        time.sleep(0.4)
    print(f"Done. indexed={ok} failed={failed}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reindex document page-text chunks into Qdrant.")
    parser.add_argument("--doc-id", help="Only reindex this document ID")
    args = parser.parse_args()
    asyncio.run(reindex(args.doc_id))


if __name__ == "__main__":
    main()
