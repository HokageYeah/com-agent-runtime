"""补齐回忆录 schema major 与基础数据约束。

Revision ID: 20260720_1320
Revises: 20260720_1300
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.db.alembic_schema_bootstrap import memory_schema_created_at_head

revision = "20260720_1320"
down_revision = "20260720_1300"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """只补版本字段和正数约束，不迁移任何加密快照或播放正文。"""
    if memory_schema_created_at_head():
        return
    for table_name in (
        "memory_snapshots", "memory_playback_documents", "memory_scenes", "memory_actions",
    ):
        op.add_column(
            table_name,
            sa.Column("schema_major", sa.Integer(), nullable=False, server_default="1"),
        )
    op.create_check_constraint(
        "ck_memory_snapshot_version_positive", "memory_snapshots", "snapshot_version >= 1"
    )
    op.create_check_constraint(
        "ck_memory_snapshot_schema_major_positive", "memory_snapshots", "schema_major >= 1"
    )
    op.create_check_constraint(
        "ck_memory_document_revision_nonnegative", "memory_playback_documents", "revision >= 0"
    )
    op.create_check_constraint(
        "ck_memory_document_schema_major_positive", "memory_playback_documents", "schema_major >= 1"
    )
    op.create_check_constraint(
        "ck_memory_scene_schema_major_positive", "memory_scenes", "schema_major >= 1"
    )
    op.create_check_constraint(
        "ck_memory_action_duration_positive", "memory_actions", "duration_ms > 0"
    )
    op.create_check_constraint(
        "ck_memory_action_schema_major_positive", "memory_actions", "schema_major >= 1"
    )


def downgrade() -> None:
    """按约束依赖顺序移除本迁移新增的字段。"""
    for table_name, constraint_name in (
        ("memory_actions", "ck_memory_action_schema_major_positive"),
        ("memory_actions", "ck_memory_action_duration_positive"),
        ("memory_scenes", "ck_memory_scene_schema_major_positive"),
        ("memory_playback_documents", "ck_memory_document_schema_major_positive"),
        ("memory_playback_documents", "ck_memory_document_revision_nonnegative"),
        ("memory_snapshots", "ck_memory_snapshot_schema_major_positive"),
        ("memory_snapshots", "ck_memory_snapshot_version_positive"),
    ):
        op.drop_constraint(constraint_name, table_name, type_="check")
    for table_name in (
        "memory_actions", "memory_scenes", "memory_playback_documents", "memory_snapshots",
    ):
        op.drop_column(table_name, "schema_major")
