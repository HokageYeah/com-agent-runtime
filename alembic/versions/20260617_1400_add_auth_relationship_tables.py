"""新增登录与情侣关系基础表

Revision ID: 20260617_1400
Revises: 20260615_1100
Create Date: 2026-06-17 14:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260617_1400"
down_revision = "20260615_1100"
branch_labels = None
depends_on = None


def _get_inspector() -> sa.Inspector:
    """获取当前迁移连接的数据库检查器。"""

    bind = op.get_bind()
    return sa.inspect(bind)


def _has_table(inspector: sa.Inspector, table_name: str) -> bool:
    """判断数据表是否已存在。"""

    return inspector.has_table(table_name)


def _has_index(inspector: sa.Inspector, table_name: str, index_name: str) -> bool:
    """判断索引是否已存在，兼容 create_all 与迁移混用场景。"""

    indexes = inspector.get_indexes(table_name)
    return any(index["name"] == index_name for index in indexes)


def _create_index_if_missing(
    inspector: sa.Inspector,
    index_name: str,
    table_name: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> None:
    """仅当索引不存在时才创建，避免重复迁移时报错。"""

    if not _has_index(inspector, table_name, index_name):
        op.create_index(index_name, table_name, columns, unique=unique)


def upgrade() -> None:
    """升级迁移：创建登录与情侣关系模块的基础表。"""

    inspector = _get_inspector()

    if not _has_table(inspector, "couple_users"):
        op.create_table(
            "couple_users",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("openid", sa.String(length=128), nullable=False),
            sa.Column("nickname", sa.String(length=100), nullable=False),
            sa.Column("avatar", sa.String(length=512), nullable=False),
            sa.Column("role_tag", sa.String(length=20), nullable=False),
            sa.Column("role_locked_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        inspector = _get_inspector()
    _create_index_if_missing(inspector, "ix_couple_users_id", "couple_users", ["id"])
    _create_index_if_missing(
        inspector,
        "ix_couple_users_openid",
        "couple_users",
        ["openid"],
        unique=True,
    )

    if not _has_table(inspector, "auth_sessions"):
        op.create_table(
            "auth_sessions",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("token_hash", sa.String(length=128), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        inspector = _get_inspector()
    _create_index_if_missing(
        inspector,
        "ix_auth_sessions_id",
        "auth_sessions",
        ["id"],
    )
    _create_index_if_missing(
        inspector,
        "ix_auth_sessions_user_id",
        "auth_sessions",
        ["user_id"],
    )
    _create_index_if_missing(
        inspector,
        "ix_auth_sessions_token_hash",
        "auth_sessions",
        ["token_hash"],
        unique=True,
    )

    if not _has_table(inspector, "couple_relationships"):
        op.create_table(
            "couple_relationships",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("user_a_id", sa.Integer(), nullable=False),
            sa.Column("user_b_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("bound_at", sa.DateTime(), nullable=True),
            sa.Column("unbound_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
        inspector = _get_inspector()
    _create_index_if_missing(
        inspector,
        "ix_couple_relationships_id",
        "couple_relationships",
        ["id"],
    )
    _create_index_if_missing(
        inspector,
        "ix_couple_relationships_user_a_id",
        "couple_relationships",
        ["user_a_id"],
    )
    _create_index_if_missing(
        inspector,
        "ix_couple_relationships_user_b_id",
        "couple_relationships",
        ["user_b_id"],
    )
    _create_index_if_missing(
        inspector,
        "ix_couple_relationships_status",
        "couple_relationships",
        ["status"],
    )

    if not _has_table(inspector, "binding_invites"):
        op.create_table(
            "binding_invites",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("binding_code", sa.String(length=32), nullable=False),
            sa.Column("inviter_user_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_by_user_id", sa.Integer(), nullable=True),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        inspector = _get_inspector()
    _create_index_if_missing(
        inspector,
        "ix_binding_invites_id",
        "binding_invites",
        ["id"],
    )
    _create_index_if_missing(
        inspector,
        "ix_binding_invites_binding_code",
        "binding_invites",
        ["binding_code"],
        unique=True,
    )
    _create_index_if_missing(
        inspector,
        "ix_binding_invites_inviter_user_id",
        "binding_invites",
        ["inviter_user_id"],
    )
    _create_index_if_missing(
        inspector,
        "ix_binding_invites_status",
        "binding_invites",
        ["status"],
    )

    if not _has_table(inspector, "relationship_blocks"):
        op.create_table(
            "relationship_blocks",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("blocker_user_id", sa.Integer(), nullable=False),
            sa.Column("blocked_user_id", sa.Integer(), nullable=False),
            sa.Column("cooldown_until", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        inspector = _get_inspector()
    _create_index_if_missing(
        inspector,
        "ix_relationship_blocks_id",
        "relationship_blocks",
        ["id"],
    )
    _create_index_if_missing(
        inspector,
        "ix_relationship_blocks_blocker_user_id",
        "relationship_blocks",
        ["blocker_user_id"],
    )
    _create_index_if_missing(
        inspector,
        "ix_relationship_blocks_blocked_user_id",
        "relationship_blocks",
        ["blocked_user_id"],
    )


def downgrade() -> None:
    """回滚迁移：删除登录与情侣关系基础表。"""

    op.drop_index(
        "ix_relationship_blocks_blocked_user_id",
        table_name="relationship_blocks",
    )
    op.drop_index(
        "ix_relationship_blocks_blocker_user_id",
        table_name="relationship_blocks",
    )
    op.drop_index("ix_relationship_blocks_id", table_name="relationship_blocks")
    op.drop_table("relationship_blocks")

    op.drop_index("ix_binding_invites_status", table_name="binding_invites")
    op.drop_index("ix_binding_invites_inviter_user_id", table_name="binding_invites")
    op.drop_index("ix_binding_invites_binding_code", table_name="binding_invites")
    op.drop_index("ix_binding_invites_id", table_name="binding_invites")
    op.drop_table("binding_invites")

    op.drop_index("ix_couple_relationships_status", table_name="couple_relationships")
    op.drop_index(
        "ix_couple_relationships_user_b_id", table_name="couple_relationships"
    )
    op.drop_index(
        "ix_couple_relationships_user_a_id", table_name="couple_relationships"
    )
    op.drop_index("ix_couple_relationships_id", table_name="couple_relationships")
    op.drop_table("couple_relationships")

    op.drop_index("ix_auth_sessions_token_hash", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")

    op.drop_index("ix_couple_users_openid", table_name="couple_users")
    op.drop_index("ix_couple_users_id", table_name="couple_users")
    op.drop_table("couple_users")
