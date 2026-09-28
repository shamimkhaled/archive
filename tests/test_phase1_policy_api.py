"""Phase 1: policy, service scopes, MCP tool catalog."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcp_project.mcp.tools import TOOL_DEFINITIONS
from bcp_project.models import Role, User
from bcp_project.policy import (
    Scope,
    principal_from_user,
    require_scope,
    scopes_for_role,
)
from bcp_project.service_auth import merge_service_scopes
from fastapi import HTTPException


def _user(role: Role, username: str = "u1") -> User:
    return User(
        id=1,
        username=username,
        hashed_password="x",
        role=role,
        is_active=True,
    )


def test_role_scopes_board_member_no_privileged():
    scopes = scopes_for_role(Role.board_member)
    assert Scope.docs_search.value in scopes
    assert Scope.docs_search_privileged.value not in scopes
    assert Scope.mcp_invoke.value in scopes


def test_role_scopes_admin_has_privileged():
    scopes = scopes_for_role(Role.admin)
    assert Scope.docs_search_privileged.value in scopes
    assert Scope.audit_read.value in scopes


def test_principal_from_user_and_require_scope():
    principal = principal_from_user(_user(Role.board_member))
    require_scope(principal, Scope.docs_search)
    with pytest.raises(HTTPException) as exc:
        require_scope(principal, Scope.docs_search_privileged)
    assert exc.value.status_code == 403


def test_merge_service_scopes_intersects_with_user():
    client_scopes = frozenset(
        {
            Scope.docs_search.value,
            Scope.docs_meta.value,
            Scope.docs_search_privileged.value,
            Scope.mcp_invoke.value,
            Scope.audit_read.value,
        }
    )
    member = _user(Role.board_member)
    merged = merge_service_scopes(client_scopes, member)
    assert Scope.docs_search.value in merged
    assert Scope.docs_meta.value in merged
    assert Scope.docs_search_privileged.value in merged  # client-side privilege retained
    assert Scope.mcp_invoke.value in merged
    assert Scope.audit_read.value not in merged  # member role lacks it


def test_merge_service_scopes_without_user_keeps_client():
    client_scopes = frozenset({Scope.docs_search.value, Scope.mcp_invoke.value})
    assert merge_service_scopes(client_scopes, None) == client_scopes


def test_mcp_tool_catalog_read_only():
    names = {t["name"] for t in TOOL_DEFINITIONS}
    # Phase 1 core read tools remain present (Phase 3 extends the catalog).
    assert {
        "search_documents",
        "get_document_metadata",
        "check_document_status",
        "list_my_meetings",
        "get_approved_customer_summary",
    }.issubset(names)
    for tool in TOOL_DEFINITIONS:
        assert "inputSchema" in tool
        assert tool["description"]


def test_api_v1_router_mounted():
    from bcp_project.main_api import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/v1/documents/search" in paths
    assert "/api/v1/mcp/tools" in paths
    assert "/api/v1/metrics" in paths
