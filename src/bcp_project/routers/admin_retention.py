"""Records retention: rules per document type, legal hold, and four-eyes disposal."""
from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import _client_ip, get_current_user, get_db, require_role, templates
from ..models import Role, User
from ..services import retention

router = APIRouter()

RECORDS_ROLES = [Role.admin, Role.board_secretary]


def _back(status: Optional[str] = None, error: Optional[str] = None) -> RedirectResponse:
    query = f"?status={quote(status)}" if status else f"?error={quote(error or '')}"
    return RedirectResponse(url=f"/admin/retention{query}", status_code=303)


@router.get("/api/admin/retention")
async def api_admin_retention(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    require_role(current_user, RECORDS_ROLES)
    return await retention.retention_overview(db)


@router.get("/admin/retention", response_class=HTMLResponse)
async def admin_retention_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, RECORDS_ROLES)
    return templates.TemplateResponse(
        request,
        "admin_retention.html",
        {
            "user": current_user,
            "page_title": "Records retention",
            "overview": await retention.retention_overview(db),
            "status": request.query_params.get("status"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/admin/retention/rules")
async def admin_retention_set_rule(
    request: Request,
    doc_type: str = Form(...),
    retain_years: str = Form(""),
    trigger: str = Form("doc_date"),
    authority: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])
    years: Optional[int]
    try:
        years = int(retain_years) if retain_years.strip() else None
    except ValueError:
        return _back(error="Retention must be a whole number of years, or blank for permanent.")
    try:
        await retention.set_rule(
            db,
            doc_type=doc_type,
            retain_years=years,
            trigger=trigger,
            authority=authority,
            actor=current_user.username,
            ip_address=_client_ip(request),
        )
    except retention.RetentionError as exc:
        await db.rollback()
        return _back(error=str(exc))
    await db.commit()
    return _back(status="rule_saved")


@router.post("/admin/retention/rules/{rule_id}/delete")
async def admin_retention_delete_rule(
    request: Request,
    rule_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])
    try:
        await retention.delete_rule(db, rule_id=rule_id, actor=current_user.username, ip_address=_client_ip(request))
    except retention.RetentionError as exc:
        await db.rollback()
        return _back(error=str(exc))
    await db.commit()
    return _back(status="rule_deleted")


@router.post("/admin/retention/holds")
async def admin_retention_hold(
    request: Request,
    doc_id: str = Form(...),
    reason: str = Form(""),
    action: str = Form("set"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, RECORDS_ROLES)
    if action not in {"set", "release"}:
        raise HTTPException(status_code=400, detail="action must be set or release")
    try:
        await retention.set_legal_hold(
            db,
            doc_id=doc_id.strip(),
            hold=action == "set",
            reason=reason,
            actor=current_user.username,
            ip_address=_client_ip(request),
        )
    except retention.RetentionError as exc:
        await db.rollback()
        return _back(error=str(exc))
    await db.commit()
    return _back(status="hold_set" if action == "set" else "hold_released")


@router.post("/admin/retention/disposals")
async def admin_retention_propose(
    request: Request,
    doc_id: str = Form(...),
    reason: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, RECORDS_ROLES)
    try:
        await retention.propose_disposal(
            db, doc_id=doc_id.strip(), reason=reason, actor=current_user.username, ip_address=_client_ip(request)
        )
    except retention.RetentionError as exc:
        await db.rollback()
        return _back(error=str(exc))
    await db.commit()
    return _back(status="proposed")


@router.post("/admin/retention/disposals/{disposal_id}/approve")
async def admin_retention_approve(
    request: Request,
    disposal_id: int,
    note: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, RECORDS_ROLES)
    try:
        row = await retention.approve_disposal(
            db, disposal_id=disposal_id, actor=current_user.username, note=note, ip_address=_client_ip(request)
        )
    except retention.RetentionError as exc:
        await db.rollback()
        return _back(error=str(exc))
    if row.status != "executed":
        return _back(error=f"Approved, but disposal did not complete: {row.last_error}. Retry once the cause is fixed.")
    return _back(status="disposed")


@router.post("/admin/retention/disposals/{disposal_id}/reject")
async def admin_retention_reject(
    request: Request,
    disposal_id: int,
    note: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, RECORDS_ROLES)
    try:
        row = await retention.reject_disposal(
            db, disposal_id=disposal_id, note=note, actor=current_user.username, ip_address=_client_ip(request)
        )
    except retention.RetentionError as exc:
        await db.rollback()
        return _back(error=str(exc))
    await db.commit()
    return _back(status=row.status)
