"""Phase 6: minutes review workflow, recorded uploads, speaker labels, decisions and actions.

Revision ID: 0008_minutes_workflow
Revises: 0007_meeting_doc_archive
Create Date: 2026-10-12
"""
from __future__ import annotations

import sqlalchemy as sa

from bcp_project import migration_ops as m

revision = "0008_minutes_workflow"
down_revision = "0007_meeting_doc_archive"
branch_labels = None
depends_on = None

MEETING_COLUMNS = [
    sa.Column("minutes_state", sa.String(16), nullable=False, server_default="draft"),
    sa.Column("minutes_version", sa.Integer(), nullable=False, server_default="1"),
    sa.Column("minutes_edited_by", sa.String(64), nullable=True),
    sa.Column("minutes_edited_at", sa.DateTime(), nullable=True),
    sa.Column("minutes_submitted_by", sa.String(64), nullable=True),
    sa.Column("minutes_submitted_at", sa.DateTime(), nullable=True),
    sa.Column("minutes_approved_by", sa.String(64), nullable=True),
    sa.Column("minutes_approved_at", sa.DateTime(), nullable=True),
    sa.Column("minutes_review_note", sa.Text(), nullable=True),
    sa.Column("minutes_sha256", sa.String(64), nullable=True),
    sa.Column("minutes_doc_id", sa.String(128), nullable=True),
    sa.Column("recording_filename", sa.String(255), nullable=True),
    sa.Column("recording_sha256", sa.String(64), nullable=True),
    sa.Column("speaker_map", sa.JSON(), nullable=True),
    sa.Column("diarization_status", sa.String(16), nullable=True),
]


def upgrade() -> None:
    m.add_columns_if_missing("board_meetings", MEETING_COLUMNS)
    m.create_index_if_missing("ix_board_meetings_minutes_state", "board_meetings", ["minutes_state"])

    m.create_table_if_missing(
        "meeting_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("meeting_id", sa.Integer(), sa.ForeignKey("board_meetings.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("agenda_item", sa.String(255), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    m.create_index_if_missing("ix_meeting_decisions_meeting_id", "meeting_decisions", ["meeting_id"])

    m.create_table_if_missing(
        "meeting_actions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("meeting_id", sa.Integer(), sa.ForeignKey("board_meetings.id"), nullable=False),
        sa.Column("decision_id", sa.Integer(), sa.ForeignKey("meeting_decisions.id"), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("owner", sa.String(128), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    m.create_index_if_missing("ix_meeting_actions_meeting_id", "meeting_actions", ["meeting_id"])
    m.create_index_if_missing("ix_meeting_actions_status", "meeting_actions", ["status"])
    m.create_index_if_missing("ix_meeting_actions_owner", "meeting_actions", ["owner"])


def downgrade() -> None:
    m.drop_table_if_present("meeting_actions")
    m.drop_table_if_present("meeting_decisions")
    for column in reversed(MEETING_COLUMNS):
        m.drop_column_if_present("board_meetings", column.name)
