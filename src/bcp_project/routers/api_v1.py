"""Versioned machine API (/api/v1) for integrators and AI agents."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import _client_ip, get_current_user, get_db
from ..jobs.handlers import JOB_CONNECTOR_PULL
from ..jobs.queue import enqueue_job, job_queue_enabled
from ..mcp.tools import TOOL_DEFINITIONS, invoke_tool
from ..models import User
from ..observability import snapshot_metrics
from ..policy import Principal, Scope, require_scope
from ..rate_limit import hit
from ..ports.loan_hr import get_hr_port, get_loan_port
from ..ports.registry import get_registry
from ..search_acl import SearchFilters
from ..service_auth import authenticate_api_v1_principal
from ..access_control import write_audit
from ..services import ask_sonali, customer_query, document_query, hitl, search_log
from ..services.connector_sync import pull_document_source

router = APIRouter(prefix="/api/v1", tags=["api-v1"])


async def get_principal(request: Request, db: AsyncSession = Depends(get_db)) -> Principal:
    return await authenticate_api_v1_principal(request, db)


@router.get("/documents/search")
async def v1_search_documents(
    request: Request,
    q: str = Query(..., min_length=1),
    lang: Optional[str] = None,
    limit: int = Query(20, ge=1, le=50),
    department: Optional[str] = None,
    category: Optional[str] = None,
    doc_type: Optional[str] = None,
    reference_number: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    subject: Optional[str] = None,
    title: Optional[str] = None,
    language: Optional[str] = None,
    tags: Optional[str] = Query(None, description="Comma-separated tag slugs; any of them matches"),
    metadata_check: Optional[str] = Query(None, description="checked | unchecked (AI-filled details not yet confirmed)"),
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Any:
    try:
        filters = SearchFilters.from_params(
            department=department,
            category=category,
            doc_type=doc_type,
            reference_number=reference_number,
            date_from=date_from,
            date_to=date_to,
            subject=subject,
            title=title,
            language=language,
            tags=tags,
            metadata_check=metadata_check,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await document_query.search_documents_for_principal(
        db,
        principal,
        q,
        lang=lang,
        limit=limit,
        filters=filters,
        ip_address=_client_ip(request),
    )


@router.get("/documents/{doc_id}")
async def v1_document_metadata(
    doc_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Any:
    return await document_query.get_document_metadata_for_principal(
        db,
        principal,
        doc_id,
        ip_address=_client_ip(request),
    )


@router.get("/documents/{doc_id}/status")
async def v1_document_status(
    doc_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Any:
    return await document_query.get_document_status_for_principal(
        db,
        principal,
        doc_id,
        ip_address=_client_ip(request),
    )


@router.get("/meetings")
async def v1_list_meetings(
    request: Request,
    limit: int = Query(20, ge=1, le=50),
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Any:
    return await document_query.list_meetings_for_principal(
        db,
        principal,
        limit=limit,
        ip_address=_client_ip(request),
    )


@router.get("/mcp/tools")
async def v1_list_mcp_tools(
    principal: Principal = Depends(get_principal),
) -> Any:
    require_scope(principal, Scope.mcp_invoke)
    return {"tools": TOOL_DEFINITIONS}


@router.post("/mcp/tools/{tool_name}")
async def v1_invoke_mcp_tool(
    tool_name: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_scope(principal, Scope.mcp_invoke)
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    arguments = body.get("arguments") if isinstance(body.get("arguments"), dict) else body
    return await invoke_tool(
        tool_name,
        arguments or {},
        db=db,
        principal=principal,
        ip_address=_client_ip(request),
    )


@router.get("/metrics")
async def v1_metrics(principal: Principal = Depends(get_principal)) -> Any:
    require_scope(principal, Scope.audit_read)
    return snapshot_metrics()


@router.get("/audit/search-activity")
async def v1_search_activity(
    request: Request,
    since_id: int = Query(0, ge=0, description="Return rows with id greater than this, oldest first"),
    limit: int = Query(200, ge=1, le=1000),
    username: Optional[str] = None,
    channel: Optional[str] = None,
    doc_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Search activity for SIEM pulls; page forward with ``next_since_id``."""
    require_scope(principal, Scope.audit_read)
    try:
        query = search_log.ActivityQuery.from_params(
            username=username, channel=channel, doc_id=doc_id, date_from=date_from, date_to=date_to
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rows = await search_log.activity_since(db, query, since_id=since_id, limit=limit)
    titles = await search_log.titles_for(db, rows)
    await write_audit(
        db,
        username=principal.username_for_grants or principal.actor_id,
        action="search_log_export",
        resource_type="search_activity",
        detail=f"api_v1 since_id={since_id} rows={len(rows)}",
        ip_address=_client_ip(request),
        actor_type=principal.actor_type,
        commit=True,
    )
    return {
        "items": [search_log.activity_dict(row, titles) for row in rows],
        "next_since_id": rows[-1].id if rows else since_id,
    }


@router.get("/integrations/health")
async def v1_integrations_health(principal: Principal = Depends(get_principal)) -> Any:
    require_scope(principal, Scope.integrations_health)
    return get_registry().health_all()


@router.get("/customers/{customer_ref}")
async def v1_customer_summary(
    customer_ref: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Any:
    return await customer_query.get_customer_summary_for_principal(
        db,
        principal,
        customer_ref,
        ip_address=_client_ip(request),
    )


@router.post("/connectors/document-source/pull")
async def v1_pull_document_source(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    principal: Principal = Depends(get_principal),
) -> Any:
    require_scope(principal, Scope.connectors_pull)
    if job_queue_enabled():
        job_id = await enqueue_job(
            JOB_CONNECTOR_PULL,
            {
                "limit": limit,
                "uploaded_by": principal.username_for_grants or principal.actor_id,
            },
        )
        if job_id:
            return {"queued": True, "job_id": job_id}
    result = await pull_document_source(
        limit=limit,
        uploaded_by=principal.username_for_grants or principal.actor_id,
    )
    return {"queued": False, **result}


@router.post("/ask")
async def v1_ask_sonali(
    request: Request,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Any:
    await hit("ask", f"{principal.actor_type}:{principal.actor_id}")
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    question = str((body or {}).get("question") or (body or {}).get("q") or "")
    lang = (body or {}).get("lang")
    limit = int((body or {}).get("limit") or 8)
    return await ask_sonali.ask_sonali_for_principal(
        db,
        principal,
        question,
        lang=lang,
        limit=min(max(limit, 1), 20),
        ip_address=_client_ip(request),
    )


@router.get("/loans/{loan_ref}")
async def v1_loan_summary(
    loan_ref: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_scope(principal, Scope.loan_read_min)
    summary = get_loan_port().get_loan_summary(loan_ref)
    if summary is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Loan not found")
    from ..access_control import write_audit

    await write_audit(
        db,
        username=principal.username_for_grants or principal.actor_id,
        action="api_loan_summary",
        resource_type="loan",
        resource_id=loan_ref,
        actor_type=principal.actor_type,
        ip_address=_client_ip(request),
        commit=True,
    )
    return {"loan": summary}


@router.get("/hr/employees/{employee_id}")
async def v1_hr_summary(
    employee_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_scope(principal, Scope.hr_read_min)
    summary = get_hr_port().get_employee_summary(employee_id)
    if summary is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Employee not found")
    from ..access_control import write_audit

    await write_audit(
        db,
        username=principal.username_for_grants or principal.actor_id,
        action="api_hr_summary",
        resource_type="hr",
        resource_id=employee_id,
        actor_type=principal.actor_type,
        ip_address=_client_ip(request),
        commit=True,
    )
    return {"employee": summary}


@router.post("/hitl/requests")
async def v1_create_hitl_request(
    request: Request,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Any:
    body = await request.json()
    row = await hitl.create_approval_request(
        db,
        principal,
        tool_name=str(body.get("tool_name") or ""),
        arguments=body.get("arguments") if isinstance(body.get("arguments"), dict) else {},
        purpose=str(body.get("purpose") or ""),
    )
    return {
        "id": row.id,
        "status": row.status.value,
        "tool_name": row.tool_name,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/hitl/requests")
async def v1_list_hitl_requests(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Any:
    require_scope(principal, Scope.hitl_review)
    rows = await hitl.list_pending_approvals(db)
    return {
        "requests": [
            {
                "id": r.id,
                "requester": r.requester,
                "tool_name": r.tool_name,
                "purpose": r.purpose,
                "arguments": r.arguments_json,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


@router.post("/hitl/requests/{request_id}/review")
async def v1_review_hitl_request(
    request_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    approve = bool(body.get("approve"))
    note = str(body.get("note") or "")
    row = await hitl.review_approval_request(
        db, current_user, request_id, approve=approve, note=note
    )
    return {"id": row.id, "status": row.status.value, "reviewed_by": row.reviewed_by}
