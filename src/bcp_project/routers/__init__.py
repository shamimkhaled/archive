"""Register all API routers on the FastAPI application."""
from __future__ import annotations

from fastapi import FastAPI

from . import access, admin, api_v1, ask, auth, board, documents, health, meetings, notifications, pages


def include_routers(app: FastAPI) -> None:
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(documents.router)
    app.include_router(admin.router)
    app.include_router(access.router)
    app.include_router(meetings.router)
    app.include_router(board.router)
    app.include_router(notifications.router)
    app.include_router(ask.router)
    app.include_router(api_v1.router)
