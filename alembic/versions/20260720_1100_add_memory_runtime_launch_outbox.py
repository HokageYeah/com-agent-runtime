"""创建回忆录业务侧 Runtime 启动意图 outbox。

Revision ID: 20260720_1100
Revises: 20260717_1200
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260720_1100"
down_revision = "20260717_1200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """持久化 create-held/start-held 意图；表中不保存日记或 Prompt 正文。"""
    op.create_table(
        "memory_runtime_launch_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.String(64), nullable=False, unique=True),
        sa.Column("archive_id", sa.String(64), nullable=False),
        sa.Column("snapshot_id", sa.String(64), nullable=False),
        sa.Column("generation_epoch", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.Column("run_id", sa.String(80), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "archive_id", "generation_epoch", "phase",
            name="uq_memory_runtime_launch_phase",
        ),
    )
    for name, columns in (
        ("ix_memory_runtime_launch_events_event_id", ["event_id"]),
        ("ix_memory_runtime_launch_events_archive_id", ["archive_id"]),
        ("ix_memory_runtime_launch_events_run_id", ["run_id"]),
    ):
        op.create_index(name, "memory_runtime_launch_events", columns)


def downgrade() -> None:
    """移除 Runtime 启动 outbox；只删除本迁移创建的 Runtime 自有表。"""
    for name in (
        "ix_memory_runtime_launch_events_run_id",
        "ix_memory_runtime_launch_events_archive_id",
        "ix_memory_runtime_launch_events_event_id",
    ):
        op.drop_index(name, table_name="memory_runtime_launch_events")
    op.drop_table("memory_runtime_launch_events")
