"""Admin view of the audit log: who did what, to which record, when, and whether the row is sealed into the chain."""
from __future__ import annotations

import csv
import io
from typing import Any, Dict, Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import write_audit
from ..deps import _client_ip, get_current_user, get_db, require_role, templates
from ..models import AuditLog, Role, User
from ..security import spreadsheet_safe
from ..services.audit_chain import seal_delay_seconds
from ..services.audit_view import (
    ACTOR_TYPES,
    CSV_COLUMNS,
    AuditQuery,
    audit_entry,
    csv_row,
    distinct_values,
    last_verification,
    list_audit,
)
from .admin_security import audit_status, verify_and_record

router = APIRouter()

PER_PAGE = 50
MAX_EXPORT_ROWS = 50_000


def _query_from_request(request: Request) -> AuditQuery:
    params = request.query_params
    try:
        return AuditQuery.from_params(**{name: params.get(name) for name in AuditQuery.__dataclass_fields__})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _describe(query: AuditQuery) -> str:
    return " ".join(f"{k}={v}" for k, v in query.as_params().items())[:400] or "all"


async def _page(db: AsyncSession, query: AuditQuery, page: int) -> Dict[str, Any]:
    rows, total = await list_audit(db, query, page=page, per_page=PER_PAGE)
    entries = [{**audit_entry(row), "created": row.created_at} for row in rows]
    return {"entries": entries, "total": total, "page": page, "pages": max(1, -(-total // PER_PAGE))}


async def _record_view(db: AsyncSession, request: Request, user: User, action: str, detail: str) -> None:
    await write_audit(
        db,
        username=user.username,
        action=action,
        resource_type="audit_log",
        detail=detail,
        ip_address=_client_ip(request),
        actor_type="user",
        commit=True,
    )


@router.get("/admin/audit", response_class=HTMLResponse)
async def admin_audit_page(
    request: Request,
    page: int = Query(1, ge=1),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, [Role.admin])
    query = _query_from_request(request)
    data = await _page(db, query, page)
    context = {
        "user": current_user,
        "page_title": "Audit log",
        "query": query,
        "params": query.as_params(),
        "actor_types": ACTOR_TYPES,
        "actions": await distinct_values(db, AuditLog.action),
        "resource_types": await distinct_values(db, AuditLog.resource_type),
        "status": await audit_status(db),
        "last_check": await last_verification(db),
        "seal_delay": int(seal_delay_seconds()),
        **data,
    }
    await _record_view(db, request, current_user, "audit_log_view", f"page={page} {_describe(query)}")
    return templates.TemplateResponse(request, "admin_audit.html", context)


@router.get("/api/admin/audit")
async def api_admin_audit(
    request: Request,
    page: int = Query(1, ge=1),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    require_role(current_user, [Role.admin])
    query = _query_from_request(request)
    data = await _page(db, query, page)
    await _record_view(db, request, current_user, "audit_log_view", f"api page={page} {_describe(query)}")
    for entry in data["entries"]:
        entry.pop("created", None)
    return {**data, "per_page": PER_PAGE, "filters": query.as_params()}


@router.get("/admin/audit.csv")
async def admin_audit_csv(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_role(current_user, [Role.admin])
    query = _query_from_request(request)
    rows, total = await list_audit(db, query, page=1, per_page=MAX_EXPORT_ROWS)
    await _record_view(db, request, current_user, "audit_log_export", f"rows={len(rows)} of={total} {_describe(query)}")

    def generate() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow([spreadsheet_safe(v) for v in csv_row(row)])
            if buffer.tell() > 64_000:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
        yield buffer.getvalue()

    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit-log.csv"', "Cache-Control": "no-store"},
    )


@router.post("/admin/audit/verify")
async def admin_audit_verify(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    require_role(current_user, [Role.admin])
    await verify_and_record(db, request, current_user)
    return RedirectResponse(url="/admin/audit", status_code=303)
