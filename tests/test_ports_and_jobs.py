"""Unit tests for ports adapters and job queue helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcp_project.jobs.handlers import JOB_CHUNK_INDEX, dispatch_job, run_chunk_index
from bcp_project.jobs.queue import job_queue_enabled
from bcp_project.ports.adapters import (
    OpenAILlmAdapter,
    ObjectStorageAdapter,
    QdrantVectorStoreAdapter,
    ResendEmailAdapter,
    get_email_client,
    get_llm_client,
    get_object_storage,
    get_vector_store,
    reset_adapters_for_tests,
)
from bcp_project.ports.protocols import EmailPort, LlmPort, ObjectStoragePort, VectorStorePort


def test_job_queue_enabled_flag(monkeypatch):
    monkeypatch.delenv("ENABLE_JOB_QUEUE", raising=False)
    assert job_queue_enabled() is False
    monkeypatch.setenv("ENABLE_JOB_QUEUE", "1")
    assert job_queue_enabled() is True
    monkeypatch.setenv("ENABLE_JOB_QUEUE", "yes")
    assert job_queue_enabled() is True
    monkeypatch.setenv("ENABLE_JOB_QUEUE", "0")
    assert job_queue_enabled() is False


def test_adapter_factories_return_protocol_instances():
    reset_adapters_for_tests()
    llm = get_llm_client()
    store = get_vector_store()
    storage = get_object_storage()
    email = get_email_client()
    assert isinstance(llm, OpenAILlmAdapter)
    assert isinstance(store, QdrantVectorStoreAdapter)
    assert isinstance(storage, ObjectStorageAdapter)
    assert isinstance(email, ResendEmailAdapter)
    assert isinstance(llm, LlmPort)
    assert isinstance(store, VectorStorePort)
    assert isinstance(storage, ObjectStoragePort)
    assert isinstance(email, EmailPort)
    # Singletons
    assert get_llm_client() is llm
    reset_adapters_for_tests()


def test_run_chunk_index_skips_empty_pages():
    assert run_chunk_index("DOC-1", [{"text": "  ", "metadata": {}}]) == 0
    assert run_chunk_index("DOC-1", []) == 0


def test_run_chunk_index_uses_ports():
    page_payloads = [{"text": "Hello board meeting minutes.", "metadata": {"page": 1}}]
    fake_chunks = [{"text": "Hello board meeting minutes.", "chunk_id": "c1"}]
    fake_store = MagicMock()
    fake_store.prepare_chunk_records.return_value = fake_chunks
    fake_llm = MagicMock()
    fake_llm.embed_texts.return_value = [[0.1, 0.2]]

    with patch("bcp_project.jobs.handlers.get_vector_store", return_value=fake_store), patch(
        "bcp_project.jobs.handlers.get_llm_client", return_value=fake_llm
    ), patch(
        "bcp_project.jobs.handlers.chunk_documents",
        return_value=[MagicMock()],
    ):
        count = run_chunk_index("SB-1", page_payloads)

    assert count == 1
    fake_store.create_collections.assert_called_once()
    fake_store.upload_chunks.assert_called_once()
    fake_llm.embed_texts.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_job_chunk_index():
    with patch("bcp_project.jobs.handlers.run_chunk_index", return_value=2) as mocked:
        await dispatch_job(
            {
                "type": JOB_CHUNK_INDEX,
                "payload": {"doc_id": "D1", "page_payloads": [{"text": "x"}]},
            }
        )
        mocked.assert_called_once_with("D1", [{"text": "x"}])


@pytest.mark.asyncio
async def test_dispatch_job_unknown_type():
    with pytest.raises(ValueError, match="Unknown job type"):
        await dispatch_job({"type": "nope", "payload": {}})


@pytest.mark.asyncio
async def test_schedule_chunk_index_enqueues_when_enabled(monkeypatch):
    from fastapi import BackgroundTasks

    from bcp_project.deps import schedule_chunk_index

    monkeypatch.setenv("ENABLE_JOB_QUEUE", "1")
    background = BackgroundTasks()
    with patch("bcp_project.deps.enqueue_job", new_callable=AsyncMock, return_value="job-1") as enq:
        await schedule_chunk_index("D1", [{"text": "a"}], background)
        enq.assert_awaited_once()
    assert background.tasks == []


@pytest.mark.asyncio
async def test_schedule_chunk_index_falls_back(monkeypatch):
    from fastapi import BackgroundTasks

    from bcp_project.deps import schedule_chunk_index

    monkeypatch.setenv("ENABLE_JOB_QUEUE", "1")
    background = BackgroundTasks()
    with patch("bcp_project.deps.enqueue_job", new_callable=AsyncMock, return_value=None):
        await schedule_chunk_index("D1", [{"text": "a"}], background)
    assert len(background.tasks) == 1
