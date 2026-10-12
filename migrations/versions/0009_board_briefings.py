"""Phase 7: Board-paper briefings with cited headings and ai_draft -> management_reviewed -> approved.

Revision ID: 0009_board_briefings
Revises: 0008_minutes_workflow
Create Date: 2026-10-12
"""
from __future__ import annotations

import sqlalchemy as sa

from bcp_project import migration_ops as m

revision = "0009_board_briefings"
down_revision = "0008_minutes_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    m.create_table_if_missing(
        "board_briefings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("doc_id", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="ai_draft"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("content", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="ai"),
        sa.Column("provenance", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.String(64), nullable=False),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
        sa.Column("edited_by", sa.String(64), nullable=True),
        sa.Column("edited_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_by", sa.String(64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by", sa.String(64), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("doc_id", "version", name="uq_board_briefing_doc_version"),
        indexes=[("ix_board_briefings_doc_id", ["doc_id"]), ("ix_board_briefings_state", ["state"])],
    )


def downgrade() -> None:
    m.drop_table_if_present("board_briefings")
