"""Phase 4: meeting attachments are archived documents (OCR, versions, search).

Revision ID: 0007_meeting_doc_archive
Revises: 0006_version_review
Create Date: 2026-10-12
"""
from __future__ import annotations

import sqlalchemy as sa

from bcp_project import migration_ops as m

revision = "0007_meeting_doc_archive"
down_revision = "0006_version_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    m.add_columns_if_missing(
        "meeting_documents",
        [sa.Column("archive_doc_id", sa.String(128), nullable=True)],
    )
    m.create_index_if_missing("ix_meeting_documents_archive_doc_id", "meeting_documents", ["archive_doc_id"])


def downgrade() -> None:
    m.drop_column_if_present("meeting_documents", "archive_doc_id")
