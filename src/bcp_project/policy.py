"""Central authorization policy for HTTP /api/v1 and MCP tools.

RBAC (roles + scopes) and ABAC (document grants) live here so agents never
bypass bank access rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, Optional

from fastapi import HTTPException, status

from .access_control import can_view_with_grant, is_archive_view_privileged
from .models import DocumentAccessRequest, Role, User


class Scope(str, Enum):
    docs_search = "docs:search"
    docs_meta = "docs:meta"
    docs_status = "docs:status"
    docs_search_privileged = "docs:search:privileged"
    meetings_list = "meetings:list"
    mcp_invoke = "mcp:invoke"
    audit_read = "audit:read"
    cbs_customer_read_min = "cbs:customer.read_min"
    integrations_health = "integrations:health"
    connectors_pull = "connectors:pull"
    ask_sonali = "ask:sonali"
    hitl_request = "hitl:request"
    hitl_review = "hitl:review"
    loan_read_min = "loan:read_min"
    hr_read_min = "hr:read_min"


# Role → default scopes for human JWT principals.
ROLE_SCOPES: dict[Role, FrozenSet[str]] = {
    Role.admin: frozenset(
        {
            Scope.docs_search.value,
            Scope.docs_meta.value,
            Scope.docs_status.value,
            Scope.docs_search_privileged.value,
            Scope.meetings_list.value,
            Scope.mcp_invoke.value,
            Scope.audit_read.value,
            Scope.cbs_customer_read_min.value,
            Scope.integrations_health.value,
            Scope.connectors_pull.value,
            Scope.ask_sonali.value,
            Scope.hitl_request.value,
            Scope.hitl_review.value,
            Scope.loan_read_min.value,
            Scope.hr_read_min.value,
        }
    ),
    Role.board_secretary: frozenset(
        {
            Scope.docs_search.value,
            Scope.docs_meta.value,
            Scope.docs_status.value,
            Scope.docs_search_privileged.value,
            Scope.meetings_list.value,
            Scope.mcp_invoke.value,
            Scope.cbs_customer_read_min.value,
            Scope.integrations_health.value,
            Scope.ask_sonali.value,
            Scope.hitl_request.value,
            Scope.hitl_review.value,
        }
    ),
    Role.board_member: frozenset(
        {
            Scope.docs_search.value,
            Scope.docs_meta.value,
            Scope.docs_status.value,
            Scope.meetings_list.value,
            Scope.mcp_invoke.value,
            Scope.ask_sonali.value,
            Scope.hitl_request.value,
        }
    ),
    Role.uploader: frozenset(),
}


@dataclass
class Principal:
    """Authenticated actor for policy decisions."""

    actor_id: str
    actor_type: str  # user | service | agent
    scopes: FrozenSet[str] = field(default_factory=frozenset)
    role: Optional[Role] = None
    user: Optional[User] = None
    client_name: Optional[str] = None

    @property
    def username_for_grants(self) -> Optional[str]:
        if self.user is not None:
            return self.user.username
        return None

    def has_scope(self, scope: str | Scope) -> bool:
        value = scope.value if isinstance(scope, Scope) else scope
        return value in self.scopes


def scopes_for_role(role: Role) -> FrozenSet[str]:
    return ROLE_SCOPES.get(role, frozenset())


def principal_from_user(user: User) -> Principal:
    return Principal(
        actor_id=user.username,
        actor_type="user",
        scopes=scopes_for_role(user.role),
        role=user.role,
        user=user,
    )


def require_scope(principal: Principal, scope: str | Scope) -> None:
    value = scope.value if isinstance(scope, Scope) else scope
    if not principal.has_scope(value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required scope: {value}",
        )


def is_view_privileged_principal(principal: Principal) -> bool:
    if principal.has_scope(Scope.docs_search_privileged):
        return True
    if principal.user is not None:
        return is_archive_view_privileged(principal.user)
    return False


def can_principal_view_document(
    principal: Principal,
    grant: Optional[DocumentAccessRequest],
) -> bool:
    if is_view_privileged_principal(principal):
        return True
    if principal.user is None:
        return False
    return can_view_with_grant(principal.user, grant)


def assert_can_use_document_tools(principal: Principal) -> None:
    """Document tools require a bound human user unless privileged service scope."""
    if is_view_privileged_principal(principal):
        return
    if principal.user is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Document access requires a user principal (JWT or X-On-Behalf-Of).",
        )
