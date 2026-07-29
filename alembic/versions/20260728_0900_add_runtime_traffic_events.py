"""新增无内容 RuntimeTrafficEvent 窗口账本。"""

import sqlalchemy as sa

from alembic import op

revision = "20260728_0900"
down_revision = "20260727_0900"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_traffic_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("route_id", sa.String(length=120), nullable=False),
        sa.Column("result_code", sa.String(length=80), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_key", sa.String(length=320), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("window_key"),
    )


def downgrade() -> None:
    op.drop_table("runtime_traffic_events")
