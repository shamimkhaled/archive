"""FastAPI application factory: middleware, lifespan, and router wiring."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .auth import create_access_token, decode_session_token, should_refresh_access_token
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
from .ai.routing import REFRESH_SECONDS as REGISTRY_REFRESH_SECONDS, refresh_registry
from .jobs.outbox import relay_mode, run_relay_loop
from .jobs.queue import job_queue_enabled
from .logging_config import configure_logging
from .migrate import auto_migrate_enabled, upgrade_head
from .observability import MetricsMiddleware, setup_tracing
from .reminders import send_due_reminders
from .routers import include_routers
from .security import CsrfMiddleware, SecurityHeadersMiddleware

configure_logging()

# Re-export for scripts (e.g. scripts/reindex_chunks.py)
__all__ = [
    "app",
    "_load_stored_pdf_bytes",
    "reindex_chunks_from_pdf_bytes",
]

logger = logging.getLogger("bcp_project.main_api")
AUDIT_SEAL_INTERVAL_SECONDS = int(os.getenv("AUDIT_SEAL_INTERVAL_SECONDS") or 30)
VECTOR_BACKFILL_MINUTES = int(os.getenv("VECTOR_BACKFILL_MINUTES") or 10)

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
app.add_middleware(MetricsMiddleware)
setup_tracing(app)
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
    payload = decode_session_token(token)
    if not payload or not should_refresh_access_token(payload):
        return response
    username = payload.get("sub")
    role = payload.get("role")
    if not username or not role:
        return response
    try:
        _set_auth_cookie(
            response,
            create_access_token(subject=username, role=str(role), mfa=bool(payload.get("mfa"))),
        )
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
    if not auto_migrate_enabled():
        logger.info("AUTO_MIGRATE=0; expecting the entrypoint or a release step to have run migrations")
        return
    await upgrade_head(engine)


async def _run_reminder_sweep() -> None:
    """Single-flight across replicas via Redis lock (falls open if Redis is down)."""
    async with redis_lock("reminder_sweep", ttl_seconds=840) as acquired:
        if not acquired:
            logger.debug("Skipping reminder sweep; another instance holds the lock")
            return
        async with get_session() as session:
            await send_due_reminders(session)


async def _run_audit_maintenance() -> None:
    """Seal the audit hash chain and forward to the SIEM; one replica at a time."""
    from .jobs.handlers import run_audit_maintenance

    async with redis_lock("audit_maintenance", ttl_seconds=max(60, AUDIT_SEAL_INTERVAL_SECONDS * 4)) as acquired:
        if not acquired:
            return
        try:
            await run_audit_maintenance()
        except Exception:
            logger.exception("Audit maintenance failed; it will retry on the next tick")


async def _run_vector_backfill() -> None:
    """Build vectors for documents archived while the embedding provider was unavailable."""
    from .services.ingest import backfill_pending_vectors

    async with redis_lock("vector_backfill", ttl_seconds=max(300, VECTOR_BACKFILL_MINUTES * 60)) as acquired:
        if not acquired:
            return
        try:
            await backfill_pending_vectors()
        except Exception:
            logger.exception("Vector backfill failed; it will retry on the next tick")


async def _run_retention_refresh() -> None:
    """Daily: give new uploads their retention date from the rules; one replica at a time."""
    from .services.retention import refresh_retention_dates

    async with redis_lock("retention_refresh", ttl_seconds=3600) as acquired:
        if not acquired:
            return
        try:
            async with get_session() as session:
                changed = await refresh_retention_dates(session)
                await session.commit()
            if changed:
                logger.info("Retention dates updated for %d documents", changed)
        except Exception:
            logger.exception("Retention refresh failed; it will retry tomorrow")
        try:
            from .services.search_log import purge_expired_activity

            async with get_session() as session:
                removed = await purge_expired_activity(session)
            if removed:
                logger.info("Search activity purge removed %d rows past retention", removed)
        except Exception:
            logger.exception("Search activity purge failed; it will retry tomorrow")


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
    scheduler.add_job(_run_reminder_sweep, "interval", minutes=15, next_run_time=datetime.now(timezone.utc))
    scheduler.add_job(
        refresh_registry, "interval", seconds=REGISTRY_REFRESH_SECONDS, next_run_time=datetime.now(timezone.utc)
    )
    scheduler.add_job(_run_retention_refresh, "interval", hours=24, next_run_time=datetime.now(timezone.utc), coalesce=True)
    if AUDIT_SEAL_INTERVAL_SECONDS > 0:
        scheduler.add_job(
            _run_audit_maintenance, "interval", seconds=AUDIT_SEAL_INTERVAL_SECONDS, max_instances=1, coalesce=True
        )
    if VECTOR_BACKFILL_MINUTES > 0:
        scheduler.add_job(
            _run_vector_backfill, "interval", minutes=VECTOR_BACKFILL_MINUTES, max_instances=1, coalesce=True
        )
    scheduler.start()
    app.state.scheduler = scheduler

    if job_queue_enabled() and relay_mode() == "inline":
        app.state.relay_stop = asyncio.Event()
        app.state.relay_task = asyncio.create_task(run_relay_loop(get_session, app.state.relay_stop))


@app.on_event("shutdown")
async def shutdown_event() -> None:
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.shutdown(wait=False)
    relay_stop = getattr(app.state, "relay_stop", None)
    if relay_stop is not None:
        relay_stop.set()
        task = getattr(app.state, "relay_task", None)
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except Exception:
                task.cancel()


include_routers(app)
