"""job_runs bookkeeping. Never lets a database hiccup fail the job itself."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from ..models import JobRun

logger = logging.getLogger("bcp_project.jobs.runs")


def _resource_for(job: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    payload = job.get("payload") or {}
    if payload.get("doc_id"):
        return "document", str(payload["doc_id"])
    if payload.get("meeting_id") is not None:
        return "meeting", str(payload["meeting_id"])
    return None, None


async def _upsert(session_factory: Callable[[], Any], job: Dict[str, Any], **fields: Any) -> None:
    job_id = job.get("id")
    if not job_id:
        return
    try:
        async with session_factory() as db:
            row = await db.get(JobRun, job_id)
            if row is None:
                resource_type, resource_id = _resource_for(job)
                row = JobRun(
                    id=job_id,
                    job_type=str(job.get("type") or "unknown"),
                    queue=job.get("queue"),
                    resource_type=resource_type,
                    resource_id=resource_id,
                )
                db.add(row)
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = datetime.utcnow()
            await db.commit()
    except Exception as exc:
        logger.warning("job_runs update failed for %s: %s", job_id, exc)


async def mark_running(session_factory: Callable[[], Any], job: Dict[str, Any], worker: str) -> None:
    await _upsert(
        session_factory,
        job,
        status="running",
        attempts=int(job.get("attempts") or 0) + 1,
        started_at=datetime.utcnow(),
        worker=worker[:128],
    )


async def mark_succeeded(session_factory: Callable[[], Any], job: Dict[str, Any]) -> None:
    await _upsert(session_factory, job, status="succeeded", finished_at=datetime.utcnow(), last_error=None)


async def mark_failed(session_factory: Callable[[], Any], job: Dict[str, Any], error: str, *, dead: bool) -> None:
    await _upsert(
        session_factory,
        job,
        status="dead" if dead else "retrying",
        finished_at=datetime.utcnow() if dead else None,
        last_error=error[:4000],
    )
