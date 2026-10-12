import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Union

from qdrant_client import QdrantClient
from qdrant_client.http import models as rest

from .config import load_environment, resolve_qdrant_settings

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

SUMMARIES_COLLECTION = "document_summaries"
CHUNKS_COLLECTION = "document_chunks"
LOGICAL_COLLECTIONS = (SUMMARIES_COLLECTION, CHUNKS_COLLECTION)
_ROUTED_METHODS = frozenset(
    {
        "upsert",
        "query_points",
        "query_points_groups",
        "search_groups",
        "retrieve",
        "scroll",
        "count",
        "delete",
        "set_payload",
        "search",
        "create_payload_index",
    }
)
# Only what search reads; summary payloads also carry people, finance notes, etc.
SUMMARY_HIT_FIELDS = ["doc_id", "document_id", "doc_type", "searchable_keywords", "major_projects"]
CHUNK_HIT_FIELDS = ["parent_doc_id", "document_id", "doc_id", "page_number", "text"]


def physical_collection_name(logical: str, model: str, dim: int) -> str:
    """``document_chunks`` + ``bge-m3`` + 1024 -> ``document_chunks__bge-m3__1024``."""
    slug = re.sub(r"[^a-z0-9]+", "-", model.lower().split("/")[-1]).strip("-") or "model"
    return f"{logical}__{slug}__{int(dim)}"


def active_embedding_space() -> tuple[str, int]:
    from .ai import embed_route

    route = embed_route()
    return route.model, int(route.params["dim"])


def _env_int(name: str) -> Optional[int]:
    raw = (os.getenv(name) or "").strip()
    return int(raw) if raw else None


def hnsw_config(*, explicit_only: bool = False) -> Optional[rest.HnswConfigDiff]:
    """HNSW graph settings for new collections (m=16, ef_construct=128 unless overridden).

    Higher m / ef_construct raise recall and memory; ``explicit_only`` returns None unless
    QDRANT_HNSW_* is set, so existing collections are only rebuilt on purpose.
    """
    m, ef = _env_int("QDRANT_HNSW_M"), _env_int("QDRANT_HNSW_EF_CONSTRUCT")
    if explicit_only and m is None and ef is None:
        return None
    return rest.HnswConfigDiff(m=m or 16, ef_construct=ef or 128)


def search_hnsw_ef() -> Optional[int]:
    """Query-time beam width; unset keeps Qdrant's default (ef_construct)."""
    return _env_int("QDRANT_SEARCH_HNSW_EF")


def upsert_batch_size() -> int:
    return max(1, _env_int("QDRANT_UPSERT_BATCH") or 32)


class _RoutedClient:
    """QdrantClient proxy that maps logical collection names to this process's physical one."""

    def __init__(self, client: QdrantClient, resolve):
        self._client = client
        self._resolve = resolve

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if name not in _ROUTED_METHODS or not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            if "collection_name" in kwargs:
                kwargs["collection_name"] = self._resolve(kwargs["collection_name"])
            return attr(*args, **kwargs)

        return call


def embed_texts(
    inputs: List[str], model: Optional[str] = None, *, sensitivity: Optional[str] = None
) -> List[List[float]]:
    from . import ai
    from .ai.routing import Route

    route = ai.embed_route()
    if model and model != route.model:
        route = Route(task="embed", provider=route.provider, model=model, params=route.params)
    return ai.embed(inputs, sensitivity=sensitivity, route=route)


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
        self._known: Optional[set[str]] = None
        self._known_at = 0.0
        self._checked: set[tuple[str, int]] = set()
        # Older qdrant-client versions forward unknown kwargs into httpx.Client,
        # so do not pass check_compatibility here.
        self.client = self._new_client()

    def _new_client(self) -> Any:
        if self._url == ":memory:":
            raw = QdrantClient(location=":memory:")
        else:
            raw = QdrantClient(
                url=self._url,
                api_key=self._api_key,
                timeout=60,
            )
        return _RoutedClient(raw, self.resolve_collection)

    def _reconnectable(self) -> bool:
        return self._url != ":memory:"

    @property
    def raw_client(self) -> QdrantClient:
        return self.client._client

    def _known_names(self, *, refresh: bool = False) -> set[str]:
        """Collection and alias names, cached for a minute."""
        if refresh or self._known is None or time.monotonic() - self._known_at > 60:
            names = {c.name for c in self.raw_client.get_collections().collections}
            try:
                names |= {a.alias_name for a in self.raw_client.get_aliases().aliases}
            except Exception:
                pass
            self._known = names
            self._known_at = time.monotonic()
        return self._known

    def resolve_collection(self, name: str) -> str:
        """Physical collection for this process's embedding model, else the logical name."""
        if name not in LOGICAL_COLLECTIONS:
            return name
        try:
            model, dim = active_embedding_space()
            physical = physical_collection_name(name, model, dim)
            return physical if physical in self._known_names() else name
        except Exception:
            return name

    def aliases(self) -> Dict[str, str]:
        return {a.alias_name: a.collection_name for a in self.raw_client.get_aliases().aliases}

    def collection_vector_size(self, name: str) -> Optional[int]:
        vectors = self.raw_client.get_collection(name).config.params.vectors
        size = getattr(vectors, "size", None)
        if size is None and isinstance(vectors, dict) and vectors:
            size = getattr(next(iter(vectors.values())), "size", None)
        return int(size) if size else None

    def ensure_physical_collection(self, name: str, vector_size: int) -> bool:
        """Create ``name`` if missing. True when it was created."""
        if name in self._known_names(refresh=True):
            return False
        self.raw_client.create_collection(
            collection_name=name,
            vectors_config=rest.VectorParams(size=vector_size, distance=rest.Distance.COSINE),
            hnsw_config=hnsw_config(),
        )
        self._known_names(refresh=True)
        return True

    def apply_hnsw_tuning(self, name: str) -> None:
        """Push QDRANT_HNSW_* to an existing collection; Qdrant rebuilds the graph in the background."""
        config = hnsw_config(explicit_only=True)
        if config is None:
            return
        try:
            self.raw_client.update_collection(collection_name=self.resolve_collection(name), hnsw_config=config)
        except Exception as exc:
            logger.warning("HNSW tuning not applied to %s: %s", name, exc)

    def point_alias(self, alias: str, collection: str) -> None:
        """Atomically (re)point ``alias`` at ``collection``."""
        ops: List[Any] = []
        if alias in self.aliases():
            ops.append(rest.DeleteAliasOperation(delete_alias=rest.DeleteAlias(alias_name=alias)))
        ops.append(
            rest.CreateAliasOperation(create_alias=rest.CreateAlias(collection_name=collection, alias_name=alias))
        )
        self.raw_client.update_collection_aliases(change_aliases_operations=ops)
        self._known_names(refresh=True)

    def _reconnect(self) -> None:
        if not self._reconnectable():
            return
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

    def ensure_payload_indexes(self, collection: str, fields: Dict[str, Any]) -> None:
        """Best-effort keyword/integer indexes so payload filters stay fast; existing ones are kept."""
        for field_name, schema in fields.items():
            try:
                self.client.create_payload_index(
                    collection_name=collection, field_name=field_name, field_schema=schema, wait=False
                )
            except Exception as exc:
                if "already exists" not in str(exc).lower():
                    logger.debug("Payload index %s.%s not created: %s", collection, field_name, exc)

    def delete_document_chunks(self, doc_id: str, collection_name: str = CHUNKS_COLLECTION) -> None:
        """Remove every chunk of ``doc_id`` before indexing a new version of it."""
        selector = rest.FilterSelector(
            filter=rest.Filter(
                must=[rest.FieldCondition(key="parent_doc_id", match=rest.MatchValue(value=doc_id))]
            )
        )
        try:
            self.client.delete(collection_name=collection_name, points_selector=selector, wait=True)
        except Exception as exc:
            if "not found" in str(exc).lower() or "404" in str(exc):
                return
            raise

    def _query_points(self, **kwargs: Any) -> Any:
        """Query Qdrant, reconnecting once after a dropped connection."""
        ef = search_hnsw_ef()
        if ef and "search_params" not in kwargs:
            kwargs["search_params"] = rest.SearchParams(hnsw_ef=ef)
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
        summaries_collection: str = SUMMARIES_COLLECTION,
        chunks_collection: str = CHUNKS_COLLECTION,
        vector_size: Optional[int] = None,
    ) -> None:
        """Make sure the collections for the active embedding model exist.

        Fresh installs get ``{logical}__{model}__{dim}`` plus an alias at the logical
        name. A pre-versioning collection at the logical name keeps serving as long as
        its dimension matches; a mismatch means the model changed without a reindex.
        """
        model, dim = active_embedding_space()
        size = int(vector_size or dim)
        if all((name, size) in self._checked for name in (summaries_collection, chunks_collection)):
            return
        try:
            existing = self._known_names(refresh=True)
        except Exception as exc:
            message = str(exc)
            if "404" in message or "Not Found" in message:
                raise RuntimeError(
                    "Qdrant Cloud returned 404. Check Railway QDRANT_URL uses https:// "
                    "(not http://) e.g. https://YOUR-CLUSTER.us-east-1-1.aws.cloud.qdrant.io:6333 "
                    "and QDRANT_API_KEY is set."
                ) from exc
            raise

        for logical in (summaries_collection, chunks_collection):
            if logical not in LOGICAL_COLLECTIONS:
                if logical not in existing:
                    self.ensure_physical_collection(logical, size)
                self._checked.add((logical, size))
                continue
            physical = physical_collection_name(logical, model, size)
            if physical in existing:
                self._checked.add((logical, size))
                continue
            if logical in existing:
                current = self.collection_vector_size(logical)
                if current and current != size:
                    raise RuntimeError(
                        f"Qdrant collection {logical!r} holds {current}-d vectors but embedding model "
                        f"{model!r} produces {size}-d. Run scripts/reindex_embeddings.py before switching "
                        "AI_EMBED_MODEL."
                    )
                self._checked.add((logical, size))
                continue
            self.ensure_physical_collection(physical, size)
            self.point_alias(logical, physical)
            logger.info("Created Qdrant collection %s (alias %s)", physical, logical)
            self._checked.add((logical, size))
        from .search_acl import payload_index_schema

        keyword = rest.PayloadSchemaType.KEYWORD
        self.ensure_payload_indexes(summaries_collection, {"doc_id": keyword, **payload_index_schema()})
        self.ensure_payload_indexes(chunks_collection, {"parent_doc_id": keyword, **payload_index_schema()})
        for name in (summaries_collection, chunks_collection):
            self.apply_hnsw_tuning(name)

    def set_search_payload(self, doc_id: str, payload: Dict[str, Any]) -> None:
        """Overwrite the ACL/metadata fields on every vector of ``doc_id`` (summary and chunks)."""
        for collection, key in ((SUMMARIES_COLLECTION, "doc_id"), (CHUNKS_COLLECTION, "parent_doc_id")):
            selector = rest.Filter(must=[rest.FieldCondition(key=key, match=rest.MatchValue(value=doc_id))])
            try:
                self.client.set_payload(collection_name=collection, payload=payload, points=selector, wait=True)
            except Exception as exc:
                if "not found" in str(exc).lower() or "404" in str(exc):
                    continue
                raise

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
        self._upsert_points(collection_name, points, batch_size=upsert_batch_size())

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
        query_filter: Any = None,
    ) -> List[Dict[str, Any]]:
        query_lower = (query or "").casefold()
        try:
            response = self._query_points(
                collection_name=collection_name,
                query=query_vector,
                query_filter=query_filter,
                limit=max(limit * 5, 40),
                with_payload=SUMMARY_HIT_FIELDS,
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
        query_filter: Any = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Best page-text chunk per document. Independent from summary search.

        Groups by document inside Qdrant so only one chunk per document crosses the
        network; falls back to over-fetching when the server cannot group.
        """
        chunk_best: Dict[str, Dict[str, Any]] = {}
        points: List[Any] = []
        grouped = dict(
            collection_name=collection_name,
            query_filter=query_filter,
            group_by="parent_doc_id",
            group_size=1,
            limit=limit,
            with_payload=CHUNK_HIT_FIELDS,
        )
        try:
            if hasattr(self.raw_client, "query_points_groups"):
                groups = self.client.query_points_groups(query=query_vector, **grouped)
            else:
                groups = self.client.search_groups(query_vector=query_vector, **grouped)
            points = [group.hits[0] for group in groups.groups if group.hits]
        except Exception as exc:
            logger.info("Grouped chunk search unavailable (%s); over-fetching", exc)
            try:
                response = self._query_points(
                    collection_name=collection_name,
                    query=query_vector,
                    query_filter=query_filter,
                    limit=max(limit * 8, 60),
                    with_payload=CHUNK_HIT_FIELDS,
                )
                points = response.points if getattr(response, "points", None) is not None else []
            except Exception as exc2:
                logger.warning("Chunk vector search failed: %s", exc2)
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

    def search_passages(
        self,
        query_vector: List[float],
        *,
        limit: int = 12,
        query_filter: Any = None,
        collection_name: str = CHUNKS_COLLECTION,
    ) -> List[Dict[str, Any]]:
        """Top page-text chunks with full text, for grounded answers."""
        response = self._query_points(
            collection_name=collection_name,
            query=query_vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        passages = []
        for point in getattr(response, "points", None) or []:
            payload = getattr(point, "payload", None) or {}
            parent = payload.get("parent_doc_id") or payload.get("document_id")
            if not parent:
                continue
            passages.append(
                {
                    "doc_id": str(parent),
                    "page": payload.get("page_number"),
                    "text": str(payload.get("text") or ""),
                    "score": float(getattr(point, "score", 0) or 0),
                }
            )
        return passages

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
                    "page": chunk_row.get("page_number"),
                }
            else:
                entry = by_id[doc_id]
                if entry.get("page") is None:
                    entry["page"] = chunk_row.get("page_number")
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
