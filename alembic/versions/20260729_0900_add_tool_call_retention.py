"""为副作用工具审计增加稳定逻辑键保留期限。"""

import sqlalchemy as sa

from alembic import op

revision = "20260729_0900"
down_revision = "20260728_0900"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 先允许旧行回填，兼容 SQLite 与 PostgreSQL 两种受支持迁移路径。
    with op.batch_alter_table("agent_tool_calls") as batch:
        batch.add_column(sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_agent_tool_calls_retention_until", ["retention_until"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("UPDATE agent_tool_calls SET retention_until = created_at + INTERVAL '30 days' WHERE retention_until IS NULL")
    else:
        op.execute("UPDATE agent_tool_calls SET retention_until = datetime(created_at, '+30 days') WHERE retention_until IS NULL")
    with op.batch_alter_table("agent_tool_calls") as batch:
        batch.alter_column("retention_until", nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("agent_tool_calls") as batch:
        batch.drop_index("ix_agent_tool_calls_retention_until")
        batch.drop_column("retention_until")
