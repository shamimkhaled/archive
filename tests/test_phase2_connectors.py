"""Phase 2: adapter registry, customer port, circuit breaker, events."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcp_project.mcp.tools import TOOL_DEFINITIONS
from bcp_project.policy import Scope, scopes_for_role
from bcp_project.ports.circuit import CircuitBreaker
from bcp_project.ports.customer import MockCustomerAdapter, _minimize, reset_customer_port_for_tests
from bcp_project.ports.document_source import LocalInboxDocumentSource, reset_document_source_for_tests
from bcp_project.ports.events import NullEventPublisher, WebhookEventPublisher, reset_event_publisher_for_tests
from bcp_project.ports.identity import LocalPasswordIdentity, oidc_enabled, reset_identity_port_for_tests
from bcp_project.ports.registry import get_registry, reset_registry_for_tests
from bcp_project.models import Role


def setup_function():
    reset_registry_for_tests()
    reset_customer_port_for_tests()
    reset_document_source_for_tests()
    reset_event_publisher_for_tests()
    reset_identity_port_for_tests()


def test_circuit_opens_after_threshold():
    breaker = CircuitBreaker("test", failure_threshold=2, reset_timeout_seconds=60)

    def boom():
        raise RuntimeError("fail")

    with pytest.raises(RuntimeError):
        breaker.call(boom)
    with pytest.raises(RuntimeError):
        breaker.call(boom)
    assert breaker.is_open
    with pytest.raises(RuntimeError, match="Circuit open"):
        breaker.call(lambda: 1)


def test_circuit_success_resets():
    breaker = CircuitBreaker("ok", failure_threshold=3)
    assert breaker.call(lambda: 42) == 42
    snap = breaker.snapshot()
    assert snap["failures"] == 0
    assert snap["open"] is False


def test_customer_field_allowlist():
    raw = {
        "customer_ref": "C1",
        "display_name": "Ada",
        "balance": 999999,
        "national_id": "secret",
        "branch_code": "01",
        "status": "active",
    }
    minimized = _minimize(raw)
    assert "balance" not in minimized
    assert "national_id" not in minimized
    assert minimized["customer_ref"] == "C1"
    assert minimized["display_name"] == "Ada"


def test_mock_customer_adapter():
    adapter = MockCustomerAdapter()
    summary = adapter.get_customer_summary("SB-100")
    assert summary is not None
    assert summary["customer_ref"] == "SB-100"
    assert set(summary.keys()) <= {
        "customer_ref",
        "display_name",
        "branch_code",
        "segment",
        "status",
        "relationship_manager",
    }


def test_local_inbox_list_and_process(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    monkeypatch.setenv("DOCUMENT_SOURCE", "local")
    src = LocalInboxDocumentSource(str(inbox), str(processed))
    pdf = inbox / "memo.pdf"
    pdf.write_bytes(b"%PDF-1.4 demo")
    pending = src.list_pending()
    assert len(pending) == 1
    assert src.fetch_bytes("memo.pdf").startswith(b"%PDF")
    src.mark_processed("memo.pdf")
    assert not pdf.exists()
    assert (processed / "memo.pdf").exists()


def test_registry_health_includes_phase2_adapters():
    report = get_registry().health_all()
    names = set(report["adapters"].keys())
    assert {"llm", "customer", "document_source", "identity", "events"}.issubset(names)
    assert "circuit" in report["adapters"]["customer"]


def test_admin_has_phase2_scopes():
    scopes = scopes_for_role(Role.admin)
    assert Scope.cbs_customer_read_min.value in scopes
    assert Scope.integrations_health.value in scopes
    assert Scope.connectors_pull.value in scopes


def test_mcp_includes_customer_tool():
    names = {t["name"] for t in TOOL_DEFINITIONS}
    assert "get_approved_customer_summary" in names


def test_null_and_webhook_publisher_modes(monkeypatch):
    reset_event_publisher_for_tests()
    monkeypatch.delenv("WEBHOOK_URLS", raising=False)
    from bcp_project.ports.events import get_event_publisher

    assert isinstance(get_event_publisher(), NullEventPublisher)
    reset_event_publisher_for_tests()
    monkeypatch.setenv("WEBHOOK_URLS", "http://127.0.0.1:9/hook")
    assert isinstance(get_event_publisher(), WebhookEventPublisher)


def test_local_identity_mode(monkeypatch):
    monkeypatch.delenv("OIDC_ENABLED", raising=False)
    reset_identity_port_for_tests()
    from bcp_project.ports.identity import get_identity_port

    assert oidc_enabled() is False
    assert isinstance(get_identity_port(), LocalPasswordIdentity)


def test_api_v1_phase2_routes_mounted():
    from bcp_project.main_api import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/v1/integrations/health" in paths
    assert "/api/v1/customers/{customer_ref}" in paths
    assert "/api/v1/connectors/document-source/pull" in paths
    assert "/auth/oidc/start" in paths
    assert "/auth/oidc/callback" in paths
