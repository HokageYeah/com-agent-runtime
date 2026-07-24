"""为安全评测聚合增加无内容布尔结论。"""

import sqlalchemy as sa

from alembic import op

revision = "20260723_1000"
down_revision = "20260723_0900"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """历史评测保留未知值，不伪造通过率。"""
    with op.batch_alter_table("agent_evaluations") as batch_op:
        batch_op.add_column(sa.Column("schema_passed", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("grounding_passed", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("emotional_safety_passed", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """回滚安全聚合列。"""
    with op.batch_alter_table("agent_evaluations") as batch_op:
        batch_op.drop_column("emotional_safety_passed")
        batch_op.drop_column("grounding_passed")
        batch_op.drop_column("schema_passed")
