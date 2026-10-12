"""Phase 3: model_registry, the audited history of runtime AI route switches.

One row per activation. The newest active row for a task overrides the env
route; rolling back re-activates an older row.

Revision ID: 0004_model_registry
Revises: 0003_jobs_outbox_ingest
Create Date: 2026-10-12
"""
from __future__ import annotations

import sqlalchemy as sa

from bcp_project import migration_ops as m

revision = "0004_model_registry"
down_revision = "0003_jobs_outbox_ingest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    m.create_table_if_missing(
        "model_registry",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task", sa.String(48), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("activated_by", sa.String(64), nullable=True),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("deactivated_at", sa.DateTime(), nullable=True),
    )
    m.create_index_if_missing("ix_model_registry_task_active", "model_registry", ["task", "is_active"])


def downgrade() -> None:
    m.drop_table_if_present("model_registry")
