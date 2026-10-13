"""Health, readiness, favicon, and service worker routes."""
from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import func, select, text

from ..aws_utils import probe_s3, storage_backend_name
from ..config import is_production
from ..db import engine, get_session
from ..deps import STATIC_DIR, UPLOAD_DIR
from ..diarization import diarization_status
from ..jobs.queue import stream_stats
from ..models import OutboxEvent
from ..observability import metrics_access, render_prometheus, set_gauge

router = APIRouter()

@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    icon = STATIC_DIR / "img" / "sonali-bank-logo.png"
    if not icon.exists():
        raise HTTPException(status_code=404, detail="Favicon not found")
    return FileResponse(icon, media_type="image/png")


@router.get("/sw.js")
async def service_worker() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "js" / "sw.js",
        media_type="application/javascript; charset=utf-8",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )


@router.get("/healthz")
async def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> PlainTextResponse:
    """Prometheus scrape endpoint. Set METRICS_TOKEN to require `Authorization: Bearer <token>`."""
    denied = metrics_access(request.headers.get("authorization"))
    if denied == 401:
        raise HTTPException(status_code=401, detail="metrics token required")
    if denied:
        raise HTTPException(status_code=404, detail="Not Found")
    await _refresh_queue_gauges()
    return PlainTextResponse(render_prometheus(), media_type="text/plain; version=0.0.4; charset=utf-8")


async def _refresh_queue_gauges() -> None:
    """Backlog, in-flight, and lag per stream, plus the unpublished outbox depth."""
    try:
        stats = await asyncio.wait_for(stream_stats(), timeout=1.5)
        for stream, row in stats.items():
            labels = {"stream": stream}
            set_gauge("job_stream_length", row.get("length") or 0, labels)
            if "pending" in row:
                set_gauge("job_stream_pending", row.get("pending") or 0, labels)
            if row.get("lag") is not None:
                set_gauge("job_stream_lag", row["lag"], labels)
    except Exception:
        set_gauge("job_stream_up", 0)
    else:
        set_gauge("job_stream_up", 1)
    try:
        async with get_session() as db:
            pending = await asyncio.wait_for(
                db.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.published_at.is_(None))),
                timeout=1.5,
            )
        set_gauge("outbox_unpublished", int(pending or 0))
    except Exception:
        pass


@router.get("/readyz")
async def readyz() -> Dict[str, Any]:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc

    storage = storage_backend_name()
    diarization = diarization_status()
    payload: Dict[str, Any] = {
        "status": "ready",
        "storage": storage,
        "upload_dir": str(UPLOAD_DIR),
        "diarization": {k: diarization[k] for k in ("provider", "ready", "issues")},
    }
    if storage == "s3":
        payload["s3"] = probe_s3()
        if not payload["s3"].get("ok"):
            payload["status"] = "degraded"
    elif is_production():
        payload["status"] = "degraded"
        payload["warning"] = (
            "Local PDF storage on Railway is ephemeral. "
            "Set STORAGE_BACKEND=s3 and AWS_* or view/download will 404 after redeploy."
        )
    return payload
