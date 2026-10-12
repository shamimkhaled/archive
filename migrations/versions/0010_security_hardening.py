"""Phase 8: hash-chained append-only audit log, SIEM cursor, and indexes for hot queries.

Revision ID: 0010_security_hardening
Revises: 0009_board_briefings
Create Date: 2026-10-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from bcp_project import migration_ops as m

revision = "0010_security_hardening"
down_revision = "0009_board_briefings"
branch_labels = None
depends_on = None

# Rows may be sealed once (prev_hash/row_hash set from NULL) and never changed or removed after.
_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION audit_logs_append_only() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' OR TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'audit_logs is append-only';
    END IF;
    IF OLD.row_hash IS NOT NULL
       OR NEW.row_hash IS NULL
       OR (NEW.id, NEW.username, NEW.action, NEW.resource_type, NEW.resource_id, NEW.detail,
           NEW.ip_address, NEW.correlation_id, NEW.actor_type, NEW.tool_name, NEW.created_at)
          IS DISTINCT FROM
          (OLD.id, OLD.username, OLD.action, OLD.resource_type, OLD.resource_id, OLD.detail,
           OLD.ip_address, OLD.correlation_id, OLD.actor_type, OLD.tool_name, OLD.created_at)
    THEN
        RAISE EXCEPTION 'audit_logs rows can only be sealed once';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
AUDIT_COLUMNS = (
    "id", "username", "action", "resource_type", "resource_id", "detail",
    "ip_address", "correlation_id", "actor_type", "tool_name", "created_at", "row_hash",
)

# Skipped on legacy databases that lack a column; the adoption test covers such partial schemas.
INDEXES = (
    ("ix_audit_logs_username_created", "audit_logs", ["username", "created_at"]),
    ("ix_audit_logs_action_created", "audit_logs", ["action", "created_at"]),
    ("ix_audit_logs_resource", "audit_logs", ["resource_type", "resource_id", "created_at"]),
    ("ix_dar_requester_doc_status", "document_access_requests", ["requester_username", "doc_id", "status"]),
    ("ix_documents_type_date", "documents", ["doc_type", "doc_date"]),
    ("ix_job_runs_status_created", "job_runs", ["status", "created_at"]),
)


def upgrade() -> None:
    m.add_columns_if_missing(
        "audit_logs",
        [
            sa.Column("prev_hash", sa.String(64), nullable=True),
            sa.Column("row_hash", sa.String(64), nullable=True),
        ],
    )
    m.create_table_if_missing(
        "integration_cursors",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("last_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
    )

    for name, table, columns in INDEXES:
        if m.has_table(table) and all(m.has_column(table, c) for c in columns):
            m.create_index_if_missing(name, table, columns)

    if m.is_postgres() and all(m.has_column("audit_logs", c) for c in AUDIT_COLUMNS):
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_audit_logs_unsealed ON audit_logs (id) WHERE row_hash IS NULL"
        )
        op.execute(_IMMUTABLE_FN)
        op.execute("DROP TRIGGER IF EXISTS audit_logs_append_only_row ON audit_logs")
        op.execute(
            "CREATE TRIGGER audit_logs_append_only_row BEFORE UPDATE OR DELETE ON audit_logs "
            "FOR EACH ROW EXECUTE FUNCTION audit_logs_append_only()"
        )
        op.execute("DROP TRIGGER IF EXISTS audit_logs_append_only_truncate ON audit_logs")
        op.execute(
            "CREATE TRIGGER audit_logs_append_only_truncate BEFORE TRUNCATE ON audit_logs "
            "FOR EACH STATEMENT EXECUTE FUNCTION audit_logs_append_only()"
        )


def downgrade() -> None:
    if m.is_postgres():
        op.execute("DROP TRIGGER IF EXISTS audit_logs_append_only_truncate ON audit_logs")
        op.execute("DROP TRIGGER IF EXISTS audit_logs_append_only_row ON audit_logs")
        op.execute("DROP FUNCTION IF EXISTS audit_logs_append_only()")
        op.execute("DROP INDEX IF EXISTS ix_audit_logs_unsealed")
    for index, table in (
        ("ix_job_runs_status_created", "job_runs"),
        ("ix_documents_type_date", "documents"),
        ("ix_dar_requester_doc_status", "document_access_requests"),
        ("ix_audit_logs_resource", "audit_logs"),
        ("ix_audit_logs_action_created", "audit_logs"),
        ("ix_audit_logs_username_created", "audit_logs"),
    ):
        if m.has_index(table, index):
            op.drop_index(index, table_name=table)
    m.drop_table_if_present("integration_cursors")
    m.drop_column_if_present("audit_logs", "row_hash")
    m.drop_column_if_present("audit_logs", "prev_hash")
