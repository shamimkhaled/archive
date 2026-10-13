"""Linked, hash-sealed meeting records and the responsible division on action items.

Revision ID: 0012_meeting_records
Revises: 0011_retention_route_approval
Create Date: 2026-10-13
"""
from __future__ import annotations

import sqlalchemy as sa

from bcp_project import migration_ops as m

revision = "0012_meeting_records"
down_revision = "0011_retention_route_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    m.add_column_if_missing("meeting_actions", sa.Column("division", sa.String(128), nullable=True))

    m.create_table_if_missing(
        "meeting_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("meeting_id", sa.Integer(), sa.ForeignKey("board_meetings.id"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("file_location", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("content_type", sa.String(64), nullable=True),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("archive_doc_id", sa.String(128), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("meeting_id", "kind", "version", name="uq_meeting_record_kind_version"),
        indexes=[("ix_meeting_records_meeting_id", ["meeting_id"])],
    )


def downgrade() -> None:
    m.drop_table_if_present("meeting_records")
    m.drop_column_if_present("meeting_actions", "division")
