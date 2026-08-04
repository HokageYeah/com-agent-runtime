"""创建回忆录归档、快照、播放文档与 Runtime 引用基础表。

Revision ID: 20260715_1500
Revises: 20260617_1400
Create Date: 2026-07-15 15:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

import app.models  # noqa: F401 - 确保新增模型注册到当前 metadata。
from alembic import op
from app.db.alembic_schema_bootstrap import mark_head_schema
from app.db.sqlalchemy_db import Base

revision = "20260715_1500"
down_revision = "20260617_1400"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """仅创建缺失的回忆录表，兼容模板工程既有 create_all 联调方式。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    memory_missing = not inspector.has_table("memory_archives")
    runtime_missing = not inspector.has_table("agent_runs")
    Base.metadata.create_all(bind=bind)
    # 从空 Runtime schema 开始时 create_all 已按当前模型创建到 head；
    # 后续迁移只需推进 revision。已有旧表则不设标记，继续真实升级。
    mark_head_schema(memory=memory_missing, runtime=runtime_missing)


def downgrade() -> None:
    """按依赖反序删除本次新增表，避免删掉已有情侣关系等基础表。"""
    for table_name in (
        "memory_agent_run_refs",
        "memory_media_assets",
        "memory_actions",
        "memory_scenes",
        "memory_playback_documents",
        "memory_snapshots",
        "memory_archives",
    ):
        op.drop_table(table_name, if_exists=True)
