"""Phase 2: Redis Streams transport, outbox relay, job_runs, and the ingest state machine."""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select

from bcp_project.jobs import queue as q
from bcp_project.jobs.outbox import relay_once, stage_job
from bcp_project.models import DocumentRecord, JobRun, OutboxEvent
from bcp_project.services.ingest import INGEST_FAILED, INGEST_QUEUED, ingest_status_payload

from fake_streams import FakeStreams


@pytest.fixture
def streams(monkeypatch):
    fake = FakeStreams()
    q.set_stream_client_for_tests(fake)
    monkeypatch.setenv("ENABLE_MULTI_QUEUE", "1")
    monkeypatch.setenv("JOB_WORKER_QUEUES", "all")
    yield fake
    q.set_stream_client_for_tests(None)


@pytest.mark.asyncio
async def test_enqueue_dequeue_ack_roundtrip(streams):
    job_id = await q.enqueue_job("chunk_index", {"doc_id": "D1", "page_payloads": []})
    assert job_id
    assert await streams.xlen(q.QUEUE_INGEST) == 1

    job = await q.dequeue_job(timeout_seconds=1)
    assert job["id"] == job_id and job["_stream"] == q.QUEUE_INGEST
    await q.ack_job(job)
    assert await streams.xlen(q.QUEUE_INGEST) == 0
    assert streams.groups[(q.QUEUE_INGEST, q.CONSUMER_GROUP)]["pending"] == {}


@pytest.mark.asyncio
async def test_failed_job_retries_then_dead_letters(streams):
    await q.enqueue_job("meeting_minutes", {"meeting_id": 7})
    for attempt in range(1, 4):
        job = await q.dequeue_job(timeout_seconds=1)
        assert job is not None and job["type"] == "meeting_minutes"
        retried = await q.requeue_job(job, max_attempts=3, error="boom")
        assert retried is (attempt < 3)
    assert await q.dequeue_job(timeout_seconds=1) is None
    dead = await q.list_dead_letters()
    assert len(dead) == 1 and dead[0]["reason"] == "max_attempts" and dead[0]["error"] == "boom"

    retried_id = await q.retry_dead_letter(dead[0]["entry_id"])
    assert retried_id == dead[0]["job_id"]
    again = await q.dequeue_job(timeout_seconds=1)
    assert again["attempts"] == 0


@pytest.mark.asyncio
async def test_crashed_consumer_entry_is_reclaimed(streams, monkeypatch):
    await q.enqueue_job("webhook_dispatch", {"event_type": "x", "payload": {}})
    monkeypatch.setenv("JOB_CONSUMER_NAME", "worker-a")
    first = await q.dequeue_job(timeout_seconds=1)
    assert first is not None  # worker-a dies without acking

    monkeypatch.setenv("JOB_CONSUMER_NAME", "worker-b")
    assert await q.dequeue_job(timeout_seconds=1) is None  # not idle long enough yet
    streams.now_ms += q.CLAIM_IDLE_MS + 1
    reclaimed = await q.dequeue_job(timeout_seconds=1)
    assert reclaimed is not None and reclaimed["id"] == first["id"]
    assert reclaimed["_deliveries"] == 2


@pytest.mark.asyncio
async def test_poison_entry_dead_letters_after_repeated_crashes(streams):
    await q.enqueue_job("audit_export", {})
    await q.dequeue_job(timeout_seconds=1)
    for _ in range(3):
        streams.now_ms += q.CLAIM_IDLE_MS + 1
        await q.claim_stale_job(max_attempts=3)
    streams.now_ms += q.CLAIM_IDLE_MS + 1
    assert await q.claim_stale_job(max_attempts=3) is None
    dead = await q.list_dead_letters()
    assert dead and dead[0]["reason"] == "max_deliveries"


@pytest.mark.asyncio
async def test_legacy_list_jobs_move_to_streams(streams):
    await streams.lpush("bcp:jobs:ingest", json.dumps({"id": "old-1", "type": "chunk_index", "payload": {"doc_id": "D"}}))
    assert await q.drain_legacy_lists() == 1
    job = await q.dequeue_job(timeout_seconds=1)
    assert job["id"] == "old-1"


@pytest.mark.asyncio
async def test_stream_stats_report_backlog(streams):
    await q.enqueue_job("chunk_index", {"doc_id": "D1"})
    await q.enqueue_job("chunk_index", {"doc_id": "D2"})
    stats = await q.stream_stats()
    assert stats[q.QUEUE_INGEST]["length"] == 2
    assert stats[q.QUEUE_INGEST]["lag"] == 2


@pytest.mark.asyncio
async def test_outbox_publishes_only_committed_jobs(streams, db_session, tmp_path):
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def factory():
        async with maker() as session:
            yield session

    job_id = stage_job(db_session, "document_ingest", {"doc_id": "D9"}, resource_type="document", resource_id="D9")
    await db_session.rollback()
    assert await relay_once(factory) == 0

    job_id = stage_job(db_session, "document_ingest", {"doc_id": "D9"}, resource_type="document", resource_id="D9")
    await db_session.commit()
    assert await relay_once(factory) == 1
    assert await relay_once(factory) == 0  # published exactly once

    row = (await db_session.execute(select(OutboxEvent))).scalar_one()
    assert row.published_at is not None and row.job_id == job_id
    run = await db_session.get(JobRun, job_id)
    assert run.status == "queued" and run.resource_id == "D9"
    job = await q.dequeue_job(timeout_seconds=1)
    assert job["id"] == job_id and job["type"] == "document_ingest"


@pytest.mark.asyncio
async def test_outbox_keeps_rows_when_redis_is_down(db_session, monkeypatch):
    from contextlib import asynccontextmanager

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import bcp_project.jobs.outbox as outbox

    async def broken(_job):
        raise ConnectionError("redis down")

    monkeypatch.setattr(outbox, "publish_job", broken)
    maker = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def factory():
        async with maker() as session:
            yield session

    stage_job(db_session, "document_ingest", {"doc_id": "D1"})
    await db_session.commit()
    assert await relay_once(factory) == 0
    row = (await db_session.execute(select(OutboxEvent))).scalar_one()
    await db_session.refresh(row)
    assert row.published_at is None and row.attempts == 1 and "redis down" in row.last_error


def test_ingest_status_payload_progress():
    doc = DocumentRecord(
        doc_id="D1",
        doc_date=date(2026, 1, 1),
        doc_type="Memo",
        keywords=[],
        file_location="x",
        uploaded_by="u",
        ingest_status=INGEST_QUEUED,
    )
    payload = ingest_status_payload(doc)
    assert payload["status"] == "queued" and payload["done"] is False and payload["progress"] == 20
    doc.ingest_status = INGEST_FAILED
    doc.ingest_error = "OCR failed"
    payload = ingest_status_payload(doc)
    assert payload["done"] is True and payload["error"] == "OCR failed"
