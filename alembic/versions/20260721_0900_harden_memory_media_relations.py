"""冻结回忆录媒体枚举并补齐播放文档关联约束。

Revision ID: 20260721_0900
Revises: 20260720_1320
"""

from __future__ import annotations

from alembic import op
from app.db.alembic_schema_bootstrap import memory_schema_created_at_head

revision = "20260721_0900"
down_revision = "20260720_1320"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if memory_schema_created_at_head():
        return
    """仅补领域约束，不回填、复制或解密任何播放文档与快照正文。"""
    op.create_unique_constraint(
        "uq_memory_document_archive_id",
        "memory_playback_documents",
        ["archive_id", "document_id"],
    )
    op.create_foreign_key(
        "fk_memory_document_archive",
        "memory_playback_documents",
        "memory_archives",
        ["archive_id"],
        ["archive_id"],
    )
    op.create_foreign_key(
        "fk_memory_scene_document",
        "memory_scenes",
        "memory_playback_documents",
        ["document_id"],
        ["document_id"],
    )
    op.create_foreign_key(
        "fk_memory_action_scene",
        "memory_actions",
        "memory_scenes",
        ["scene_id"],
        ["scene_id"],
    )
    op.create_foreign_key(
        "fk_memory_media_document_archive",
        "memory_media_assets",
        "memory_playback_documents",
        ["archive_id", "document_id"],
        ["archive_id", "document_id"],
    )
    for table_name, constraint_name, expression in (
        ("memory_scenes", "ck_memory_scene_type", "scene_type IN ('cover', 'stats', 'diary_highlight', 'bet_highlight', 'image', 'milestone', 'summary')"),
        ("memory_scenes", "ck_memory_scene_safety_level", "safety_level IN ('normal', 'sensitive', 'fallback')"),
        ("memory_actions", "ck_memory_action_type", "action_type IN ('show_card', 'focus_image', 'type_text', 'hold', 'play_tts', 'transition')"),
        ("memory_media_assets", "ck_memory_media_type", "media_type IN ('image', 'audio', 'video')"),
        ("memory_media_assets", "ck_memory_media_source_type", "source_type IN ('diary_original', 'ai_generated', 'tts', 'default_asset')"),
        ("memory_media_assets", "ck_memory_media_status", "status IN ('ready', 'deleting', 'deleted')"),
    ):
        op.create_check_constraint(constraint_name, table_name, expression)


def downgrade() -> None:
    """先删除枚举约束，再按引用反向解除本迁移的关联。"""
    for table_name, constraint_name in (
        ("memory_media_assets", "ck_memory_media_status"),
        ("memory_media_assets", "ck_memory_media_source_type"),
        ("memory_media_assets", "ck_memory_media_type"),
        ("memory_actions", "ck_memory_action_type"),
        ("memory_scenes", "ck_memory_scene_safety_level"),
        ("memory_scenes", "ck_memory_scene_type"),
    ):
        op.drop_constraint(constraint_name, table_name, type_="check")
    for table_name, constraint_name in (
        ("memory_media_assets", "fk_memory_media_document_archive"),
        ("memory_actions", "fk_memory_action_scene"),
        ("memory_scenes", "fk_memory_scene_document"),
        ("memory_playback_documents", "fk_memory_document_archive"),
    ):
        op.drop_constraint(constraint_name, table_name, type_="foreignkey")
    op.drop_constraint(
        "uq_memory_document_archive_id",
        "memory_playback_documents",
        type_="unique",
    )
