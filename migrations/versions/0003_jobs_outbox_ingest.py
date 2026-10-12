"""Phase 2: job_runs, transactional outbox, and the document ingest state machine.

Revision ID: 0003_jobs_outbox_ingest
Revises: 0002_permission_mfa
Create Date: 2026-10-12
"""
from __future__ import annotations

import sqlalchemy as sa

from bcp_project import migration_ops as m

revision = "0003_jobs_outbox_ingest"
down_revision = "0002_permission_mfa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    m.create_table_if_missing(
        "job_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_type", sa.String(64), nullable=False),
        sa.Column("queue", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resource_type", sa.String(32), nullable=True),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("worker", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        indexes=[
            ("ix_job_runs_status", ["status"]),
            ("ix_job_runs_resource", ["resource_type", "resource_id"]),
            ("ix_job_runs_created_at", ["created_at"]),
        ],
    )
    m.create_table_if_missing(
        "outbox_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("topic", sa.String(64), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        indexes=[("ix_outbox_events_unpublished", ["published_at", "id"])],
    )
    m.add_columns_if_missing(
        "documents",
        [
            sa.Column("ingest_status", sa.String(24), nullable=False, server_default="indexed"),
            sa.Column("ingest_error", sa.Text(), nullable=True),
            sa.Column("ingest_updated_at", sa.DateTime(), nullable=True),
            sa.Column("page_count", sa.Integer(), nullable=True),
        ],
    )
    m.create_index_if_missing("ix_documents_ingest_status", "documents", ["ingest_status"])


def downgrade() -> None:
    m.drop_column_if_present("documents", "page_count")
    m.drop_column_if_present("documents", "ingest_updated_at")
    m.drop_column_if_present("documents", "ingest_error")
    m.drop_column_if_present("documents", "ingest_status")
    m.drop_table_if_present("outbox_events")
    m.drop_table_if_present("job_runs")
