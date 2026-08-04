"""为迟到模型 usage 结算保存无内容 Provider 请求身份。"""

import sqlalchemy as sa

from alembic import op
from app.db.alembic_schema_bootstrap import runtime_schema_created_at_head

revision = "20260723_1200"
down_revision = "20260723_1100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if runtime_schema_created_at_head():
        return
    """身份为空的历史未知 usage 保持不可被迟到计量收敛。"""
    with op.batch_alter_table("agent_model_usages") as batch_op:
        batch_op.add_column(sa.Column("provider_request_id", sa.String(length=120), nullable=True))


def downgrade() -> None:
    """回滚仅删除本迁移引入的无内容身份字段。"""
    with op.batch_alter_table("agent_model_usages") as batch_op:
        batch_op.drop_column("provider_request_id")
