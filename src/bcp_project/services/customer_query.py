"""Customer summary queries behind policy + audit."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..access_control import write_audit
from ..observability import incr
from ..policy import Principal, Scope, require_scope
from ..ports.customer import get_customer_port
from ..ports.registry import get_registry


async def get_customer_summary_for_principal(
    db: AsyncSession,
    principal: Principal,
    customer_ref: str,
    *,
    ip_address: Optional[str] = None,
    tool_name: Optional[str] = None,
) -> Dict[str, Any]:
    require_scope(principal, Scope.cbs_customer_read_min)
    ref = (customer_ref or "").strip()
    if not ref:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="customer_ref required")

    reg = get_registry().get("customer")
    try:
        summary = reg.circuit.call(lambda: get_customer_port().get_customer_summary(ref))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Customer system unavailable: {str(exc)[:160]}",
        ) from exc

    if summary is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")

    await write_audit(
        db,
        username=principal.username_for_grants or principal.actor_id,
        action="api_customer_summary",
        resource_type="customer",
        resource_id=ref,
        detail="fields=" + ",".join(sorted(summary.keys())),
        ip_address=ip_address,
        actor_type=principal.actor_type,
        tool_name=tool_name,
        commit=True,
    )
    incr("api_v1.customer.ok")
    return {"customer": summary}
