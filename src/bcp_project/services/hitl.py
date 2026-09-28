"""Human-in-the-loop approval gate for mutating agent/tool actions."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import write_audit
from ..models import ApprovalRequest, ApprovalStatus, Role, User
from ..policy import Principal, Scope, require_scope

logger = logging.getLogger("bcp_project.services.hitl")

# Tools that must never execute without an approved HITL request.
MUTATING_TOOLS = frozenset(
    {
        "trigger_approved_workflow",
        "request_document_access",
        "connector_pull_now",
    }
)


def tool_requires_approval(tool_name: str) -> bool:
    return (tool_name or "").strip() in MUTATING_TOOLS


async def create_approval_request(
    db: AsyncSession,
    principal: Principal,
    *,
    tool_name: str,
    arguments: Dict[str, Any],
    purpose: str,
) -> ApprovalRequest:
    require_scope(principal, Scope.hitl_request)
    if not tool_requires_approval(tool_name):
        raise HTTPException(status_code=400, detail="Tool does not require HITL approval")
    row = ApprovalRequest(
        requester=principal.username_for_grants or principal.actor_id,
        actor_type=principal.actor_type,
        tool_name=tool_name,
        arguments_json=arguments or {},
        purpose=(purpose or "").strip() or "Agent requested mutation",
        status=ApprovalStatus.pending,
    )
    db.add(row)
    await write_audit(
        db,
        username=row.requester,
        action="hitl_request",
        resource_type="approval",
        detail=f"tool={tool_name}",
        actor_type=principal.actor_type,
        tool_name=tool_name,
        commit=False,
    )
    await db.commit()
    await db.refresh(row)
    return row


async def review_approval_request(
    db: AsyncSession,
    reviewer: User,
    request_id: int,
    *,
    approve: bool,
    note: str = "",
) -> ApprovalRequest:
    if reviewer.role not in (Role.admin, Role.board_secretary):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
    result = await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == int(request_id)))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if row.status != ApprovalStatus.pending:
        raise HTTPException(status_code=409, detail="Request already reviewed")
    row.status = ApprovalStatus.approved if approve else ApprovalStatus.denied
    row.reviewed_by = reviewer.username
    row.review_note = note or None
    row.reviewed_at = datetime.utcnow()
    await write_audit(
        db,
        username=reviewer.username,
        action="hitl_approve" if approve else "hitl_deny",
        resource_type="approval",
        resource_id=str(row.id),
        detail=f"tool={row.tool_name}",
        actor_type="user",
        commit=False,
    )
    await db.commit()
    await db.refresh(row)
    return row


async def list_pending_approvals(db: AsyncSession, *, limit: int = 50) -> List[ApprovalRequest]:
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.status == ApprovalStatus.pending)
        .order_by(ApprovalRequest.created_at.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_approved_request(db: AsyncSession, request_id: int) -> ApprovalRequest:
    result = await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == int(request_id)))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if row.status != ApprovalStatus.approved:
        raise HTTPException(status_code=403, detail="Approval not granted")
    return row
