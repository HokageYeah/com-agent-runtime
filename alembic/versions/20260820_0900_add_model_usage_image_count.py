"""M6 媒体通道按张计量：agent_model_usages 增加 image_count 列。

Revision ID: 20260820_0900
Revises: 20260729_1000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260820_0900"
down_revision = "20260729_1000"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    """按列存在性幂等判断，不读取业务数据。

    注意：不能再用 memory_schema_created_at_head 之类的建库标记守卫——
    本迁移改的是 runtime 核心表 agent_model_usages，而 create_all 建出的
    存量库会带着全部 memory 标记列，导致守卫误判"已建库即最新"并静默跳过
    加列，版本号却照常推进（本地 dev 库实际踩过：1054 Unknown column）。
    """
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    """加可空 image_count 列；LLM token 计量行保持 NULL 不受影响。"""
    if "image_count" not in _columns("agent_model_usages"):
        with op.batch_alter_table("agent_model_usages") as batch:
            batch.add_column(sa.Column("image_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    """回滚：删除 image_count 列（媒体计量行会丢张数，属可接受回退）。"""
    if "image_count" in _columns("agent_model_usages"):
        with op.batch_alter_table("agent_model_usages") as batch:
            batch.drop_column("image_count")
