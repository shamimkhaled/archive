"""Document access-request routes."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import (
    default_expires_at,
    has_open_pending_request,
    is_archive_privileged,
    write_audit,
)
from ..deps import (
    _client_ip,
    _load_document_or_404,
    get_current_user,
    get_db,
    require_role,
    templates,
)
from ..models import AccessMode, AccessRequestStatus, DocumentAccessRequest, Role, User

router = APIRouter()

@router.get("/access-requests", response_class=HTMLResponse)
async def my_access_requests_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status: Optional[str] = None,
    error: Optional[str] = None,
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    statement = (
        select(DocumentAccessRequest)
        .where(DocumentAccessRequest.requester_username == current_user.username)
        .order_by(DocumentAccessRequest.created_at.desc())
        .limit(100)
    )
    result = await db.execute(statement)
    rows = result.scalars().all()
    return templates.TemplateResponse(
        request,
        "access_requests.html",
        {
            "user": current_user,
            "requests": rows,
            "status": status,
            "error": error,
            "modes": [AccessMode.view_only],
            "can_create_requests": not is_archive_privileged(current_user),
            "is_reviewer": current_user.role in (Role.admin, Role.board_secretary),
        },
    )


@router.post("/access-requests")
async def create_access_request(
    request: Request,
    doc_id: str = Form(...),
    purpose: str = Form(...),
    requested_mode: AccessMode = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    doc_id = doc_id.strip()
    purpose = purpose.strip()
    if not doc_id or not purpose:
        return RedirectResponse(
            url="/access-requests?error=Document+ID+and+purpose+are+required",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    await _load_document_or_404(doc_id, db)

    if is_archive_privileged(current_user):
        return RedirectResponse(
            url="/access-requests?error=Your+role+already+has+archive+view+access",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if requested_mode != AccessMode.view_only:
        return RedirectResponse(
            url="/access-requests?error=Download+requests+are+not+available",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if await has_open_pending_request(db, current_user.username, doc_id):
        return RedirectResponse(
            url="/access-requests?error=You+already+have+a+pending+request+for+this+document",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    row = DocumentAccessRequest(
        doc_id=doc_id,
        requester_username=current_user.username,
        purpose=purpose,
        requested_mode=requested_mode,
        status=AccessRequestStatus.pending,
    )
    db.add(row)
    await write_audit(
        db,
        username=current_user.username,
        action="access_request_create",
        resource_type="document",
        resource_id=doc_id,
        detail=f"mode={requested_mode.value}",
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    return RedirectResponse(url="/access-requests?status=submitted", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/documents/{doc_id}/access-requests")
async def api_create_access_request(
    request: Request,
    doc_id: str,
    purpose: str = Form(...),
    requested_mode: AccessMode = Form(AccessMode.view_only),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary, Role.board_member])
    await _load_document_or_404(doc_id, db)
    if is_archive_privileged(current_user):
        raise HTTPException(status_code=400, detail="Your role already has archive view access")
    if requested_mode != AccessMode.view_only:
        raise HTTPException(status_code=400, detail="Download requests are not available")
    if await has_open_pending_request(db, current_user.username, doc_id):
        raise HTTPException(status_code=400, detail="Pending request already exists")
    purpose = purpose.strip()
    if not purpose:
        raise HTTPException(status_code=400, detail="Purpose is required")
    row = DocumentAccessRequest(
        doc_id=doc_id,
        requester_username=current_user.username,
        purpose=purpose,
        requested_mode=requested_mode,
        status=AccessRequestStatus.pending,
    )
    db.add(row)
    await write_audit(
        db,
        username=current_user.username,
        action="access_request_create",
        resource_type="document",
        resource_id=doc_id,
        detail=f"mode={requested_mode.value}",
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    return {"ok": True, "id": row.id, "status": row.status.value}


@router.get("/admin/access-requests", response_class=HTMLResponse)
async def review_access_requests_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    status_filter: Optional[str] = None,
) -> Any:
    require_role(current_user, [Role.admin, Role.board_secretary])
    statement = select(DocumentAccessRequest).order_by(DocumentAccessRequest.created_at.desc()).limit(200)
    if status_filter in {s.value for s in AccessRequestStatus}:
        statement = statement.where(DocumentAccessRequest.status == AccessRequestStatus(status_filter))
    else:
        statement = statement.where(DocumentAccessRequest.status == AccessRequestStatus.pending)
    result = await db.execute(statement)
    rows = result.scalars().all()
    return templates.TemplateResponse(
        request,
        "admin_access_requests.html",
        {
            "user": current_user,
            "requests": rows,
            "status_filter": status_filter or AccessRequestStatus.pending.value,
            "statuses": list(AccessRequestStatus),
        },
    )


@router.post("/admin/access-requests/{request_id}/review")
async def review_access_request(
    request: Request,
    request_id: int,
    decision: str = Form(...),
    review_note: str = Form(""),
    grant_days: int = Form(7),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin, Role.board_secretary])
    statement = select(DocumentAccessRequest).where(DocumentAccessRequest.id == request_id)
    result = await db.execute(statement)
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Access request not found")
    if row.status != AccessRequestStatus.pending:
        return RedirectResponse(url="/admin/access-requests?status_filter=pending", status_code=303)

    decision_norm = decision.strip().lower()
    if decision_norm not in {"approve", "deny"}:
        raise HTTPException(status_code=400, detail="decision must be approve or deny")

    row.reviewed_by = current_user.username
    row.reviewed_at = datetime.utcnow()
    row.review_note = review_note.strip() or None
    if decision_norm == "approve":
        row.status = AccessRequestStatus.approved
        days = max(1, min(grant_days, 365))
        row.expires_at = default_expires_at(days)
    else:
        row.status = AccessRequestStatus.denied
        row.expires_at = None

    await write_audit(
        db,
        username=current_user.username,
        action=f"access_request_{decision_norm}",
        resource_type="document",
        resource_id=row.doc_id,
        detail=f"request_id={row.id};requester={row.requester_username};mode={row.requested_mode.value}",
        ip_address=_client_ip(request),
        commit=False,
    )
    await db.commit()
    return RedirectResponse(url="/admin/access-requests", status_code=status.HTTP_303_SEE_OTHER)
