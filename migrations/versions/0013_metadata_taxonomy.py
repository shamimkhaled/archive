"""Document language, per-field metadata confidence, and the tag taxonomy (backfilled from keywords).

Revision ID: 0013_metadata_taxonomy
Revises: 0012_meeting_records
Create Date: 2026-10-15
"""
from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from alembic import op

from bcp_project import migration_ops as m

revision = "0013_metadata_taxonomy"
down_revision = "0012_meeting_records"
branch_labels = None
depends_on = None


def _backfill_tags() -> None:
    from bcp_project.services.taxonomy import clean_labels, tag_slug

    bind = op.get_bind()
    documents = sa.table(
        "documents",
        sa.column("doc_id", sa.String),
        sa.column("keywords", sa.JSON),
        sa.column("metadata_confirmed_by", sa.String),
    )
    tags = sa.table(
        "tags",
        sa.column("id", sa.Integer),
        sa.column("slug", sa.String),
        sa.column("label_en", sa.String),
        sa.column("kind", sa.String),
        sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime),
    )
    links = sa.table(
        "document_tags",
        sa.column("doc_id", sa.String),
        sa.column("tag_id", sa.Integer),
        sa.column("source", sa.String),
        sa.column("confirmed", sa.Boolean),
        sa.column("created_at", sa.DateTime),
    )
    known = {slug: tag_id for tag_id, slug in bind.execute(sa.select(tags.c.id, tags.c.slug)).all()}
    linked = {(d, t) for d, t in bind.execute(sa.select(links.c.doc_id, links.c.tag_id)).all()}
    now = datetime.utcnow()
    for doc_id, keywords, confirmed_by in bind.execute(
        sa.select(documents.c.doc_id, documents.c.keywords, documents.c.metadata_confirmed_by)
    ).all():
        if not isinstance(keywords, list):
            continue
        for label in clean_labels(str(k) for k in keywords):
            slug = tag_slug(label)
            if slug not in known:
                bind.execute(
                    tags.insert().values(slug=slug, label_en=label, kind="keyword", is_active=True, created_at=now)
                )
                known[slug] = bind.execute(sa.select(tags.c.id).where(tags.c.slug == slug)).scalar_one()
            key = (doc_id, known[slug])
            if key in linked:
                continue
            linked.add(key)
            bind.execute(
                links.insert().values(
                    doc_id=doc_id,
                    tag_id=known[slug],
                    source="reviewer" if confirmed_by else "ai",
                    confirmed=bool(confirmed_by),
                    created_at=now,
                )
            )


def upgrade() -> None:
    m.add_columns_if_missing(
        "documents",
        [
            sa.Column("language", sa.String(8), nullable=True),
            sa.Column("metadata_confidence", sa.JSON(), nullable=True),
        ],
    )
    m.create_index_if_missing("ix_documents_language", "documents", ["language"])

    m.create_table_if_missing(
        "tags",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("slug", sa.String(96), nullable=False, unique=True),
        sa.Column("label_en", sa.String(120), nullable=False),
        sa.Column("label_bn", sa.String(120), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="keyword"),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("tags.id"), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    m.create_table_if_missing(
        "document_tags",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("doc_id", sa.String(128), nullable=False),
        sa.Column("tag_id", sa.Integer(), sa.ForeignKey("tags.id"), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="ai"),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("doc_id", "tag_id", name="uq_document_tag"),
        indexes=[("ix_document_tags_doc_id", ["doc_id"]), ("ix_document_tags_tag_id", ["tag_id"])],
    )
    if m.has_column("documents", "keywords"):
        _backfill_tags()


def downgrade() -> None:
    m.drop_table_if_present("document_tags")
    m.drop_table_if_present("tags")
    if m.has_index("documents", "ix_documents_language"):
        op.drop_index("ix_documents_language", table_name="documents")
    m.drop_column_if_present("documents", "metadata_confidence")
    m.drop_column_if_present("documents", "language")
