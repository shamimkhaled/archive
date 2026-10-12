"""Phase 5: ACL inside the vector query, metadata filters, rerank, citation deep links."""

from __future__ import annotations

import sys
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcp_project.ai.providers import _tokens
from bcp_project.models import Role, User
from bcp_project.policy import principal_from_user
from bcp_project.search_acl import (
    CHUNK_ID_FIELD,
    SUMMARY_ID_FIELD,
    AclScope,
    SearchFilters,
    build_filter,
    search_payload,
)
from bcp_project.services import ask_sonali
from bcp_project.services import search as search_service

DOCS = [
    SimpleNamespace(
        doc_id="SB-OPEN-1",
        doc_type="Circular",
        department="Credit",
        category="circular",
        reference_number="SB/CR/2026/01",
        doc_date=date(2026, 2, 3),
        sensitivity="internal",
        current_version=1,
        text="Crop loan interest rate reduced to 8 percent for farmers.",
    ),
    SimpleNamespace(
        doc_id="SB-SECRET-1",
        doc_type="Memo",
        department="Board Affairs",
        category="board_paper",
        reference_number=None,
        doc_date=date(2026, 3, 30),
        sensitivity="restricted",
        current_version=1,
        text="Confidential crop loan write-off of Tk 2,150 crore for Hallmark Group.",
    ),
]


@pytest.fixture
def memory_qdrant(monkeypatch):
    """In-memory Qdrant with both documents indexed exactly as ``index_version`` does."""
    import bcp_project.qdrant_store as qdrant_store
    from bcp_project.services.ingest import index_summary_vector

    monkeypatch.setenv("AI_EMBED_PROVIDER", "stub")
    monkeypatch.setenv("AI_EMBED_MODEL", "stub-embed")
    monkeypatch.setenv("AI_EMBED_DIM", "64")
    indexer = qdrant_store.QdrantIndexer(url=":memory:")
    monkeypatch.setattr(qdrant_store, "_indexer", indexer)
    indexer.create_collections()
    for doc in DOCS:
        acl = search_payload(doc)
        index_summary_vector(
            indexer,
            doc.doc_id,
            {"doc_id": doc.doc_id, "doc_type": doc.doc_type, "searchable_keywords": doc.text.split()[:6], **acl},
            doc.sensitivity,
        )
        vector = qdrant_store.embed_texts([doc.text])[0]
        indexer.upload_chunks(
            [
                {
                    "id": str(uuid.uuid5(uuid.NAMESPACE_URL, doc.doc_id)),
                    "text": doc.text,
                    "metadata": {**acl, "parent_doc_id": doc.doc_id, "page_number": 3},
                }
            ],
            [vector],
        )
    return indexer


def _query(indexer, acl, filters=None, q="crop loan"):
    from bcp_project.qdrant_store import embed_texts

    vector = embed_texts([q])[0]
    summaries = indexer.search_summary_hits(vector, q, limit=10, query_filter=build_filter(acl, filters, SUMMARY_ID_FIELD))
    chunks = indexer.search_chunk_hits(vector, limit=10, query_filter=build_filter(acl, filters, CHUNK_ID_FIELD))
    return {r["doc_id"] for r in summaries} | set(chunks)


def test_search_payload_restricts_roles():
    open_payload = search_payload(DOCS[0])
    secret_payload = search_payload(DOCS[1])
    assert "board_member" in open_payload["allowed_roles"]
    assert secret_payload["allowed_roles"] == ["admin", "board_secretary"]
    assert open_payload["department"] == "credit"
    assert open_payload["doc_day"] == 20260203


def test_restricted_vectors_hidden_from_board_member(memory_qdrant):
    member = AclScope(unrestricted=False, role="board_member")
    assert _query(memory_qdrant, member) == {"SB-OPEN-1"}


def test_unfiltered_query_would_leak(memory_qdrant):
    """Negative control: without the ACL filter the restricted vector does match."""
    assert "SB-SECRET-1" in _query(memory_qdrant, AclScope(unrestricted=True, role="admin"))


def test_grant_reveals_restricted_vector(memory_qdrant):
    granted = AclScope(unrestricted=False, role="board_member", granted_ids=("SB-SECRET-1",))
    assert _query(memory_qdrant, granted) == {"SB-OPEN-1", "SB-SECRET-1"}


def test_service_principal_sees_only_open(memory_qdrant):
    assert _query(memory_qdrant, AclScope(unrestricted=False)) == {"SB-OPEN-1"}


def test_metadata_filters_inside_vector_query(memory_qdrant):
    admin = AclScope(unrestricted=True, role="admin")
    assert _query(memory_qdrant, admin, SearchFilters.from_params(department=" CREDIT ")) == {"SB-OPEN-1"}
    assert _query(memory_qdrant, admin, SearchFilters.from_params(category="board_paper")) == {"SB-SECRET-1"}
    assert _query(memory_qdrant, admin, SearchFilters.from_params(date_from="2026-03-01")) == {"SB-SECRET-1"}
    assert _query(memory_qdrant, admin, SearchFilters.from_params(date_to="2026-02-28")) == {"SB-OPEN-1"}
    assert _query(memory_qdrant, admin, SearchFilters.from_params(reference_number="SB/CR/2026/01")) == {"SB-OPEN-1"}
    assert _query(memory_qdrant, admin, SearchFilters.from_params(doc_type="memo")) == {"SB-SECRET-1"}


def test_chunk_search_groups_per_document(memory_qdrant, monkeypatch):
    """One best chunk per document comes back from Qdrant itself, not by over-fetching."""
    from bcp_project.qdrant_store import embed_texts

    acl = search_payload(DOCS[0])
    texts = ["crop loan crop loan farmers", "unrelated treasury bonds"]
    memory_qdrant.upload_chunks(
        [
            {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"extra-{n}")),
                "text": text,
                "metadata": {**acl, "parent_doc_id": "SB-OPEN-1", "page_number": 10 + n},
            }
            for n, text in enumerate(texts)
        ],
        embed_texts(texts),
    )

    def no_overfetch(**_):
        raise AssertionError("fell back to over-fetching")

    monkeypatch.setattr(memory_qdrant, "_query_points", no_overfetch)
    vector = embed_texts(["crop loan farmers"])[0]
    best = memory_qdrant.search_chunk_hits(vector, limit=10)
    assert set(best) == {"SB-OPEN-1", "SB-SECRET-1"}
    assert best["SB-OPEN-1"]["page_number"] == 10


@pytest.mark.asyncio
async def test_grounded_passages_respect_acl_and_carry_page(memory_qdrant):
    member = AclScope(unrestricted=False, role="board_member")
    passages = await search_service.grounded_passages(member, "crop loan", ["SB-OPEN-1", "SB-SECRET-1"])
    assert [p["doc_id"] for p in passages] == ["SB-OPEN-1"]
    assert passages[0]["page"] == 3


def test_filters_validate_dates_and_scope_cache():
    with pytest.raises(ValueError):
        SearchFilters.from_params(date_from="03/01/2026")
    a = SearchFilters.from_params(department="Credit")
    b = SearchFilters.from_params(department="Treasury")
    assert a.cache_part() != b.cache_part()
    assert SearchFilters().cache_part() == SearchFilters.from_params().cache_part()
    plain = AclScope(unrestricted=False, role="board_member")
    granted = AclScope(unrestricted=False, role="board_member", granted_ids=("SB-SECRET-1",))
    assert plain.cache_part() != granted.cache_part()
    assert AclScope(unrestricted=True, role="admin").cache_part() == "all"


def test_citation_url():
    assert search_service.citation_url("SB-2026-001", 4) == "/view/SB-2026-001?page=4"
    assert search_service.citation_url("SB-2026-001", None) == "/view/SB-2026-001"
    assert search_service.citation_url("SB 1/2", 2).startswith("/view/SB%201%2F2")


def test_stub_tokenizer_keeps_bangla_words_whole():
    assert _tokens("সোনালী ব্যাংক বোর্ড") == ["সোনালী", "ব্যাংক", "বোর্ড"]


def test_rerank_text_handles_keywords():
    text = search_service._rerank_text({"title": "Budget", "searchable_keywords": ["profit", 2026], "snippet": "x"})
    assert "profit 2026" in text


@pytest.mark.asyncio
async def test_rerank_reorders_rows():
    rows = [{"doc_id": "A", "snippet": "a"}, {"doc_id": "B", "snippet": "b"}]
    with patch.object(search_service.ai, "rerank", return_value=[0.1, 0.9]):
        assert await search_service._rerank("q", rows, search_service._rerank_text) is True
    assert [r["doc_id"] for r in rows] == ["B", "A"]
    assert rows[0]["rerank_score"] == 0.9


@pytest.mark.asyncio
async def test_rerank_absent_keeps_fusion_order():
    rows = [{"doc_id": "A"}, {"doc_id": "B"}]
    with patch.object(search_service.ai, "rerank", return_value=None):
        assert await search_service._rerank("q", rows, search_service._rerank_text) is False
    assert [r["doc_id"] for r in rows] == ["A", "B"]


@pytest.mark.asyncio
async def test_ask_sonali_cites_pages_and_reports_model():
    principal = principal_from_user(User(id=1, username="admin1", hashed_password="x", role=Role.admin, is_active=True))
    evidence = {"results": [{"doc_id": "SB-1", "can_view": True, "snippet": "ICT", "page": 2, "doc_type": "minutes"}]}
    passages = [{"doc_id": "SB-1", "page": 5, "text": "The Board approved ICT procurement of Tk 12 crore.", "score": 0.9}]
    fake_llm = MagicMock()
    fake_llm.chat_json.return_value = {
        "refused": False,
        "answer": "The Board approved ICT procurement of Tk 12 crore.",
        "citations": [
            {"doc_id": "SB-1", "page": 5, "quote": "Tk 12 crore"},
            {"doc_id": "SB-OTHER", "page": 1, "quote": "not a source"},
        ],
    }
    with patch(
        "bcp_project.services.ask_sonali.document_query.search_documents_for_principal",
        new_callable=AsyncMock,
        return_value=evidence,
    ), patch(
        "bcp_project.services.ask_sonali.get_sensitivity_for_docs",
        new_callable=AsyncMock,
        return_value={"SB-1": "internal"},
    ), patch("bcp_project.services.ask_sonali.get_llm_client", return_value=fake_llm), patch(
        "bcp_project.services.ask_sonali.grounded_passages", new_callable=AsyncMock, return_value=passages
    ), patch(
        "bcp_project.services.ask_sonali.ai.last_call",
        return_value={"provider": "openrouter", "model": "qwen/qwen3", "source": "task:ask"},
    ), patch("bcp_project.services.ask_sonali.write_audit", new_callable=AsyncMock):
        result = await ask_sonali.ask_sonali_for_principal(AsyncMock(), principal, "ICT procurement?")

    assert result["refused"] is False
    assert result["citations"] == [
        {"doc_id": "SB-1", "page": 5, "quote": "Tk 12 crore", "url": "/view/SB-1?page=5"}
    ]
    assert result["evidence"][0]["url"] == "/view/SB-1?page=5"
    assert result["answered_by"] == {"provider": "openrouter", "model": "qwen/qwen3", "route": "task:ask"}
    assert "Tk 12 crore" in fake_llm.chat_json.call_args.kwargs["user"]
