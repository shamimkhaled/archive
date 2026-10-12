"""Tamper-evident audit log.

Requests insert audit rows without coordination. A single sealer then links committed rows into
a hash chain: ``row_hash = sha256(prev_hash || canonical(row))``. Each row stores its predecessor's
hash, so the chain is checked by following links, not ids; a row committed late by a slow
transaction is simply sealed after the current tip. Postgres triggers (migration 0010) reject any
UPDATE except sealing and every DELETE or TRUNCATE, so removing or editing a row breaks the chain.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditLog, IntegrationCursor

logger = logging.getLogger("bcp_project.services.audit_chain")

GENESIS_HASH = "0" * 64
CHAIN_CURSOR = "audit_chain"
SEAL_LOCK_KEY = 0x53424141  # pg_advisory_xact_lock key shared by every sealer
CHAIN_FIELDS = (
    "id",
    "username",
    "action",
    "resource_type",
    "resource_id",
    "detail",
    "ip_address",
    "correlation_id",
    "actor_type",
    "tool_name",
    "created_at",
)


def audit_row_payload(row: AuditLog) -> Dict[str, Any]:
    """The audited fields of a row, as exported to files and the SIEM."""
    payload = {field: getattr(row, field, None) for field in CHAIN_FIELDS}
    created = payload["created_at"]
    payload["created_at"] = created.isoformat() if isinstance(created, datetime) else created
    return payload


def canonical(row: AuditLog) -> bytes:
    return json.dumps(audit_row_payload(row), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def row_digest(prev_hash: str, row: AuditLog) -> str:
    return hashlib.sha256(prev_hash.encode("ascii") + b"\n" + canonical(row)).hexdigest()


def seal_delay_seconds() -> float:
    return max(0.0, float(os.getenv("AUDIT_SEAL_DELAY_SECONDS") or 2))


async def _tip_cursor(db: AsyncSession) -> IntegrationCursor:
    """The chain tip lives in ``integration_cursors`` so sealing never scans the table.

    Rebuilt once from the data if missing: the tip is the one sealed hash no row points to.
    """
    cursor = await db.get(IntegrationCursor, CHAIN_CURSOR)
    if cursor is not None:
        return cursor
    hashes = select(AuditLog.prev_hash).where(AuditLog.prev_hash.is_not(None))
    tip = (
        await db.execute(
            select(AuditLog.row_hash)
            .where(AuditLog.row_hash.is_not(None), AuditLog.row_hash.not_in(hashes))
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    cursor = IntegrationCursor(name=CHAIN_CURSOR, last_id=0, detail=tip or GENESIS_HASH)
    db.add(cursor)
    return cursor


async def seal_pending(db: AsyncSession, *, limit: int = 1000, now: Optional[datetime] = None) -> int:
    """Seal unsealed rows older than the delay, in id order. Returns the number sealed."""
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SEAL_LOCK_KEY})
    cutoff = (now or datetime.utcnow()) - timedelta(seconds=seal_delay_seconds())
    rows = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.row_hash.is_(None), AuditLog.created_at <= cutoff)
            .order_by(AuditLog.id)
            .limit(limit)
        )
    ).scalars().all()
    if not rows:
        await db.rollback()
        return 0
    cursor = await _tip_cursor(db)
    prev = cursor.detail or GENESIS_HASH
    for row in rows:
        row.prev_hash = prev
        row.row_hash = row_digest(prev, row)
        prev = row.row_hash
    cursor.detail = prev
    cursor.last_id = max(int(cursor.last_id or 0), rows[-1].id)
    cursor.updated_at = datetime.utcnow()
    await db.commit()
    return len(rows)


async def verify_chain(db: AsyncSession, *, batch: int = 5000) -> Dict[str, Any]:
    """Recompute every sealed row and follow the links from the genesis hash.

    Fails on an edited row (hash mismatch), a fork (two rows with the same predecessor),
    or a missing row (a link that never connects to the chain).
    """
    tip, linked, sealed, last_id = GENESIS_HASH, 0, 0, 0
    waiting: Dict[str, AuditLog] = {}
    while True:
        rows = (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.row_hash.is_not(None), AuditLog.id > last_id)
                .order_by(AuditLog.id)
                .limit(batch)
            )
        ).scalars().all()
        if not rows:
            break
        for row in rows:
            sealed += 1
            last_id = row.id
            if row_digest(row.prev_hash or "", row) != row.row_hash:
                return {"ok": False, "sealed": sealed, "linked": linked, "broken_at": row.id, "reason": "row content changed"}
            if row.prev_hash in waiting:
                return {"ok": False, "sealed": sealed, "linked": linked, "broken_at": row.id, "reason": "fork in chain"}
            waiting[row.prev_hash] = row
            while tip in waiting:
                tip = waiting.pop(tip).row_hash
                linked += 1
        db.expunge_all()
    unsealed = (
        await db.execute(select(AuditLog.id).where(AuditLog.row_hash.is_(None)).limit(1))
    ).first() is not None
    if waiting:
        orphan = min(row.id for row in waiting.values())
        return {"ok": False, "sealed": sealed, "linked": linked, "broken_at": orphan, "reason": "missing predecessor (row removed)"}
    cursor = await db.get(IntegrationCursor, CHAIN_CURSOR)
    if cursor is not None and (cursor.detail or GENESIS_HASH) != tip:
        return {"ok": False, "sealed": sealed, "linked": linked, "broken_at": None, "reason": "newest sealed rows removed"}
    return {"ok": True, "sealed": sealed, "linked": linked, "tip": tip, "has_unsealed": unsealed}
