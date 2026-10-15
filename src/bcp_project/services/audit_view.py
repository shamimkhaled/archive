"""Admin queries over ``audit_logs``: filters, paging, per-row seal check, CSV rows and the last chain check."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AuditLog
from .audit_chain import row_digest

ACTOR_TYPES = {"user": "Person", "agent": "AI agent", "system": "System"}
VERIFY_ACTIONS = ("audit_chain_verified", "audit_chain_broken")
CSV_COLUMNS = (
    "id", "created_at", "username", "actor_type", "action", "resource_type", "resource_id",
    "detail", "ip_address", "correlation_id", "tool_name", "sealed", "prev_hash", "row_hash",
)


@dataclass(frozen=True)
class AuditQuery:
    username: str = ""
    action: str = ""
    resource_type: str = ""
    resource_id: str = ""
    q: str = ""
    actor_type: str = ""
    correlation_id: str = ""
    date_from: Optional[date] = None
    date_to: Optional[date] = None

    @classmethod
    def from_params(
        cls,
        *,
        username: Optional[str] = None,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        q: Optional[str] = None,
        actor_type: Optional[str] = None,
        correlation_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> "AuditQuery":
        def parse(value: Optional[str], name: str) -> Optional[date]:
            if not value:
                return None
            try:
                return date.fromisoformat(value.strip()[:10])
            except ValueError as exc:
                raise ValueError(f"Invalid {name}, expected YYYY-MM-DD") from exc

        actor = (actor_type or "").strip()
        return cls(
            username=(username or "").strip()[:64],
            action=(action or "").strip()[:64],
            resource_type=(resource_type or "").strip()[:64],
            resource_id=(resource_id or "").strip()[:128],
            q=(q or "").strip()[:200],
            actor_type=actor if actor in ACTOR_TYPES else "",
            correlation_id=(correlation_id or "").strip()[:64],
            date_from=parse(date_from, "date_from"),
            date_to=parse(date_to, "date_to"),
        )

    def as_params(self) -> Dict[str, str]:
        out = {
            "username": self.username,
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "q": self.q,
            "actor_type": self.actor_type,
            "correlation_id": self.correlation_id,
            "date_from": self.date_from.isoformat() if self.date_from else "",
            "date_to": self.date_to.isoformat() if self.date_to else "",
        }
        return {k: v for k, v in out.items() if v}

    def conditions(self) -> List[Any]:
        conditions: List[Any] = []
        if self.username:
            conditions.append(func.lower(AuditLog.username) == self.username.lower())
        if self.action:
            conditions.append(AuditLog.action == self.action)
        if self.resource_type:
            conditions.append(AuditLog.resource_type == self.resource_type)
        if self.resource_id:
            conditions.append(AuditLog.resource_id == self.resource_id)
        if self.q:
            needle = self.q.lower()
            conditions.append(
                or_(
                    func.lower(AuditLog.detail).contains(needle, autoescape=True),
                    func.lower(AuditLog.resource_id).contains(needle, autoescape=True),
                )
            )
        if self.actor_type:
            conditions.append(AuditLog.actor_type == self.actor_type)
        if self.correlation_id:
            conditions.append(AuditLog.correlation_id == self.correlation_id)
        if self.date_from:
            conditions.append(AuditLog.created_at >= datetime.combine(self.date_from, datetime.min.time()))
        if self.date_to:
            conditions.append(AuditLog.created_at < datetime.combine(self.date_to + timedelta(days=1), datetime.min.time()))
        return conditions


async def list_audit(
    db: AsyncSession, query: AuditQuery, *, page: int = 1, per_page: int = 50
) -> Tuple[List[AuditLog], int]:
    conditions = query.conditions()
    where = and_(*conditions) if conditions else None
    count_stmt = select(func.count(AuditLog.id))
    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if where is not None:
        count_stmt = count_stmt.where(where)
        stmt = stmt.where(where)
    total = int((await db.execute(count_stmt)).scalar() or 0)
    page = max(1, page)
    rows = (await db.execute(stmt.offset((page - 1) * per_page).limit(per_page))).scalars().all()
    return list(rows), total


async def distinct_values(db: AsyncSession, column: Any) -> List[str]:
    return [v for v in (await db.execute(select(column).distinct().order_by(column))).scalars().all() if v]


async def last_verification(db: AsyncSession) -> Optional[AuditLog]:
    return (
        await db.execute(
            select(AuditLog).where(AuditLog.action.in_(VERIFY_ACTIONS)).order_by(AuditLog.id.desc()).limit(1)
        )
    ).scalar_one_or_none()


def action_label(action: str) -> str:
    return (action or "").replace("_", " ").capitalize()


def resource_href(resource_type: Optional[str], resource_id: Optional[str]) -> Optional[str]:
    if not resource_id:
        return None
    if resource_type == "document":
        return f"/view/{resource_id}"
    if resource_type == "meeting" and resource_id.isdigit():
        return f"/meetings/{resource_id}"
    if resource_type == "board_package" and resource_id.isdigit():
        return f"/board/packages/{resource_id}"
    return None


def seal_state(row: AuditLog) -> Optional[bool]:
    """None while waiting to be sealed; otherwise whether the row still matches its sealed hash."""
    if not row.row_hash:
        return None
    return row_digest(row.prev_hash or "", row) == row.row_hash


def audit_entry(row: AuditLog) -> Dict[str, Any]:
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "username": row.username,
        "actor_type": row.actor_type,
        "action": row.action,
        "action_label": action_label(row.action),
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "resource_href": resource_href(row.resource_type, row.resource_id),
        "detail": row.detail,
        "ip_address": row.ip_address,
        "correlation_id": row.correlation_id,
        "tool_name": row.tool_name,
        "sealed": bool(row.row_hash),
        "intact": seal_state(row),
        "row_hash": row.row_hash,
    }


def csv_row(row: AuditLog) -> Sequence[Any]:
    return (
        row.id,
        row.created_at.isoformat() if row.created_at else "",
        row.username,
        row.actor_type or "",
        row.action,
        row.resource_type,
        row.resource_id or "",
        row.detail or "",
        row.ip_address or "",
        row.correlation_id or "",
        row.tool_name or "",
        "yes" if row.row_hash else "no",
        row.prev_hash or "",
        row.row_hash or "",
    )
