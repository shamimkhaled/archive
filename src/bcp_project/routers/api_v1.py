"""Versioned machine API (/api/v1) for integrators and AI agents."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import _client_ip, get_current_user, get_db
from ..jobs.handlers import JOB_CONNECTOR_PULL
from ..jobs.queue import enqueue_job, job_queue_enabled
from ..mcp.tools import TOOL_DEFINITIONS, invoke_tool
from ..models import User
from ..observability import snapshot_metrics
from ..policy import Principal, Scope, require_scope
from ..ports.loan_hr import get_hr_port, get_loan_port
from ..ports.registry import get_registry
from ..service_auth import authenticate_api_v1_principal
from ..services import ask_sonali, customer_query, document_query, hitl
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
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> Any:
    return await document_query.search_documents_for_principal(
        db,
        principal,
        q,
        lang=lang,
        limit=limit,
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
