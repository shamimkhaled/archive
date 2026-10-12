"""Model-route changes applied directly, or held for a second administrator.

With ``AI_ROUTE_FOUR_EYES=1`` a switch, reset or rollback is stored as a pending
change; a different administrator approves it, and only then does it take
effect. The requester and the approver are both in the audit log and in the
registry row's notes.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import write_audit
from ..models import AiRouteChange, ModelRegistryEntry
from . import routing

KINDS = ("switch", "reset", "rollback")


class RouteChangeError(ValueError):
    pass


def four_eyes_enabled() -> bool:
    return (os.getenv("AI_ROUTE_FOUR_EYES") or "").strip().lower() in {"1", "true", "yes", "on"}


def _describe(change: AiRouteChange) -> str:
    if change.kind == "reset":
        return f"reset {change.task} to env"
    target = f"{change.provider}/{change.model}"
    return f"rollback {change.task} to #{change.source_entry_id} {target}" if change.kind == "rollback" else f"{change.task} -> {target}"


async def _validate(db: AsyncSession, change: AiRouteChange) -> None:
    if change.kind not in KINDS:
        raise RouteChangeError(f"Unknown change kind {change.kind!r}")
    if change.task not in routing.ROUTABLE_TASKS:
        raise RouteChangeError(f"Unknown task {change.task!r}")
    if change.kind == "rollback":
        row = await db.get(ModelRegistryEntry, change.source_entry_id or 0)
        if row is None:
            raise RouteChangeError("Registry entry not found")
        change.task, change.provider, change.model = row.task, row.provider, row.model
    if change.kind in {"switch", "rollback"}:
        problem = routing.validate_route(change.task, change.provider or "", change.model or "")
        if problem:
            raise RouteChangeError(problem)


async def submit(
    db: AsyncSession,
    *,
    kind: str,
    task: str,
    actor: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    source_entry_id: Optional[int] = None,
    notes: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> AiRouteChange:
    """Apply now (single approver) or store as pending (four-eyes). Caller commits."""
    change = AiRouteChange(
        kind=kind,
        task=task,
        provider=provider,
        model=model,
        source_entry_id=source_entry_id,
        notes=(notes or "").strip() or None,
        requested_by=actor,
    )
    await _validate(db, change)
    if not four_eyes_enabled():
        change.status = "applied"
        change.reviewed_by = actor
        change.reviewed_at = datetime.utcnow()
        await _apply(db, change, approver=None, ip_address=ip_address)
        return change
    pending = (
        await db.execute(
            select(AiRouteChange).where(AiRouteChange.task == change.task, AiRouteChange.status == "pending")
        )
    ).scalars().first()
    if pending is not None:
        raise RouteChangeError(f"Change #{pending.id} for {change.task} is already waiting for approval")
    db.add(change)
    await db.flush()
    await write_audit(
        db,
        username=actor,
        action="ai_route_change_requested",
        resource_type="ai_route",
        resource_id=change.task,
        detail=f"#{change.id} {_describe(change)} {change.notes or ''}".strip()[:500],
        ip_address=ip_address,
    )
    return change


async def _apply(db: AsyncSession, change: AiRouteChange, *, approver: Optional[str], ip_address: Optional[str]) -> None:
    actor = approver or change.requested_by
    suffix = f" (requested by {change.requested_by}, approved by {approver})" if approver else ""
    if change.kind == "reset":
        count = await routing.reset_to_env(db, task=change.task)
        action, detail = "ai_route_reset", f"deactivated={count}{suffix}"
    else:
        notes = change.notes if change.kind == "switch" else f"rollback to #{change.source_entry_id}"
        await routing.activate_route(
            db,
            task=change.task,
            provider=change.provider or "",
            model=change.model or "",
            actor=change.requested_by,
            notes=f"{notes or ''}{suffix}".strip() or None,
        )
        action = "ai_route_switch" if change.kind == "switch" else "ai_route_rollback"
        detail = f"{change.provider}/{change.model} {change.notes or ''}{suffix}".strip()
    await write_audit(
        db,
        username=actor,
        action=action,
        resource_type="ai_route",
        resource_id=change.task,
        detail=detail[:500],
        ip_address=ip_address,
    )


async def _pending(db: AsyncSession, change_id: int) -> AiRouteChange:
    change = await db.get(AiRouteChange, change_id)
    if change is None:
        raise RouteChangeError("Change not found")
    if change.status != "pending":
        raise RouteChangeError(f"Change is already {change.status}")
    return change


async def approve(db: AsyncSession, *, change_id: int, actor: str, ip_address: Optional[str] = None) -> AiRouteChange:
    change = await _pending(db, change_id)
    if change.requested_by == actor:
        raise RouteChangeError("A second administrator must approve this change")
    await _validate(db, change)
    change.status = "applied"
    change.reviewed_by = actor
    change.reviewed_at = datetime.utcnow()
    await write_audit(
        db,
        username=actor,
        action="ai_route_change_approved",
        resource_type="ai_route",
        resource_id=change.task,
        detail=f"#{change.id} {_describe(change)} requested by {change.requested_by}"[:500],
        ip_address=ip_address,
    )
    await _apply(db, change, approver=actor, ip_address=ip_address)
    return change


async def reject(
    db: AsyncSession, *, change_id: int, actor: str, note: str = "", ip_address: Optional[str] = None
) -> AiRouteChange:
    change = await _pending(db, change_id)
    change.status = "cancelled" if change.requested_by == actor else "rejected"
    change.reviewed_by = actor
    change.reviewed_at = datetime.utcnow()
    change.review_note = (note or "").strip() or None
    await write_audit(
        db,
        username=actor,
        action=f"ai_route_change_{change.status}",
        resource_type="ai_route",
        resource_id=change.task,
        detail=f"#{change.id} {_describe(change)} {change.review_note or ''}".strip()[:500],
        ip_address=ip_address,
    )
    return change


async def recent(db: AsyncSession, *, limit: int = 20) -> List[Dict[str, Any]]:
    """Four-eyes requests, newest first (direct changes are recorded in the registry only)."""
    changes = (
        await db.execute(
            select(AiRouteChange).order_by(AiRouteChange.requested_at.desc(), AiRouteChange.id.desc()).limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": c.id,
            "kind": c.kind,
            "task": c.task,
            "provider": c.provider,
            "model": c.model,
            "notes": c.notes,
            "summary": _describe(c),
            "status": c.status,
            "requested_by": c.requested_by,
            "requested_at": c.requested_at.isoformat() if c.requested_at else None,
            "reviewed_by": c.reviewed_by,
            "reviewed_at": c.reviewed_at.isoformat() if c.reviewed_at else None,
            "review_note": c.review_note,
        }
        for c in changes
    ]
