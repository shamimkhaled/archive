"""Shared read-side document operations for /api/v1 and MCP tools."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import cast, or_, select, String
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import (
    enrich_results_with_access,
    get_active_grants_for_docs,
    quality_search_results,
    write_audit,
)
from ..cache import get_cached_search, set_cached_search
from ..models import BoardMeeting, DocumentRecord, MeetingInvitation, Role
from ..observability import Timer, incr
from ..policy import (
    Principal,
    Scope,
    assert_can_use_document_tools,
    can_principal_view_document,
    is_view_privileged_principal,
    require_scope,
)
from ..qdrant_store import embed_texts, make_qdrant_indexer

logger = logging.getLogger("bcp_project.services.document_query")

SEARCH_EMBED_TIMEOUT_SECONDS = 8.0
SEARCH_SUMMARY_TIMEOUT_SECONDS = 6.0
SEARCH_CHUNK_TIMEOUT_SECONDS = 6.0


async def _capped(label: str, coro, timeout: float):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except Exception as exc:
        logger.warning("%s skipped (%s: %s)", label, type(exc).__name__, exc)
        return None


def _public_result_row(row: dict) -> dict:
    """Strip internal scores; keep access-safe fields for API/MCP consumers."""
    return {
        "doc_id": row.get("doc_id"),
        "doc_type": row.get("doc_type"),
        "snippet": row.get("snippet"),
        "match_label": row.get("match_label") or row.get("relevance_label"),
        "relevance_label": row.get("relevance_label"),
        "can_view": bool(row.get("can_view")),
        "access_status": row.get("access_status"),
        "source": row.get("source"),
    }


async def search_documents_for_principal(
    db: AsyncSession,
    principal: Principal,
    q: str,
    *,
    lang: Optional[str] = None,
    limit: int = 20,
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
        cached = await get_cached_search(q, lang=lang_hint)
        if cached is not None and principal.user is not None:
            enriched = await enrich_results_with_access(db, principal.user, cached.get("results") or [])
            results = [_public_result_row(r) for r in enriched if r.get("can_view") or r.get("access_status")]
            # Non-privileged callers only see rows they can view (metadata of denied stays for request UX)
            if not is_view_privileged_principal(principal):
                pass  # keep access_status for request flow
            await _audit_search(db, principal, q, len(results), ip_address, tool_name)
            return {
                "results": results[:limit],
                "count": min(len(results), limit),
                "query": q,
                "mode": cached.get("mode") or "hybrid",
                "cached": True,
            }

        summary_rows: List[dict] = []
        chunk_best: Dict[str, dict] = {}
        try:
            qdrant = make_qdrant_indexer()
            query_vector = await asyncio.wait_for(
                asyncio.to_thread(embed_texts, [q]),
                timeout=SEARCH_EMBED_TIMEOUT_SECONDS,
            )
            query_vector = query_vector[0] if query_vector else None
        except Exception as exc:
            logger.warning("Search embedding unavailable (%s)", exc)
            query_vector = None
            qdrant = None

        if qdrant is not None and query_vector is not None:
            summary_result = await _capped(
                "Summary",
                asyncio.to_thread(lambda: qdrant.search_summary_hits(query_vector, q, limit=20)),
                SEARCH_SUMMARY_TIMEOUT_SECONDS,
            )
            chunk_result = await _capped(
                "Page-text",
                asyncio.to_thread(lambda: qdrant.search_chunk_hits(query_vector, limit=20)),
                SEARCH_CHUNK_TIMEOUT_SECONDS,
            )
            if isinstance(summary_result, list):
                summary_rows = summary_result
            if isinstance(chunk_result, dict):
                chunk_best = chunk_result

        results: List[dict] = []
        if qdrant is not None and (summary_rows or chunk_best):
            results = qdrant.fuse_hybrid_results(
                summary_rows,
                chunk_best,
                q,
                limit=20,
                lang=lang_hint,
                use_chunks=True,
            )

        needle = q.casefold()
        like = f"%{q}%"
        pg_filters = [
            DocumentRecord.doc_id.ilike(like),
            DocumentRecord.doc_type.ilike(like),
            cast(DocumentRecord.keywords, String).ilike(like),
        ]
        if len(needle) >= 3:
            pg_filters.append(cast(DocumentRecord.summary_json, String).ilike(like))
        id_rows = (
            await db.execute(
                select(
                    DocumentRecord.doc_id,
                    DocumentRecord.doc_type,
                    DocumentRecord.keywords,
                    DocumentRecord.summary_json,
                )
                .where(or_(*pg_filters))
                .order_by(DocumentRecord.created_at.desc())
                .limit(30)
            )
        ).all()
        seen = {r.get("doc_id") for r in results if r.get("doc_id")}
        for row in id_rows:
            if row.doc_id in seen:
                continue
            results.append(
                {
                    "doc_id": row.doc_id,
                    "doc_type": row.doc_type,
                    "keywords": row.keywords,
                    "score": 0.4,
                    "source": "keyword",
                    "snippet": None,
                }
            )
            seen.add(row.doc_id)

        filtered = quality_search_results(results)
        await set_cached_search(q, {"results": filtered, "mode": "hybrid"}, lang=lang_hint)

        if principal.user is not None:
            enriched = await enrich_results_with_access(db, principal.user, filtered)
        else:
            # Privileged service without on-behalf-of: treat as privileged view.
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
            "mode": "hybrid",
            "cached": False,
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

    grant = None
    if principal.user is not None and not is_view_privileged_principal(principal):
        grants = await get_active_grants_for_docs(db, principal.user.username, [doc_id])
        grant = grants.get(doc_id)

    can_view = can_principal_view_document(principal, grant)
    payload = {
        "doc_id": document.doc_id,
        "doc_type": document.doc_type,
        "doc_date": document.doc_date.isoformat() if document.doc_date else None,
        "uploaded_by": document.uploaded_by,
        "created_at": document.created_at.isoformat() if document.created_at else None,
        "keywords": document.keywords or [],
        "can_view": can_view,
        "access_status": "privileged"
        if is_view_privileged_principal(principal)
        else ("approved_view" if can_view else "none"),
    }
    if can_view:
        summary = document.summary_json or {}
        payload["summary"] = {
            "core_info": summary.get("core_info") or {},
            "searchable_keywords": summary.get("searchable_keywords") or [],
            "major_projects": summary.get("major_projects") or [],
        }
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
