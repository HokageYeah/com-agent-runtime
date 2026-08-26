"""回忆录场景的播放动作模型。"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import JSON

from app.db.sqlalchemy_db import Base


class MemoryAction(Base):
    __tablename__ = "memory_actions"
    __table_args__ = (
        UniqueConstraint("scene_id", "action_order", name="uq_memory_action_order"),
        CheckConstraint("duration_ms > 0", name="ck_memory_action_duration_positive"),
        CheckConstraint("schema_major >= 1", name="ck_memory_action_schema_major_positive"),
        CheckConstraint(
            "action_type IN ('show_card', 'focus_image', 'type_text', 'hold', 'play_tts', 'transition')",
            name="ck_memory_action_type",
        ),
    )

    id = Column(Integer, primary_key=True)
    action_id = Column(String(64), unique=True, nullable=False, index=True)
    # Action 必须跟随同一 document 内已存在的 Scene，禁止孤儿动作。
    scene_id = Column(
        String(64),
        ForeignKey("memory_scenes.scene_id", name="fk_memory_action_scene"),
        nullable=False,
        index=True,
    )
    action_order = Column(Integer, nullable=False)
    # 动作 payload 的领域 schema major。
    schema_major = Column(Integer, nullable=False, default=1)
    action_type = Column(String(32), nullable=False)
    duration_ms = Column(Integer, nullable=False)
    payload_json = Column(JSON, nullable=False)
