"""Transactional outbox: stage a job with the domain write, publish after commit.

``stage_job`` adds an ``outbox_events`` row and a ``job_runs`` row to the caller's
session, so the job exists if and only if the transaction commits. The relay
(``python -m bcp_project.jobs.relay`` or the in-process loop) publishes pending
rows to Redis Streams with ``FOR UPDATE SKIP LOCKED``, so any number of relays
can run without double-publishing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import JobRun, OutboxEvent
from ..observability import incr
from .queue import build_job, publish_job

logger = logging.getLogger("bcp_project.jobs.outbox")

RELAY_BATCH = int(os.getenv("OUTBOX_RELAY_BATCH", "100"))
RELAY_IDLE_SECONDS = float(os.getenv("OUTBOX_RELAY_IDLE_SECONDS", "2"))
RETENTION_DAYS = int(os.getenv("OUTBOX_RETENTION_DAYS", "7"))

_wakeup: Optional[asyncio.Event] = None


def relay_mode() -> str:
    """``inline`` runs the relay inside the web process; ``process`` expects the outbox-relay service."""
    return (os.getenv("OUTBOX_RELAY_MODE") or "inline").strip().lower()


def stage_job(
    db: AsyncSession,
    job_type: str,
    payload: Dict[str, Any],
    *,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    job_id: Optional[str] = None,
) -> str:
    job = build_job(job_type, payload, job_id=job_id)
    db.add(OutboxEvent(topic=job_type, job_id=job["id"], payload=job))
    db.add(
        JobRun(
            id=job["id"],
            job_type=job_type,
            queue=job["queue"],
            status="queued",
            resource_type=resource_type,
            resource_id=resource_id,
        )
    )
    return job["id"]


def notify_outbox() -> None:
    """Wake an in-process relay right after commit (otherwise it polls)."""
    if _wakeup is not None:
        _wakeup.set()


async def relay_once(session_factory: Callable[[], Any], *, batch: int = RELAY_BATCH) -> int:
    published = 0
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
                .order_by(OutboxEvent.id)
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for row in rows:
            try:
                await publish_job(dict(row.payload))
            except Exception as exc:
                row.attempts = int(row.attempts or 0) + 1
                row.last_error = f"{type(exc).__name__}: {exc}"[:500]
                incr("outbox.publish_failed")
                logger.warning("Outbox publish failed for job %s: %s", row.job_id, exc)
                break
            row.published_at = datetime.utcnow()
            published += 1
        await db.commit()
    if published:
        incr("outbox.published", published)
    return published


async def purge_published(session_factory: Callable[[], Any], *, days: int = RETENTION_DAYS) -> None:
    cutoff = datetime.utcnow() - timedelta(days=days)
    async with session_factory() as db:
        await db.execute(
            delete(OutboxEvent).where(OutboxEvent.published_at.is_not(None), OutboxEvent.published_at < cutoff)
        )
        await db.commit()


async def run_relay_loop(session_factory: Callable[[], Any], stop: asyncio.Event) -> None:
    global _wakeup
    _wakeup = asyncio.Event()
    logger.info("Outbox relay started (mode=%s)", relay_mode())
    loops = 0
    while not stop.is_set():
        try:
            published = await relay_once(session_factory)
        except Exception:
            logger.exception("Outbox relay iteration failed")
            published = 0
        loops += 1
        if loops % 1800 == 0:
            try:
                await purge_published(session_factory)
            except Exception:
                logger.exception("Outbox purge failed")
        if published >= RELAY_BATCH:
            continue
        _wakeup.clear()
        waiters = [asyncio.create_task(_wakeup.wait()), asyncio.create_task(stop.wait())]
        try:
            await asyncio.wait(waiters, timeout=RELAY_IDLE_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waiters:
                task.cancel()
    logger.info("Outbox relay stopped")
