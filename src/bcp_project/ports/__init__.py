"""Integration ports — swap adapters without coupling the app to vendors."""

from .adapters import (
    get_email_client,
    get_llm_client,
    get_object_storage,
    get_vector_store,
)
from .customer import get_customer_port
from .document_source import get_document_source
from .events import get_event_publisher, publish_event
from .identity import get_identity_port, oidc_enabled
from .registry import get_registry

__all__ = [
    "get_customer_port",
    "get_document_source",
    "get_email_client",
    "get_event_publisher",
    "get_identity_port",
    "get_llm_client",
    "get_object_storage",
    "get_registry",
    "get_vector_store",
    "oidc_enabled",
    "publish_event",
]
