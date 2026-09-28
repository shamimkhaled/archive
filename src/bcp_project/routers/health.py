"""Health, readiness, favicon, and service worker routes."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import text

from ..aws_utils import probe_s3, storage_backend_name
from ..config import is_production
from ..db import engine
from ..deps import STATIC_DIR, UPLOAD_DIR

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


@router.get("/readyz")
async def readyz() -> Dict[str, Any]:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc

    storage = storage_backend_name()
    payload: Dict[str, Any] = {
        "status": "ready",
        "storage": storage,
        "upload_dir": str(UPLOAD_DIR),
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
