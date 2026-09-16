import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Union

from qdrant_client import QdrantClient
from qdrant_client.http import models as rest

from .config import (
    load_environment,
    openai_embedding_model,
    resolve_openai_credentials,
    resolve_qdrant_settings,
)

load_environment()

logger = logging.getLogger("bcp_project.qdrant_store")

DOC_POINT_NAMESPACE = uuid.UUID("f47ac10b-58cc-4372-a567-0e02b2c3d479")


def normalize_doc_point_id(point_id: Union[str, int, uuid.UUID]) -> Union[int, uuid.UUID]:
    """Map archive doc_ids (e.g. SB-2026-001) to Qdrant-compatible point IDs."""
    if isinstance(point_id, int):
        return point_id
    if isinstance(point_id, uuid.UUID):
        return point_id
    if isinstance(point_id, str):
        stripped = point_id.strip()
        if not stripped:
            raise ValueError("Point id cannot be empty")
        if stripped.isdigit():
            return int(stripped)
        try:
            return uuid.UUID(stripped)
        except ValueError:
            return uuid.uuid5(DOC_POINT_NAMESPACE, stripped)
    raise TypeError(f"Unsupported point id type: {type(point_id)!r}")

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

def _openai_client() -> "OpenAI":
    if OpenAI is None:
        raise RuntimeError("The openai package is required for embedding text.")
    api_key, base_url = resolve_openai_credentials()
    if base_url:
        return OpenAI(api_key=api_key, base_url=base_url)
    return OpenAI(api_key=api_key)


def embed_texts(inputs: List[str], model: Optional[str] = None) -> List[List[float]]:
    client = _openai_client()
    response = client.embeddings.create(model=model or openai_embedding_model(), input=inputs)
    return [item.embedding for item in response.data]


def _casefold_contains(haystack: str, needle: str) -> bool:
    """Unicode-aware substring check (Bangla + Latin)."""
    if not needle:
        return False
    return needle.casefold() in haystack.casefold()


def _contains_bangla(text: str) -> bool:
    return any("\u0980" <= ch <= "\u09FF" for ch in (text or ""))


def reciprocal_rank_fusion(
    ranked_lists: List[List[str]],
    *,
    k: int = 60,
) -> Dict[str, float]:
    """Merge multiple ranked doc-id lists with Reciprocal Rank Fusion."""
    scores: Dict[str, float] = {}
    for ranked in ranked_lists:
        seen = set()
        for rank, doc_id in enumerate(ranked):
            if not doc_id or doc_id in seen:
                continue
            seen.add(doc_id)
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


def build_summary_embedding_text(summary: dict) -> str:
    """Concatenate structured summary fields for bilingual semantic retrieval."""
    parts: List[str] = []
    parts.append(" ".join(str(kw) for kw in (summary.get("searchable_keywords") or []) if kw))

    core = summary.get("core_info") or {}
    if isinstance(core, dict):
        for key in ("organization", "meeting_title", "date", "status"):
            val = core.get(key)
            if val:
                parts.append(str(val))

    for person in summary.get("key_personnel") or []:
        if isinstance(person, dict):
            parts.append(str(person.get("name") or ""))
            parts.append(str(person.get("role") or ""))
        else:
            parts.append(str(person))

    for project in summary.get("major_projects") or []:
        if isinstance(project, dict):
            parts.append(str(project.get("project_name") or ""))
            parts.append(str(project.get("brief_context") or ""))
        else:
            parts.append(str(project))

    for item in summary.get("finance_and_admin") or []:
        if item:
            parts.append(str(item))

    return " \n ".join([p for p in parts if p and str(p).strip()])


def summary_haystack(summary: Optional[dict]) -> str:
    """Flatten LLM summary fields for lexical search and snippets."""
    if not isinstance(summary, dict):
        return ""
    parts: List[str] = []
    core = summary.get("core_info") or {}
    if isinstance(core, dict):
        for key in ("organization", "meeting_title", "date", "status"):
            val = core.get(key)
            if val:
                parts.append(str(val))
    for person in summary.get("key_personnel") or []:
        if isinstance(person, dict):
            parts.append(str(person.get("name") or ""))
            parts.append(str(person.get("role") or ""))
        elif person:
            parts.append(str(person))
    for project in summary.get("major_projects") or []:
        if isinstance(project, dict):
            parts.append(str(project.get("project_name") or ""))
            parts.append(str(project.get("brief_context") or ""))
        elif project:
            parts.append(str(project))
    for item in summary.get("finance_and_admin") or []:
        if item:
            parts.append(str(item))
    for kw in summary.get("searchable_keywords") or []:
        if kw:
            parts.append(str(kw))
    return " · ".join(p.strip() for p in parts if p and str(p).strip())


def summary_search_snippet(summary: Optional[dict], needle: str) -> str:
    hay = summary_haystack(summary)
    if not hay:
        return ""
    if not needle:
        return hay[:220]
    idx = hay.casefold().find(needle.casefold())
    if idx < 0:
        return hay[:220]
    start = max(0, idx - 48)
    snippet = hay[start : start + 220].strip()
    if start:
        snippet = "…" + snippet
    if start + 220 < len(hay):
        snippet = snippet.rstrip() + "…"
    return snippet


_indexer_lock = threading.Lock()
_indexer: Optional["QdrantIndexer"] = None


def make_qdrant_indexer() -> "QdrantIndexer":
    global _indexer
    if _indexer is not None:
        return _indexer
    with _indexer_lock:
        if _indexer is None:
            url, api_key = resolve_qdrant_settings()
            _indexer = QdrantIndexer(url=url, api_key=api_key)
    return _indexer


def reset_qdrant_indexer() -> None:
    """Drop a stale HTTP client after Qdrant connection resets."""
    global _indexer
    with _indexer_lock:
        client = _indexer
        _indexer = None
    if client is None:
        return
    try:
        close = getattr(client.client, "close", None)
        if callable(close):
            close()
    except Exception:
        pass


class QdrantIndexer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6333,
        api_key: Optional[str] = None,
        url: Optional[str] = None,
    ):
        self._url = url or f"http://{host}:{port}"
        self._api_key = api_key
        # Older qdrant-client versions forward unknown kwargs into httpx.Client,
        # so do not pass check_compatibility here.
        self.client = self._new_client()

    def _new_client(self) -> QdrantClient:
        return QdrantClient(
            url=self._url,
            api_key=self._api_key,
            timeout=60,
        )

    def _reconnect(self) -> None:
        try:
            close = getattr(self.client, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
        self.client = self._new_client()

    def _upsert_points(self, collection_name: str, points: List[Any], *, batch_size: int = 8) -> None:
        """Upsert in small batches; reconnect if Qdrant resets the socket."""
        if not points:
            return
        last_exc: Optional[Exception] = None
        for start in range(0, len(points), batch_size):
            batch = points[start : start + batch_size]
            last_exc = None
            for attempt in range(1, 4):
                try:
                    self.client.upsert(collection_name=collection_name, points=batch, wait=True)
                    last_exc = None
                    break
                except TypeError:
                    self.client.upsert(collection_name=collection_name, points=batch)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "Qdrant upsert failed for %s (batch %s, attempt %s): %s",
                        collection_name,
                        start // batch_size + 1,
                        attempt,
                        exc,
                    )
                    self._reconnect()
                    time.sleep(0.7 * attempt)
            if last_exc is not None:
                raise last_exc

    def _query_points(self, **kwargs: Any) -> Any:
        """Query Qdrant, reconnecting once after a dropped connection."""
        try:
            return self.client.query_points(**kwargs)
        except TypeError:
            kwargs.pop("timeout", None)
            return self.client.query_points(**kwargs)
        except Exception as exc:
            logger.warning("Qdrant query failed (%s); reconnecting once", exc)
            self._reconnect()
            try:
                return self.client.query_points(**kwargs)
            except TypeError:
                kwargs.pop("timeout", None)
                return self.client.query_points(**kwargs)

    def create_collections(
        self,
        summaries_collection: str = "document_summaries",
        chunks_collection: str = "document_chunks",
        vector_size: int = 1536,
    ) -> None:
        summary_config = rest.VectorParams(size=vector_size, distance=rest.Distance.COSINE)
        chunk_config = rest.VectorParams(size=vector_size, distance=rest.Distance.COSINE)

        try:
            existing = {c.name for c in self.client.get_collections().collections}
        except Exception as exc:
            message = str(exc)
            if "404" in message or "Not Found" in message:
                raise RuntimeError(
                    "Qdrant Cloud returned 404. Check Railway QDRANT_URL uses https:// "
                    "(not http://) e.g. https://YOUR-CLUSTER.us-east-1-1.aws.cloud.qdrant.io:6333 "
                    "and QDRANT_API_KEY is set."
                ) from exc
            raise

        if summaries_collection not in existing:
            self.client.create_collection(collection_name=summaries_collection, vectors_config=summary_config)

        if chunks_collection not in existing:
            self.client.create_collection(collection_name=chunks_collection, vectors_config=chunk_config)

    def _normalize_id(self, point_id: str) -> Union[int, uuid.UUID]:
        try:
            return normalize_doc_point_id(point_id)
        except (TypeError, ValueError):
            return uuid.uuid4()

    def _normalize_point_id(self, point_id: Union[str, int, uuid.UUID]) -> Union[int, uuid.UUID]:
        """Stable Qdrant point IDs for business doc_ids like SB-2026-001."""
        return normalize_doc_point_id(point_id)

    def _client_point_id(self, point_id: Union[str, int, uuid.UUID]) -> Union[int, str]:
        """Older qdrant-client HTTP models accept int or UUID strings, not UUID objects."""
        normalized = self._normalize_point_id(point_id)
        if isinstance(normalized, uuid.UUID):
            return str(normalized)
        return normalized

    def get_summary_by_doc_id(
        self,
        doc_id: str,
        collection_name: str = "document_summaries",
        *,
        allow_scroll: bool = True,
    ) -> Optional[Any]:
        """Retrieve a summary point by archive doc_id payload or point id."""
        normalized_id = self._client_point_id(doc_id)
        try:
            retrieved = self.client.retrieve(
                collection_name=collection_name,
                ids=[normalized_id],
                with_vectors=True,
                with_payload=True,
            )
            if retrieved:
                return retrieved[0]
        except Exception:
            pass

        if not allow_scroll:
            return None

        try:
            scroll_filter = rest.Filter(
                must=[rest.FieldCondition(key="doc_id", match=rest.MatchValue(value=doc_id))]
            )
            points, _ = self.client.scroll(
                collection_name=collection_name,
                scroll_filter=scroll_filter,
                limit=1,
                with_vectors=True,
                with_payload=True,
            )
            return points[0] if points else None
        except Exception:
            return None

    def _point_vector(self, point: Any) -> Optional[List[float]]:
        vector = getattr(point, "vector", None)
        if vector is None:
            return None
        if isinstance(vector, dict):
            return next(iter(vector.values()), None)
        return vector

    def find_similar_documents(
        self,
        doc_id: str,
        *,
        limit: int = 8,
        min_score: float = 0.0,
        collection_name: str = "document_summaries",
        allow_fallback: bool = True,
    ) -> List[Dict[str, Any]]:
        """Find similar summary vectors.

        ``allow_fallback=False`` skips collection scroll. It still retrieves the
        one focus vector (by id) and queries neighbors — that is the fast path.
        """
        fetch_limit = max(limit + 6, 12)
        response = None
        query_kwargs: Dict[str, Any] = {
            "collection_name": collection_name,
            "query": self._client_point_id(doc_id),
            "limit": fetch_limit,
            "with_payload": True,
        }
        if not allow_fallback:
            query_kwargs["timeout"] = 3
        try:
            response = self.client.query_points(**query_kwargs)
        except TypeError:
            query_kwargs.pop("timeout", None)
            try:
                response = self.client.query_points(**query_kwargs)
            except Exception:
                response = None
        except Exception:
            response = None

        if response is None or not getattr(response, "points", None):
            try:
                point = self.get_summary_by_doc_id(
                    doc_id,
                    collection_name=collection_name,
                    allow_scroll=allow_fallback,
                )
                if point is None:
                    return []
                vector = self._point_vector(point)
                if not vector:
                    return []
                vector_kwargs: Dict[str, Any] = {
                    "collection_name": collection_name,
                    "query": vector,
                    "limit": fetch_limit,
                    "with_payload": True,
                }
                if not allow_fallback:
                    vector_kwargs["timeout"] = 3
                try:
                    response = self.client.query_points(**vector_kwargs)
                except TypeError:
                    vector_kwargs.pop("timeout", None)
                    response = self.client.query_points(**vector_kwargs)
            except Exception:
                if response is None:
                    return []
        if response is None or not getattr(response, "points", None):
            return []

        points = response.points if getattr(response, "points", None) is not None else []
        results: List[Dict[str, Any]] = []
        for hit in points:
            payload = getattr(hit, "payload", None) or {}
            other_id = payload.get("doc_id") or payload.get("document_id") or str(hit.id)
            if other_id == doc_id:
                continue
            score = float(getattr(hit, "score", 0) or 0)
            if score < min_score:
                continue
            results.append(
                {
                    "doc_id": other_id,
                    "doc_type": payload.get("doc_type", "Document"),
                    "score": score,
                    "source": "semantic",
                }
            )
            if len(results) >= limit:
                break
        return results

    def upload_summary(
        self,
        summary_id: str,
        payload: Dict[str, Any],
        vector: List[float],
        collection_name: str = "document_summaries",
    ) -> None:
        payload.setdefault("document_id", summary_id)
        payload.setdefault("doc_id", summary_id)
        normalized_id = self._client_point_id(summary_id)
        self._upsert_points(
            collection_name,
            [rest.PointStruct(id=normalized_id, vector=vector, payload=payload)],
            batch_size=1,
        )

    def upload_chunks(
        self,
        chunks: List[Dict[str, Any]],
        vectors: List[List[float]],
        collection_name: str = "document_chunks",
    ) -> None:
        points = []
        for chunk, vector in zip(chunks, vectors):
            normalized_id = self._client_point_id(chunk["id"])
            payload = {"text": chunk["text"], **chunk["metadata"]}
            payload.setdefault("document_id", chunk["metadata"].get("parent_doc_id"))
            points.append(
                rest.PointStruct(
                    id=normalized_id,
                    vector=vector,
                    payload=payload,
                )
            )
        self._upsert_points(collection_name, points, batch_size=8)

    def _normalize_query_response(self, point: Any, query_lower: str) -> Dict[str, Any]:
        """Label keyword/project hits vs pure summary similarity."""
        payload = getattr(point, "payload", None) or {}
        doc_id = payload.get("document_id") or payload.get("doc_id") or payload.get("parent_doc_id") or str(point.id)
        keywords = payload.get("searchable_keywords", []) or []
        projects = payload.get("major_projects", []) or []

        keyword_hit = any(_casefold_contains(str(kw), query_lower) for kw in keywords)
        project_hit = any(
            _casefold_contains(str(project.get("project_name", "")), query_lower)
            or _casefold_contains(str(project.get("brief_context", "")), query_lower)
            for project in projects
            if isinstance(project, dict)
        )

        return {
            "id": str(point.id),
            "doc_id": doc_id,
            "doc_type": payload.get("doc_type", "Unknown"),
            "searchable_keywords": keywords,
            "major_projects": projects,
            "score": float(getattr(point, "score", 0) or 0),
            "source": "keyword" if (keyword_hit or project_hit) else "summary",
            "match_reasons": (
                (["keyword"] if keyword_hit else [])
                + (["project"] if project_hit else [])
                + (["summary"] if not (keyword_hit or project_hit) else [])
            ),
        }

    def search_documents(
        self,
        query: str,
        limit: int = 10,
        collection_name: str = "document_summaries",
        lang: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Summary-vector search with keyword/project preference (legacy path)."""
        return self.search_documents_hybrid(
            query=query,
            limit=limit,
            lang=lang,
            use_chunks=False,
            summaries_collection=collection_name,
        )

    def search_summary_hits(
        self,
        query_vector: List[float],
        query: str,
        *,
        limit: int = 10,
        collection_name: str = "document_summaries",
    ) -> List[Dict[str, Any]]:
        query_lower = (query or "").casefold()
        try:
            response = self._query_points(
                collection_name=collection_name,
                query=query_vector,
                limit=max(limit * 5, 40),
                with_payload=True,
            )
            points = response.points if getattr(response, "points", None) is not None else []
        except Exception as exc:
            logger.warning("Summary vector search failed: %s", exc)
            return []
        return [self._normalize_query_response(point, query_lower) for point in points]

    def search_chunk_hits(
        self,
        query_vector: List[float],
        *,
        limit: int = 10,
        collection_name: str = "document_chunks",
    ) -> Dict[str, Dict[str, Any]]:
        """Search OCR / extracted page text. Independent from summary search."""
        chunk_best: Dict[str, Dict[str, Any]] = {}
        try:
            response = self._query_points(
                collection_name=collection_name,
                query=query_vector,
                limit=max(limit * 8, 60),
                with_payload=True,
            )
            points = response.points if getattr(response, "points", None) is not None else []
        except Exception as exc:
            logger.warning("Chunk vector search failed: %s", exc)
            return {}

        for point in points:
            payload = getattr(point, "payload", None) or {}
            parent = (
                payload.get("parent_doc_id")
                or payload.get("document_id")
                or payload.get("doc_id")
            )
            if not parent:
                continue
            score = float(getattr(point, "score", 0) or 0)
            text = str(payload.get("text") or "")
            snippet = " ".join(text.split())
            if len(snippet) > 220:
                snippet = snippet[:217].rstrip() + "…"
            existing = chunk_best.get(str(parent))
            if existing is None or score > existing["score"]:
                chunk_best[str(parent)] = {
                    "doc_id": str(parent),
                    "score": score,
                    "snippet": snippet,
                    "page_number": payload.get("page_number"),
                    "source": "chunk",
                }
        return chunk_best

    def fuse_hybrid_results(
        self,
        summary_rows: List[Dict[str, Any]],
        chunk_best: Dict[str, Dict[str, Any]],
        query: str,
        *,
        limit: int = 10,
        lang: Optional[str] = None,
        use_chunks: bool = True,
    ) -> List[Dict[str, Any]]:
        query_lower = (query or "").casefold()
        lang_hint = (lang or "").strip().lower()
        summary_rows = summary_rows or []
        chunk_best = chunk_best or {}

        summary_rank = [row["doc_id"] for row in summary_rows if row.get("doc_id")]
        chunk_rank = sorted(chunk_best.keys(), key=lambda did: -chunk_best[did]["score"])
        fused = reciprocal_rank_fusion([summary_rank, chunk_rank] if use_chunks else [summary_rank])

        by_id: Dict[str, Dict[str, Any]] = {}
        for row in summary_rows:
            doc_id = row.get("doc_id")
            if not doc_id:
                continue
            by_id[doc_id] = {
                **row,
                "match_reasons": list(row.get("match_reasons") or []),
                "snippet": "",
                "sources": [row.get("source") or "summary"],
            }

        for doc_id, chunk_row in chunk_best.items():
            if doc_id not in by_id:
                by_id[doc_id] = {
                    "id": doc_id,
                    "doc_id": doc_id,
                    "doc_type": "Document",
                    "searchable_keywords": [],
                    "major_projects": [],
                    "score": chunk_row["score"],
                    "source": "chunk",
                    "match_reasons": ["chunk"],
                    "snippet": chunk_row.get("snippet") or "",
                    "sources": ["chunk"],
                }
            else:
                entry = by_id[doc_id]
                if "chunk" not in entry["match_reasons"]:
                    entry["match_reasons"].append("chunk")
                if "chunk" not in entry["sources"]:
                    entry["sources"].append("chunk")
                if not entry.get("snippet"):
                    entry["snippet"] = chunk_row.get("snippet") or ""
                entry["score"] = max(float(entry.get("score") or 0), chunk_row["score"])

        results: List[Dict[str, Any]] = []
        for doc_id, entry in by_id.items():
            rrf = fused.get(doc_id, 0.0)
            vector_score = float(entry.get("score") or 0)
            boost = 0.0
            reasons = list(entry.get("match_reasons") or [])

            if _casefold_contains(str(doc_id), query_lower):
                boost += 0.28
                if "doc_id" not in reasons:
                    reasons.append("doc_id")

            keywords = entry.get("searchable_keywords") or []
            if any(_casefold_contains(str(kw), query_lower) for kw in keywords):
                boost += 0.16
                if "keyword" not in reasons:
                    reasons.append("keyword")

            projects = entry.get("major_projects") or []
            if any(
                isinstance(p, dict)
                and (
                    _casefold_contains(str(p.get("project_name") or ""), query_lower)
                    or _casefold_contains(str(p.get("brief_context") or ""), query_lower)
                )
                for p in projects
            ):
                boost += 0.12
                if "project" not in reasons:
                    reasons.append("project")

            evidence = " ".join(
                [
                    str(doc_id),
                    " ".join(str(k) for k in keywords),
                    entry.get("snippet") or "",
                ]
            )
            has_bn = _contains_bangla(evidence)
            if lang_hint == "bn" and has_bn:
                boost += 0.06
            elif lang_hint == "en" and evidence and not has_bn:
                boost += 0.03

            final_score = (rrf * 4.0) + vector_score + boost
            sources = entry.get("sources") or ["summary"]
            if len(sources) > 1:
                primary = "hybrid"
            elif "keyword" in reasons:
                primary = "keyword"
            else:
                primary = sources[0]

            results.append(
                {
                    **entry,
                    "score": round(final_score, 4),
                    "vector_score": round(vector_score, 4),
                    "rrf_score": round(rrf, 4),
                    "source": primary,
                    "match_reasons": reasons,
                    "snippet": entry.get("snippet") or "",
                }
            )

        results.sort(key=lambda row: (-(row.get("score") or 0), str(row.get("doc_id") or "")))
        return results[:limit]

    def search_documents_hybrid(
        self,
        query: str,
        limit: int = 10,
        lang: Optional[str] = None,
        use_chunks: bool = True,
        summaries_collection: str = "document_summaries",
        chunks_collection: str = "document_chunks",
    ) -> List[Dict[str, Any]]:
        """Hybrid archive search: summary vectors + chunk vectors + keyword boosts."""
        query = (query or "").strip()
        if not query:
            return []

        query_vector = embed_texts([query])[0]
        summary_rows = self.search_summary_hits(
            query_vector, query, limit=limit, collection_name=summaries_collection
        )
        chunk_best = (
            self.search_chunk_hits(query_vector, limit=limit, collection_name=chunks_collection)
            if use_chunks
            else {}
        )
        return self.fuse_hybrid_results(
            summary_rows,
            chunk_best,
            query,
            limit=limit,
            lang=lang,
            use_chunks=use_chunks,
        )

    def prepare_chunk_records(self, documents: List[Any], parent_doc_id: str) -> List[Dict[str, Any]]:
        records = []
        for document in documents:
            records.append(
                {
                    "id": uuid.uuid4(),
                    "text": document.text,
                    "metadata": {**document.metadata, "parent_doc_id": parent_doc_id},
                }
            )
        return records
