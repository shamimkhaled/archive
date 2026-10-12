"""Archive search shared by the web UI, /api/v1, MCP, and Ask Sonali.

Order of operations: embed the query, run summary and page-chunk vector searches
with the caller's ACL and metadata filters inside Qdrant, fuse them (RRF + keyword
boosts), add lexical Postgres matches under the same ACL and filters, rerank the
top candidates when a reranker is configured, then attach confirmed metadata and
a citation deep link. Callers still run ``enrich_results_with_access`` (grants,
redaction) on what comes back.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import ai
from ..access_control import UNSEARCHABLE_VERIFICATION
from ..cache import get_cached_search, set_cached_search
from ..models import DocumentRecord
from ..observability import Timer, incr
from ..search_acl import CHUNK_ID_FIELD, SUMMARY_ID_FIELD, AclScope, SearchFilters, build_filter

logger = logging.getLogger("bcp_project.services.search")

EMBED_TIMEOUT_SECONDS = float(os.getenv("SEARCH_EMBED_TIMEOUT_SECONDS", "8"))
VECTOR_TIMEOUT_SECONDS = float(os.getenv("SEARCH_VECTOR_TIMEOUT_SECONDS", "5"))
RERANK_TIMEOUT_SECONDS = float(os.getenv("SEARCH_RERANK_TIMEOUT_SECONDS", "4"))
RERANK_CANDIDATES = int(os.getenv("SEARCH_RERANK_CANDIDATES", "30"))
LEXICAL_LIMIT = 40
QUERY_VECTOR_CACHE_SIZE = int(os.getenv("SEARCH_QUERY_VECTOR_CACHE", "512"))


@dataclass
class SearchOutcome:
    results: List[Dict[str, Any]]
    mode: str
    degraded: bool
    cached: bool = False
    reranked: bool = False

    def as_cache(self) -> Dict[str, Any]:
        return {"results": self.results, "mode": self.mode, "degraded": self.degraded, "reranked": self.reranked}


def citation_url(doc_id: str, page: Optional[int]) -> str:
    path = f"/view/{quote(str(doc_id), safe='')}"
    return f"{path}?page={int(page)}" if page else path


async def _capped(label: str, coro, timeout: float):
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except Exception as exc:
        logger.warning("%s skipped (%s: %s)", label, type(exc).__name__, exc)
        return None


def _searchable(acl: AclScope, filters: SearchFilters) -> List[Any]:
    """SQL conditions every result must satisfy: verified, visible to the caller, matches filters."""
    conditions: List[Any] = [
        or_(DocumentRecord.verification_status.is_(None), DocumentRecord.verification_status.notin_(UNSEARCHABLE_VERIFICATION)),
        *filters.sql_conditions(),
    ]
    acl_condition = acl.sql_condition()
    if acl_condition is not None:
        conditions.append(acl_condition)
    return conditions


_query_vectors: "OrderedDict[tuple, List[float]]" = OrderedDict()
_query_vectors_lock = threading.Lock()


async def embed_query(q: str) -> Optional[List[float]]:
    """Query embedding with a small LRU, keyed by the active embedding space."""
    from ..qdrant_store import embed_texts

    route = ai.embed_route()
    key = (route.provider, route.model, route.params.get("dim"), q)
    with _query_vectors_lock:
        if key in _query_vectors:
            _query_vectors.move_to_end(key)
            return _query_vectors[key]
    vectors = await asyncio.wait_for(asyncio.to_thread(embed_texts, [q]), timeout=EMBED_TIMEOUT_SECONDS)
    vector = vectors[0] if vectors else None
    if vector is not None:
        with _query_vectors_lock:
            _query_vectors[key] = vector
            while len(_query_vectors) > QUERY_VECTOR_CACHE_SIZE:
                _query_vectors.popitem(last=False)
    return vector


async def _vector_hits(q: str, acl: AclScope, filters: SearchFilters, limit: int):
    from ..qdrant_store import make_qdrant_indexer

    try:
        qdrant = make_qdrant_indexer()
        vector = await embed_query(q)
    except Exception as exc:
        logger.warning("Search embedding unavailable (%s: %s)", type(exc).__name__, exc)
        return None, [], {}, False
    if vector is None:
        return None, [], {}, False
    summary_filter = build_filter(acl, filters, SUMMARY_ID_FIELD)
    chunk_filter = build_filter(acl, filters, CHUNK_ID_FIELD)
    summary_result, chunk_result = await asyncio.gather(
        _capped(
            "Summary search",
            asyncio.to_thread(lambda: qdrant.search_summary_hits(vector, q, limit=limit, query_filter=summary_filter)),
            VECTOR_TIMEOUT_SECONDS,
        ),
        _capped(
            "Page-text search",
            asyncio.to_thread(lambda: qdrant.search_chunk_hits(vector, limit=limit, query_filter=chunk_filter)),
            VECTOR_TIMEOUT_SECONDS,
        ),
    )
    summary_rows = summary_result if isinstance(summary_result, list) else []
    chunk_best = chunk_result if isinstance(chunk_result, dict) else {}
    ok = isinstance(summary_result, list) or isinstance(chunk_result, dict)
    return qdrant, summary_rows, chunk_best, ok


async def _lexical_rows(db: AsyncSession, q: str, acl: AclScope, filters: SearchFilters) -> List[Any]:
    like = f"%{q}%"
    text_match = [
        DocumentRecord.doc_id.ilike(like),
        DocumentRecord.doc_type.ilike(like),
        DocumentRecord.title.ilike(like),
        DocumentRecord.reference_number.ilike(like),
        DocumentRecord.subject.ilike(like),
        cast(DocumentRecord.keywords, String).ilike(like),
    ]
    if len(q) >= 3:
        text_match.append(cast(DocumentRecord.summary_json, String).ilike(like))
    return (
        await db.execute(
            select(DocumentRecord.doc_id, DocumentRecord.doc_type, DocumentRecord.keywords, DocumentRecord.summary_json, DocumentRecord.title)
            .where(or_(*text_match), *_searchable(acl, filters))
            .order_by(DocumentRecord.created_at.desc())
            .limit(LEXICAL_LIMIT)
        )
    ).all()


def _lexical_merge(q: str, results: List[Dict[str, Any]], rows: List[Any]) -> List[Dict[str, Any]]:
    from ..qdrant_store import summary_haystack, summary_search_snippet

    needle = q.casefold()
    by_id = {row["doc_id"]: row for row in results if row.get("doc_id")}
    for doc_id, doc_type, keywords, summary_json, title in rows:
        id_hit = needle in (doc_id or "").casefold()
        type_hit = needle in (doc_type or "").casefold() or needle in (title or "").casefold()
        keyword_hit = any(needle in str(kw).casefold() for kw in (keywords or []))
        summary_hit = len(needle) >= 3 and needle in summary_haystack(summary_json).casefold()
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
            continue
        reasons = [r for r, hit in (("doc_id", id_hit), ("keyword", keyword_hit or type_hit), ("summary", summary_hit)) if hit]
        by_id[doc_id] = {
            "doc_id": doc_id,
            "doc_type": doc_type,
            "searchable_keywords": keywords or [],
            "score": 1.2 if id_hit else (0.95 if summary_hit else 0.85),
            "source": "doc_id" if id_hit else ("summary" if summary_hit else "keyword"),
            "match_reasons": reasons or ["keyword"],
            "snippet": summary_search_snippet(summary_json, q) if (summary_hit or keyword_hit) else "",
        }
    return sorted(by_id.values(), key=lambda row: (-(row.get("score") or 0), str(row.get("doc_id") or "")))


async def _attach_metadata(
    db: AsyncSession, results: List[Dict[str, Any]], acl: AclScope, filters: SearchFilters
) -> List[Dict[str, Any]]:
    """Drop anything Postgres says the caller may not find; add confirmed metadata and links."""
    ids = [row["doc_id"] for row in results if row.get("doc_id")]
    if not ids:
        return []
    stored = {
        row.doc_id: row
        for row in (
            await db.execute(
                select(
                    DocumentRecord.doc_id,
                    DocumentRecord.doc_type,
                    DocumentRecord.keywords,
                    DocumentRecord.doc_date,
                    DocumentRecord.title,
                    DocumentRecord.department,
                    DocumentRecord.category,
                    DocumentRecord.reference_number,
                    DocumentRecord.sensitivity,
                ).where(DocumentRecord.doc_id.in_(ids), *_searchable(acl, filters))
            )
        ).all()
    }
    out = []
    for row in results:
        meta = stored.get(row.get("doc_id"))
        if meta is None:
            continue
        page = row.get("page")
        out.append(
            {
                **row,
                "doc_type": meta.doc_type or row.get("doc_type", "Document"),
                "doc_date": meta.doc_date.isoformat() if meta.doc_date else row.get("doc_date"),
                "searchable_keywords": row.get("searchable_keywords") or meta.keywords or [],
                "title": meta.title,
                "department": meta.department,
                "category": meta.category,
                "reference_number": meta.reference_number,
                "sensitivity": meta.sensitivity,
                "page": page,
                "view_url": citation_url(meta.doc_id, page),
            }
        )
    return out


def _rerank_text(row: Dict[str, Any]) -> str:
    parts = [row.get("title") or "", row.get("doc_type") or "", " ".join(str(k) for k in (row.get("searchable_keywords") or [])[:12]), row.get("snippet") or ""]
    return " — ".join(p for p in parts if p)[:1200]


async def _rerank(q: str, rows: List[Dict[str, Any]], text_of) -> bool:
    """Reorder ``rows`` in place by reranker score. False when no reranker ran."""
    if len(rows) < 2:
        return False
    sensitivity = ai.max_sensitivity(r.get("sensitivity") for r in rows)
    scores = await _capped(
        "Rerank",
        asyncio.to_thread(ai.rerank, q, [text_of(r) for r in rows], sensitivity=sensitivity),
        RERANK_TIMEOUT_SECONDS,
    )
    if not scores or len(scores) != len(rows):
        return False
    for row, score in zip(rows, scores):
        row["rerank_score"] = round(float(score), 4)
    rows.sort(key=lambda r: -r["rerank_score"])
    return True


async def hybrid_search(
    db: AsyncSession,
    *,
    acl: AclScope,
    q: str,
    lang: Optional[str] = None,
    filters: Optional[SearchFilters] = None,
    limit: int = 20,
    use_cache: bool = True,
) -> SearchOutcome:
    filters = filters or SearchFilters()
    q = (q or "").strip()
    if not q:
        return SearchOutcome([], "hybrid", False)
    scope = f"{acl.cache_part()}|{filters.cache_part()}"
    if use_cache:
        cached = await get_cached_search(q, lang=lang, scope=scope)
        if cached is not None:
            return SearchOutcome(
                list(cached.get("results") or [])[:limit],
                cached.get("mode") or "hybrid",
                bool(cached.get("degraded")),
                cached=True,
                reranked=bool(cached.get("reranked")),
            )

    with Timer("search.hybrid"):
        candidates = max(limit, RERANK_CANDIDATES)
        (qdrant, summary_rows, chunk_best, vector_ok), lexical = await asyncio.gather(
            _vector_hits(q, acl, filters, candidates), _lexical_rows(db, q, acl, filters)
        )
        results: List[Dict[str, Any]] = []
        if qdrant is not None and (summary_rows or chunk_best):
            results = qdrant.fuse_hybrid_results(summary_rows, chunk_best, q, limit=candidates, lang=lang, use_chunks=True)
        results = _lexical_merge(q, results, lexical)
        results = await _attach_metadata(db, results[:candidates], acl, filters)
        reranked = await _rerank(q, results, _rerank_text)
        outcome = SearchOutcome(results[:limit], "hybrid" if vector_ok else "keyword", not vector_ok, reranked=reranked)
    incr("search.reranked" if reranked else "search.fusion_only")
    if vector_ok and use_cache:
        await set_cached_search(q, outcome.as_cache(), lang=lang, scope=scope)
    return outcome


async def grounded_passages(
    acl: AclScope, q: str, doc_ids: Iterable[str], *, k: int = 8, levels: Optional[Dict[str, str]] = None
) -> List[Dict[str, Any]]:
    """Top page-text passages from ``doc_ids`` (already access-checked), reranked, for Ask Sonali."""
    from ..qdrant_store import make_qdrant_indexer

    ids = [d for d in doc_ids if d]
    if not ids:
        return []
    try:
        qdrant = make_qdrant_indexer()
        vector = await embed_query(q)
        if vector is None:
            return []
        passages = await asyncio.wait_for(
            asyncio.to_thread(
                qdrant.search_passages,
                vector,
                limit=k * 3,
                query_filter=build_filter(acl, None, CHUNK_ID_FIELD, doc_ids=ids),
            ),
            timeout=VECTOR_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("Passage retrieval failed (%s: %s)", type(exc).__name__, exc)
        return []
    for p in passages:
        p["sensitivity"] = (levels or {}).get(p["doc_id"])
    await _rerank(q, passages, lambda p: p["text"][:1500])
    return passages[:k]
