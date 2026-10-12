"""Phase 0: migrations, metrics, and logging."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcp_project.logging_config import CorrelationFilter, JsonFormatter
from bcp_project.migrate import upgrade_connection
from bcp_project.observability import (
    observe_histogram,
    incr,
    render_prometheus,
    reset_metrics_for_tests,
)


def test_migrations_upgrade_fresh_db_and_rerun_is_noop(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        upgrade_connection(conn)
    with engine.begin() as conn:
        upgrade_connection(conn)
    tables = set(sa.inspect(engine).get_table_names())
    assert {"users", "documents", "audit_logs", "board_meetings", "alembic_version"} <= tables
    with engine.connect() as conn:
        versions = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
    assert len(versions) == 1


def test_migrations_adopt_legacy_schema(tmp_path):
    """A pre-Alembic database missing later columns is brought up to date."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE audit_logs (id INTEGER PRIMARY KEY, username VARCHAR(64), action VARCHAR(64), resource_type VARCHAR(64))"))
    with engine.begin() as conn:
        upgrade_connection(conn)
    cols = {c["name"] for c in sa.inspect(engine).get_columns("audit_logs")}
    assert {"correlation_id", "actor_type", "tool_name"} <= cols


def test_prometheus_render_contains_counters_and_histograms():
    reset_metrics_for_tests()
    incr("api_v1.search.ok", 3)
    observe_histogram("http_request_duration", 0.2, {"method": "GET", "route": "/api/search", "status": "2xx"})
    text = render_prometheus()
    assert "bcp_api_v1_search_ok_total 3" in text
    assert 'bcp_http_request_duration_seconds_bucket{method="GET",route="/api/search",status="2xx",le="0.25"} 1' in text
    assert 'bcp_http_request_duration_seconds_count{method="GET",route="/api/search",status="2xx"} 1' in text
    reset_metrics_for_tests()


def test_json_formatter_includes_service_and_correlation():
    record = logging.makeLogRecord({"name": "x", "levelname": "INFO", "msg": "hello %s", "args": ("bank",)})
    CorrelationFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "hello bank"
    assert payload["service"]
    assert "correlation_id" in payload
