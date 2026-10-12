"""Phase 1: document sensitivity label and TOTP MFA enrolment.

Revision ID: 0002_permission_mfa
Revises: 0001_baseline
Create Date: 2026-10-12
"""
from __future__ import annotations

import sqlalchemy as sa

from bcp_project import migration_ops as m

revision = "0002_permission_mfa"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    m.add_column_if_missing(
        "documents",
        sa.Column("sensitivity", sa.String(16), nullable=False, server_default="internal"),
    )
    m.create_index_if_missing("ix_documents_sensitivity", "documents", ["sensitivity"])

    m.create_table_if_missing(
        "user_mfa",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_used_step", sa.BigInteger(), nullable=True),
        sa.Column("recovery_code_hashes", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
    )
    m.create_index_if_missing("ix_user_mfa_username", "user_mfa", ["username"], unique=True)


def downgrade() -> None:
    m.drop_table_if_present("user_mfa")
    m.drop_column_if_present("documents", "sensitivity")
