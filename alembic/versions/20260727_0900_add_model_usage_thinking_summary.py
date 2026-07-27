"""保存受控模型 thinking 摘要，禁止持久化隐藏推理。"""

import sqlalchemy as sa

from alembic import op

revision = "20260727_0900"
down_revision = "20260723_1200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_model_usages") as batch_op:
        batch_op.add_column(sa.Column("thinking_summary_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_model_usages") as batch_op:
        batch_op.drop_column("thinking_summary_json")
