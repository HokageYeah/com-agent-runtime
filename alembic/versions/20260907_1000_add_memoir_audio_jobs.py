"""M8 R7 新增 Memoir 音频作业账本：memoir_audio_jobs 与 memoir_audio_run_budgets。

Revision ID: 20260907_1000
Revises: 20260820_0900
Create Date: 2026-09-07 10:00:00
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260907_1000"
down_revision = "20260820_0900"
branch_labels = None
depends_on = None

# 与 app/models/memoir_audio_job.py 的枚举保持一致（单引号字面量跨库安全）。
_ROLE_IN = "('narration', 'background_music', 'segment')"
_STATE_IN = (
    "('reserved', 'submitting', 'submitted', 'processing', 'uploaded', "
    "'published', 'failed', 'cancelled', 'submission_unknown', 'cleaned')"
)


def _has_table(table_name: str) -> bool:
    """按表存在性幂等判断：create_all 建出的存量库不得重复建表。"""
    return sa.inspect(op.get_bind()).has_table(table_name)


def upgrade() -> None:
    """创建音频作业账本两表：唯一槽 + Run 级预算。"""
    if not _has_table("memoir_audio_jobs"):
        op.create_table(
            "memoir_audio_jobs",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("job_id", sa.String(length=64), nullable=False),
            sa.Column("business_id", sa.String(length=64), nullable=False),
            sa.Column("run_id", sa.String(length=64), nullable=False),
            sa.Column("generation_epoch", sa.Integer(), nullable=False),
            sa.Column("package_version", sa.String(length=32), nullable=False),
            sa.Column("role", sa.String(length=32), nullable=False),
            sa.Column("scene_id", sa.String(length=64), nullable=False),
            sa.Column("segment_index", sa.Integer(), nullable=False),
            sa.Column("input_hmac", sa.String(length=64), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("provider_task_id", sa.String(length=128), nullable=True),
            sa.Column("object_key", sa.String(length=512), nullable=True),
            sa.Column("mime", sa.String(length=64), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column(
                "reserved_cost", sa.Numeric(precision=12, scale=6), nullable=False
            ),
            sa.Column(
                "settled_cost", sa.Numeric(precision=12, scale=6), nullable=True
            ),
            sa.Column("currency", sa.String(length=8), nullable=False),
            sa.Column("usage_text_words", sa.Integer(), nullable=True),
            sa.Column("requested_music_seconds", sa.Integer(), nullable=True),
            sa.Column("lease_owner", sa.String(length=128), nullable=False),
            sa.Column("lease_token", sa.BigInteger(), nullable=False),
            sa.Column(
                "expires_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column("error_code", sa.String(length=64), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_memoir_audio_jobs")),
            sa.UniqueConstraint("job_id", name="uq_memoir_audio_jobs_job_id"),
            # 唯一槽：BGM 以哨兵 scene_id=__work__、segment_index=-1 参与，
            # 不依赖数据库 NULL 相等语义。
            sa.UniqueConstraint(
                "run_id",
                "generation_epoch",
                "package_version",
                "role",
                "scene_id",
                "segment_index",
                "input_hmac",
                name="uq_memoir_audio_job_slot",
            ),
            sa.CheckConstraint(
                f"role IN {_ROLE_IN}", name="ck_memoir_audio_job_role"
            ),
            sa.CheckConstraint(
                f"state IN {_STATE_IN}", name="ck_memoir_audio_job_state"
            ),
            sa.CheckConstraint(
                "attempt >= 1", name="ck_memoir_audio_job_attempt"
            ),
            sa.CheckConstraint(
                "lease_token >= 1", name="ck_memoir_audio_job_lease_token"
            ),
            sa.CheckConstraint(
                "segment_index >= -1", name="ck_memoir_audio_job_segment"
            ),
            sa.CheckConstraint(
                "reserved_cost >= 0", name="ck_memoir_audio_job_reserved"
            ),
            sa.CheckConstraint(
                "settled_cost IS NULL OR settled_cost >= 0",
                name="ck_memoir_audio_job_settled",
            ),
        )
        op.create_index(
            "ix_memoir_audio_jobs_business_id",
            "memoir_audio_jobs",
            ["business_id"],
        )
        op.create_index(
            "ix_memoir_audio_jobs_run_id",
            "memoir_audio_jobs",
            ["run_id"],
        )

    if not _has_table("memoir_audio_run_budgets"):
        op.create_table(
            "memoir_audio_run_budgets",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("run_id", sa.String(length=64), nullable=False),
            sa.Column("currency", sa.String(length=8), nullable=False),
            sa.Column(
                "reserved_total_cost",
                sa.Numeric(precision=12, scale=6),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.PrimaryKeyConstraint("id", name=op.f("pk_memoir_audio_run_budgets")),
            # 一个 Run 一行预算：所有场景/音乐共用同一封顶。
            sa.UniqueConstraint("run_id", name="uq_memoir_audio_run_budgets_run_id"),
            sa.CheckConstraint(
                "reserved_total_cost >= 0",
                name="ck_memoir_audio_run_budget_reserved",
            ),
        )


def downgrade() -> None:
    """回滚：删除音频账本两表（账本数据随之丢弃，属可接受回退）。"""
    if _has_table("memoir_audio_run_budgets"):
        op.drop_table("memoir_audio_run_budgets")
    if _has_table("memoir_audio_jobs"):
        op.drop_index(
            "ix_memoir_audio_jobs_run_id", table_name="memoir_audio_jobs"
        )
        op.drop_index(
            "ix_memoir_audio_jobs_business_id", table_name="memoir_audio_jobs"
        )
        op.drop_table("memoir_audio_jobs")
