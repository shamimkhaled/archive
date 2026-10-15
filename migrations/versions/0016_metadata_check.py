"""Flag documents whose metadata the AI filled in until a person confirms it.

Revision ID: 0016_metadata_check
Revises: 0015_board_packages
Create Date: 2026-10-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from bcp_project import migration_ops as m

revision = "0016_metadata_check"
down_revision = "0015_board_packages"
branch_labels = None
depends_on = None


def _backfill() -> None:
    bind = op.get_bind()
    documents = sa.table(
        "documents",
        sa.column("id", sa.Integer),
        sa.column("suggested_metadata", sa.JSON),
        sa.column("metadata_confirmed_by", sa.String),
        sa.column("metadata_check_pending", sa.Boolean),
    )
    pending = [
        doc_id
        for doc_id, suggested in bind.execute(
            sa.select(documents.c.id, documents.c.suggested_metadata).where(documents.c.metadata_confirmed_by.is_(None))
        ).all()
        if isinstance(suggested, dict) and suggested.get("_applied")
    ]
    for start in range(0, len(pending), 500):
        bind.execute(
            documents.update().where(documents.c.id.in_(pending[start : start + 500])).values(metadata_check_pending=True)
        )


def upgrade() -> None:
    m.add_columns_if_missing(
        "documents",
        [sa.Column("metadata_check_pending", sa.Boolean(), nullable=False, server_default=sa.false())],
    )
    m.create_index_if_missing("ix_documents_metadata_check_pending", "documents", ["metadata_check_pending"])
    _backfill()


def downgrade() -> None:
    if m.has_index("documents", "ix_documents_metadata_check_pending"):
        op.drop_index("ix_documents_metadata_check_pending", table_name="documents")
    m.drop_column_if_present("documents", "metadata_check_pending")
