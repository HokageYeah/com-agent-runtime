"""创建 AgentRuntime 第一版权威表。

Revision ID: 20260707_0001
Revises:
Create Date: 2026-07-07
"""

from __future__ import annotations

import app.models  # noqa: F401  # 触发模型注册，供 metadata 生成所有初始表。
from alembic import op
from app.db.base import Base

revision = "20260707_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """初始版本一次性建立空 Runtime 库；不会对历史业务数据做猜测性回填。"""
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    """仅用于开发环境回滚；生产回滚前必须确认没有运行中的 AgentRun。"""
    Base.metadata.drop_all(bind=op.get_bind())
