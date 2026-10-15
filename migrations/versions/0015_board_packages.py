"""Board paper packages: a memo with annexures and supporting papers, briefed as one item.

Revision ID: 0015_board_packages
Revises: 0014_search_activity
Create Date: 2026-10-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from bcp_project import migration_ops as m

revision = "0015_board_packages"
down_revision = "0014_search_activity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    m.create_table_if_missing(
        "board_paper_packages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("memo_doc_id", sa.String(128), nullable=True),
        sa.Column("meeting_id", sa.Integer(), sa.ForeignKey("board_meetings.id"), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        indexes=[
            ("ix_board_paper_packages_memo_doc_id", ["memo_doc_id"]),
            ("ix_board_paper_packages_meeting_id", ["meeting_id"]),
        ],
    )
    m.create_table_if_missing(
        "board_paper_package_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("package_id", sa.Integer(), sa.ForeignKey("board_paper_packages.id"), nullable=False),
        sa.Column("doc_id", sa.String(128), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="annexure"),
        sa.Column("label", sa.String(8), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("added_by", sa.String(64), nullable=True),
        sa.Column("added_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("package_id", "doc_id", name="uq_package_item_doc"),
        indexes=[
            ("ix_board_paper_package_items_package_id", ["package_id"]),
            ("ix_board_paper_package_items_doc_id", ["doc_id"]),
        ],
    )

    m.add_columns_if_missing(
        "board_briefings",
        [
            sa.Column("package_id", sa.Integer(), nullable=True),
            sa.Column("package_revision", sa.Integer(), nullable=True),
            sa.Column("members", sa.JSON(), nullable=True),
        ],
    )
    if m.is_postgres():
        op.execute(
            "DO $$ BEGIN "
            "ALTER TABLE board_briefings ADD CONSTRAINT fk_board_briefings_package "
            "FOREIGN KEY (package_id) REFERENCES board_paper_packages (id); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
    m.drop_unique_if_present("board_briefings", "uq_board_briefing_doc_version")
    m.create_partial_index_if_missing(
        "uq_board_briefing_single_doc_version",
        "board_briefings",
        ["doc_id", "version"],
        "package_id IS NULL",
        unique=True,
    )
    m.create_index_if_missing(
        "uq_board_briefing_package_revision", "board_briefings", ["package_id", "package_revision"], unique=True
    )


def downgrade() -> None:
    for index in ("uq_board_briefing_package_revision", "uq_board_briefing_single_doc_version"):
        if m.has_index("board_briefings", index):
            op.drop_index(index, table_name="board_briefings")
    if m.is_postgres():
        op.execute("ALTER TABLE board_briefings DROP CONSTRAINT IF EXISTS fk_board_briefings_package")
    op.execute("DELETE FROM board_briefings WHERE package_id IS NOT NULL")
    for column in ("members", "package_revision", "package_id"):
        m.drop_column_if_present("board_briefings", column)
    if m.has_table("board_briefings") and not m.has_unique_constraint("board_briefings", "uq_board_briefing_doc_version"):
        with op.batch_alter_table("board_briefings") as batch:
            batch.create_unique_constraint("uq_board_briefing_doc_version", ["doc_id", "version"])
    m.drop_table_if_present("board_paper_package_items")
    m.drop_table_if_present("board_paper_packages")
