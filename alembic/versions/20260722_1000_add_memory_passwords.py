"""增加回忆录独立密码与短期解锁凭证状态。

Revision ID: 20260722_1000
Revises: 20260722_0900
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260722_1000"
down_revision = "20260722_0900"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """仅保存盐、派生值与摘要；不保存 PIN、JWT 或解锁 token 原文。"""
    op.create_table(
        "memory_passwords",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("salt", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=128), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("unlock_session_digest", sa.String(length=64), nullable=True),
        sa.Column("unlock_token_digest", sa.String(length=64), nullable=True),
        sa.Column("unlock_expires_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id"),
    )
    op.create_index("ix_memory_passwords_user_id", "memory_passwords", ["user_id"])


def downgrade() -> None:
    """回滚时仅删除密码派生状态，不影响归档或播放文档。"""
    op.drop_index("ix_memory_passwords_user_id")
    op.drop_table("memory_passwords")
