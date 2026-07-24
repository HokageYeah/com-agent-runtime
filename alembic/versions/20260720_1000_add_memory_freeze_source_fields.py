"""补齐回忆录冻结器依赖的关系段与素材来源字段。

Revision ID: 20260720_1000
Revises: 20260717_1200
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260720_1000"
down_revision = "20260717_1200"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    """开发库已存在字段时保持幂等，不读取或回填任何业务正文。"""
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table_name)}


def _add_if_missing(table_name: str, column: sa.Column[object]) -> None:
    """仅添加缺失的冻结定位字段，不覆盖既有业务数据。"""
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def upgrade() -> None:
    """为关系、日记和赌局补齐按关系段冻结回忆录所需最小字段。"""
    _add_if_missing("couple_relationships", sa.Column("space_id", sa.Integer(), nullable=True))
    _add_if_missing(
        "couple_relationships", sa.Column("relationship_segment_no", sa.Integer(), nullable=True)
    )
    _add_if_missing(
        "couple_relationships", sa.Column("unbound_by_user_id", sa.Integer(), nullable=True)
    )
    _add_if_missing(
        "couple_relationships", sa.Column("unbound_reason", sa.String(32), nullable=True)
    )
    for name, type_ in (
        ("space_id", sa.Integer()),
        ("relationship_id", sa.Integer()),
        ("relationship_segment_no", sa.Integer()),
        ("author_user_id", sa.Integer()),
    ):
        _add_if_missing("diary_entries", sa.Column(name, type_, nullable=True))
    _add_if_missing("diary_entries", sa.Column("images_json", sa.JSON(), nullable=True))
    _add_if_missing("diary_entries", sa.Column("status", sa.String(32), nullable=True))
    _add_if_missing("diary_entries", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    _add_if_missing("diary_entries", sa.Column("tags", sa.JSON(), nullable=True))
    if not sa.inspect(op.get_bind()).has_table("bets"):
        op.create_table(
            "bets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("space_id", sa.Integer(), nullable=False),
            sa.Column("relationship_id", sa.Integer(), nullable=False),
            sa.Column("relationship_segment_no", sa.Integer(), nullable=False),
            sa.Column("creator_user_id", sa.Integer(), nullable=False),
            sa.Column("receiver_user_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(120), nullable=False),
            sa.Column("reward", sa.String(120), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("winner_user_id", sa.Integer(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    """来源字段是正式业务契约；降级时不得删除可能仍被回忆录引用的数据。"""
    pass
