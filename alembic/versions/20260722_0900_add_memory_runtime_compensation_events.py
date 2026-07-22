"""增加回忆录删除补偿事件表。

Revision ID: 20260722_0900
Revises: 20260721_0900
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260722_0900"
down_revision = "20260721_0900"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """只建立无内容的外部副作用意图表，不迁移快照或播放文档。"""
    op.create_table(
        "memory_runtime_compensation_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("archive_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("generation_epoch", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "action IN ('privacy_purge', 'cancel')",
            name="ck_memory_runtime_compensation_action",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered')",
            name="ck_memory_runtime_compensation_status",
        ),
        sa.UniqueConstraint("event_id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint(
            "archive_id", "run_id", "generation_epoch", "action",
            name="uq_memory_runtime_compensation_operation",
        ),
    )
    op.create_index(
        "ix_memory_runtime_compensation_events_event_id",
        "memory_runtime_compensation_events",
        ["event_id"],
    )
    op.create_index(
        "ix_memory_runtime_compensation_events_archive_id",
        "memory_runtime_compensation_events",
        ["archive_id"],
    )
    op.create_index(
        "ix_memory_runtime_compensation_events_run_id",
        "memory_runtime_compensation_events",
        ["run_id"],
    )


def downgrade() -> None:
    """回滚时只移除本迁移创建的无内容补偿事件表。"""
    op.drop_index("ix_memory_runtime_compensation_events_run_id")
    op.drop_index("ix_memory_runtime_compensation_events_archive_id")
    op.drop_index("ix_memory_runtime_compensation_events_event_id")
    op.drop_table("memory_runtime_compensation_events")
