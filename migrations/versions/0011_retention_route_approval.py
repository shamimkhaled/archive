"""Retention rules, legal hold, four-eyes disposal with tombstones, and model-route change approval.

Revision ID: 0011_retention_route_approval
Revises: 0010_security_hardening
Create Date: 2026-10-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from bcp_project import migration_ops as m

revision = "0011_retention_route_approval"
down_revision = "0010_security_hardening"
branch_labels = None
depends_on = None

DOCUMENT_COLUMNS = ("retention_until", "legal_hold", "legal_hold_reason", "legal_hold_by", "legal_hold_at")


def upgrade() -> None:
    m.add_columns_if_missing(
        "documents",
        [
            sa.Column("retention_until", sa.Date(), nullable=True),
            sa.Column("legal_hold", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("legal_hold_reason", sa.Text(), nullable=True),
            sa.Column("legal_hold_by", sa.String(64), nullable=True),
            sa.Column("legal_hold_at", sa.DateTime(), nullable=True),
        ],
    )
    m.create_index_if_missing("ix_documents_retention_until", "documents", ["retention_until"])

    m.create_table_if_missing(
        "retention_rules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("doc_type", sa.String(64), nullable=False, unique=True),
        sa.Column("retain_years", sa.Integer(), nullable=True),
        sa.Column("trigger", sa.String(16), nullable=False, server_default="doc_date"),
        sa.Column("authority", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    m.create_table_if_missing(
        "document_disposals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("doc_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("proposed_by", sa.String(64), nullable=False),
        sa.Column("proposed_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_by", sa.String(64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("title", sa.String(300), nullable=True),
        sa.Column("doc_type", sa.String(64), nullable=True),
        sa.Column("doc_date", sa.Date(), nullable=True),
        sa.Column("sensitivity", sa.String(16), nullable=True),
        sa.Column("retention_until", sa.Date(), nullable=True),
        sa.Column("tombstone", sa.JSON(), nullable=True),
        indexes=[
            ("ix_document_disposals_doc_id", ["doc_id"]),
            ("ix_document_disposals_status", ["status", "proposed_at"]),
        ],
    )

    m.create_table_if_missing(
        "ai_route_changes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("task", sa.String(48), nullable=False),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("model", sa.String(160), nullable=True),
        sa.Column("source_entry_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("requested_by", sa.String(64), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_by", sa.String(64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        indexes=[("ix_ai_route_changes_status", ["status", "requested_at"])],
    )


def downgrade() -> None:
    m.drop_table_if_present("ai_route_changes")
    m.drop_table_if_present("document_disposals")
    m.drop_table_if_present("retention_rules")
    if m.has_index("documents", "ix_documents_retention_until"):
        op.drop_index("ix_documents_retention_until", table_name="documents")
    for column in reversed(DOCUMENT_COLUMNS):
        m.drop_column_if_present("documents", column)
