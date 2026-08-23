"""Archive document access grants, privileged bypass, and audit helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    AccessMode,
    AccessRequestStatus,
    AuditLog,
    DocumentAccessRequest,
    Role,
    User,
)

# May view archive PDFs without a prior access request.
ARCHIVE_VIEW_PRIVILEGED_ROLES = frozenset({Role.admin, Role.board_secretary, Role.board_member})

# Only admins may download (always watermarked).
ARCHIVE_DOWNLOAD_ROLES = frozenset({Role.admin})

DEFAULT_GRANT_DAYS = 7


def is_archive_view_privileged(user: User) -> bool:
    return user.role in ARCHIVE_VIEW_PRIVILEGED_ROLES


def is_archive_download_allowed(user: User) -> bool:
    """Direct download is admin-only (watermarked)."""
    return user.role in ARCHIVE_DOWNLOAD_ROLES


# Back-compat aliases used elsewhere
def is_archive_privileged(user: User) -> bool:
    return is_archive_view_privileged(user)


def _grant_is_active(row: DocumentAccessRequest, now: Optional[datetime] = None) -> bool:
    now = now or datetime.utcnow()
    if row.status != AccessRequestStatus.approved:
        return False
    if row.expires_at is not None and row.expires_at <= now:
        return False
    return True


async def get_active_grants_for_docs(
    db: AsyncSession,
    username: str,
    doc_ids: Iterable[str],
) -> Dict[str, DocumentAccessRequest]:
    """Return the best active approved grant per doc_id for a user (download beats view)."""
    ids = [d for d in doc_ids if d]
    if not ids:
        return {}
    now = datetime.utcnow()
    statement = select(DocumentAccessRequest).where(
        DocumentAccessRequest.requester_username == username,
        DocumentAccessRequest.doc_id.in_(ids),
        DocumentAccessRequest.status == AccessRequestStatus.approved,
        or_(DocumentAccessRequest.expires_at.is_(None), DocumentAccessRequest.expires_at > now),
    )
    result = await db.execute(statement)
    rows = result.scalars().all()
    best: Dict[str, DocumentAccessRequest] = {}
    rank = {AccessMode.view_only: 1, AccessMode.download: 2}
    for row in rows:
        current = best.get(row.doc_id)
        if current is None or rank.get(row.requested_mode, 0) > rank.get(current.requested_mode, 0):
            best[row.doc_id] = row
    return best


async def get_pending_or_latest_request(
    db: AsyncSession,
    username: str,
    doc_id: str,
) -> Optional[DocumentAccessRequest]:
    statement = (
        select(DocumentAccessRequest)
        .where(
            DocumentAccessRequest.requester_username == username,
            DocumentAccessRequest.doc_id == doc_id,
        )
        .order_by(DocumentAccessRequest.created_at.desc())
        .limit(1)
    )
    result = await db.execute(statement)
    return result.scalar_one_or_none()


def can_view_with_grant(user: User, grant: Optional[DocumentAccessRequest]) -> bool:
    if is_archive_view_privileged(user):
        return True
    if grant is None:
        return False
    return _grant_is_active(grant)


def can_download_with_grant(user: User, grant: Optional[DocumentAccessRequest] = None) -> bool:
    """Only administrators may download archive PDFs (watermarked)."""
    return is_archive_download_allowed(user)


async def assert_can_view(user: User, doc_id: str, db: AsyncSession) -> DocumentAccessRequest | None:
    if is_archive_view_privileged(user):
        return None
    grants = await get_active_grants_for_docs(db, user.username, [doc_id])
    grant = grants.get(doc_id)
    if not can_view_with_grant(user, grant):
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Document access not granted.",
        )
    return grant


async def assert_can_download(user: User, doc_id: str, db: AsyncSession) -> DocumentAccessRequest | None:
    if not is_archive_download_allowed(user):
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can download archive PDFs.",
        )
    return None


async def enrich_results_with_access(
    db: AsyncSession,
    user: User,
    results: List[dict],
) -> List[dict]:
    doc_ids = [row.get("doc_id") for row in results if row.get("doc_id")]
    can_dl = is_archive_download_allowed(user)

    if is_archive_view_privileged(user):
        return [
            {
                **row,
                "can_view": True,
                "can_download": can_dl,
                "access_status": "privileged",
            }
            for row in results
        ]

    grants = await get_active_grants_for_docs(db, user.username, doc_ids)
    pending_ids: Set[str] = set()
    if doc_ids:
        pending_stmt = select(DocumentAccessRequest.doc_id).where(
            DocumentAccessRequest.requester_username == user.username,
            DocumentAccessRequest.doc_id.in_(doc_ids),
            DocumentAccessRequest.status == AccessRequestStatus.pending,
        )
        pending_result = await db.execute(pending_stmt)
        pending_ids = {row[0] for row in pending_result.all()}

    enriched = []
    for row in results:
        doc_id = row.get("doc_id")
        grant = grants.get(doc_id) if doc_id else None
        can_view = can_view_with_grant(user, grant)
        if can_view and can_dl:
            status_label = "approved_download"
        elif can_view:
            status_label = "approved_view"
        elif doc_id in pending_ids:
            status_label = "pending"
        else:
            status_label = "none"
        enriched.append(
            {
                **row,
                "can_view": can_view,
                "can_download": bool(can_view and can_dl),
                "access_status": status_label,
            }
        )
    return enriched


def default_expires_at(days: int = DEFAULT_GRANT_DAYS) -> datetime:
    return datetime.utcnow() + timedelta(days=days)


async def write_audit(
    db: AsyncSession,
    *,
    username: str,
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
    detail: Optional[str] = None,
    ip_address: Optional[str] = None,
    commit: bool = False,
) -> None:
    db.add(
        AuditLog(
            username=username,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail,
            ip_address=ip_address,
        )
    )
    if commit:
        await db.commit()


async def has_open_pending_request(db: AsyncSession, username: str, doc_id: str) -> bool:
    statement = select(DocumentAccessRequest.id).where(
        and_(
            DocumentAccessRequest.requester_username == username,
            DocumentAccessRequest.doc_id == doc_id,
            DocumentAccessRequest.status == AccessRequestStatus.pending,
        )
    )
    result = await db.execute(statement)
    return result.scalar_one_or_none() is not None


def relevance_label(score: Optional[float], source: Optional[str] = None) -> Optional[str]:
    """Only Strong/Good labels — never Possible/Weak."""
    if source == "keyword":
        if score is None or float(score) >= 0.45:
            return "Strong match"
        if float(score) >= 0.30:
            return "Good match"
        return None
    if score is None:
        return None
    value = float(score)
    if value >= 0.55:
        return "Strong match"
    if value >= 0.35:
        return "Good match"
    return None


def match_reason_label(source: Optional[str]) -> str:
    mapping = {
        "keyword": "Found in document keywords",
        "chunk": "Found in document text",
        "summary": "Found in document summary",
        "project": "Found in related projects",
    }
    return mapping.get(source or "", "Found in the archive")


def quality_search_results(rows: List[dict]) -> List[dict]:
    """Keep Strong/Good matches only. Never expose raw vector scores to the UI."""
    kept: List[dict] = []
    for row in rows:
        source = row.get("source") if isinstance(row.get("source"), str) else None
        score = row.get("score") if isinstance(row.get("score"), (int, float)) else None
        label = relevance_label(score, source)
        if not label:
            continue
        cleaned = {
            **row,
            "match_label": match_reason_label(source),
            "relevance_label": label,
        }
        cleaned.pop("score", None)
        if cleaned.get("snippet"):
            cleaned["snippet"] = str(cleaned["snippet"])[:180]
        kept.append(cleaned)
    return kept
