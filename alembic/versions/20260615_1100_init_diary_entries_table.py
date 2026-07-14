"""初始化 diary_entries 表

Revision ID: 20260615_1100
Revises:
Create Date: 2026-06-15 11:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260615_1100"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """升级迁移：创建 diary_entries 表。"""
    # 这里保留较详细中文注释，方便后续你自己排查迁移逻辑。
    # 这张表代表模板工程里的“正式业务域最小持久化模型”，
    # 不再沿用旧模板里的文章抓取业务语义。
    op.create_table(
        "diary_entries",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("mood", sa.String(length=50), nullable=True),
        sa.Column("entry_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )

    # 索引仍然拆开写，便于看迁移日志和后续追加字段时阅读。
    op.create_index("ix_diary_entries_id", "diary_entries", ["id"], unique=False)
    op.create_index(
        "ix_diary_entries_title",
        "diary_entries",
        ["title"],
        unique=False,
    )
    op.create_index(
        "ix_diary_entries_entry_date",
        "diary_entries",
        ["entry_date"],
        unique=False,
    )


def downgrade() -> None:
    """回滚迁移：删除 diary_entries 表。"""
    op.drop_index("ix_diary_entries_entry_date", table_name="diary_entries")
    op.drop_index("ix_diary_entries_title", table_name="diary_entries")
    op.drop_index("ix_diary_entries_id", table_name="diary_entries")
    op.drop_table("diary_entries")
