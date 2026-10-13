"""Shared read-side document operations for /api/v1 and MCP tools."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import (
    enrich_results_with_access,
    get_active_grants_for_docs,
    quality_search_results,
    write_audit,
)
from ..models import BoardMeeting, DocumentRecord, MeetingInvitation, Role, Sensitivity
from ..observability import Timer, incr
from ..policy import (
    Principal,
    Scope,
    assert_can_use_document_tools,
    can_principal_view_document,
    is_view_privileged_principal,
    require_scope,
)
from ..search_acl import SERVICE_ROLE, AclScope, SearchFilters, acl_scope_for_user
from .search import hybrid_search

logger = logging.getLogger("bcp_project.services.document_query")

def _public_result_row(row: dict) -> dict:
    """Strip internal scores; excerpts only for rows the caller may view."""
    can_view = bool(row.get("can_view"))
    return {
        "doc_id": row.get("doc_id"),
        "doc_type": row.get("doc_type"),
        "snippet": row.get("snippet") if can_view else None,
        "match_label": row.get("match_label") or row.get("relevance_label"),
        "relevance_label": row.get("relevance_label"),
        "can_view": can_view,
        "access_status": row.get("access_status"),
        "source": row.get("source") if can_view else None,
        "page": row.get("page") if can_view else None,
        "pages": row.get("pages") if can_view else None,
        "view_url": row.get("view_url") if can_view else None,
        "title": row.get("title") if can_view else None,
    }


async def _hide_if_restricted(db: AsyncSession, principal: Principal, document: DocumentRecord) -> None:
    """Restricted documents are 404 to anyone who cannot view them (no existence leak)."""
    if document.sensitivity != Sensitivity.restricted.value or is_view_privileged_principal(principal):
        return
    if principal.user is not None:
        grants = await get_active_grants_for_docs(db, principal.user.username, [document.doc_id])
        if can_principal_view_document(principal, grants.get(document.doc_id)):
            return
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")


async def acl_scope_for_principal(db: AsyncSession, principal: Principal) -> AclScope:
    if is_view_privileged_principal(principal):
        return AclScope(unrestricted=True)
    if principal.user is not None:
        return await acl_scope_for_user(db, principal.user)
    return AclScope(unrestricted=False, role=SERVICE_ROLE)


async def search_documents_for_principal(
    db: AsyncSession,
    principal: Principal,
    q: str,
    *,
    lang: Optional[str] = None,
    limit: int = 20,
    filters: Optional[SearchFilters] = None,
    ip_address: Optional[str] = None,
    tool_name: Optional[str] = None,
) -> Dict[str, Any]:
    require_scope(principal, Scope.docs_search)
    assert_can_use_document_tools(principal)
    q = (q or "").strip()
    lang_hint = (lang or "").strip().lower() or None
    if lang_hint not in (None, "en", "bn", "any"):
        lang_hint = None
    if not q:
        return {"results": [], "count": 0, "query": q, "mode": "hybrid"}

    with Timer("api_v1.search"):
        acl = await acl_scope_for_principal(db, principal)
        outcome = await hybrid_search(db, acl=acl, q=q, lang=lang_hint, filters=filters, limit=max(limit, 20))
        filtered = quality_search_results(outcome.results)
        if principal.user is not None:
            enriched = await enrich_results_with_access(db, principal.user, filtered)
        else:
            # Service principal without on-behalf-of: the vector ACL already limited what it can find.
            enriched = [
                {**row, "can_view": True, "can_download": False, "access_status": "privileged"}
                for row in filtered
            ]

        public = [_public_result_row(r) for r in enriched]
        await _audit_search(db, principal, q, len(public), ip_address, tool_name)
        incr("api_v1.search.ok")
        return {
            "results": public[:limit],
            "count": min(len(public), limit),
            "query": q,
            "mode": outcome.mode,
            "cached": outcome.cached,
            "reranked": outcome.reranked,
        }


async def _audit_search(
    db: AsyncSession,
    principal: Principal,
    q: str,
    count: int,
    ip_address: Optional[str],
    tool_name: Optional[str],
) -> None:
    await write_audit(
        db,
        username=principal.username_for_grants or principal.actor_id,
        action="api_search",
        resource_type="document",
        detail=f"q={q[:120]} count={count} actor={principal.actor_type}",
        ip_address=ip_address,
        actor_type=principal.actor_type,
        tool_name=tool_name,
        commit=True,
    )


async def get_document_metadata_for_principal(
    db: AsyncSession,
    principal: Principal,
    doc_id: str,
    *,
    ip_address: Optional[str] = None,
    tool_name: Optional[str] = None,
) -> Dict[str, Any]:
    require_scope(principal, Scope.docs_meta)
    assert_can_use_document_tools(principal)

    statement = select(DocumentRecord).where(DocumentRecord.doc_id == doc_id)
    result = await db.execute(statement)
    document = result.scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    await _hide_if_restricted(db, principal, document)

    grant = None
    if principal.user is not None and not is_view_privileged_principal(principal):
        grants = await get_active_grants_for_docs(db, principal.user.username, [doc_id])
        grant = grants.get(doc_id)

    can_view = can_principal_view_document(principal, grant)
    payload = {
        "doc_id": document.doc_id,
        "doc_type": document.doc_type,
        "doc_date": document.doc_date.isoformat() if document.doc_date else None,
        "created_at": document.created_at.isoformat() if document.created_at else None,
        "sensitivity": document.sensitivity,
        "can_view": can_view,
        "access_status": "privileged"
        if is_view_privileged_principal(principal)
        else ("approved_view" if can_view else "none"),
    }
    if can_view:
        payload["uploaded_by"] = document.uploaded_by
        payload["keywords"] = document.keywords or []
        summary = document.summary_json or {}
        payload["summary"] = {
            "core_info": summary.get("core_info") or {},
            "searchable_keywords": summary.get("searchable_keywords") or [],
            "major_projects": summary.get("major_projects") or [],
        }
        payload["ai_provenance"] = summary.get("_ai")
    else:
        payload["summary"] = None

    await write_audit(
        db,
        username=principal.username_for_grants or principal.actor_id,
        action="api_metadata",
        resource_type="document",
        resource_id=doc_id,
        detail=f"can_view={can_view}",
        ip_address=ip_address,
        actor_type=principal.actor_type,
        tool_name=tool_name,
        commit=True,
    )
    incr("api_v1.metadata.ok")
    return payload


async def get_document_status_for_principal(
    db: AsyncSession,
    principal: Principal,
    doc_id: str,
    *,
    ip_address: Optional[str] = None,
    tool_name: Optional[str] = None,
) -> Dict[str, Any]:
    require_scope(principal, Scope.docs_status)
    assert_can_use_document_tools(principal)

    statement = select(DocumentRecord).where(DocumentRecord.doc_id == doc_id)
    result = await db.execute(statement)
    document = result.scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    await _hide_if_restricted(db, principal, document)

    grant = None
    if principal.user is not None and not is_view_privileged_principal(principal):
        grants = await get_active_grants_for_docs(db, principal.user.username, [doc_id])
        grant = grants.get(doc_id)
    can_view = can_principal_view_document(principal, grant)

    indexed_summary = bool(document.summary_json)
    status_payload = {
        "doc_id": document.doc_id,
        "exists": True,
        "has_summary": indexed_summary,
        "has_file": bool(document.file_location),
        "can_view": can_view,
        "uploaded_at": document.created_at.isoformat() if document.created_at else None,
    }
    await write_audit(
        db,
        username=principal.username_for_grants or principal.actor_id,
        action="api_status",
        resource_type="document",
        resource_id=doc_id,
        ip_address=ip_address,
        actor_type=principal.actor_type,
        tool_name=tool_name,
        commit=True,
    )
    return status_payload


async def list_meetings_for_principal(
    db: AsyncSession,
    principal: Principal,
    *,
    limit: int = 20,
    ip_address: Optional[str] = None,
    tool_name: Optional[str] = None,
) -> Dict[str, Any]:
    require_scope(principal, Scope.meetings_list)
    if principal.user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Meetings list requires a user principal (JWT or X-On-Behalf-Of).",
        )
    username = principal.user.username
    if principal.user.role in (Role.admin, Role.board_secretary):
        stmt = (
            select(BoardMeeting)
            .order_by(BoardMeeting.scheduled_at.desc())
            .limit(limit)
        )
        rows = (await db.execute(stmt)).scalars().all()
    else:
        stmt = (
            select(BoardMeeting)
            .join(MeetingInvitation, MeetingInvitation.meeting_id == BoardMeeting.id)
            .where(MeetingInvitation.username == username)
            .order_by(BoardMeeting.scheduled_at.desc())
            .limit(limit)
        )
        rows = (await db.execute(stmt)).scalars().all()

    meetings = [
        {
            "id": m.id,
            "title": m.title,
            "scheduled_at": m.scheduled_at.isoformat() if m.scheduled_at else None,
            "location": m.location,
            "status": m.status.value if hasattr(m.status, "value") else str(m.status),
        }
        for m in rows
    ]
    await write_audit(
        db,
        username=username,
        action="api_meetings_list",
        resource_type="meeting",
        detail=f"count={len(meetings)}",
        ip_address=ip_address,
        actor_type=principal.actor_type,
        tool_name=tool_name,
        commit=True,
    )
    return {"meetings": meetings, "count": len(meetings)}
