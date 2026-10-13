"""Admin view of the job streams: backlog, lag, recent failures, dead letters."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import write_audit
from ..deps import _client_ip, get_current_user, get_db, require_role, templates
from ..jobs.queue import list_dead_letters, retry_dead_letter, stream_stats
from ..models import DocumentRecord, JobRun, OutboxEvent, Role, User

logger = logging.getLogger("bcp_project.routers.admin_queues")
router = APIRouter()


async def queue_overview(db: AsyncSession) -> Dict[str, Any]:
    try:
        streams = await stream_stats()
        dead = await list_dead_letters(limit=50)
        redis_ok = True
    except Exception as exc:
        logger.warning("Stream stats unavailable: %s", exc)
        streams, dead, redis_ok = {}, [], False

    status_counts = dict(
        (await db.execute(select(JobRun.status, func.count()).group_by(JobRun.status))).all()
    )
    recent_failures = (
        await db.execute(
            select(JobRun)
            .where(JobRun.status.in_(("retrying", "dead")))
            .order_by(JobRun.updated_at.desc())
            .limit(25)
        )
    ).scalars().all()
    unpublished = int(
        await db.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.published_at.is_(None))) or 0
    )
    ingest_counts = dict(
        (
            await db.execute(
                select(DocumentRecord.ingest_status, func.count()).group_by(DocumentRecord.ingest_status)
            )
        ).all()
    )
    return {
        "redis_ok": redis_ok,
        "streams": streams,
        "dead_letters": dead,
        "job_status_counts": status_counts,
        "outbox_unpublished": unpublished,
        "ingest_counts": ingest_counts,
        "recent_failures": [
            {
                "id": row.id,
                "job_type": row.job_type,
                "status": row.status,
                "attempts": row.attempts,
                "resource": f"{row.resource_type}:{row.resource_id}" if row.resource_type else "",
                "last_error": (row.last_error or "")[:300],
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in recent_failures
        ],
    }


@router.get("/api/admin/queues")
async def api_admin_queues(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    require_role(current_user, [Role.admin])
    return await queue_overview(db)


@router.get("/admin/queues", response_class=HTMLResponse)
async def admin_queues_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, [Role.admin])
    overview = await queue_overview(db)
    return templates.TemplateResponse(
        request,
        "admin_queues.html",
        {
            "user": current_user,
            "page_title": "Background tasks",
            "overview": overview,
            "status": request.query_params.get("status"),
            "checked_at": datetime.utcnow(),
        },
    )


@router.post("/admin/queues/dead/{entry_id}/retry")
async def admin_retry_dead_letter(
    request: Request,
    entry_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])
    job_id = await retry_dead_letter(entry_id)
    if job_id is None:
        raise HTTPException(status_code=404, detail="Dead-letter entry not found")
    await write_audit(
        db,
        username=current_user.username,
        action="job_retry",
        resource_type="job",
        resource_id=str(job_id),
        detail=f"dead_letter={entry_id}",
        ip_address=_client_ip(request),
        commit=True,
    )
    return RedirectResponse(url="/admin/queues?status=retried", status_code=303)
