"""为回忆录 Run 引用保存安全公开轨迹。

Revision ID: 20260717_0940
Revises: 20260717_0900
Create Date: 2026-07-17 09:40:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260717_0940"
down_revision = "20260717_0900"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增只含节点展示状态的 JSON 字段，已有引用默认空轨迹。"""
    op.add_column(
        "memory_agent_run_refs",
        sa.Column("public_trace_json", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    """回滚公开轨迹字段。"""
    op.drop_column("memory_agent_run_refs", "public_trace_json")
