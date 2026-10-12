#!/usr/bin/env python3
"""Record SHA-256 for document versions uploaded before hashes were kept.

Versions created before Phase 4 have no ``sha256``, so neither the viewer seal nor a restore
drill can prove their bytes are unchanged. This reads each stored original once, records its
hash where the row has none (existing hashes are never overwritten), and writes one
``version_hash_backfill`` audit row per version so the change sits in the hash-chained log.

The hash certifies the file as stored today, not as uploaded; the audit detail says so.

  PYTHONPATH=src python scripts/backfill_version_hashes.py            # dry run: report only
  PYTHONPATH=src python scripts/backfill_version_hashes.py --apply --operator "records officer"
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def run(apply: bool, operator: str, limit: int) -> int:
    from sqlalchemy import select

    from bcp_project.access_control import write_audit
    from bcp_project.db import AsyncSessionLocal
    from bcp_project.dr.sources import object_location, read_object, sha256_hex
    from bcp_project.models import DocumentRecord, DocumentVersion

    async with AsyncSessionLocal() as db:
        query = select(DocumentVersion).where(DocumentVersion.sha256.is_(None)).order_by(DocumentVersion.id)
        if limit:
            query = query.limit(limit)
        versions = (await db.execute(query)).scalars().all()
        print(f"{len(versions)} version(s) without a recorded SHA-256")
        failed = 0
        for v in versions:
            location = object_location(v.file_location, v.original_location)
            try:
                digest = sha256_hex(await asyncio.to_thread(read_object, location)) if location else None
            except Exception as exc:  # noqa: BLE001 - listed for follow-up
                print(f"  {v.doc_id} v{v.version}: cannot read {location}: {type(exc).__name__}")
                failed += 1
                continue
            if digest is None:
                print(f"  {v.doc_id} v{v.version}: no stored file")
                failed += 1
                continue
            print(f"  {v.doc_id} v{v.version}: {digest}")
            if not apply:
                continue
            v.sha256 = digest
            document = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == v.doc_id))).scalar_one_or_none()
            if document is not None and document.sha256 is None and document.current_version == v.version:
                document.sha256 = digest
            await write_audit(
                db,
                username=operator,
                action="version_hash_backfill",
                resource_type="document",
                resource_id=v.doc_id,
                detail=f"v{v.version} sha256={digest} basis=stored_object_at_backfill",
            )
        if apply:
            await db.commit()
            print(f"recorded {len(versions) - failed} hash(es)")
        else:
            print("dry run: nothing written (use --apply)")
        return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--operator", default=getpass.getuser())
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    from bcp_project.config import load_environment

    load_environment()
    return asyncio.run(run(args.apply, args.operator, args.limit))


if __name__ == "__main__":
    sys.exit(main())
