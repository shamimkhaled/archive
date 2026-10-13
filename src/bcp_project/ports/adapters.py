"""Concrete adapters wrapping today's OpenAI / Qdrant / S3 / Resend integrations."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from .. import aws_utils
from ..notifications import send_meeting_email as _send_meeting_email
from ..qdrant_store import embed_texts as _embed_texts
from ..qdrant_store import make_qdrant_indexer
from ..summary_extractor import DocumentSummary, extract_document_summary as _extract_document_summary
from ..summary_extractor import _remove_trailing_commas, _repair_incomplete_json, _strip_code_fence

logger = logging.getLogger("bcp_project.ports.adapters")


def _parse_json_object(raw: str) -> Dict[str, Any]:
    """The JSON object in a model reply, closing it when the reply was cut off mid-object."""
    text = _strip_code_fence(raw or "")
    start = text.find("{")
    if start < 0:
        raise ValueError("Could not extract JSON object from model output")
    end = text.rfind("}")
    candidate = text[start : end + 1] if end > start else text[start:]
    try:
        parsed = json.loads(_remove_trailing_commas(candidate))
    except json.JSONDecodeError:
        try:
            parsed = json.loads(_repair_incomplete_json(text[start:]))
        except json.JSONDecodeError as exc:
            raise ValueError("Could not parse JSON object from model output") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Model did not return a JSON object")
    return parsed


class OpenAILlmAdapter:
    """LLM port over the ``ai`` package (OpenAI, OpenRouter, or a local OpenAI-compatible server)."""

    def embed_texts(
        self, inputs: List[str], model: Optional[str] = None, *, sensitivity: Optional[str] = None
    ) -> List[List[float]]:
        return _embed_texts(inputs, model=model, sensitivity=sensitivity)

    def extract_document_summary(self, text: str, *, sensitivity: Optional[str] = None) -> DocumentSummary:
        return _extract_document_summary(text, sensitivity=sensitivity)

    def chat_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.0,
        task: str = "ask",
        sensitivity: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Chat completion that must return a JSON object; one retry when the reply is unreadable."""
        from .. import ai

        # Reasoning models spend part of the budget thinking; too small a cap leaves the JSON empty or cut off.
        max_tokens = int(os.getenv("AI_JSON_MAX_TOKENS") or 4000)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        error: Exception = ValueError("Model did not return a JSON object")
        for _ in range(2):
            raw = ai.chat(
                task, messages, sensitivity=sensitivity, temperature=temperature, max_tokens=max_tokens, json_mode=True
            )
            try:
                return _parse_json_object(raw)
            except ValueError as exc:
                error = exc
                logger.warning("Unreadable JSON from the %s model (%d chars); retrying once", task, len(raw or ""))
        raise error



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
