"""为回忆录 Run 引用增加 callback 对账状态。

Revision ID: 20260716_1700
Revises: 20260715_1600
Create Date: 2026-07-16 16:20:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.db.alembic_schema_bootstrap import memory_schema_created_at_head

revision = "20260716_1700"
down_revision = "20260715_1600"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """记录成功 callback 缺失原子发布时的待对账状态，不保存业务正文。"""
    if memory_schema_created_at_head():
        return
    op.add_column(
        "memory_agent_run_refs",
        sa.Column(
            "reconciliation_status",
            sa.String(length=32),
            nullable=False,
            server_default="not_needed",
        ),
    )


def downgrade() -> None:
    """回滚 callback 对账状态字段。"""
    op.drop_column("memory_agent_run_refs", "reconciliation_status")
