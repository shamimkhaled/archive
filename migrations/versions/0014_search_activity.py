"""Search activity log: queries, filters, results returned, and the documents AI answers drew on.

Revision ID: 0014_search_activity
Revises: 0013_metadata_taxonomy
Create Date: 2026-10-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from bcp_project import migration_ops as m

revision = "0014_search_activity"
down_revision = "0013_metadata_taxonomy"
branch_labels = None
depends_on = None

# Rows are immutable except for linking the audit row once. Deletes are allowed only for the
# retention purge, which sets bcp.search_log_purge for its own transaction.
_IMMUTABLE_FN = """
CREATE OR REPLACE FUNCTION search_activity_append_only() RETURNS trigger AS $$
DECLARE
    probe search_activity%ROWTYPE;
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'search_activity is append-only';
    END IF;
    IF TG_OP = 'DELETE' THEN
        IF coalesce(current_setting('bcp.search_log_purge', true), '') = 'on' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'search_activity is append-only';
    END IF;
    probe := NEW;
    probe.audit_log_id := NULL;
    IF OLD.audit_log_id IS NOT NULL
       OR NEW.audit_log_id IS NULL
       OR to_jsonb(probe) IS DISTINCT FROM to_jsonb(OLD)
    THEN
        RAISE EXCEPTION 'search_activity rows can only be linked to their audit row once';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    m.create_table_if_missing(
        "search_activity",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=True),
        sa.Column("role", sa.String(32), nullable=True),
        sa.Column("channel", sa.String(24), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("query", sa.Text(), nullable=False, server_default=""),
        sa.Column("lang", sa.String(8), nullable=True),
        sa.Column("filters", sa.JSON(), nullable=True),
        sa.Column("mode", sa.String(24), nullable=True),
        sa.Column("cached", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("viewable_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("redacted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("restricted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("results", sa.JSON(), nullable=True),
        sa.Column("doc_ids", sa.Text(), nullable=True),
        sa.Column("ai_provider", sa.String(64), nullable=True),
        sa.Column("ai_model", sa.String(128), nullable=True),
        sa.Column("ai_refused", sa.Boolean(), nullable=True),
        sa.Column("ai_refusal_reason", sa.Text(), nullable=True),
        sa.Column("ai_source_docs", sa.JSON(), nullable=True),
        sa.Column("ai_cited_docs", sa.JSON(), nullable=True),
        sa.Column("ai_withheld_docs", sa.JSON(), nullable=True),
        sa.Column("row_sha256", sa.String(64), nullable=True),
        sa.Column("audit_log_id", sa.Integer(), nullable=True),
        indexes=[
            ("ix_search_activity_created_at", ["created_at"]),
            ("ix_search_activity_user_created", ["username", "created_at"]),
            ("ix_search_activity_channel_created", ["channel", "created_at"]),
        ],
    )
    if m.is_postgres():
        op.execute(_IMMUTABLE_FN)
        op.execute("DROP TRIGGER IF EXISTS search_activity_append_only_row ON search_activity")
        op.execute(
            "CREATE TRIGGER search_activity_append_only_row BEFORE UPDATE OR DELETE ON search_activity "
            "FOR EACH ROW EXECUTE FUNCTION search_activity_append_only()"
        )
        op.execute("DROP TRIGGER IF EXISTS search_activity_append_only_truncate ON search_activity")
        op.execute(
            "CREATE TRIGGER search_activity_append_only_truncate BEFORE TRUNCATE ON search_activity "
            "FOR EACH STATEMENT EXECUTE FUNCTION search_activity_append_only()"
        )


def downgrade() -> None:
    if m.is_postgres():
        op.execute("DROP TRIGGER IF EXISTS search_activity_append_only_truncate ON search_activity")
        op.execute("DROP TRIGGER IF EXISTS search_activity_append_only_row ON search_activity")
        op.execute("DROP FUNCTION IF EXISTS search_activity_append_only()")
    m.drop_table_if_present("search_activity")
