"""Records retention: per-type rules, legal hold, and four-eyes disposal that leaves a tombstone.

The periods are the bank's policy, not product defaults: with no rule a document
is kept permanently. A document becomes due when ``retention_until`` has passed;
it can only be disposed of when it is not on legal hold, is not part of a Board
meeting record, and a second person approves the disposal.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import write_audit
from ..models import (
    BoardBriefing,
    DocumentAccessRequest,
    DocumentDisposal,
    DocumentPage,
    DocumentRecord,
    DocumentVersion,
    MeetingDocument,
    RetentionRule,
)

logger = logging.getLogger("bcp_project.services.retention")

TRIGGERS = ("doc_date", "upload_date")
MAX_RETAIN_YEARS = 200
OPEN_DISPOSAL = ("proposed", "approved")


class RetentionError(ValueError):
    pass


def _key(doc_type: Optional[str]) -> str:
    return " ".join((doc_type or "").split()).casefold()


def add_years(start: date, years: int) -> date:
    try:
        return start.replace(year=start.year + years)
    except ValueError:  # 29 February into a non-leap year
        return start.replace(year=start.year + years, day=28)


def retention_until_for(doc: DocumentRecord, rule: Optional[RetentionRule]) -> Optional[date]:
    if rule is None or rule.retain_years is None:
        return None
    if rule.trigger == "upload_date":
        start = doc.created_at.date() if doc.created_at else None
    else:
        start = doc.doc_date
    return add_years(start, int(rule.retain_years)) if start else None


async def rules_by_type(db: AsyncSession) -> Dict[str, RetentionRule]:
    rows = (await db.execute(select(RetentionRule))).scalars().all()
    return {_key(r.doc_type): r for r in rows}


async def refresh_retention_dates(db: AsyncSession, *, batch: int = 1000) -> int:
    """Recompute ``retention_until`` for every document. Returns rows changed (not committed)."""
    rules = await rules_by_type(db)
    changed = 0
    last_id = 0
    while True:
        docs = (
            await db.execute(
                select(DocumentRecord).where(DocumentRecord.id > last_id).order_by(DocumentRecord.id).limit(batch)
            )
        ).scalars().all()
        if not docs:
            break
        for doc in docs:
            target = retention_until_for(doc, rules.get(_key(doc.doc_type)))
            if doc.retention_until != target:
                doc.retention_until = target
                changed += 1
        last_id = docs[-1].id
        await db.flush()
    return changed


async def set_rule(
    db: AsyncSession,
    *,
    doc_type: str,
    retain_years: Optional[int],
    trigger: str,
    authority: Optional[str],
    actor: str,
    ip_address: Optional[str] = None,
) -> RetentionRule:
    label = " ".join((doc_type or "").split())[:64]
    if not label:
        raise RetentionError("Document type is required.")
    if trigger not in TRIGGERS:
        raise RetentionError(f"Trigger must be one of: {', '.join(TRIGGERS)}.")
    if retain_years is not None and not 1 <= retain_years <= MAX_RETAIN_YEARS:
        raise RetentionError(f"Retention must be between 1 and {MAX_RETAIN_YEARS} years, or blank for permanent.")
    rule = (await rules_by_type(db)).get(_key(label))
    if rule is None:
        rule = RetentionRule(doc_type=label, updated_by=actor)
        db.add(rule)
    rule.retain_years = retain_years
    rule.trigger = trigger
    rule.authority = (authority or "").strip() or None
    rule.updated_by = actor
    rule.updated_at = datetime.utcnow()
    await db.flush()
    changed = await refresh_retention_dates(db)
    period = "permanent" if retain_years is None else f"{retain_years}y from {trigger}"
    await write_audit(
        db,
        username=actor,
        action="retention_rule_set",
        resource_type="retention_rule",
        resource_id=label,
        detail=f"{period}; authority={rule.authority or '-'}; documents_updated={changed}"[:500],
        ip_address=ip_address,
    )
    return rule


async def delete_rule(db: AsyncSession, *, rule_id: int, actor: str, ip_address: Optional[str] = None) -> None:
    rule = await db.get(RetentionRule, rule_id)
    if rule is None:
        raise RetentionError("Rule not found.")
    label = rule.doc_type
    await db.delete(rule)
    await db.flush()
    changed = await refresh_retention_dates(db)
    await write_audit(
        db,
        username=actor,
        action="retention_rule_delete",
        resource_type="retention_rule",
        resource_id=label,
        detail=f"now permanent; documents_updated={changed}",
        ip_address=ip_address,
    )


async def _document(db: AsyncSession, doc_id: str) -> DocumentRecord:
    doc = (await db.execute(select(DocumentRecord).where(DocumentRecord.doc_id == doc_id))).scalar_one_or_none()
    if doc is None:
        raise RetentionError(f"Document {doc_id} not found.")
    return doc


async def set_legal_hold(
    db: AsyncSession, *, doc_id: str, hold: bool, reason: str, actor: str, ip_address: Optional[str] = None
) -> DocumentRecord:
    reason = (reason or "").strip()
    if not reason:
        raise RetentionError("A reason (case or instruction reference) is required.")
    doc = await _document(db, doc_id)
    if hold == bool(doc.legal_hold):
        raise RetentionError("Document is already on legal hold." if hold else "Document is not on legal hold.")
    doc.legal_hold = hold
    doc.legal_hold_reason = reason if hold else None
    doc.legal_hold_by = actor if hold else None
    doc.legal_hold_at = datetime.utcnow() if hold else None
    await write_audit(
        db,
        username=actor,
        action="legal_hold_set" if hold else "legal_hold_release",
        resource_type="document",
        resource_id=doc_id,
        detail=reason[:500],
        ip_address=ip_address,
    )
    return doc


async def disposal_blockers(db: AsyncSession, doc: DocumentRecord, *, today: Optional[date] = None) -> List[str]:
    today = today or date.today()
    reasons: List[str] = []
    if doc.legal_hold:
        reasons.append(f"on legal hold ({doc.legal_hold_reason or 'no reason recorded'})")
    if doc.retention_until is None:
        reasons.append("no retention period applies (kept permanently)")
    elif doc.retention_until > today:
        reasons.append(f"retention runs until {doc.retention_until.isoformat()}")
    linked = doc.meeting_id is not None or await db.scalar(
        select(func.count()).select_from(MeetingDocument).where(MeetingDocument.archive_doc_id == doc.doc_id)
    )
    if linked:
        reasons.append("part of a Board meeting record")
    briefings = await db.scalar(select(func.count()).select_from(BoardBriefing).where(BoardBriefing.doc_id == doc.doc_id))
    if briefings:
        reasons.append("has a Board briefing")
    return reasons


async def due_for_disposal(db: AsyncSession, *, today: Optional[date] = None, limit: int = 200) -> List[Dict[str, Any]]:
    """Documents past retention, not on hold, with no open disposal request."""
    today = today or date.today()
    open_ids = select(DocumentDisposal.doc_id).where(DocumentDisposal.status.in_(OPEN_DISPOSAL))
    docs = (
        await db.execute(
            select(DocumentRecord)
            .where(
                DocumentRecord.retention_until.is_not(None),
                DocumentRecord.retention_until <= today,
                or_(DocumentRecord.legal_hold.is_(False), DocumentRecord.legal_hold.is_(None)),
                DocumentRecord.doc_id.not_in(open_ids),
            )
            .order_by(DocumentRecord.retention_until)
            .limit(limit)
        )
    ).scalars().all()
    rows = []
    for doc in docs:
        rows.append({**_doc_summary(doc), "blockers": await disposal_blockers(db, doc, today=today)})
    return rows


def _doc_summary(doc: DocumentRecord) -> Dict[str, Any]:
    return {
        "doc_id": doc.doc_id,
        "title": doc.title,
        "doc_type": doc.doc_type,
        "doc_date": doc.doc_date.isoformat() if doc.doc_date else None,
        "sensitivity": doc.sensitivity,
        "retention_until": doc.retention_until.isoformat() if doc.retention_until else None,
        "legal_hold": bool(doc.legal_hold),
        "legal_hold_reason": doc.legal_hold_reason,
        "legal_hold_by": doc.legal_hold_by,
        "legal_hold_at": doc.legal_hold_at.isoformat() if doc.legal_hold_at else None,
    }


async def propose_disposal(
    db: AsyncSession, *, doc_id: str, reason: str, actor: str, ip_address: Optional[str] = None
) -> DocumentDisposal:
    reason = (reason or "").strip()
    if not reason:
        raise RetentionError("A disposal reason (schedule reference) is required.")
    doc = await _document(db, doc_id)
    blockers = await disposal_blockers(db, doc)
    if blockers:
        raise RetentionError("Cannot propose disposal: " + "; ".join(blockers) + ".")
    existing = await db.scalar(
        select(func.count())
        .select_from(DocumentDisposal)
        .where(DocumentDisposal.doc_id == doc_id, DocumentDisposal.status.in_(OPEN_DISPOSAL))
    )
    if existing:
        raise RetentionError("A disposal request for this document is already open.")
    row = DocumentDisposal(
        doc_id=doc_id,
        reason=reason,
        proposed_by=actor,
        title=doc.title,
        doc_type=doc.doc_type,
        doc_date=doc.doc_date,
        sensitivity=doc.sensitivity,
        retention_until=doc.retention_until,
    )
    db.add(row)
    await db.flush()
    await write_audit(
        db,
        username=actor,
        action="disposal_propose",
        resource_type="document",
        resource_id=doc_id,
        detail=f"disposal #{row.id}: {reason}"[:500],
        ip_address=ip_address,
    )
    return row


async def reject_disposal(
    db: AsyncSession, *, disposal_id: int, note: str, actor: str, ip_address: Optional[str] = None
) -> DocumentDisposal:
    row = await _open_disposal(db, disposal_id)
    row.status = "rejected" if row.proposed_by != actor else "cancelled"
    row.reviewed_by = actor
    row.reviewed_at = datetime.utcnow()
    row.review_note = (note or "").strip() or None
    await write_audit(
        db,
        username=actor,
        action="disposal_reject" if row.status == "rejected" else "disposal_cancel",
        resource_type="document",
        resource_id=row.doc_id,
        detail=f"disposal #{row.id}: {row.review_note or '-'}"[:500],
        ip_address=ip_address,
    )
    return row


async def _open_disposal(db: AsyncSession, disposal_id: int) -> DocumentDisposal:
    row = await db.get(DocumentDisposal, disposal_id)
    if row is None:
        raise RetentionError("Disposal request not found.")
    if row.status not in OPEN_DISPOSAL:
        raise RetentionError(f"Disposal request is already {row.status}.")
    return row


async def approve_disposal(
    db: AsyncSession,
    *,
    disposal_id: int,
    actor: str,
    note: str = "",
    ip_address: Optional[str] = None,
    delete_vectors: Optional[Callable[[str], None]] = None,
    delete_object: Optional[Callable[[str], int]] = None,
) -> DocumentDisposal:
    """Second-person approval, then execution. A failed execution leaves the request
    ``approved`` with ``last_error`` so it can be retried once the cause (for example
    an unexpired Object Lock) is cleared."""
    row = await _open_disposal(db, disposal_id)
    if row.status == "proposed":
        if row.proposed_by == actor:
            raise RetentionError("Disposal needs a second person: the proposer cannot approve it.")
        doc = await _document(db, row.doc_id)
        blockers = await disposal_blockers(db, doc)
        if blockers:
            raise RetentionError("Cannot approve disposal: " + "; ".join(blockers) + ".")
        row.status = "approved"
        row.reviewed_by = actor
        row.reviewed_at = datetime.utcnow()
        row.review_note = (note or "").strip() or None
        await write_audit(
            db,
            username=actor,
            action="disposal_approve",
            resource_type="document",
            resource_id=row.doc_id,
            detail=f"disposal #{row.id} proposed by {row.proposed_by}",
            ip_address=ip_address,
        )
        await db.commit()
    elif row.reviewed_by is None or actor not in {row.reviewed_by, row.proposed_by}:
        raise RetentionError("Only the proposer or the approver can retry an approved disposal.")
    await execute_disposal(
        db, row, actor=actor, ip_address=ip_address, delete_vectors=delete_vectors, delete_object=delete_object
    )
    return row


def _default_delete_vectors(doc_id: str) -> None:
    from qdrant_client.http import models as rest

    from ..qdrant_store import SUMMARIES_COLLECTION, make_qdrant_indexer

    indexer = make_qdrant_indexer()
    indexer.delete_document_chunks(doc_id)
    selector = rest.FilterSelector(
        filter=rest.Filter(must=[rest.FieldCondition(key="doc_id", match=rest.MatchValue(value=doc_id))])
    )
    try:
        indexer.client.delete(collection_name=SUMMARIES_COLLECTION, points_selector=selector, wait=True)
    except Exception as exc:
        if "not found" not in str(exc).lower() and "404" not in str(exc):
            raise


def _default_delete_object(location: str) -> int:
    from ..intake import delete_stored_object

    return delete_stored_object(location)


def _locations(doc: DocumentRecord, versions: Iterable[DocumentVersion]) -> List[str]:
    seen: List[str] = []
    for loc in [doc.file_location, *[v.file_location for v in versions], *[v.original_location for v in versions]]:
        loc = (loc or "").strip()
        if loc and loc not in seen:
            seen.append(loc)
    return seen


async def execute_disposal(
    db: AsyncSession,
    row: DocumentDisposal,
    *,
    actor: str,
    ip_address: Optional[str] = None,
    delete_vectors: Optional[Callable[[str], None]] = None,
    delete_object: Optional[Callable[[str], int]] = None,
) -> DocumentDisposal:
    """Delete vectors, then stored originals, then database rows; keep a tombstone and audit it.

    Vectors go first so nothing stays findable; database rows go last so a failure
    part-way leaves the record (and its hashes) in place for a retry.
    """
    delete_vectors = delete_vectors or _default_delete_vectors
    delete_object = delete_object or _default_delete_object
    doc = await _document(db, row.doc_id)
    if doc.legal_hold:
        raise RetentionError("Document was placed on legal hold after approval; disposal stopped.")
    versions = (
        await db.execute(select(DocumentVersion).where(DocumentVersion.doc_id == doc.doc_id).order_by(DocumentVersion.version))
    ).scalars().all()
    locations = _locations(doc, versions)
    removed = 0
    try:
        await asyncio.to_thread(delete_vectors, doc.doc_id)
        for location in locations:
            removed += await asyncio.to_thread(delete_object, location)
    except Exception as exc:
        row.last_error = f"{type(exc).__name__}: {exc}"[:1000]
        await write_audit(
            db,
            username=actor,
            action="disposal_failed",
            resource_type="document",
            resource_id=doc.doc_id,
            detail=f"disposal #{row.id}: {row.last_error}"[:500],
            ip_address=ip_address,
        )
        await db.commit()
        logger.warning("Disposal #%s of %s failed: %s", row.id, doc.doc_id, exc)
        return row

    row.tombstone = {
        "doc_id": doc.doc_id,
        "title": doc.title,
        "doc_type": doc.doc_type,
        "doc_date": doc.doc_date.isoformat() if doc.doc_date else None,
        "uploaded_by": doc.uploaded_by,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "reference_number": doc.reference_number,
        "department": doc.department,
        "sensitivity": doc.sensitivity,
        "retention_until": doc.retention_until.isoformat() if doc.retention_until else None,
        "versions": [
            {"version": v.version, "sha256": v.sha256, "size_bytes": v.size_bytes, "created_by": v.created_by}
            for v in versions
        ],
        "objects_deleted": removed,
        "approved_by": row.reviewed_by,
        "proposed_by": row.proposed_by,
    }
    for model in (DocumentPage, DocumentVersion, DocumentAccessRequest):
        await db.execute(delete(model).where(model.doc_id == doc.doc_id))
    await db.delete(doc)
    row.status = "executed"
    row.executed_at = datetime.utcnow()
    row.last_error = None
    await write_audit(
        db,
        username=actor,
        action="document_disposed",
        resource_type="document",
        resource_id=row.doc_id,
        detail=(
            f"disposal #{row.id}; proposed_by={row.proposed_by}; approved_by={row.reviewed_by}; "
            f"versions={len(versions)}; objects_deleted={removed}"
        ),
        ip_address=ip_address,
    )
    await db.commit()
    try:
        from ..cache import bump_search_cache_version

        await bump_search_cache_version()
    except Exception as exc:
        logger.debug("Search cache bump after disposal skipped: %s", exc)
    return row


async def doc_id_conflict(db: AsyncSession, doc_id: str) -> Optional[str]:
    """Why ``doc_id`` cannot be used for a new upload, or None. Disposed IDs stay reserved
    so a tombstone never points at a different document."""
    if await db.scalar(select(DocumentRecord.id).where(DocumentRecord.doc_id == doc_id)) is not None:
        return "A document with this Document ID already exists"
    disposed = await db.scalar(
        select(func.count())
        .select_from(DocumentDisposal)
        .where(DocumentDisposal.doc_id == doc_id, DocumentDisposal.status == "executed")
    )
    if disposed:
        return "This Document ID belonged to a disposed record and cannot be reused"
    return None


async def retention_overview(db: AsyncSession) -> Dict[str, Any]:
    rules = (await db.execute(select(RetentionRule).order_by(RetentionRule.doc_type))).scalars().all()
    holds = (
        await db.execute(
            select(DocumentRecord).where(DocumentRecord.legal_hold.is_(True)).order_by(DocumentRecord.legal_hold_at.desc())
        )
    ).scalars().all()
    disposals = (
        await db.execute(select(DocumentDisposal).order_by(DocumentDisposal.proposed_at.desc()).limit(100))
    ).scalars().all()
    return {
        "rules": [
            {
                "id": r.id,
                "doc_type": r.doc_type,
                "retain_years": r.retain_years,
                "trigger": r.trigger,
                "authority": r.authority,
                "updated_by": r.updated_by,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rules
        ],
        "triggers": list(TRIGGERS),
        "holds": [_doc_summary(d) for d in holds],
        "due": await due_for_disposal(db),
        "disposals": [
            {
                "id": d.id,
                "doc_id": d.doc_id,
                "title": d.title,
                "doc_type": d.doc_type,
                "status": d.status,
                "reason": d.reason,
                "proposed_by": d.proposed_by,
                "proposed_at": d.proposed_at.isoformat() if d.proposed_at else None,
                "reviewed_by": d.reviewed_by,
                "reviewed_at": d.reviewed_at.isoformat() if d.reviewed_at else None,
                "review_note": d.review_note,
                "executed_at": d.executed_at.isoformat() if d.executed_at else None,
                "last_error": d.last_error,
            }
            for d in disposals
        ],
    }
