"""保存受控模型 thinking 摘要，禁止持久化隐藏推理。"""

import sqlalchemy as sa

from alembic import op
from app.db.alembic_schema_bootstrap import runtime_schema_created_at_head

revision = "20260727_0900"
down_revision = "20260723_1200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if runtime_schema_created_at_head():
        return
    with op.batch_alter_table("agent_model_usages") as batch_op:
        batch_op.add_column(sa.Column("thinking_summary_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_model_usages") as batch_op:
        batch_op.drop_column("thinking_summary_json")
