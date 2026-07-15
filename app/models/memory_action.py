"""回忆录场景的播放动作模型。"""

from __future__ import annotations

from sqlalchemy import Column, Integer, String, UniqueConstraint
from sqlalchemy.dialects.mysql import JSON

from app.db.sqlalchemy_db import Base


class MemoryAction(Base):
    __tablename__ = "memory_actions"
    __table_args__ = (
        UniqueConstraint("scene_id", "action_order", name="uq_memory_action_order"),
    )

    id = Column(Integer, primary_key=True)
    action_id = Column(String(64), unique=True, nullable=False, index=True)
    scene_id = Column(String(64), nullable=False, index=True)
    action_order = Column(Integer, nullable=False)
    action_type = Column(String(32), nullable=False)
    duration_ms = Column(Integer, nullable=False)
    payload_json = Column(JSON, nullable=False)
