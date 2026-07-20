"""冻结回忆录 Run 可访问的 snapshot 标识。

Revision ID: 20260720_1200
Revises: 20260720_1100
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260720_1200"
down_revision = "20260720_1100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保存安全引用，不保存任何快照正文。"""
    op.add_column("memory_agent_run_refs", sa.Column("snapshot_id", sa.String(64)))
    op.create_index("ix_memory_agent_run_refs_snapshot_id", "memory_agent_run_refs", ["snapshot_id"])


def downgrade() -> None:
    """仅回滚本迁移新增的安全引用字段。"""
    op.drop_index("ix_memory_agent_run_refs_snapshot_id", table_name="memory_agent_run_refs")
    op.drop_column("memory_agent_run_refs", "snapshot_id")
