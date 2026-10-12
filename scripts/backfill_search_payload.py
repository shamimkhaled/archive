#!/usr/bin/env python3
"""Stamp ACL and filter fields onto vectors indexed before Phase 5.

Search filters every vector query on ``allowed_roles``; points without that field are
invisible to non-privileged users until this runs. Safe to re-run.

Example:
  PYTHONPATH=src python scripts/backfill_search_payload.py --dry-run
  PYTHONPATH=src python scripts/backfill_search_payload.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select

from bcp_project.config import load_environment
from bcp_project.db import get_session
from bcp_project.models import DocumentRecord
from bcp_project.search_acl import search_payload

load_environment()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="Print what would change without writing")
    parser.add_argument("--doc-id", action="append", default=[], help="Limit to these doc ids (repeatable)")
    args = parser.parse_args()

    async with get_session() as session:
        statement = select(DocumentRecord).order_by(DocumentRecord.doc_id)
        if args.doc_id:
            statement = statement.where(DocumentRecord.doc_id.in_(args.doc_id))
        documents = (await session.execute(statement)).scalars().all()
        payloads = [
            (doc.doc_id, {**search_payload(doc), "version": int(doc.current_version or 1)}) for doc in documents
        ]

    if args.dry_run:
        for doc_id, payload in payloads:
            print(f"{doc_id}: roles={payload['allowed_roles']} sensitivity={payload['sensitivity']}")
        print(f"{len(payloads)} document(s) would be updated")
        return

    from bcp_project.qdrant_store import make_qdrant_indexer

    qdrant = make_qdrant_indexer()
    await asyncio.to_thread(qdrant.create_collections)
    failed = 0
    for doc_id, payload in payloads:
        try:
            await asyncio.to_thread(qdrant.set_search_payload, doc_id, payload)
        except Exception as exc:
            failed += 1
            print(f"{doc_id}: FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"Updated {len(payloads) - failed} document(s); {failed} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
