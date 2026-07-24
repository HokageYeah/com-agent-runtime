"""为模型 token 预算增加最小预留计量字段。"""

import sqlalchemy as sa

from alembic import op

revision = "20260723_0900"
down_revision = "20260722_1000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增可空预留 token，历史未知调用保持未知而非伪造零。"""
    with op.batch_alter_table("agent_model_usages") as batch_op:
        batch_op.add_column(sa.Column("reserved_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    """回滚仅移除本迁移引入的 token 预留列。"""
    with op.batch_alter_table("agent_model_usages") as batch_op:
        batch_op.drop_column("reserved_tokens")
