"""收口回忆录权威状态与关系快照元数据。

Revision ID: 20260729_1000
Revises: 20260729_0900
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260729_1000"
down_revision = "20260729_0900"
branch_labels = None
depends_on = None

_CONTENT_STATUS_CHECK = (
    "content_status IN "
    "('baseline','pending','running','waiting_human','succeeded',"
    "'failed','cancelled')"
)
_ENHANCEMENT_STATUS_CHECK = (
    "enhancement_status IN "
    "('disabled','pending','running','succeeded','partial','failed')"
)


def upgrade() -> None:
    """先补可空快照列、归一化旧状态，再冻结状态枚举。"""
    with op.batch_alter_table("memory_archives") as batch:
        batch.add_column(
            sa.Column("partner_nickname_snapshot", sa.String(length=100), nullable=True)
        )
        batch.add_column(
            sa.Column("partner_avatar_snapshot", sa.String(length=255), nullable=True)
        )
        batch.add_column(sa.Column("bound_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("unbound_at", sa.DateTime(timezone=True), nullable=True)
        )

    op.execute(
        "UPDATE memory_archives "
        "SET enhancement_status = 'disabled' "
        "WHERE enhancement_status = 'not_started'"
    )
    with op.batch_alter_table("memory_archives") as batch:
        batch.alter_column(
            "enhancement_status",
            existing_type=sa.String(length=32),
            nullable=False,
            server_default="disabled",
        )
        batch.create_check_constraint(
            "ck_memory_archive_content_status", _CONTENT_STATUS_CHECK
        )
        batch.create_check_constraint(
            "ck_memory_archive_enhancement_status", _ENHANCEMENT_STATUS_CHECK
        )
    with op.batch_alter_table("memory_agent_run_refs") as batch:
        batch.create_unique_constraint(
            "uq_memory_run_ref_archive_generation",
            ["archive_id", "generation_epoch"],
        )


def downgrade() -> None:
    """解除新约束并将关闭态还原为旧版本可识别的 not_started。"""
    with op.batch_alter_table("memory_agent_run_refs") as batch:
        batch.drop_constraint(
            "uq_memory_run_ref_archive_generation", type_="unique"
        )
    with op.batch_alter_table("memory_archives") as batch:
        batch.drop_constraint(
            "ck_memory_archive_enhancement_status", type_="check"
        )
        batch.drop_constraint("ck_memory_archive_content_status", type_="check")
    op.execute(
        "UPDATE memory_archives "
        "SET enhancement_status = 'not_started' "
        "WHERE enhancement_status = 'disabled'"
    )
    with op.batch_alter_table("memory_archives") as batch:
        batch.alter_column(
            "enhancement_status",
            existing_type=sa.String(length=32),
            nullable=False,
            server_default=None,
        )
        batch.drop_column("unbound_at")
        batch.drop_column("bound_at")
        batch.drop_column("partner_avatar_snapshot")
        batch.drop_column("partner_nickname_snapshot")
