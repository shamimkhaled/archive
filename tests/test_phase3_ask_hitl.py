"""Phase 3: Ask Sonali, HITL, multi-queue, loan/HR stubs."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcp_project.jobs.queue import (
    QUEUE_INGEST,
    QUEUE_MEETING,
    QUEUE_NOTIFY,
    QUEUE_KEY,
    multi_queue_enabled,
    queue_for_job_type,
    worker_queue_pairs,
)
from bcp_project.mcp.tools import TOOL_DEFINITIONS, invoke_tool
from bcp_project.models import Role, User
from bcp_project.policy import Scope, principal_from_user, scopes_for_role
from bcp_project.ports.loan_hr import (
    HR_ALLOWED,
    LOAN_ALLOWED,
    MockHrAdapter,
    MockLoanAdapter,
    reset_loan_hr_for_tests,
)
from bcp_project.ports.registry import get_registry, reset_registry_for_tests
from bcp_project.services import ask_sonali, hitl


def _user(role: Role = Role.admin, username: str = "admin1") -> User:
    return User(id=1, username=username, hashed_password="x", role=role, is_active=True)


def test_phase3_scopes_on_admin_and_member():
    admin = scopes_for_role(Role.admin)
    member = scopes_for_role(Role.board_member)
    assert Scope.ask_sonali.value in admin
    assert Scope.ask_sonali.value in member
    assert Scope.hitl_review.value in admin
    assert Scope.hitl_review.value not in member
    assert Scope.loan_read_min.value in admin
    assert Scope.hr_read_min.value in admin


def test_mcp_catalog_includes_ask_and_hitl_tools():
    names = {t["name"] for t in TOOL_DEFINITIONS}
    assert "ask_sonali" in names
    assert "get_approved_loan_summary" in names
    assert "get_approved_hr_summary" in names
    assert "connector_pull_now" in names
    assert "request_document_access" in names
    assert "trigger_approved_workflow" in names


def test_hitl_mutating_tools_require_flag():
    assert hitl.tool_requires_approval("connector_pull_now")
    assert hitl.tool_requires_approval("request_document_access")
    assert hitl.tool_requires_approval("trigger_approved_workflow")
    assert not hitl.tool_requires_approval("ask_sonali")
    assert not hitl.tool_requires_approval("search_documents")


@pytest.mark.asyncio
async def test_mutating_tool_blocked_without_approval():
    principal = principal_from_user(_user())
    db = MagicMock()
    with pytest.raises(HTTPException) as exc:
        await invoke_tool(
            "trigger_approved_workflow",
            {"workflow": "demo"},
            db=db,
            principal=principal,
        )
    assert exc.value.status_code == 403
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail.get("error") == "hitl_required"


def test_multi_queue_routing(monkeypatch):
    monkeypatch.setenv("ENABLE_MULTI_QUEUE", "1")
    assert multi_queue_enabled() is True
    assert queue_for_job_type("chunk_index")[0] == QUEUE_INGEST
    assert queue_for_job_type("connector_pull")[0] == QUEUE_INGEST
    assert queue_for_job_type("meeting_minutes")[0] == QUEUE_MEETING
    assert queue_for_job_type("webhook_dispatch")[0] == QUEUE_NOTIFY
    assert queue_for_job_type("unknown_job")[0] == QUEUE_KEY

    monkeypatch.setenv("JOB_WORKER_QUEUES", "meeting")
    pairs = worker_queue_pairs()
    assert pairs == [(QUEUE_MEETING, pairs[0][1])]

    monkeypatch.setenv("ENABLE_MULTI_QUEUE", "0")
    assert queue_for_job_type("chunk_index")[0] == QUEUE_KEY


def test_loan_hr_field_allowlist():
    reset_loan_hr_for_tests()
    loan = MockLoanAdapter().get_loan_summary("LN-1")
    hr = MockHrAdapter().get_employee_summary("E-1")
    assert loan is not None and set(loan.keys()) <= set(LOAN_ALLOWED)
    assert "principal" not in loan
    assert hr is not None and set(hr.keys()) <= set(HR_ALLOWED)
    assert "salary" not in hr


def test_registry_includes_loan_hr():
    reset_registry_for_tests()
    report = get_registry().health_all()
    assert "loan" in report["adapters"]
    assert "hr" in report["adapters"]
    assert report["adapters"]["loan"]["health"]["ok"] is True
    assert report["adapters"]["hr"]["health"]["ok"] is True


@pytest.mark.asyncio
async def test_ask_sonali_refuses_without_sources():
    principal = principal_from_user(_user(Role.board_member))
    db = AsyncMock()
    with patch(
        "bcp_project.services.ask_sonali.document_query.search_documents_for_principal",
        new_callable=AsyncMock,
        return_value={"results": [{"doc_id": "X", "can_view": False}]},
    ), patch(
        "bcp_project.services.ask_sonali.write_audit",
        new_callable=AsyncMock,
    ):
        result = await ask_sonali.ask_sonali_for_principal(db, principal, "What is policy X?")
    assert result["refused"] is True
    assert result["citations"] == []
    assert "No Source" in (result["refusal_reason"] or "")


@pytest.mark.asyncio
async def test_ask_sonali_enforces_citations():
    principal = principal_from_user(_user())
    db = AsyncMock()
    evidence_search = {
        "results": [
            {
                "doc_id": "SB-1",
                "can_view": True,
                "snippet": "Board approved ICT procurement.",
                "page": 2,
                "doc_type": "minutes",
            }
        ]
    }
    fake_llm = MagicMock()
    fake_llm.chat_json.return_value = {
        "refused": False,
        "answer": "ICT procurement was approved.",
        "citations": [],  # missing → must refuse
    }
    with patch(
        "bcp_project.services.ask_sonali.document_query.search_documents_for_principal",
        new_callable=AsyncMock,
        return_value=evidence_search,
    ), patch(
        "bcp_project.services.ask_sonali.get_llm_client",
        return_value=fake_llm,
    ), patch(
        "bcp_project.services.ask_sonali.write_audit",
        new_callable=AsyncMock,
    ):
        result = await ask_sonali.ask_sonali_for_principal(db, principal, "ICT procurement?")
    assert result["refused"] is True
    assert result["answer"] is None


def test_api_v1_phase3_routes_mounted():
    from bcp_project.main_api import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/ask" in paths
    assert "/api/v1/ask" in paths
    assert "/api/v1/hitl/requests" in paths
    assert "/api/v1/hitl/requests/{request_id}/review" in paths
    assert "/api/v1/loans/{loan_ref}" in paths
    assert "/api/v1/hr/employees/{employee_id}" in paths


def test_healthz_route_exists():
    from bcp_project.main_api import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/healthz" in paths
    assert "/readyz" in paths
