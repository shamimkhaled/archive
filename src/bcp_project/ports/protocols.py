"""Port protocols for bank-system-integrator adapters."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class LlmPort(Protocol):
    def embed_texts(self, inputs: List[str], model: Optional[str] = None) -> List[List[float]]:
        ...

    def extract_document_summary(self, text: str) -> Any:
        ...

    def chat_json(self, *, system: str, user: str, temperature: float = 0.0) -> Dict[str, Any]:
        ...


@runtime_checkable
class VectorStorePort(Protocol):
    def create_collections(self) -> None:
        ...

    def prepare_chunk_records(self, child_documents: List[Any], parent_doc_id: str) -> List[Dict[str, Any]]:
        ...

    def upload_chunks(self, chunks: List[Dict[str, Any]], vectors: List[List[float]]) -> None:
        ...

    def upload_summary(self, summary_id: str, payload: Dict[str, Any], vector: List[float]) -> None:
        ...


@runtime_checkable
class ObjectStoragePort(Protocol):
    def store_pdf(self, local_path: str, key: str) -> str:
        ...

    def use_local_storage(self) -> bool:
        ...

    def storage_backend_name(self) -> str:
        ...

    def probe_s3(self) -> dict:
        ...


@runtime_checkable
class EmailPort(Protocol):
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
        ...


@runtime_checkable
class DocumentSourcePort(Protocol):
    """Pull documents from an external bank inbox without changing the source system."""

    def list_pending(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        ...

    def fetch_bytes(self, item_id: str) -> bytes:
        ...

    def mark_processed(self, item_id: str) -> None:
        ...


@runtime_checkable
class CustomerPort(Protocol):
    """Read-minimized customer summary from CBS / CIF facade."""

    def get_customer_summary(self, customer_ref: str) -> Optional[Dict[str, Any]]:
        ...


@runtime_checkable
class IdentityPort(Protocol):
    def mode(self) -> str:
        ...

    def authorization_url(self, *, state: str, redirect_uri: str) -> Optional[str]:
        ...

    def exchange_code(self, *, code: str, redirect_uri: str) -> Optional[Dict[str, Any]]:
        ...


@runtime_checkable
class EventPublisherPort(Protocol):
    def publish(self, event_type: str, payload: Dict[str, Any]) -> bool:
        ...
