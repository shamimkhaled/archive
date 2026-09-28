"""Concrete adapters wrapping today's OpenAI / Qdrant / S3 / Resend integrations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from .. import aws_utils
from ..notifications import send_meeting_email as _send_meeting_email
from ..qdrant_store import embed_texts as _embed_texts
from ..qdrant_store import make_qdrant_indexer
from ..summary_extractor import DocumentSummary, extract_document_summary as _extract_document_summary


class OpenAILlmAdapter:
    """LLM port backed by OpenAI / OpenRouter (embeddings + summary chat)."""

    def embed_texts(self, inputs: List[str], model: Optional[str] = None) -> List[List[float]]:
        return _embed_texts(inputs, model=model)

    def extract_document_summary(self, text: str) -> DocumentSummary:
        return _extract_document_summary(text)

    def chat_json(self, *, system: str, user: str, temperature: float = 0.0) -> Dict[str, Any]:
        """Chat completion that must return a JSON object."""
        from ..config import openai_chat_model, resolve_openai_credentials
        from ..summary_extractor import _extract_json
        import json

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai package required") from exc
        api_key, base_url = resolve_openai_credentials()
        client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=openai_chat_model(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=1200,
        )
        raw = response.choices[0].message.content or ""
        payload = _extract_json(raw)
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            from ..summary_extractor import _repair_incomplete_json

            parsed = json.loads(_repair_incomplete_json(payload))
        if not isinstance(parsed, dict):
            raise ValueError("Model did not return a JSON object")
        return parsed



class QdrantVectorStoreAdapter:
    """Vector store port wrapping the shared QdrantIndexer singleton."""

    def __init__(self) -> None:
        self._indexer = make_qdrant_indexer()

    def create_collections(self) -> None:
        self._indexer.create_collections()

    def prepare_chunk_records(self, child_documents: List[Any], parent_doc_id: str) -> List[Dict[str, Any]]:
        return self._indexer.prepare_chunk_records(child_documents, parent_doc_id=parent_doc_id)

    def upload_chunks(self, chunks: List[Dict[str, Any]], vectors: List[List[float]]) -> None:
        self._indexer.upload_chunks(chunks=chunks, vectors=vectors)

    def upload_summary(self, summary_id: str, payload: Dict[str, Any], vector: List[float]) -> None:
        self._indexer.upload_summary(summary_id=summary_id, payload=payload, vector=vector)

    @property
    def indexer(self):
        """Escape hatch for search/graph code still on QdrantIndexer APIs."""
        return self._indexer


class ObjectStorageAdapter:
    def store_pdf(self, local_path: str, key: str) -> str:
        return aws_utils.store_pdf(local_path, key)

    def use_local_storage(self) -> bool:
        return aws_utils.use_local_storage()

    def storage_backend_name(self) -> str:
        return aws_utils.storage_backend_name()

    def probe_s3(self) -> dict:
        return aws_utils.probe_s3()


class ResendEmailAdapter:
    def send_meeting_email(
        self,
        kind: str,
        to_username: str,
        to_email: Optional[str],
        meeting_title: str,
        scheduled_at: datetime,
        location: str,
        agenda: str,
        ics_content: str,
        google_calendar_link: str,
    ) -> bool:
        return _send_meeting_email(
            kind,  # type: ignore[arg-type]
            to_username,
            to_email,
            meeting_title,
            scheduled_at,
            location,
            agenda,
            ics_content,
            google_calendar_link,
        )


_llm: Optional[OpenAILlmAdapter] = None
_vector: Optional[QdrantVectorStoreAdapter] = None
_storage: Optional[ObjectStorageAdapter] = None
_email: Optional[ResendEmailAdapter] = None


def get_llm_client() -> OpenAILlmAdapter:
    global _llm
    if _llm is None:
        _llm = OpenAILlmAdapter()
    return _llm


def get_vector_store() -> QdrantVectorStoreAdapter:
    global _vector
    if _vector is None:
        _vector = QdrantVectorStoreAdapter()
    return _vector


def get_object_storage() -> ObjectStorageAdapter:
    global _storage
    if _storage is None:
        _storage = ObjectStorageAdapter()
    return _storage


def get_email_client() -> ResendEmailAdapter:
    global _email
    if _email is None:
        _email = ResendEmailAdapter()
    return _email


def reset_adapters_for_tests() -> None:
    """Clear cached adapters (unit tests only)."""
    global _llm, _vector, _storage, _email
    _llm = None
    _vector = None
    _storage = None
    _email = None
