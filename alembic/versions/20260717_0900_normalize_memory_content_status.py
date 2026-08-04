"""统一回忆录内容状态语义。

Revision ID: 20260717_0900
Revises: 20260716_1700
Create Date: 2026-07-17 09:40:00
"""

from __future__ import annotations

from alembic import op
from app.db.alembic_schema_bootstrap import memory_schema_created_at_head

revision = "20260717_0900"
down_revision = "20260716_1700"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """将旧命名归一为 callback 与发布契约使用的状态值。"""
    if memory_schema_created_at_head():
        return
    op.execute("UPDATE memory_archives SET content_status = 'baseline' WHERE content_status = 'baseline_ready'")
    op.execute("UPDATE memory_archives SET content_status = 'succeeded' WHERE content_status = 'enhanced_ready'")


def downgrade() -> None:
    """状态语义已被业务事件消费，降级时不反向篡改已发布归档状态。"""
