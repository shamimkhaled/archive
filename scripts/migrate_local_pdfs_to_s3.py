#!/usr/bin/env python3
"""Upload locally referenced document PDFs to S3 and rewrite DocumentRecord.file_location.

Use when Neon/Postgres is shared between local/Railway but uploads were done with
STORAGE_BACKEND=local (Railway then 404s on /view/.../file).

  cd Document-Archiver-main
  PYTHONPATH=src STORAGE_BACKEND=s3 python scripts/migrate_local_pdfs_to_s3.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ["STORAGE_BACKEND"] = "s3"

from bcp_project.config import load_environment

load_environment()
os.environ["STORAGE_BACKEND"] = "s3"

from sqlalchemy import select

from bcp_project.aws_utils import probe_s3, store_pdf, use_local_storage
from bcp_project.db import get_session
from bcp_project.models import DocumentRecord


async def main() -> None:
    upload_dir = Path(os.getenv("UPLOAD_DIR", "uploaded_pdfs"))
    if not upload_dir.is_absolute():
        upload_dir = ROOT / upload_dir

    print("storage local?", use_local_storage())
    print("s3 probe:", probe_s3())

    async with get_session() as session:
        docs = (await session.execute(select(DocumentRecord))).scalars().all()
        migrated = already = missing = 0
        for doc in docs:
            loc = (doc.file_location or "").strip()
            if loc.startswith("s3://"):
                already += 1
                print(f"already S3: {doc.doc_id}")
                continue
            local = Path(loc) if Path(loc).is_absolute() else upload_dir / loc
            if not local.exists():
                missing += 1
                print(f"missing: {doc.doc_id} -> {local}")
                continue
            s3_uri = store_pdf(str(local), local.name)
            doc.file_location = s3_uri
            migrated += 1
            print(f"migrated: {doc.doc_id} -> {s3_uri}")
        await session.commit()
        print(f"done migrated={migrated} already={already} missing={missing}")


if __name__ == "__main__":
    asyncio.run(main())
