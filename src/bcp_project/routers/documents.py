"""Document upload, search, archive, view, and download routes."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import and_, cast, func, or_, select, String
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import (
    assert_can_download,
    assert_can_view,
    enrich_results_with_access,
    is_archive_download_allowed,
    write_audit,
)
from ..aws_utils import store_pdf
from ..brand import BRAND
from ..cache import (
    bump_search_cache_version,
    get_cached_graph,
    get_cached_metadata_search,
    get_cached_search,
    set_cached_graph,
    set_cached_metadata_search,
    set_cached_search,
)
from ..ports.events import publish_event
from ..deps import (
    UPLOAD_DIR,
    _client_ip,
    _load_document_or_404,
    _load_pdf_bytes,
    _parse_upload_keywords,
    _safe_back_url,
    get_current_user,
    get_db,
    require_role,
    schedule_chunk_index,
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
from ..models import DocumentRecord, Role, User
from ..pdf_parser import parse_pdf
from ..pdf_watermark import stamp_pdf_bytes
from ..qdrant_store import (
    build_summary_embedding_text,
    embed_texts,
    make_qdrant_indexer,
    summary_haystack,
    summary_search_snippet,
)
from ..summary_extractor import extract_document_summary

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


SEARCH_SUMMARY_TIMEOUT_SECONDS = 5.0
SEARCH_CHUNK_TIMEOUT_SECONDS = 5.0
SEARCH_EMBED_TIMEOUT_SECONDS = 8.0


async def _capped_search(label: str, coro, timeout: float):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except Exception as exc:
        logger.warning("%s search skipped (%s: %s)", label, type(exc).__name__, exc)
        return None


@router.get("/api/search")
async def search_documents(
    q: str,
    lang: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Hybrid intelligent search: summary vectors + page chunks + keyword/project boosts."""
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    q = q.strip()
    lang_hint = (lang or "").strip().lower() or None
    if lang_hint not in (None, "en", "bn", "any"):
        lang_hint = None
    if not q:
        return {"results": [], "count": 0, "query": q, "mode": "hybrid"}

    cached = await get_cached_search(q, lang=lang_hint)
    if cached is not None:
        cached_results = await enrich_results_with_access(db, current_user, cached.get("results") or [])
        return {
            **cached,
            "results": cached_results,
            "count": len(cached_results),
            "cached": True,
            "mode": cached.get("mode") or "hybrid",
        }

    needle = q.casefold()
    summary_rows: List[dict] = []
    chunk_best: Dict[str, dict] = {}
    summary_ok = False
    chunk_ok = False

    try:
        qdrant = make_qdrant_indexer()
        query_vector = await asyncio.wait_for(
            asyncio.to_thread(embed_texts, [q]),
            timeout=SEARCH_EMBED_TIMEOUT_SECONDS,
        )
        query_vector = query_vector[0] if query_vector else None
    except Exception as exc:
        logger.warning("Search embedding unavailable (%s: %s)", type(exc).__name__, exc)
        query_vector = None
        qdrant = None

    if qdrant is not None and query_vector is not None:
        summary_task = _capped_search(
            "Summary",
            asyncio.to_thread(lambda: qdrant.search_summary_hits(query_vector, q, limit=20)),
            SEARCH_SUMMARY_TIMEOUT_SECONDS,
        )
        chunk_task = _capped_search(
            "Page-text",
            asyncio.to_thread(lambda: qdrant.search_chunk_hits(query_vector, limit=20)),
            SEARCH_CHUNK_TIMEOUT_SECONDS,
        )
        summary_result, chunk_result = await asyncio.gather(summary_task, chunk_task)
        if isinstance(summary_result, list):
            summary_rows = summary_result
            summary_ok = True
        if isinstance(chunk_result, dict):
            chunk_best = chunk_result
            chunk_ok = True

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
    vector_ok = bool(summary_ok or chunk_ok)

    # Postgres: document ID / type / keywords, plus LLM summary fields (option B).
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
            .limit(40)
        )
    ).all()
    by_id = {row.get("doc_id"): row for row in results if row.get("doc_id")}
    for doc_id, doc_type, keywords, summary_json in id_rows:
        id_hit = needle in (doc_id or "").casefold()
        type_hit = needle in (doc_type or "").casefold()
        keyword_hit = any(needle in str(kw).casefold() for kw in (keywords or []))
        hay = summary_haystack(summary_json)
        summary_hit = len(needle) >= 3 and needle in hay.casefold()
        if doc_id in by_id:
            entry = by_id[doc_id]
            reasons = list(entry.get("match_reasons") or [])
            if id_hit and "doc_id" not in reasons:
                reasons.append("doc_id")
                entry["score"] = float(entry.get("score") or 0) + 0.35
            if (keyword_hit or type_hit) and "keyword" not in reasons:
                reasons.append("keyword")
                entry["score"] = float(entry.get("score") or 0) + 0.16
            if summary_hit and "summary" not in reasons:
                reasons.append("summary")
                entry["score"] = float(entry.get("score") or 0) + 0.2
            if not entry.get("snippet") and (summary_hit or keyword_hit):
                entry["snippet"] = summary_search_snippet(summary_json, q)
            entry["match_reasons"] = reasons
        else:
            reasons = []
            if id_hit:
                reasons.append("doc_id")
            if keyword_hit or type_hit:
                reasons.append("keyword")
            if summary_hit:
                reasons.append("summary")
            if not reasons:
                reasons = ["keyword"]
            source = "doc_id" if id_hit else ("summary" if summary_hit else "keyword")
            by_id[doc_id] = {
                "doc_id": doc_id,
                "doc_type": doc_type,
                "searchable_keywords": keywords or [],
                "score": 1.2 if id_hit else (0.95 if summary_hit else 0.85),
                "source": source,
                "match_reasons": reasons,
                "snippet": summary_search_snippet(summary_json, q) if (summary_hit or keyword_hit) else "",
            }
    results = sorted(by_id.values(), key=lambda row: (-(row.get("score") or 0), str(row.get("doc_id") or "")))[:20]

    document_ids = [row["doc_id"] for row in results if row.get("doc_id")]
    if document_ids:
        statement = select(
            DocumentRecord.doc_id,
            DocumentRecord.doc_type,
            DocumentRecord.keywords,
            DocumentRecord.doc_date,
        ).where(DocumentRecord.doc_id.in_(document_ids))
        stored_docs = await db.execute(statement)
        stored_map = {
            doc_id: (doc_type, keywords, doc_date)
            for doc_id, doc_type, keywords, doc_date in stored_docs.all()
        }
        filtered_results = []
        for row in results:
            doc_id = row.get("doc_id")
            if doc_id not in stored_map:
                continue
            doc_type, keywords, doc_date = stored_map[doc_id]
            filtered_results.append(
                {
                    **row,
                    "doc_type": doc_type or row.get("doc_type", "Document"),
                    "doc_date": doc_date.isoformat() if doc_date else row.get("doc_date"),
                    "searchable_keywords": row.get("searchable_keywords") or keywords or [],
                }
            )
    else:
        filtered_results = []

    payload = {
        "results": filtered_results,
        "count": len(filtered_results),
        "query": q,
        "mode": "hybrid" if vector_ok else "keyword",
        "lang": lang_hint or "any",
        "degraded": not vector_ok,
    }
    if vector_ok:
        await set_cached_search(q, payload, lang=lang_hint)

    enriched = await enrich_results_with_access(db, current_user, filtered_results)
    return {
        "results": enriched,
        "count": len(enriched),
        "query": q,
        "cached": False,
        "mode": payload["mode"],
        "lang": lang_hint or "any",
        "degraded": not vector_ok,
    }



@router.get("/api/search/metadata")
async def search_documents_by_metadata(
    doc_id: Optional[str] = None,
    doc_type: Optional[str] = None,
    uploaded_by: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
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
    }

    cached = await get_cached_metadata_search(filters)
    if cached is not None:
        enriched = await enrich_results_with_access(db, current_user, cached.get("results") or [])
        return {**cached, "results": enriched, "count": len(enriched), "cached": True}

    conditions = []
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
        }
        for doc in documents
    ]
    cache_payload = {"results": base_results, "count": len(base_results)}
    await set_cached_metadata_search(filters, cache_payload)

    enriched = await enrich_results_with_access(db, current_user, base_results)
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
    items = [document_summary_card(doc) for doc in docs]

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
    if cached:
        cached["cached"] = True
        return cached

    document = await _load_document_or_404(focus, db)
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
    return payload


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
            "page_subtitle": f"Viewing {doc_id} · {document.doc_type}",
            "file_url": f"/view/{doc_id}/file",
            "download_url": f"/download/{doc_id}" if can_download else None,
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

    pdf_bytes = await _load_pdf_bytes(document)
    seal_text = BRAND.seal_stream(current_user.username)
    try:
        stamped = stamp_pdf_bytes(pdf_bytes, seal_text)
    except Exception as exc:
        logger.exception("Watermark stamping failed for %s", doc_id)
        raise HTTPException(status_code=502, detail="Could not prepare secure document stream") from exc

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

    return Response(
        content=stamped,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{doc_id}-view.pdf"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


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
        stamped = stamp_pdf_bytes(pdf_bytes, seal_text)
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
    return Response(
        content=stamped,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{doc_id}-watermarked.pdf"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )

@router.post("/api/upload/stream")
async def upload_document_stream(
    request: Request,
    background_tasks: BackgroundTasks,
    doc_date: str = Form(...),
    doc_id: str = Form(...),
    doc_type: str = Form(...),
    keywords: str = Form(""),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """NDJSON progress stream: real % as upload → parse → summarize → index complete."""
    require_role(current_user, [Role.admin, Role.uploader])
    doc_type = normalize_document_type(doc_type)
    if not doc_type:
        async def _bad_type():
            yield json.dumps(
                {"pct": 0, "step": "upload", "title": "Upload failed", "error": "Document type is required"}
            ) + "\n"

        return StreamingResponse(_bad_type(), media_type="application/x-ndjson")

    import asyncio

    def _event(pct: int, step: str, title: str, hint: str = "", **extra: Any) -> str:
        payload = {"pct": pct, "step": step, "title": title, "hint": hint, **extra}
        return json.dumps(payload, ensure_ascii=False) + "\n"

    async def generate():
        destination = None
        try:
            yield _event(5, "upload", "Starting upload…", "Checking the file and saving it.")

            content_type = (file.content_type or "").lower()
            name = (file.filename or "").lower()
            if content_type not in ("application/pdf", "application/x-pdf", "") and not name.endswith(".pdf"):
                yield _event(0, "upload", "Upload failed", error="Only PDF files are accepted")
                return

            statement = select(DocumentRecord).where(DocumentRecord.doc_id == doc_id)
            result = await db.execute(statement)
            if result.scalar_one_or_none() is not None:
                yield _event(0, "upload", "Upload failed", error="A document with this Document ID already exists")
                return

            yield _event(12, "upload", "Receiving PDF…", "Transferring file to the server.")
            file_bytes = await file.read()
            if len(file_bytes) > 50 * 1024 * 1024:
                yield _event(0, "upload", "Upload failed", error="PDF exceeds 50MB upload limit")
                return

            safe_name = Path(file.filename or "document.pdf").name.replace("..", "")
            filename = f"{uuid.uuid4().hex}_{safe_name}"
            destination = UPLOAD_DIR / filename
            destination.write_bytes(file_bytes)
            pdf_path = str(destination.resolve())
            yield _event(22, "upload", "File saved", "PDF stored. Starting text extraction.")

            yield _event(28, "parse", "Parsing PDF…", "Extracting text from each page.")
            pages = await asyncio.to_thread(parse_pdf, pdf_path)
            if not pages:
                yield _event(0, "parse", "Parsing failed", error="Could not extract text from PDF")
                return
            yield _event(48, "parse", "Parsing complete", f"Read {len(pages)} page(s).")

            yield _event(55, "summarize", "Summarizing…", "Building bilingual keywords and an executive summary.")
            document_text = "\n\n".join(page.text for page in pages)
            if len(document_text) > 60000:
                document_text = document_text[:60000] + "\n\n[Truncated for summarization]"
            summary = await asyncio.to_thread(extract_document_summary, document_text)
            stored_keywords = _parse_upload_keywords(keywords, summary.searchable_keywords)
            yield _event(72, "summarize", "Summary ready", "Keywords and structure extracted.")

            yield _event(78, "index", "Indexing summary…", "Saving searchable summary to the archive.")
            remote_location = await asyncio.to_thread(store_pdf, pdf_path, filename)
            qdrant = make_qdrant_indexer()
            await asyncio.to_thread(qdrant.create_collections)

            summary_payload = {
                "doc_id": doc_id,
                "doc_type": doc_type,
                "doc_date": doc_date,
                "searchable_keywords": summary.searchable_keywords,
                "major_projects": [
                    project if isinstance(project, dict) else project.dict() for project in summary.major_projects
                ],
                "core_info": summary.core_info or {},
                "key_personnel": [p if isinstance(p, dict) else p.dict() for p in summary.key_personnel],
                "finance_and_admin": list(summary.finance_and_admin or []),
            }
            summary_text = build_summary_embedding_text(summary_payload)
            summary_vector = (await asyncio.to_thread(embed_texts, [summary_text]))[0]
            await asyncio.to_thread(
                qdrant.upload_summary,
                doc_id,
                summary_payload,
                summary_vector,
            )
            yield _event(92, "index", "Summary indexed", "Document is searchable. Finishing save…")

            page_payloads = [{"text": page.text, "metadata": dict(page.metadata or {})} for page in pages]
            await schedule_chunk_index(doc_id, page_payloads, background_tasks)

            document = DocumentRecord(
                doc_id=doc_id,
                doc_date=datetime.fromisoformat(doc_date).date(),
                doc_type=doc_type,
                keywords=stored_keywords,
                summary_json=summary_payload,
                file_location=remote_location,
                uploaded_by=current_user.username,
            )
            db.add(document)
            await write_audit(
                db,
                username=current_user.username,
                action="upload",
                resource_type="document",
                resource_id=doc_id,
                detail=doc_type,
                ip_address=_client_ip(request),
                commit=False,
            )
            await db.commit()
            await bump_search_cache_version()
            publish_event(
                "document.indexed",
                {
                    "doc_id": doc_id,
                    "doc_type": doc_type,
                    "uploaded_by": current_user.username,
                    "source": "upload_stream",
                },
            )

            yield _event(
                100,
                "index",
                "Ingestion complete",
                "Summary is searchable now. Deeper text indexing continues briefly in the background.",
                done=True,
                redirect="/upload?status=success",
            )
        except Exception as exc:
            logger.exception("Streaming upload failed for %s", doc_id)
            if destination is not None:
                try:
                    Path(destination).unlink(missing_ok=True)
                except Exception:
                    pass
            yield _event(0, "upload", "Processing failed", error=str(exc)[:240] or "Document processing failed")

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.post("/upload")
async def upload_document(
    request: Request,
    background_tasks: BackgroundTasks,
    doc_date: str = Form(...),
    doc_id: str = Form(...),
    doc_type: str = Form(...),
    keywords: str = Form(""),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Fallback non-stream upload (no-JS). Prefer /api/upload/stream from the UI."""
    require_role(current_user, [Role.admin, Role.uploader])
    doc_type = normalize_document_type(doc_type)
    if not doc_type:
        raise HTTPException(status_code=400, detail="Document type is required")

    content_type = (file.content_type or "").lower()
    if content_type not in ("application/pdf", "application/x-pdf", ""):
        name = (file.filename or "").lower()
        if not name.endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    statement = select(DocumentRecord).where(DocumentRecord.doc_id == doc_id)
    result = await db.execute(statement)
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=400, detail="A document with this DocId already exists")

    safe_name = Path(file.filename or "document.pdf").name.replace("..", "")
    filename = f"{uuid.uuid4().hex}_{safe_name}"
    destination = UPLOAD_DIR / filename
    file_bytes = await file.read()
    if len(file_bytes) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="PDF exceeds 50MB upload limit")
    destination.write_bytes(file_bytes)

    pdf_path = str(destination.resolve())
    try:
        pages = parse_pdf(pdf_path)
        if not pages:
            raise HTTPException(status_code=400, detail="Could not extract text from PDF")

        document_text = "\n\n".join(page.text for page in pages)
        if len(document_text) > 60000:
            document_text = document_text[:60000] + "\n\n[Truncated for summarization]"

        summary = extract_document_summary(document_text)
        stored_keywords = _parse_upload_keywords(keywords, summary.searchable_keywords)
        remote_location = store_pdf(pdf_path, filename)

        qdrant = make_qdrant_indexer()
        qdrant.create_collections()

        summary_payload = {
            "doc_id": doc_id,
            "doc_type": doc_type,
            "doc_date": doc_date,
            "searchable_keywords": summary.searchable_keywords,
            "major_projects": [project if isinstance(project, dict) else project.dict() for project in summary.major_projects],
            "core_info": summary.core_info or {},
            "key_personnel": [p if isinstance(p, dict) else p.dict() for p in summary.key_personnel],
            "finance_and_admin": list(summary.finance_and_admin or []),
        }
        summary_text = build_summary_embedding_text(summary_payload)
        summary_vector = embed_texts([summary_text])[0]
        qdrant.upload_summary(summary_id=doc_id, payload=summary_payload, vector=summary_vector)

        page_payloads = [{"text": page.text, "metadata": dict(page.metadata or {})} for page in pages]
        await schedule_chunk_index(doc_id, page_payloads, background_tasks)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Document processing failed for %s", doc_id)
        raise HTTPException(status_code=502, detail="Document processing failed") from exc

    document = DocumentRecord(
        doc_id=doc_id,
        doc_date=datetime.fromisoformat(doc_date).date(),
        doc_type=doc_type,
        keywords=stored_keywords,
        summary_json=summary_payload,
        file_location=remote_location,
        uploaded_by=current_user.username,
    )
    db.add(document)
    await write_audit(
        db,
        username=current_user.username,
        action="upload",
        resource_type="document",
        resource_id=doc_id,
        detail=doc_type,
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    await bump_search_cache_version()
    publish_event(
        "document.indexed",
        {
            "doc_id": doc_id,
            "doc_type": doc_type,
            "uploaded_by": current_user.username,
            "source": "upload",
        },
    )
    return RedirectResponse(url="/upload?status=success", status_code=status.HTTP_303_SEE_OTHER)
