"""Words inside documents stay findable, with their page, when the embedding provider is down."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcp_project.search_acl import AclScope
from bcp_project.services import search as search_service
from bcp_project.services.ask_sonali import _page_for_quote

ADMIN = AclScope(unrestricted=True, role="admin")
MEMBER = AclScope(unrestricted=False, role="board_member")


@pytest_asyncio.fixture
async def maker(tmp_path):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from bcp_project.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pages.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def _doc(doc_id, **kw):
    from bcp_project.models import DocumentRecord

    return DocumentRecord(
        doc_id=doc_id,
        doc_date=date(2026, 3, 1),
        doc_type=kw.pop("doc_type", "Circular"),
        keywords=[],
        file_location=f"/tmp/{doc_id}.pdf",
        uploaded_by="alice",
        title=kw.pop("title", f"Title of {doc_id}"),
        **kw,
    )


def _page(doc_id, number, text, version=1):
    from bcp_project.models import DocumentPage

    return DocumentPage(doc_id=doc_id, version=version, page_number=number, text=text)


@pytest_asyncio.fixture
async def archive(maker):
    async with maker() as db:
        db.add_all(
            [
                _doc("SB-OPEN-1", current_version=2),
                _doc("SB-SECRET-1", sensitivity="restricted"),
                _doc("SB-PENDING-1", verification_status="pending"),
                _page("SB-OPEN-1", 1, "Old draft mentioning the crop loan rate.", version=1),
                _page("SB-OPEN-1", 1, "Introduction to agricultural credit.", version=2),
                _page("SB-OPEN-1", 2, "The crop loan interest rate is reduced to 8 percent for farmers.", version=2),
                _page("SB-OPEN-1", 3, "Annex: tables.", version=2),
                _page("SB-OPEN-1", 4, "Repayment of a crop loan is due after harvest.", version=2),
                _page("SB-SECRET-1", 1, "Confidential crop loan write-off."),
                _page("SB-PENDING-1", 1, "Unreviewed crop loan scan."),
            ]
        )
        await db.commit()
    return maker


@pytest.fixture
def vectors_down(monkeypatch):
    monkeypatch.setattr(search_service, "_vector_hits", AsyncMock(return_value=(None, [], {}, False)))
    monkeypatch.setattr(search_service, "_rerank", AsyncMock(return_value=False))


@pytest.mark.asyncio
async def test_keyword_search_finds_words_inside_pages_with_their_page(archive, vectors_down):
    async with archive() as db:
        outcome = await search_service.hybrid_search(db, acl=ADMIN, q="crop loan", use_cache=False)
    by_id = {r["doc_id"]: r for r in outcome.results}
    assert outcome.mode == "keyword" and outcome.degraded

    hit = by_id["SB-OPEN-1"]
    assert hit["pages"] == [2, 4] and hit["pages_matched"] == 2
    assert hit["page"] == 2
    assert "chunk" in hit["match_reasons"]
    assert "crop loan interest rate" in hit["snippet"]
    assert hit["view_url"] == "/view/SB-OPEN-1?page=2&hl=crop%20loan"
    # A document waiting for review is never searchable.
    assert "SB-PENDING-1" not in by_id


@pytest.mark.asyncio
async def test_page_text_search_respects_access_and_needs_every_word(archive, vectors_down):
    async with archive() as db:
        member = await search_service.hybrid_search(db, acl=MEMBER, q="crop loan", use_cache=False)
        both_words = await search_service.hybrid_search(db, acl=ADMIN, q="loan harvest", use_cache=False)
        phrase_only = await search_service.hybrid_search(db, acl=ADMIN, q="farmers zebra", use_cache=False)
    assert {r["doc_id"] for r in member.results} == {"SB-OPEN-1"}
    assert both_words.results[0]["pages"] == [4]
    assert phrase_only.results == []


@pytest.mark.asyncio
async def test_ask_passages_fall_back_to_page_text(archive, monkeypatch):
    def no_qdrant():
        raise RuntimeError("402 Insufficient credits")

    monkeypatch.setattr("bcp_project.qdrant_store.make_qdrant_indexer", no_qdrant)
    monkeypatch.setattr(search_service, "_rerank", AsyncMock(return_value=False))
    async with archive() as db:
        passages = await search_service.grounded_passages(
            MEMBER, "What is the crop loan interest rate?", ["SB-OPEN-1", "SB-SECRET-1"], db=db
        )
        without_db = await search_service.grounded_passages(MEMBER, "crop loan", ["SB-OPEN-1"])
    assert passages[0]["doc_id"] == "SB-OPEN-1" and passages[0]["page"] == 2
    assert all(p["doc_id"] != "SB-SECRET-1" for p in passages)
    assert without_db == []


def test_citation_page_is_filled_from_the_passage_holding_the_quote():
    sources = [
        {"doc_id": "SB-1", "page": 2, "text": "The crop loan  interest rate is 8 percent."},
        {"doc_id": "SB-1", "page": 4, "text": "Repayment after harvest."},
        {"doc_id": "SB-2", "page": 7, "text": "Single page source."},
    ]
    assert _page_for_quote(sources, "SB-1", "crop loan interest rate") == 2
    assert _page_for_quote(sources, "SB-1", "not in any passage") is None
    assert _page_for_quote(sources, "SB-2", "") == 7


def test_citation_url_carries_highlight():
    assert search_service.citation_url("SB/1", 3, "  Tk 12   crore ") == "/view/SB%2F1?page=3&hl=Tk%2012%20crore"
    assert search_service.citation_url("SB-1", None) == "/view/SB-1"


@pytest.mark.asyncio
async def test_embedding_outage_archives_document_and_backfill_builds_vectors(maker, monkeypatch, vectors_down):
    from sqlalchemy import select

    import bcp_project.cache as cache
    from bcp_project.models import DocumentRecord, DocumentVersion
    from bcp_project.services import ingest

    monkeypatch.setenv("AI_EGRESS", "internal_only")
    for name in ("LOCAL_LLM_BASE_URL", "LOCAL_EMBED_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cache, "bump_search_cache_version", AsyncMock())
    monkeypatch.setattr(
        "bcp_project.qdrant_store.make_qdrant_indexer",
        lambda: type("Q", (), {"create_collections": lambda self: None, "delete_document_chunks": lambda self, d: None})(),
    )
    outage = {"on": True}
    chunked = []

    def summary_vector(*args, **kwargs):
        if outage["on"]:
            raise RuntimeError("Error code: 402 - Insufficient credits")
        return True

    monkeypatch.setattr(ingest, "index_summary_vector", summary_vector)
    monkeypatch.setattr("bcp_project.jobs.handlers.run_chunk_index", lambda doc_id, pages, **kw: chunked.append(doc_id) or 1)

    async with maker() as db:
        document = _doc("SB-2026-022", verification_status="pending", ingest_status="indexing")
        db.add_all([document, _page("SB-2026-022", 1, "Invoice for March printing.")])
        await db.flush()
        ver = await ingest.version_row(db, document, 1)
        await ingest.index_version(db, document, ver, await ingest.load_pages(db, "SB-2026-022", 1))
        await db.commit()

    async with maker() as db:
        doc = (await db.execute(select(DocumentRecord))).scalar_one()
        assert doc.ingest_status == "indexed" and doc.verification_status == "approved"
        assert doc.summary_json["_ai"]["vectors"] == "pending"
        assert "402" in doc.summary_json["_ai"]["vector_error"]
        assert (await db.execute(select(DocumentVersion))).scalar_one().status == "approved"
        found = await search_service.hybrid_search(db, acl=ADMIN, q="invoice", use_cache=False)
    assert [r["doc_id"] for r in found.results] == ["SB-2026-022"] and found.results[0]["page"] == 1

    assert await ingest.backfill_pending_vectors(session_factory=maker) == 0
    assert chunked == []

    outage["on"] = False
    assert await ingest.backfill_pending_vectors(session_factory=maker) == 1
    assert chunked == ["SB-2026-022"]
    async with maker() as db:
        doc = (await db.execute(select(DocumentRecord))).scalar_one()
        assert doc.summary_json["_ai"]["vectors"] == "indexed"
        assert "vector_error" not in doc.summary_json["_ai"]
    assert await ingest.backfill_pending_vectors(session_factory=maker) == 0
