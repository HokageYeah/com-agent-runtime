"""为回忆录发布版本增加冻结素材反查映射。

Revision ID: 20260720_1300
Revises: 20260720_1200
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app.db.alembic_schema_bootstrap import memory_schema_created_at_head

revision = "20260720_1300"
down_revision = "20260720_1200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """仅保存来源类型和 ID，禁止把日记正文复制到反查索引。"""
    if memory_schema_created_at_head():
        return
    op.create_table(
        "memory_source_references",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("archive_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=24), nullable=False),
        sa.Column("source_id", sa.String(length=80), nullable=False),
        sa.UniqueConstraint(
            "document_id", "source_type", "source_id",
            name="uq_memory_source_reference_document_source",
        ),
    )
    op.create_index(
        "ix_memory_source_reference_source",
        "memory_source_references", ["source_type", "source_id"],
    )
    op.create_index(
        "ix_memory_source_reference_archive_revision",
        "memory_source_references", ["archive_id", "revision"],
    )


def downgrade() -> None:
    """反查映射可由已发布 document 重建，回滚时仅删除该派生表。"""
    op.drop_index("ix_memory_source_reference_archive_revision", table_name="memory_source_references")
    op.drop_index("ix_memory_source_reference_source", table_name="memory_source_references")
    op.drop_table("memory_source_references")
