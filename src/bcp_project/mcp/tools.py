"""MCP tool catalog — read tools + HITL-gated mutations."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..jobs.handlers import JOB_CONNECTOR_PULL
from ..jobs.queue import enqueue_job, job_queue_enabled
from ..policy import Principal, Scope, require_scope
from ..ports.loan_hr import get_hr_port, get_loan_port
from ..services import ask_sonali, customer_query, document_query, hitl
from ..services.connector_sync import pull_document_source

TOOL_DEFINITIONS = [
    {
        "name": "search_documents",
        "description": "Search the bank archive with hybrid semantic + keyword retrieval. Results are filtered by the caller's authorization.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Search query (English or Bangla)"},
                "lang": {"type": "string", "enum": ["en", "bn", "any"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["q"],
        },
    },
    {
        "name": "get_document_metadata",
        "description": "Retrieve metadata (and summary if authorized) for a document ID. Never returns PDF bytes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
            },
            "required": ["doc_id"],
        },
    },
    {
        "name": "check_document_status",
        "description": "Check whether a document exists, has a summary, and whether the principal may view it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
            },
            "required": ["doc_id"],
        },
    },
    {
        "name": "list_my_meetings",
        "description": "List board meetings visible to the authenticated / on-behalf-of user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
        },
    },
    {
        "name": "get_approved_customer_summary",
        "description": "Fetch a read-minimized customer summary via the bank CustomerPort (allowlisted fields only). Never returns account balances or full CIF dumps.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_ref": {
                    "type": "string",
                    "description": "Bank customer reference / CIF id",
                },
            },
            "required": ["customer_ref"],
        },
    },
    {
        "name": "ask_sonali",
        "description": "Ask Sonali Bank — governed Q&A with mandatory Document+Page citations. Refuses when no authorized sources match (No Source, No Answer).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "lang": {"type": "string", "enum": ["en", "bn", "any"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
            },
            "required": ["question"],
        },
    },
    {
        "name": "get_approved_loan_summary",
        "description": "Read-minimized loan summary via LoanPort. Never returns balances or full facility dumps.",
        "inputSchema": {
            "type": "object",
            "properties": {"loan_ref": {"type": "string"}},
            "required": ["loan_ref"],
        },
    },
    {
        "name": "get_approved_hr_summary",
        "description": "Read-minimized HR employee summary via HrPort. Never returns salary or PII dumps.",
        "inputSchema": {
            "type": "object",
            "properties": {"employee_id": {"type": "string"}},
            "required": ["employee_id"],
        },
    },
    {
        "name": "connector_pull_now",
        "description": "HITL-gated: pull pending documents from the configured document source. Requires an approved approval_request_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "approval_request_id": {"type": "integer"},
            },
            "required": ["approval_request_id"],
        },
    },
    {
        "name": "request_document_access",
        "description": "HITL-gated: request document access for a user. Requires an approved approval_request_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "purpose": {"type": "string"},
                "approval_request_id": {"type": "integer"},
            },
            "required": ["doc_id", "approval_request_id"],
        },
    },
    {
        "name": "trigger_approved_workflow",
        "description": "HITL-gated: trigger a named bank workflow after human approval. Requires approval_request_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow": {"type": "string"},
                "payload": {"type": "object"},
                "approval_request_id": {"type": "integer"},
            },
            "required": ["workflow", "approval_request_id"],
        },
    },
]


async def _require_hitl_approval(
    db: AsyncSession,
    tool_name: str,
    arguments: Dict[str, Any],
) -> None:
    approval_id = arguments.get("approval_request_id")
    if approval_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "hitl_required",
                "message": f"Tool '{tool_name}' requires human approval. POST /api/v1/hitl/requests first.",
                "tool_name": tool_name,
            },
        )
    await hitl.get_approved_request(db, int(approval_id))


async def invoke_tool(
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    db: AsyncSession,
    principal: Principal,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    name = (tool_name or "").strip()
    args = arguments or {}

    if hitl.tool_requires_approval(name):
        await _require_hitl_approval(db, name, args)

    if name == "search_documents":
        return await document_query.search_documents_for_principal(
            db,
            principal,
            str(args.get("q") or ""),
            lang=args.get("lang"),
            limit=int(args.get("limit") or 10),
            ip_address=ip_address,
            tool_name=name,
        )
    if name == "get_document_metadata":
        return await document_query.get_document_metadata_for_principal(
            db,
            principal,
            str(args.get("doc_id") or ""),
            ip_address=ip_address,
            tool_name=name,
        )
    if name == "check_document_status":
        return await document_query.get_document_status_for_principal(
            db,
            principal,
            str(args.get("doc_id") or ""),
            ip_address=ip_address,
            tool_name=name,
        )
    if name == "list_my_meetings":
        return await document_query.list_meetings_for_principal(
            db,
            principal,
            limit=int(args.get("limit") or 20),
            ip_address=ip_address,
            tool_name=name,
        )
    if name == "get_approved_customer_summary":
        return await customer_query.get_customer_summary_for_principal(
            db,
            principal,
            str(args.get("customer_ref") or ""),
            ip_address=ip_address,
            tool_name=name,
        )
    if name == "ask_sonali":
        return await ask_sonali.ask_sonali_for_principal(
            db,
            principal,
            str(args.get("question") or args.get("q") or ""),
            lang=args.get("lang"),
            limit=int(args.get("limit") or 8),
            ip_address=ip_address,
            tool_name=name,
        )
    if name == "get_approved_loan_summary":
        require_scope(principal, Scope.loan_read_min)
        summary = get_loan_port().get_loan_summary(str(args.get("loan_ref") or ""))
        if summary is None:
            raise HTTPException(status_code=404, detail="Loan not found")
        return {"loan": summary}
    if name == "get_approved_hr_summary":
        require_scope(principal, Scope.hr_read_min)
        summary = get_hr_port().get_employee_summary(str(args.get("employee_id") or ""))
        if summary is None:
            raise HTTPException(status_code=404, detail="Employee not found")
        return {"employee": summary}
    if name == "connector_pull_now":
        require_scope(principal, Scope.connectors_pull)
        limit = int(args.get("limit") or 20)
        uploaded_by = principal.username_for_grants or principal.actor_id
        if job_queue_enabled():
            job_id = await enqueue_job(
                JOB_CONNECTOR_PULL,
                {"limit": limit, "uploaded_by": uploaded_by},
            )
            if job_id:
                return {"queued": True, "job_id": job_id, "hitl_approved": True}
        result = await pull_document_source(limit=limit, uploaded_by=uploaded_by)
        return {"queued": False, "hitl_approved": True, **result}
    if name == "request_document_access":
        return {
            "accepted": True,
            "hitl_approved": True,
            "doc_id": str(args.get("doc_id") or ""),
            "purpose": str(args.get("purpose") or ""),
            "note": "Access request recorded for secretariat review (stub).",
        }
    if name == "trigger_approved_workflow":
        return {
            "accepted": True,
            "hitl_approved": True,
            "workflow": str(args.get("workflow") or ""),
            "payload": args.get("payload") if isinstance(args.get("payload"), dict) else {},
            "note": "Workflow trigger acknowledged (stub — bank ESB wiring TBD).",
        }
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown tool: {name}")
