"""Baseline: tables from models.py plus the legacy init_db / migrate_schema columns.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from bcp_project import migration_ops as m
from bcp_project.models import Base

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Fresh databases get every current table. Existing databases keep theirs;
    # later revisions add columns with add_column_if_missing.
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)
    m._changed()

    m.add_columns_if_missing(
        "users",
        [
            sa.Column("email", sa.String(255), nullable=True),
            sa.Column("notifications_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        ],
    )
    m.add_columns_if_missing(
        "board_meetings",
        [
            sa.Column("attendance_open", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("attendance_opened_at", sa.DateTime(), nullable=True),
            sa.Column("attendance_closed_at", sa.DateTime(), nullable=True),
            sa.Column("notifications_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("transcription_status", sa.String(16), nullable=False, server_default="off"),
            sa.Column("transcription_started_at", sa.DateTime(), nullable=True),
            sa.Column("transcription_stopped_at", sa.DateTime(), nullable=True),
            sa.Column("transcription_started_by", sa.String(64), nullable=True),
            sa.Column("transcription_json", sa.JSON(), nullable=True),
            sa.Column("minutes_status", sa.String(16), nullable=False, server_default="off"),
            sa.Column("minutes_draft", sa.Text(), nullable=True),
            sa.Column("minutes_source", sa.String(16), nullable=True),
            sa.Column("minutes_generated_at", sa.DateTime(), nullable=True),
        ],
    )
    m.add_columns_if_missing(
        "meeting_invitations",
        [
            sa.Column("invitation_email_sent_at", sa.DateTime(), nullable=True),
            sa.Column("reminder_48h_sent_at", sa.DateTime(), nullable=True),
            sa.Column("reminder_24h_sent_at", sa.DateTime(), nullable=True),
        ],
    )
    m.add_column_if_missing("documents", sa.Column("summary_json", sa.JSON(), nullable=True))
    m.add_columns_if_missing(
        "audit_logs",
        [
            sa.Column("correlation_id", sa.String(64), nullable=True),
            sa.Column("actor_type", sa.String(16), nullable=True),
            sa.Column("tool_name", sa.String(64), nullable=True),
        ],
    )

    m.create_index_if_missing("ix_audit_logs_correlation_id", "audit_logs", ["correlation_id"])
    m.create_index_if_missing("ix_documents_created_at", "documents", ["created_at"])
    m.create_index_if_missing("ix_documents_doc_type", "documents", ["doc_type"])
    m.create_index_if_missing("ix_documents_doc_date", "documents", ["doc_date"])
    m.create_index_if_missing("ix_board_meetings_scheduled_at", "board_meetings", ["scheduled_at"])


def downgrade() -> None:
    # The baseline adopts a pre-existing production schema; it is not reversible.
    pass
