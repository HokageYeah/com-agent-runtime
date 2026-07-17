"""补充回忆录 RunRef 的安全生命周期审计字段。

Revision ID: 20260717_1130
Revises: 20260717_0940
Create Date: 2026-07-17 11:30:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260717_1130"
down_revision = "20260717_0940"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为已有绑定补默认 row_version，其他元数据可随安全重试逐步补齐。"""
    op.add_column(
        "memory_agent_run_refs",
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("memory_agent_run_refs", sa.Column("retry_idempotency_key", sa.String(200)))
    op.add_column(
        "memory_agent_run_refs", sa.Column("privacy_purge_idempotency_key", sa.String(200))
    )
    op.add_column("memory_agent_run_refs", sa.Column("contract_version", sa.String(40)))
    op.add_column("memory_agent_run_refs", sa.Column("package_digest", sa.String(80)))
    op.add_column("memory_agent_run_refs", sa.Column("authorization_version", sa.Integer()))
    op.add_column("memory_agent_run_refs", sa.Column("privacy_purge_requested_at", sa.DateTime()))
    op.add_column("memory_agent_run_refs", sa.Column("privacy_purge_completed_at", sa.DateTime()))


def downgrade() -> None:
    """按反向依赖顺序删除本迁移新增的审计字段。"""
    for column in (
        "privacy_purge_completed_at", "privacy_purge_requested_at", "authorization_version",
        "package_digest", "contract_version", "privacy_purge_idempotency_key",
        "retry_idempotency_key", "row_version",
    ):
        op.drop_column("memory_agent_run_refs", column)
