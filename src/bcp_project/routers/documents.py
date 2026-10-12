"""Document upload, search, archive, view, and download routes."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import and_, cast, func, or_, select, String
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import (
    UNSEARCHABLE_VERIFICATION,
    assert_can_download,
    assert_can_view,
    enrich_results_with_access,
    hidden_restricted_doc_ids,
    is_archive_download_allowed,
    is_archive_view_privileged,
    viewable_doc_ids,
    write_audit,
)
from ..brand import BRAND
from ..cache import (
    get_cached_graph,
    get_cached_metadata_search,
    set_cached_graph,
    set_cached_metadata_search,
)
from ..pdf_delivery import is_initial_request, pdf_response, stamped_pdf
from ..rate_limit import rate_limit
from ..deps import (
    _client_ip,
    _load_document_or_404,
    _load_pdf_bytes,
    _parse_upload_keywords,
    _safe_back_url,
    get_current_user,
    get_db,
    require_role,
    templates,
)
from ..document_types import merge_document_types, normalize_document_type
from ..graph_builder import (
    GraphBuildResult,
    GraphDoc,
    build_document_graph,
    document_summary_card,
    related_documents_payload,
    summary_entities,
)
from ..models import DocumentRecord, Role, User, parse_sensitivity
from .ingest import dispatch_after_commit, ingest_mode, read_validated
from ..pdf_watermark import stamp_pdf_bytes
from ..qdrant_store import make_qdrant_indexer
from ..intake import IntakeRejected, validate_upload
from ..services.document_versions import intake_new_document
from ..search_acl import SearchFilters, acl_scope_for_user, norm
from ..services.ingest import ai_provenance_label, run_document_ingest
from ..services.retention import doc_id_conflict
from ..services.search import hybrid_search

logger = logging.getLogger("bcp_project.routers.documents")
router = APIRouter()

@router.get("/upload", response_class=HTMLResponse)
async def upload_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
) -> Any:
    require_role(current_user, [Role.admin, Role.uploader])
    today = datetime.utcnow().date()
    suggested_doc_id = await suggest_next_doc_id(db, today.year)
    document_types = await list_document_types(db)
    return templates.TemplateResponse(
        request,
        "upload.html",
        {
            "user": current_user,
            "status": status,
            "suggested_doc_id": suggested_doc_id,
            "default_doc_date": today.isoformat(),
            "document_types": document_types,
            "ingest_mode": ingest_mode(),
        },
    )


@router.get("/api/document-types")
async def api_document_types(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Suggested + previously used document types (dynamic catalog)."""
    require_role(current_user, [Role.admin, Role.uploader, Role.board_secretary, Role.board_member])
    types = await list_document_types(db)
    return {"types": types, "count": len(types)}


async def list_document_types(db: AsyncSession) -> List[str]:
    """Defaults plus distinct types already stored in the archive."""
    rows = (
        await db.execute(
            select(DocumentRecord.doc_type)
            .where(DocumentRecord.doc_type.is_not(None))
            .where(DocumentRecord.doc_type != "")
            .distinct()
            .order_by(DocumentRecord.doc_type.asc())
        )
    ).all()
    used = [row[0] for row in rows if row[0]]
    return merge_document_types(used)


@router.get("/api/documents/next-id")
async def api_next_document_id(
    year: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Suggest the next unique Document ID (SB-YYYY-NNN). Editable before upload."""
    require_role(current_user, [Role.admin, Role.uploader])
    resolved_year = year if year and 1990 <= year <= 2100 else datetime.utcnow().year
    doc_id = await suggest_next_doc_id(db, resolved_year)
    return {"doc_id": doc_id, "year": resolved_year}


async def suggest_next_doc_id(db: AsyncSession, year: int) -> str:
    """Next free SB-{year}-{NNN} ID based on existing archive documents."""
    prefix = BRAND.doc_id_prefix_for_year(year)
    result = await db.execute(
        select(DocumentRecord.doc_id).where(DocumentRecord.doc_id.like(f"{prefix}%"))
    )
    max_n = 0
    for (existing_id,) in result.all():
        suffix = (existing_id or "")[len(prefix) :]
        if suffix.isdigit():
            max_n = max(max_n, int(suffix))
    return f"{prefix}{max_n + 1:03d}"


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    return templates.TemplateResponse(
        request,
        "search.html",
        {"user": current_user},
    )


async def _audit_staff_search(
    db: AsyncSession,
    request: Request,
    user: User,
    *,
    action: str,
    query: str,
    results: List[dict],
) -> None:
    viewable = sum(1 for row in results if row.get("can_view"))
    await write_audit(
        db,
        username=user.username,
        action=action,
        resource_type="document",
        detail=f"q={query[:120]} count={len(results)} viewable={viewable}",
        ip_address=_client_ip(request),
        actor_type="user",
        commit=True,
    )


@router.get("/api/search", dependencies=[Depends(rate_limit("search"))])
async def search_documents(
    request: Request,
    q: str,
    lang: Optional[str] = None,
    department: Optional[str] = None,
    category: Optional[str] = None,
    doc_type: Optional[str] = None,
    reference_number: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Hybrid search (summary vectors + page chunks + keywords) under the caller's ACL and filters."""
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    q = q.strip()
    lang_hint = (lang or "").strip().lower() or None
    if lang_hint not in (None, "en", "bn", "any"):
        lang_hint = None
    try:
        filters = SearchFilters.from_params(
            department=department,
            category=category,
            doc_type=doc_type,
            reference_number=reference_number,
            date_from=date_from,
            date_to=date_to,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not q:
        return {"results": [], "count": 0, "query": q, "mode": "hybrid"}

    acl = await acl_scope_for_user(db, current_user)
    outcome = await hybrid_search(db, acl=acl, q=q, lang=lang_hint, filters=filters, limit=20)
    enriched = await enrich_results_with_access(db, current_user, outcome.results)
    await _audit_staff_search(db, request, current_user, action="search", query=q, results=enriched)
    return {
        "results": enriched,
        "count": len(enriched),
        "query": q,
        "cached": outcome.cached,
        "mode": outcome.mode,
        "lang": lang_hint or "any",
        "degraded": outcome.degraded,
        "reranked": outcome.reranked,
        "filters": {k: v for k, v in filters.as_dict().items() if v},
    }


@router.get("/api/search/facets")
async def search_facets(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Distinct confirmed metadata values for the search filter dropdowns."""
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])

    async def distinct(column) -> List[str]:
        rows = await db.execute(select(column).where(column.isnot(None), column != "").distinct().order_by(column).limit(200))
        return [r[0] for r in rows.all()]

    return {
        "departments": await distinct(DocumentRecord.department),
        "categories": await distinct(DocumentRecord.category),
        "doc_types": await list_document_types(db),
    }


@router.get("/api/search/metadata")
async def search_documents_by_metadata(
    request: Request,
    doc_id: Optional[str] = None,
    doc_type: Optional[str] = None,
    uploaded_by: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    department: Optional[str] = None,
    category: Optional[str] = None,
    reference_number: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])

    filters = {
        "doc_id": (doc_id or "").strip(),
        "doc_type": (doc_type or "").strip(),
        "uploaded_by": (uploaded_by or "").strip(),
        "date_from": date_from or "",
        "date_to": date_to or "",
        "department": norm(department),
        "category": norm(category),
        "reference_number": (reference_number or "").strip(),
    }

    audit_query = " ".join(f"{key}={value}" for key, value in filters.items() if value)
    cached = await get_cached_metadata_search(filters)
    if cached is not None:
        enriched = await enrich_results_with_access(db, current_user, cached.get("results") or [])
        await _audit_staff_search(
            db, request, current_user, action="search_metadata", query=audit_query, results=enriched
        )
        return {**cached, "results": enriched, "count": len(enriched), "cached": True}

    conditions = [
        or_(
            DocumentRecord.verification_status.is_(None),
            DocumentRecord.verification_status.notin_(UNSEARCHABLE_VERIFICATION),
        ),
        *SearchFilters(
            department=filters["department"],
            category=filters["category"],
            reference_number=filters["reference_number"],
        ).sql_conditions(),
    ]
    if filters["doc_id"]:
        conditions.append(DocumentRecord.doc_id.ilike(f"%{filters['doc_id']}%"))
    if filters["doc_type"]:
        conditions.append(DocumentRecord.doc_type.ilike(f"%{filters['doc_type']}%"))
    if filters["uploaded_by"]:
        conditions.append(DocumentRecord.uploaded_by.ilike(f"%{filters['uploaded_by']}%"))
    if filters["date_from"]:
        try:
            conditions.append(DocumentRecord.doc_date >= datetime.fromisoformat(filters["date_from"]).date())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_from, expected YYYY-MM-DD")
    if filters["date_to"]:
        try:
            conditions.append(DocumentRecord.doc_date <= datetime.fromisoformat(filters["date_to"]).date())
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_to, expected YYYY-MM-DD")

    statement = select(DocumentRecord).order_by(DocumentRecord.doc_date.desc()).limit(50)
    if conditions:
        statement = statement.where(and_(*conditions))

    result = await db.execute(statement)
    documents = result.scalars().all()

    base_results = [
        {
            "doc_id": doc.doc_id,
            "doc_type": doc.doc_type,
            "doc_date": doc.doc_date.isoformat(),
            "uploaded_by": doc.uploaded_by,
            "searchable_keywords": doc.keywords,
            "title": doc.title,
            "department": doc.department,
            "category": doc.category,
            "reference_number": doc.reference_number,
        }
        for doc in documents
    ]
    cache_payload = {"results": base_results, "count": len(base_results)}
    await set_cached_metadata_search(filters, cache_payload)

    enriched = await enrich_results_with_access(db, current_user, base_results)
    await _audit_staff_search(
        db, request, current_user, action="search_metadata", query=audit_query, results=enriched
    )
    return {"results": enriched, "count": len(enriched), "cached": False}


async def _load_graph_documents(
    db: AsyncSession,
    *,
    limit: int = 80,
    doc_ids: Optional[List[str]] = None,
    max_limit: int = 500,
) -> List[GraphDoc]:
    # Column projection keeps payload smaller than full ORM entities.
    statement = (
        select(
            DocumentRecord.doc_id,
            DocumentRecord.doc_type,
            DocumentRecord.doc_date,
            DocumentRecord.summary_json,
        )
        .order_by(DocumentRecord.created_at.desc())
    )
    if doc_ids:
        statement = statement.where(DocumentRecord.doc_id.in_(doc_ids))
    else:
        statement = statement.limit(max(1, min(limit, max_limit)))
    result = await db.execute(statement)
    rows = result.all()
    return [
        GraphDoc(
            doc_id=row.doc_id,
            doc_type=row.doc_type,
            doc_date=row.doc_date.isoformat(),
            summary_json=row.summary_json,
        )
        for row in rows
    ]


async def _find_entity_related_doc_ids(
    db: AsyncSession,
    center: GraphDoc,
    *,
    exclude: Optional[Set[str]] = None,
    limit: int = 40,
) -> List[str]:
    """Find related docs by shared keywords / summary entities.

    Avoids full-table ILIKE on JSON (unusable at 10k+ rows). Uses:
    1) JSON array contains on keywords (cheap with an index)
    2) A bounded recent-window scan for project/person/org overlap
    """
    entities = summary_entities(center.summary_json)
    terms: List[str] = []
    for key in ("keyword", "project", "person", "organization"):
        for value in list(entities[key])[:6]:
            if value and value not in terms:
                terms.append(value)
    if not terms:
        return []

    skip = set(exclude or set())
    skip.add(center.doc_id)
    matched: List[str] = []

    contains_filters = [DocumentRecord.keywords.contains([term]) for term in terms[:6]]
    if contains_filters:
        try:
            statement = (
                select(DocumentRecord.doc_id)
                .where(or_(*contains_filters), DocumentRecord.doc_id != center.doc_id)
                .order_by(DocumentRecord.created_at.desc())
                .limit(max(1, min(limit, 80)))
            )
            result = await db.execute(statement)
            for row in result.all():
                doc_id = row[0]
                if doc_id not in skip:
                    matched.append(doc_id)
            if matched:
                return matched[:limit]
        except Exception as exc:
            logger.debug("Keyword contains lookup skipped: %s", exc)
            await db.rollback()

    window = (
        select(DocumentRecord.doc_id, DocumentRecord.summary_json)
        .where(DocumentRecord.doc_id != center.doc_id)
        .order_by(DocumentRecord.created_at.desc())
        .limit(150)
    )
    rows = (await db.execute(window)).all()
    already = set(matched) | skip
    for doc_id, summary in rows:
        if doc_id in already:
            continue
        other = summary_entities(summary)
        for key in ("project", "person", "organization", "keyword"):
            if entities[key] & other[key]:
                matched.append(doc_id)
                already.add(doc_id)
                break
        if len(matched) >= limit:
            break
    return matched[:limit]


GRAPH_SEMANTIC_TIMEOUT_SECONDS = 3.0
GRAPH_REQUEST_BUDGET_SECONDS = 6.0


async def _semantic_neighbors_safe(
    doc_id: str,
    *,
    limit: int,
    min_score: float,
) -> List[dict]:
    """Qdrant similarity with a hard deadline. Never raises — empty on timeout/outage."""
    try:
        qdrant = make_qdrant_indexer()
    except Exception as exc:
        logger.warning("Qdrant indexer unavailable for graph: %s", exc)
        return []
    try:
        hits = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: qdrant.find_similar_documents(
                    doc_id,
                    limit=limit,
                    min_score=min_score,
                    allow_fallback=False,
                )
            ),
            timeout=GRAPH_SEMANTIC_TIMEOUT_SECONDS,
        )
        return hits or []
    except asyncio.TimeoutError:
        logger.warning("Qdrant similarity timed out after %.1fs for %s", GRAPH_SEMANTIC_TIMEOUT_SECONDS, doc_id)
        return []
    except Exception as exc:
        logger.warning("Qdrant similarity failed for %s: %s", doc_id, exc)
        return []


async def _filter_graph_for_user(db: AsyncSession, user: User, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Drop nodes (and their edges) the user may not view; graph labels come from summaries."""
    if is_archive_view_privileged(user):
        return payload
    node_ids = [node.get("id") for node in payload.get("nodes") or [] if node.get("id")]
    allowed = await viewable_doc_ids(db, user, node_ids)
    nodes = [node for node in payload.get("nodes") or [] if node.get("id") in allowed]
    edges = [
        edge
        for edge in payload.get("edges") or []
        if edge.get("source") in allowed and edge.get("target") in allowed
    ]
    return {**payload, "nodes": nodes, "edges": edges, "count": len(nodes), "edge_count": len(edges)}


def _graph_response(
    graph: Any,
    *,
    center_doc_id: Optional[str] = None,
    relation_guide: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    return {
        "nodes": graph.nodes,
        "edges": graph.edges,
        "count": len(graph.nodes),
        "edge_count": len(graph.edges),
        "center": center_doc_id,
        "degraded": False,
        "relation_guide": relation_guide
        or {
            "semantic": "Similar meaning from document summaries (AI embeddings)",
            "project": "Same major project named in the summary",
            "person": "Same key person named in the summary",
            "organization": "Same organization in core info",
            "keyword": "Shared searchable keywords from the summary",
        },
    }


@router.get("/api/archive/documents")
async def archive_documents(
    q: Optional[str] = None,
    doc_type: Optional[str] = None,
    year: Optional[int] = None,
    limit: int = 40,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Search-first document picker — paginated for large archives (10k+)."""
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    limit = max(10, min(limit, 100))
    offset = max(0, offset)
    needle = (q or "").strip()
    type_filter = (doc_type or "").strip()
    year_filter = year if year and 1990 <= year <= 2100 else None

    filters = []
    if needle:
        like = f"%{needle}%"
        filters.append(
            or_(
                DocumentRecord.doc_id.ilike(like),
                DocumentRecord.doc_type.ilike(like),
                cast(DocumentRecord.keywords, String).ilike(like),
            )
        )
    if type_filter:
        filters.append(DocumentRecord.doc_type == type_filter)
    if year_filter:
        year_start = date(year_filter, 1, 1)
        year_end = date(year_filter + 1, 1, 1)
        filters.append(
            and_(DocumentRecord.doc_date >= year_start, DocumentRecord.doc_date < year_end)
        )

    count_stmt = select(func.count()).select_from(DocumentRecord)
    if filters:
        count_stmt = count_stmt.where(and_(*filters))
    total = int(await db.scalar(count_stmt) or 0)

    statement = (
        select(
            DocumentRecord.doc_id,
            DocumentRecord.doc_type,
            DocumentRecord.doc_date,
            DocumentRecord.summary_json,
        )
        .order_by(DocumentRecord.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if filters:
        statement = statement.where(and_(*filters))
    rows = (await db.execute(statement)).all()
    docs = [
        GraphDoc(
            doc_id=row.doc_id,
            doc_type=row.doc_type,
            doc_date=row.doc_date.isoformat(),
            summary_json=row.summary_json,
        )
        for row in rows
    ]
    page_ids = [doc.doc_id for doc in docs]
    allowed = await viewable_doc_ids(db, current_user, page_ids)
    hidden = await hidden_restricted_doc_ids(db, current_user, page_ids)
    items = []
    for doc in docs:
        if doc.doc_id in hidden:
            continue
        if doc.doc_id in allowed:
            items.append({**document_summary_card(doc), "can_view": True})
        else:
            items.append(
                {
                    "doc_id": doc.doc_id,
                    "title": doc.doc_type,
                    "doc_type": doc.doc_type,
                    "doc_date": doc.doc_date,
                    "organization": "",
                    "can_view": False,
                }
            )

    cached_types = await get_cached_metadata_search({"facet": "archive_doc_types"})
    if cached_types and isinstance(cached_types.get("types"), list):
        types = cached_types["types"]
    else:
        type_rows = (
            await db.execute(
                select(DocumentRecord.doc_type, func.count())
                .group_by(DocumentRecord.doc_type)
                .order_by(func.count().desc())
            )
        ).all()
        types = [{"doc_type": row[0], "count": int(row[1])} for row in type_rows]
        await set_cached_metadata_search({"facet": "archive_doc_types"}, {"types": types})

    return {
        "documents": items,
        "count": len(items),
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(items) < total,
        "types": types,
    }


@router.get("/archive/map", response_class=HTMLResponse)
async def archive_map_page(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    return templates.TemplateResponse(
        request,
        "archive_map.html",
        {"user": current_user},
    )


@router.get("/api/archive/graph")
async def archive_graph(
    limit: int = 16,
    min_similarity: float = 0.68,
    focus: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Focus-centric mind map. Never 502s: Qdrant timeouts degrade to keyword links."""
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    max_related = max(8, min(limit, 40))
    min_similarity = max(0.5, min(min_similarity, 0.99))
    focus = (focus or "").strip() or None
    if not focus:
        return _graph_response(GraphBuildResult(), center_doc_id=None)

    try:
        cached = await asyncio.wait_for(
            get_cached_graph(focus, max_related, min_similarity),
            timeout=0.5,
        )
    except Exception:
        cached = None
    document = await _load_document_or_404(focus, db)
    await assert_can_view(current_user, focus, db)
    if cached:
        cached["cached"] = True
        return await _filter_graph_for_user(db, current_user, cached)

    center = GraphDoc(
        doc_id=document.doc_id,
        doc_type=document.doc_type,
        doc_date=document.doc_date.isoformat(),
        summary_json=document.summary_json,
    )

    async def assemble() -> Dict[str, Any]:
        try:
            semantic_hits, entity_ids = await asyncio.gather(
                _semantic_neighbors_safe(focus, limit=max_related, min_score=min_similarity),
                _find_entity_related_doc_ids(db, center, exclude={focus}, limit=max_related),
            )
        except Exception as exc:
            logger.warning("Graph neighbor lookup failed for %s: %s", focus, exc)
            try:
                await db.rollback()
            except Exception:
                pass
            semantic_hits, entity_ids = [], []

        related_ids: Set[str] = set()
        for row in semantic_hits or []:
            other = row.get("doc_id")
            if other and other != focus:
                related_ids.add(str(other))
        related_ids.update(entity_ids or [])

        capped_ids = list(related_ids)[: max_related + 8]
        neighbors = await _load_graph_documents(db, doc_ids=capped_ids) if capped_ids else []
        docs = [center] + [d for d in neighbors if d.doc_id != focus]
        graph = build_document_graph(
            docs,
            None,
            center_doc_id=focus,
            semantic_neighbors=max_related,
            min_similarity=min_similarity,
            include_semantic=True,
            include_entities=True,
            semantic_hits=semantic_hits,
            use_provided_cluster=True,
        )
        payload = _graph_response(graph, center_doc_id=focus)
        payload["degraded"] = not bool(semantic_hits)
        return payload

    try:
        payload = await asyncio.wait_for(assemble(), timeout=GRAPH_REQUEST_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("Graph assemble exceeded %.1fs for %s", GRAPH_REQUEST_BUDGET_SECONDS, focus)
        try:
            await db.rollback()
        except Exception:
            pass
        graph = build_document_graph(
            [center],
            None,
            center_doc_id=focus,
            semantic_neighbors=max_related,
            min_similarity=min_similarity,
            include_semantic=False,
            include_entities=True,
            semantic_hits=[],
            use_provided_cluster=True,
        )
        payload = _graph_response(graph, center_doc_id=focus)
        payload["degraded"] = True
        return payload
    except Exception as exc:
        logger.warning("Graph assemble failed for %s: %s", focus, exc)
        try:
            await db.rollback()
        except Exception:
            pass
        graph = build_document_graph(
            [center],
            None,
            center_doc_id=focus,
            semantic_neighbors=max_related,
            min_similarity=min_similarity,
            include_semantic=False,
            include_entities=True,
            semantic_hits=[],
            use_provided_cluster=True,
        )
        payload = _graph_response(graph, center_doc_id=focus)
        payload["degraded"] = True
        return payload

    try:
        await asyncio.wait_for(
            set_cached_graph(focus, max_related, min_similarity, payload),
            timeout=0.5,
        )
    except Exception:
        pass
    return await _filter_graph_for_user(db, current_user, payload)


@router.get("/api/documents/{doc_id}/related")
async def document_related(
    doc_id: str,
    limit: int = 12,
    min_similarity: float = 0.68,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    document = await _load_document_or_404(doc_id, db)
    await assert_can_view(current_user, doc_id, db)

    limit = max(3, min(limit, 20))
    min_similarity = max(0.5, min(min_similarity, 0.99))
    center = GraphDoc(
        doc_id=document.doc_id,
        doc_type=document.doc_type,
        doc_date=document.doc_date.isoformat(),
        summary_json=document.summary_json,
    )

    related_ids: Set[str] = set()
    semantic_hits = await _semantic_neighbors_safe(doc_id, limit=limit + 4, min_score=min_similarity)
    for row in semantic_hits:
        other = row.get("doc_id")
        if other and other != doc_id:
            related_ids.add(str(other))

    entity_ids = await _find_entity_related_doc_ids(
        db, center, exclude={doc_id}, limit=limit + 8
    )
    related_ids.update(entity_ids)
    capped_ids = list(related_ids)[: limit + 8]
    allowed = await viewable_doc_ids(db, current_user, capped_ids)
    capped_ids = [other for other in capped_ids if other in allowed]
    semantic_hits = [row for row in semantic_hits if str(row.get("doc_id")) in allowed]
    neighbors = await _load_graph_documents(db, doc_ids=capped_ids) if capped_ids else []
    docs = [center] + [d for d in neighbors if d.doc_id != doc_id]

    graph = build_document_graph(
        docs,
        None,
        center_doc_id=doc_id,
        semantic_neighbors=limit,
        min_similarity=min_similarity,
        include_semantic=True,
        include_entities=True,
        semantic_hits=semantic_hits,
        use_provided_cluster=True,
    )
    return related_documents_payload(center, graph, limit=limit)


@router.get("/view/{doc_id}", response_class=HTMLResponse)
async def view_document(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    document = await _load_document_or_404(doc_id, db)

    try:
        await assert_can_view(current_user, doc_id, db)
    except HTTPException:
        return templates.TemplateResponse(
            request,
            "access_denied.html",
            {
                "user": current_user,
                "doc_id": doc_id,
                "doc_type": document.doc_type,
                "page_title": "Access required",
            },
            status_code=403,
        )

    can_download = is_archive_download_allowed(current_user)

    seal_text = BRAND.seal(current_user.username)
    back_url = _safe_back_url(request, fallback="/search" if current_user.role == Role.board_member else "/")
    back_label = "Back"
    if back_url.startswith("/search"):
        back_label = "Back to search"
    elif back_url.startswith("/board"):
        back_label = "Back to meetings"
    elif back_url == "/":
        back_label = "Back to home"

    await write_audit(
        db,
        username=current_user.username,
        action="view",
        resource_type="document",
        resource_id=doc_id,
        detail="viewer_page",
        ip_address=_client_ip(request),
        commit=True,
    )

    return templates.TemplateResponse(
        request,
        "viewer.html",
        {
            "user": current_user,
            "page_title": "Document Viewer",
            "page_subtitle": " · ".join(
                part
                for part in (f"Viewing {doc_id}", document.doc_type, ai_provenance_label(document.summary_json))
                if part
            ),
            "file_url": f"/view/{doc_id}/file",
            "download_url": f"/download/{doc_id}" if can_download else None,
            "versions_url": f"/documents/{doc_id}/versions"
            if current_user.role in {Role.admin, Role.board_secretary}
            else None,
            "current_version": document.current_version,
            "back_url": back_url,
            "back_label": back_label,
            "seal_text": seal_text,
            "hide_app_chrome": True,
            "can_download": can_download,
            "archive_doc_id": doc_id,
        },
    )


@router.get("/view/{doc_id}/file")
async def view_document_file(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    document = await _load_document_or_404(doc_id, db)
    await assert_can_view(current_user, doc_id, db)

    key = ("view", doc_id, document.file_location, document.sha256, current_user.username)
    try:
        stamped = await stamped_pdf(key, lambda: _load_pdf_bytes(document), BRAND.seal_stream(current_user.username))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Watermark stamping failed for %s", doc_id)
        raise HTTPException(status_code=502, detail="Could not prepare secure document stream") from exc

    if is_initial_request(request):
        await write_audit(
            db,
            username=current_user.username,
            action="view_file",
            resource_type="document",
            resource_id=doc_id,
            detail="watermarked_stream",
            ip_address=_client_ip(request),
            commit=True,
        )
    return pdf_response(request, stamped, filename=f"{doc_id}-view.pdf")


@router.get("/download/{doc_id}")
async def download_document(
    request: Request,
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    require_role(current_user, [Role.admin])
    document = await _load_document_or_404(doc_id, db)
    await assert_can_download(current_user, doc_id, db)

    pdf_bytes = await _load_pdf_bytes(document)
    seal_text = BRAND.seal_download(current_user.username)
    try:
        stamped = await asyncio.to_thread(stamp_pdf_bytes, pdf_bytes, seal_text)
    except Exception as exc:
        logger.exception("Watermark stamping failed for download %s", doc_id)
        raise HTTPException(status_code=502, detail="Could not prepare watermarked download") from exc

    await write_audit(
        db,
        username=current_user.username,
        action="download",
        resource_type="document",
        resource_id=doc_id,
        detail="admin_watermarked_download",
        ip_address=_client_ip(request),
        commit=True,
    )
    return pdf_response(request, stamped, filename=f"{doc_id}-watermarked.pdf", disposition="attachment")

@router.post("/api/upload/stream", dependencies=[Depends(rate_limit("upload"))])
async def upload_document_stream(
    request: Request,
    background_tasks: BackgroundTasks,
    doc_date: str = Form(...),
    doc_id: str = Form(...),
    doc_type: str = Form(...),
    keywords: str = Form(""),
    sensitivity: str = Form("internal"),
    title: str = Form(""),
    reference_number: str = Form(""),
    department: str = Form(""),
    subject: str = Form(""),
    category: str = Form(""),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """NDJSON progress stream for the in-request pipeline (used when no job worker runs)."""
    require_role(current_user, [Role.admin, Role.uploader])
    sensitivity = parse_sensitivity(sensitivity)
    doc_type = normalize_document_type(doc_type)
    doc_id = (doc_id or "").strip()

    def _event(pct: int, step: str, title: str, hint: str = "", **extra: Any) -> str:
        payload = {"pct": pct, "step": step, "title": title, "hint": hint, **extra}
        return json.dumps(payload, ensure_ascii=False) + "\n"

    async def generate():
        if not doc_type or not doc_id:
            yield _event(0, "upload", "Upload failed", error="Document ID and type are required")
            return
        yield _event(5, "upload", "Starting upload…", "Checking the file and saving it.")
        try:
            parsed_date = datetime.fromisoformat(doc_date).date()
        except ValueError:
            yield _event(0, "upload", "Upload failed", error="Invalid document date")
            return
        conflict = await doc_id_conflict(db, doc_id)
        if conflict:
            yield _event(0, "upload", "Upload failed", error=conflict)
            return
        yield _event(12, "upload", "Receiving file…", "Transferring file to the server.")
        try:
            upload = validate_upload(await file.read())
        except IntakeRejected as exc:
            yield _event(0, "upload", "Upload failed", error=str(exc))
            return
        try:
            result = await intake_new_document(
                db,
                upload=upload,
                doc_id=doc_id,
                doc_date=parsed_date,
                doc_type=doc_type,
                keywords=_parse_upload_keywords(keywords, []),
                sensitivity=sensitivity,
                uploaded_by=current_user.username,
                metadata={
                    "title": title.strip(),
                    "reference_number": reference_number.strip(),
                    "department": department.strip(),
                    "subject": subject.strip(),
                    "category": category.strip(),
                },
                stage=False,
            )
            await write_audit(
                db,
                username=current_user.username,
                action="upload",
                resource_type="document",
                resource_id=doc_id,
                detail=f"{doc_type} sensitivity={sensitivity} type={upload.content_type} sha256={upload.sha256}",
                ip_address=_client_ip(request),
                commit=False,
            )
            await db.commit()
        except Exception as exc:
            logger.exception("Storing upload failed for %s", doc_id)
            yield _event(0, "upload", "Upload failed", error=str(exc)[:240] or "Could not store the file")
            return
        yield _event(22, "upload", "File saved", f"Original stored write-once (SHA-256 {upload.sha256[:12]}…).")

        task = asyncio.create_task(run_document_ingest(doc_id, version=result.version.version))
        shown = None
        while not task.done():
            await asyncio.sleep(0.7)
            current = await _ingest_state(doc_id)
            if current and current != shown and current in _STREAM_STEPS:
                shown = current
                yield _event(*_STREAM_STEPS[current])
        try:
            outcome = task.result()
        except Exception as exc:
            logger.exception("Streaming ingest failed for %s", doc_id)
            yield _event(0, "parse", "Processing failed", error=str(exc)[:240] or "Document processing failed")
            return
        if outcome == "needs_review":
            yield _event(
                100,
                "parse",
                "Sent for verification",
                "OCR confidence is low. A reviewer must confirm the text before it becomes searchable.",
                done=True,
                redirect="/upload?status=review",
            )
            return
        yield _event(
            100,
            "index",
            "Ingestion complete",
            "The document is searchable now.",
            done=True,
            redirect="/upload?status=success",
        )

    return StreamingResponse(generate(), media_type="application/x-ndjson")


_STREAM_STEPS = {
    "parsing": (35, "parse", "Reading pages…", "Extracting text (OCR for scanned pages)."),
    "summarizing": (60, "summarize", "Summarizing…", "Building bilingual keywords and an executive summary."),
    "indexing": (82, "index", "Indexing…", "Making the document searchable."),
}


async def _ingest_state(doc_id: str) -> Optional[str]:
    from ..db import get_session

    async with get_session() as session:
        return await session.scalar(select(DocumentRecord.ingest_status).where(DocumentRecord.doc_id == doc_id))


@router.post("/upload", dependencies=[Depends(rate_limit("upload"))])
async def upload_document(
    request: Request,
    background_tasks: BackgroundTasks,
    doc_date: str = Form(...),
    doc_id: str = Form(...),
    doc_type: str = Form(...),
    keywords: str = Form(""),
    sensitivity: str = Form("internal"),
    title: str = Form(""),
    reference_number: str = Form(""),
    department: str = Form(""),
    subject: str = Form(""),
    category: str = Form(""),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """No-JS fallback: same intake as the API, then redirect while the ingest runs."""
    require_role(current_user, [Role.admin, Role.uploader])
    sensitivity = parse_sensitivity(sensitivity)
    doc_type = normalize_document_type(doc_type)
    doc_id = (doc_id or "").strip()
    if not doc_type or not doc_id:
        raise HTTPException(status_code=400, detail="Document ID and type are required")
    try:
        parsed_date = datetime.fromisoformat(doc_date).date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid document date") from exc
    conflict = await doc_id_conflict(db, doc_id)
    if conflict:
        raise HTTPException(status_code=400, detail=conflict)
    upload = await read_validated(file)
    result = await intake_new_document(
        db,
        upload=upload,
        doc_id=doc_id,
        doc_date=parsed_date,
        doc_type=doc_type,
        keywords=_parse_upload_keywords(keywords, []),
        sensitivity=sensitivity,
        uploaded_by=current_user.username,
        metadata={
            "title": title.strip(),
            "reference_number": reference_number.strip(),
            "department": department.strip(),
            "subject": subject.strip(),
            "category": category.strip(),
        },
    )
    await write_audit(
        db,
        username=current_user.username,
        action="upload",
        resource_type="document",
        resource_id=doc_id,
        detail=f"{doc_type} sensitivity={sensitivity} type={upload.content_type} sha256={upload.sha256}",
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    dispatch_after_commit(result, background_tasks)
    return RedirectResponse(url="/upload?status=queued", status_code=status.HTTP_303_SEE_OTHER)
