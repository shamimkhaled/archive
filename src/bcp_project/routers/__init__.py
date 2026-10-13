"""Register all API routers on the FastAPI application."""
from __future__ import annotations

from fastapi import FastAPI

from . import (
    access,
    admin,
    admin_ai,
    admin_queues,
    admin_retention,
    admin_security,
    api_v1,
    ask,
    auth,
    board,
    briefings,
    documents,
    health,
    ingest,
    meeting_minutes,
    meetings,
    notifications,
    pages,
    review,
)


def include_routers(app: FastAPI) -> None:
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(ingest.router)
    app.include_router(documents.router)
    app.include_router(review.router)
    app.include_router(admin.router)
    app.include_router(admin_queues.router)
    app.include_router(admin_ai.router)
    app.include_router(admin_retention.router)
    app.include_router(admin_security.router)
    app.include_router(access.router)
    app.include_router(meetings.router)
    app.include_router(meeting_minutes.router)
    app.include_router(board.router)
    app.include_router(briefings.router)
    app.include_router(notifications.router)
    app.include_router(ask.router)
    app.include_router(api_v1.router)
