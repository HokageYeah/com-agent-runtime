"""新增 Runtime 对账器的持久扫描租约。

Revision ID: 20260717_1200
Revises: 20260717_1130
Create Date: 2026-07-17 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260717_1200"
down_revision = "20260717_1130"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建单行租约表，过期后允许另一实例接管。"""
    op.create_table(
        "runtime_reconciliation_leases",
        sa.Column("lease_key", sa.String(80), primary_key=True),
        sa.Column("owner_id", sa.String(120), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_runtime_reconciliation_leases_expires_at",
        "runtime_reconciliation_leases",
        ["expires_at"],
    )


def downgrade() -> None:
    """移除对账租约表。"""
    op.drop_index("ix_runtime_reconciliation_leases_expires_at", table_name="runtime_reconciliation_leases")
    op.drop_table("runtime_reconciliation_leases")
