"""Admin security status: audit hash-chain verification, SIEM forwarding position, identity configuration."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import write_audit
from ..deps import _client_ip, get_current_user, get_db, require_role
from ..models import AuditLog, IntegrationCursor, Role, User
from ..ports.identity import health_check as identity_health
from ..services.audit_chain import CHAIN_CURSOR, verify_chain
from ..services.siem import siem_enabled

router = APIRouter()


async def audit_status(db: AsyncSession) -> Dict[str, Any]:
    total = int(await db.scalar(select(func.count()).select_from(AuditLog)) or 0)
    unsealed = int(await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.row_hash.is_(None))) or 0)
    oldest_unsealed = await db.scalar(select(func.min(AuditLog.created_at)).where(AuditLog.row_hash.is_(None)))
    cursors = {
        row.name: {"last_id": row.last_id, "updated_at": row.updated_at.isoformat() if row.updated_at else None}
        for row in (await db.execute(select(IntegrationCursor))).scalars()
    }
    return {
        "audit_rows": total,
        "unsealed_rows": unsealed,
        "oldest_unsealed_at": oldest_unsealed.isoformat() if oldest_unsealed else None,
        "chain": cursors.get(CHAIN_CURSOR),
        "siem": {"enabled": siem_enabled(), "cursor": cursors.get("siem")},
        "identity": identity_health(),
    }


@router.get("/api/admin/security/status")
async def api_security_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    require_role(current_user, [Role.admin])
    return await audit_status(db)


@router.post("/api/admin/audit/verify")
async def api_verify_audit_chain(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Recompute every sealed row's hash and walk the chain from genesis (read-only, may take seconds)."""
    require_role(current_user, [Role.admin])
    result = await verify_chain(db)
    await write_audit(
        db,
        username=current_user.username,
        action="audit_chain_verified" if result.get("ok") else "audit_chain_broken",
        resource_type="audit_log",
        resource_id=str(result.get("broken_at") or result.get("tip") or "")[:64],
        detail=str(result.get("reason") or f"linked={result.get('linked')}")[:500],
        ip_address=_client_ip(request),
        commit=True,
    )
    return result
