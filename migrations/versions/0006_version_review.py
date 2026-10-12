"""Phase 4: who verified each document version, and when.

Revision ID: 0006_version_review
Revises: 0005_ocr_versions
Create Date: 2026-10-12
"""
from __future__ import annotations

import sqlalchemy as sa

from bcp_project import migration_ops as m

revision = "0006_version_review"
down_revision = "0005_ocr_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    m.add_columns_if_missing(
        "document_versions",
        [
            sa.Column("reviewed_by", sa.String(64), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        ],
    )
    m.create_index_if_missing("ix_document_versions_status", "document_versions", ["status"])


def downgrade() -> None:
    m.drop_column_if_present("document_versions", "reviewed_at")
    m.drop_column_if_present("document_versions", "reviewed_by")
