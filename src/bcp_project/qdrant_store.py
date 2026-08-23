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


def make_qdrant_indexer() -> "QdrantIndexer":
    url, api_key = resolve_qdrant_settings()
    return QdrantIndexer(url=url, api_key=api_key)


class QdrantIndexer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 6333,
        api_key: Optional[str] = None,
        url: Optional[str] = None,
    ):
        resolved_url = url or f"http://{host}:{port}"
        self.client = QdrantClient(url=resolved_url, api_key=api_key, check_compatibility=False)

    def create_collections(
        self,
        summaries_collection: str = "document_summaries",
        chunks_collection: str = "document_chunks",
        vector_size: int = 1536,
    ) -> None:
        summary_config = rest.VectorParams(size=vector_size, distance=rest.Distance.COSINE)
        chunk_config = rest.VectorParams(size=vector_size, distance=rest.Distance.COSINE)

        if not self.client.collection_exists(collection_name=summaries_collection):
            self.client.create_collection(collection_name=summaries_collection, vectors_config=summary_config)

        if not self.client.collection_exists(collection_name=chunks_collection):
            self.client.create_collection(collection_name=chunks_collection, vectors_config=chunk_config)

    def _normalize_id(self, point_id: str) -> Union[int, uuid.UUID]:
        try:
            return normalize_doc_point_id(point_id)
        except (TypeError, ValueError):
            return uuid.uuid4()

    def _normalize_point_id(self, point_id: Union[str, int, uuid.UUID]) -> Union[int, uuid.UUID]:
        """Stable Qdrant point IDs for business doc_ids like SB-2026-001."""
        return normalize_doc_point_id(point_id)

    def get_summary_by_doc_id(
        self,
        doc_id: str,
        collection_name: str = "document_summaries",
    ) -> Optional[Any]:
        """Retrieve a summary point by archive doc_id payload or point id."""
        normalized_id = self._normalize_point_id(doc_id)
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
    ) -> List[Dict[str, Any]]:
        try:
            point = self.get_summary_by_doc_id(doc_id, collection_name=collection_name)
            if point is None:
                return []

            vector = self._point_vector(point)
            if not vector:
                return []

            response = self.client.query_points(
                collection_name=collection_name,
                query=vector,
                limit=max(limit * 3, 24),
                with_payload=True,
            )
        except Exception:
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
        normalized_id = self._normalize_point_id(summary_id)
        self.client.upsert(
            collection_name=collection_name,
            points=[rest.PointStruct(id=normalized_id, vector=vector, payload=payload)],
        )

    def upload_chunks(
        self,
        chunks: List[Dict[str, Any]],
        vectors: List[List[float]],
        collection_name: str = "document_chunks",
    ) -> None:
        points = []
        for chunk, vector in zip(chunks, vectors):
            normalized_id = self._normalize_id(chunk["id"])
            payload = {"text": chunk["text"], **chunk["metadata"]}
            payload.setdefault("document_id", chunk["metadata"].get("parent_doc_id"))
            points.append(
                rest.PointStruct(
                    id=normalized_id,
                    vector=vector,
                    payload=payload,
                )
            )
        self.client.upsert(collection_name=collection_name, points=points)

    def _normalize_query_response(self, point: Any, query_lower: str) -> Dict[str, Any]:
        """Init-commit labeling: keyword/project substring match vs summary hit."""
        payload = getattr(point, "payload", None) or {}
        doc_id = payload.get("document_id") or payload.get("parent_doc_id") or str(point.id)
        keywords = payload.get("searchable_keywords", []) or []
        projects = payload.get("major_projects", []) or []

        keyword_hit = any(query_lower in str(kw).lower() for kw in keywords)
        project_hit = any(
            query_lower in str(project.get("project_name", "")).lower()
            or query_lower in str(project.get("brief_context", "")).lower()
            for project in projects
            if isinstance(project, dict)
        )

        return {
            "id": str(point.id),
            "doc_id": doc_id,
            "doc_type": payload.get("doc_type", "Unknown"),
            "searchable_keywords": keywords,
            "major_projects": projects,
            "score": getattr(point, "score", None),
            "source": "keyword" if (keyword_hit or project_hit) else "summary",
        }

    def search_documents(
        self,
        query: str,
        limit: int = 10,
        collection_name: str = "document_summaries",
        lang: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Init-commit search: embed query → summary vectors → prefer keyword hits."""
        query = query.strip()
        if not query:
            return []

        query_vector = embed_texts([query])[0]
        query_lower = query.lower()

        # Pull a wider candidate pool via vector similarity, then evaluate
        # keyword/project matches client-side (same as first github commit).
        response = self.client.query_points(
            collection_name=collection_name,
            query=query_vector,
            limit=max(limit * 5, 50),
            with_payload=True,
        )
        points = response.points if getattr(response, "points", None) is not None else []

        results = [self._normalize_query_response(point, query_lower) for point in points]

        priority = {"keyword": 0, "summary": 1}
        results.sort(key=lambda row: (priority.get(row["source"], 2), -(row["score"] or 0)))
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
        """Unused by Archive UI — kept for compatibility. Prefers summary-only init path."""
        return self.search_documents(query=query, limit=limit, collection_name=summaries_collection, lang=lang)

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
