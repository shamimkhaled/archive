"""Admin view and runtime switch for AI providers (chat per task, reranker, STT)."""
from __future__ import annotations

import logging
from typing import Any, Dict
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import ai
from ..ai import route_changes, routing
from ..ai.providers import ALL_PROVIDERS, provider_configured
from ..deps import _client_ip, get_current_user, get_db, require_role, templates
from ..diarization import diarization_status
from ..models import ModelRegistryEntry, Role, User

logger = logging.getLogger("bcp_project.routers.admin_ai")
router = APIRouter()


async def ai_overview(db: AsyncSession) -> Dict[str, Any]:
    await routing.refresh_registry()
    history = (
        await db.execute(
            select(ModelRegistryEntry).order_by(ModelRegistryEntry.activated_at.desc(), ModelRegistryEntry.id.desc()).limit(50)
        )
    ).scalars().all()
    return {
        "four_eyes": route_changes.four_eyes_enabled(),
        "changes": await route_changes.recent(db),
        "egress_policy": ai.egress_policy(),
        "routes": routing.describe_routes(),
        "tasks": list(routing.ROUTABLE_TASKS),
        "providers": {
            "chat": sorted(ALL_PROVIDERS),
            "rerank": list(routing.RERANK_PROVIDERS),
            "stt": list(routing.STT_PROVIDERS),
        },
        "configured": {
            name: provider_configured(name) for name in sorted(ALL_PROVIDERS)
        },
        "diarization": diarization_status(),
        "history": [
            {
                "id": row.id,
                "task": row.task,
                "provider": row.provider,
                "model": row.model,
                "is_active": row.is_active,
                "notes": row.notes,
                "activated_by": row.activated_by,
                "activated_at": row.activated_at.isoformat() if row.activated_at else None,
                "deactivated_at": row.deactivated_at.isoformat() if row.deactivated_at else None,
            }
            for row in history
        ],
    }


@router.get("/api/admin/ai")
async def api_admin_ai(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    require_role(current_user, [Role.admin])
    return await ai_overview(db)


@router.get("/admin/ai", response_class=HTMLResponse)
async def admin_ai_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, [Role.admin])
    return templates.TemplateResponse(
        request,
        "admin_ai.html",
        {
            "user": current_user,
            "page_title": "AI providers",
            "overview": await ai_overview(db),
            "status": request.query_params.get("status"),
            "error": request.query_params.get("error"),
        },
    )


async def _submit(request: Request, db: AsyncSession, user: User, **change: Any) -> RedirectResponse:
    try:
        row = await route_changes.submit(db, actor=user.username, ip_address=_client_ip(request), **change)
    except route_changes.RouteChangeError as exc:
        await db.rollback()
        return RedirectResponse(url=f"/admin/ai?error={quote(str(exc))}", status_code=303)
    await db.commit()
    if row.status == "pending":
        return RedirectResponse(url="/admin/ai?status=pending", status_code=303)
    await routing.refresh_registry()
    done = {"switch": "switched", "reset": "reset", "rollback": "rolled_back"}[row.kind]
    return RedirectResponse(url=f"/admin/ai?status={done}", status_code=303)


@router.post("/admin/ai/route")
async def admin_ai_set_route(
    request: Request,
    task: str = Form(...),
    provider: str = Form(...),
    model: str = Form(...),
    notes: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])
    return await _submit(
        request, db, current_user, kind="switch", task=task.strip(), provider=provider.strip().lower(),
        model=model.strip(), notes=notes,
    )


@router.post("/admin/ai/route/{task}/reset")
async def admin_ai_reset_route(
    request: Request,
    task: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])
    if task not in routing.ROUTABLE_TASKS:
        raise HTTPException(status_code=404, detail="Unknown task")
    return await _submit(request, db, current_user, kind="reset", task=task)


@router.post("/admin/ai/history/{entry_id}/activate")
async def admin_ai_rollback(
    request: Request,
    entry_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Roll back by re-activating an earlier registry row as a new activation."""
    require_role(current_user, [Role.admin])
    row = await db.get(ModelRegistryEntry, entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Registry entry not found")
    return await _submit(request, db, current_user, kind="rollback", task=row.task, source_entry_id=row.id)


@router.post("/admin/ai/changes/{change_id}/approve")
async def admin_ai_approve_change(
    request: Request,
    change_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])
    try:
        await route_changes.approve(db, change_id=change_id, actor=current_user.username, ip_address=_client_ip(request))
    except route_changes.RouteChangeError as exc:
        await db.rollback()
        return RedirectResponse(url=f"/admin/ai?error={quote(str(exc))}", status_code=303)
    await db.commit()
    await routing.refresh_registry()
    return RedirectResponse(url="/admin/ai?status=approved", status_code=303)


@router.post("/admin/ai/changes/{change_id}/reject")
async def admin_ai_reject_change(
    request: Request,
    change_id: int,
    note: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])
    try:
        row = await route_changes.reject(
            db, change_id=change_id, actor=current_user.username, note=note, ip_address=_client_ip(request)
        )
    except route_changes.RouteChangeError as exc:
        await db.rollback()
        return RedirectResponse(url=f"/admin/ai?error={quote(str(exc))}", status_code=303)
    await db.commit()
    return RedirectResponse(url=f"/admin/ai?status={row.status}", status_code=303)
