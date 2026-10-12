"""Phase 4: OCR confidence and verification, confirmed metadata, document versions, page text.

Revision ID: 0005_ocr_versions
Revises: 0004_model_registry
Create Date: 2026-10-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from bcp_project import migration_ops as m

revision = "0005_ocr_versions"
down_revision = "0004_model_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    m.add_columns_if_missing(
        "documents",
        [
            sa.Column("title", sa.String(300), nullable=True),
            sa.Column("reference_number", sa.String(120), nullable=True),
            sa.Column("department", sa.String(120), nullable=True),
            sa.Column("subject", sa.Text(), nullable=True),
            sa.Column("category", sa.String(80), nullable=True),
            sa.Column("ocr_confidence", sa.Float(), nullable=True),
            sa.Column(
                "verification_status", sa.String(16), nullable=False, server_default="not_required"
            ),
            sa.Column("suggested_metadata", sa.JSON(), nullable=True),
            sa.Column("metadata_confirmed_by", sa.String(64), nullable=True),
            sa.Column("metadata_confirmed_at", sa.DateTime(), nullable=True),
            sa.Column("reviewed_by", sa.String(64), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(), nullable=True),
            sa.Column("current_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("sha256", sa.String(64), nullable=True),
            sa.Column("content_type", sa.String(64), nullable=True),
            sa.Column("meeting_id", sa.Integer(), nullable=True),
        ],
    )
    for name, cols in (
        ("ix_documents_reference_number", ["reference_number"]),
        ("ix_documents_department", ["department"]),
        ("ix_documents_category", ["category"]),
        ("ix_documents_verification_status", ["verification_status"]),
        ("ix_documents_meeting_id", ["meeting_id"]),
    ):
        m.create_index_if_missing(name, "documents", cols)

    m.create_table_if_missing(
        "document_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("doc_id", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("file_location", sa.Text(), nullable=False),
        sa.Column("original_location", sa.Text(), nullable=True),
        sa.Column("content_type", sa.String(64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="approved"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    m.create_index_if_missing("ix_document_versions_doc_version", "document_versions", ["doc_id", "version"], unique=True)
    m.create_index_if_missing("ix_document_versions_sha256", "document_versions", ["sha256"])

    m.create_table_if_missing(
        "document_pages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("doc_id", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="text"),
        sa.Column("edited_by", sa.String(64), nullable=True),
        sa.Column("edited_at", sa.DateTime(), nullable=True),
    )
    m.create_index_if_missing(
        "ix_document_pages_doc_version_page", "document_pages", ["doc_id", "version", "page_number"], unique=True
    )

    # Every existing document becomes version 1 of itself (hash computed on next re-ingest).
    op.execute(
        sa.text(
            "INSERT INTO document_versions (doc_id, version, file_location, status, created_by, created_at) "
            "SELECT d.doc_id, 1, d.file_location, 'approved', d.uploaded_by, d.created_at FROM documents d "
            "WHERE NOT EXISTS (SELECT 1 FROM document_versions v WHERE v.doc_id = d.doc_id)"
        )
    )


def downgrade() -> None:
    m.drop_table_if_present("document_pages")
    m.drop_table_if_present("document_versions")
    for column in (
        "title",
        "reference_number",
        "department",
        "subject",
        "category",
        "ocr_confidence",
        "verification_status",
        "suggested_metadata",
        "metadata_confirmed_by",
        "metadata_confirmed_at",
        "reviewed_by",
        "reviewed_at",
        "current_version",
        "sha256",
        "content_type",
        "meeting_id",
    ):
        m.drop_column_if_present("documents", column)
