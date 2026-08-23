from importlib import import_module
from typing import Any

__all__ = [
    "Document",
    "parse_pdf",
    "DocumentSummary",
    "extract_document_summary",
    "chunk_documents",
    "QdrantIndexer",
]


def __getattr__(name: str) -> Any:
    if name in {"Document", "parse_pdf"}:
        module = import_module(".pdf_parser", __name__)
        return getattr(module, name)
    if name in {"DocumentSummary", "extract_document_summary"}:
        module = import_module(".summary_extractor", __name__)
        return getattr(module, name)
    if name == "chunk_documents":
        module = import_module(".chunker", __name__)
        return getattr(module, name)
    if name == "QdrantIndexer":
        module = import_module(".qdrant_store", __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
