"""将公共 AgentRuntime 表纳入根工程迁移链。"""

from __future__ import annotations

import app.models  # noqa: F401
from alembic import op
from app.db.sqlalchemy_db import Base

revision = "20260715_1600"
down_revision = "20260715_1500"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 Runtime 权威表；业务回忆录表不在此迁移中重复创建。"""
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    """按依赖关系删除 Runtime 表，保留情侣日记业务数据。"""
    for table_name in (
        "runtime_audit_records", "idempotency_records", "runtime_outbox_events",
        "callback_events", "agent_model_usages", "agent_artifacts", "agent_checkpoints",
        "agent_evaluations", "agent_tool_calls", "agent_steps", "agent_plans",
        "admission_buckets", "agent_runs", "agent_definitions",
    ):
        op.drop_table(table_name, if_exists=True)
