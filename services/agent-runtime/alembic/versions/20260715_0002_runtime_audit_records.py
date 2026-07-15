"""补充 Runtime 安全审计账本。

Revision ID: 20260715_0002
Revises: 20260707_0001
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260715_0002"
down_revision = "20260707_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为已经运行过初始迁移的库增加追加写审计表。

    初始迁移采用 Base.metadata.create_all；新环境在执行初始迁移时已经会带上
    当前模型的本表。因此这里先检查，保证新旧库均可安全升级。
    """
    bind = op.get_bind()
    if sa.inspect(bind).has_table("runtime_audit_records"):
        return
    op.create_table(
        "runtime_audit_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("audit_id", sa.String(length=80), nullable=False, unique=True),
        sa.Column("actor_type", sa.String(length=40), nullable=False),
        sa.Column("actor_id", sa.String(length=120), nullable=False),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("resource_type", sa.String(length=80), nullable=False),
        sa.Column("resource_id", sa.String(length=120), nullable=False),
        sa.Column("reason_code", sa.String(length=80)),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("trace_id", sa.String(length=120)),
        sa.Column("metadata_summary", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_runtime_audit_records_action", "runtime_audit_records", ["action"])
    op.create_index(
        "ix_runtime_audit_records_resource_id", "runtime_audit_records", ["resource_id"]
    )
    op.create_index("ix_runtime_audit_records_trace_id", "runtime_audit_records", ["trace_id"])
    op.create_index(
        "ix_runtime_audit_records_occurred_at", "runtime_audit_records", ["occurred_at"]
    )


def downgrade() -> None:
    """开发环境回滚时移除本审计表。"""
    bind = op.get_bind()
    if sa.inspect(bind).has_table("runtime_audit_records"):
        op.drop_table("runtime_audit_records")
