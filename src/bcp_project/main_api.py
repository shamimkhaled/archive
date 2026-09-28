"""FastAPI application factory: middleware, lifespan, and router wiring."""
from __future__ import annotations

import logging
import os
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .auth import create_access_token, decode_access_token, should_refresh_access_token
from .aws_utils import storage_backend_name, use_local_storage
from .brand import BRAND
from .config import is_production
from .db import get_session, engine
from .deps import (
    STATIC_DIR,
    _load_stored_pdf_bytes,
    _set_auth_cookie,
    ensure_upload_dirs,
    reindex_chunks_from_pdf_bytes,
)
from .correlation import CorrelationIdMiddleware
from .jobs.locks import redis_lock
from .models import Base
from .reminders import send_due_reminders
from .routers import include_routers
from .security import CsrfMiddleware, SecurityHeadersMiddleware

# Re-export for scripts (e.g. scripts/reindex_chunks.py)
__all__ = [
    "app",
    "_load_stored_pdf_bytes",
    "reindex_chunks_from_pdf_bytes",
]

logger = logging.getLogger("bcp_project.main_api")

_docs_enabled = not is_production()
app = FastAPI(
    title=BRAND.product_name,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)
app.add_middleware(SecurityHeadersMiddleware, is_production=is_production())
app.add_middleware(CsrfMiddleware)
app.add_middleware(CorrelationIdMiddleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def sliding_session_middleware(request: Request, call_next):
    """Extend auth cookie while the user is actively using the app."""
    response = await call_next(request)
    if request.method == "OPTIONS":
        return response
    path = request.url.path or ""
    if path.startswith("/static/") or path in {"/sw.js", "/healthz", "/readyz"}:
        return response

    token = request.cookies.get("access_token")
    if not token:
        return response
    payload = decode_access_token(token)
    if not payload or not should_refresh_access_token(payload):
        return response
    username = payload.get("sub")
    role = payload.get("role")
    if not username or not role:
        return response
    try:
        _set_auth_cookie(response, create_access_token(subject=username, role=str(role)))
    except Exception:
        logger.exception("Failed to refresh access token cookie")
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Send full-page navigations to login on auth failure; keep JSON for APIs/SPA fetches."""
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        is_spa = request.headers.get("X-Requested-With") == "BCPNav"
        accept = (request.headers.get("accept") or "").lower()
        wants_html = "text/html" in accept
        if wants_html and not is_spa and not request.url.path.startswith("/api/"):
            return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text("ALTER TABLE board_meetings ADD COLUMN IF NOT EXISTS transcription_status VARCHAR(16) NOT NULL DEFAULT 'off'")
        )
        await conn.execute(text("ALTER TABLE board_meetings ADD COLUMN IF NOT EXISTS transcription_started_at TIMESTAMP"))
        await conn.execute(text("ALTER TABLE board_meetings ADD COLUMN IF NOT EXISTS transcription_stopped_at TIMESTAMP"))
        await conn.execute(text("ALTER TABLE board_meetings ADD COLUMN IF NOT EXISTS transcription_started_by VARCHAR(64)"))
        await conn.execute(text("ALTER TABLE board_meetings ADD COLUMN IF NOT EXISTS transcription_json JSON"))
        await conn.execute(text("ALTER TABLE board_meetings ADD COLUMN IF NOT EXISTS minutes_status VARCHAR(16) NOT NULL DEFAULT 'off'"))
        await conn.execute(text("ALTER TABLE board_meetings ADD COLUMN IF NOT EXISTS minutes_draft TEXT"))
        await conn.execute(text("ALTER TABLE board_meetings ADD COLUMN IF NOT EXISTS minutes_source VARCHAR(16)"))
        await conn.execute(text("ALTER TABLE board_meetings ADD COLUMN IF NOT EXISTS minutes_generated_at TIMESTAMP"))
        await conn.execute(text("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS correlation_id VARCHAR(64)"))
        await conn.execute(text("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS actor_type VARCHAR(16)"))
        await conn.execute(text("ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS tool_name VARCHAR(64)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_audit_logs_correlation_id ON audit_logs (correlation_id)"))


async def _run_reminder_sweep() -> None:
    """Single-flight across replicas via Redis lock (falls open if Redis is down)."""
    async with redis_lock("reminder_sweep", ttl_seconds=840) as acquired:
        if not acquired:
            logger.debug("Skipping reminder sweep; another instance holds the lock")
            return
        async with get_session() as session:
            await send_due_reminders(session)


@app.on_event("startup")
async def startup_event() -> None:
    ensure_upload_dirs()
    await init_db()

    backend = storage_backend_name()
    if is_production() and use_local_storage():
        allow = (os.getenv("ALLOW_EPHEMERAL_UPLOADS") or "").strip().lower() in {"1", "true", "yes", "on"}
        if allow:
            logger.warning(
                "STORAGE_BACKEND=local on production (ALLOW_EPHEMERAL_UPLOADS=1). "
                "PDF view/download will break after Railway redeploys."
            )
        else:
            logger.error(
                "STORAGE_BACKEND is local/ephemeral while APP_ENV=production. "
                "Set STORAGE_BACKEND=s3 and AWS_* secrets or uploads cannot be viewed after redeploy."
            )
    else:
        logger.info("PDF storage backend: %s", backend)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(_run_reminder_sweep, "interval", minutes=15, next_run_time=datetime.utcnow())
    scheduler.start()
    app.state.scheduler = scheduler


@app.on_event("shutdown")
async def shutdown_event() -> None:
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.shutdown(wait=False)


include_routers(app)
