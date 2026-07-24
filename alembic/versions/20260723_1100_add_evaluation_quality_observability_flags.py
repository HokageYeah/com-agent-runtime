"""为评测质量聚合增加无内容引用与编造结论。"""

import sqlalchemy as sa

from alembic import op

revision = "20260723_1100"
down_revision = "20260723_1000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """历史评测保持未知，避免将未计算的引用或编造风险伪造为通过。"""
    with op.batch_alter_table("agent_evaluations") as batch_op:
        batch_op.add_column(sa.Column("material_reference_passed", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("hallucination_detected", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """回滚本轮新增的两项安全质量结论。"""
    with op.batch_alter_table("agent_evaluations") as batch_op:
        batch_op.drop_column("hallucination_detected")
        batch_op.drop_column("material_reference_passed")
