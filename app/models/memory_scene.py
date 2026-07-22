"""回忆录播放场景模型。"""

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


class MemoryScene(Base):
    __tablename__ = "memory_scenes"
    __table_args__ = (
        UniqueConstraint("document_id", "scene_order", name="uq_memory_scene_order"),
        # 每个场景都带独立 major，避免未来场景结构被旧播放器静默误读。
        CheckConstraint("schema_major >= 1", name="ck_memory_scene_schema_major_positive"),
        CheckConstraint(
            "scene_type IN ('cover', 'stats', 'diary_highlight', 'bet_highlight', 'image', 'milestone', 'summary')",
            name="ck_memory_scene_type",
        ),
        CheckConstraint(
            "safety_level IN ('normal', 'sensitive', 'fallback')",
            name="ck_memory_scene_safety_level",
        ),
    )

    id = Column(Integer, primary_key=True)
    scene_id = Column(String(64), unique=True, nullable=False, index=True)
    # 场景只属于一个作品版本，禁止跨 revision 拼装播放器数据。
    document_id = Column(
        String(64),
        ForeignKey("memory_playback_documents.document_id", name="fk_memory_scene_document"),
        nullable=False,
        index=True,
    )
    scene_order = Column(Integer, nullable=False)
    # 场景 payload 的领域 schema major。
    schema_major = Column(Integer, nullable=False, default=1)
    scene_type = Column(String(32), nullable=False)
    safety_level = Column(String(24), nullable=False, default="normal")
    payload_json = Column(JSON, nullable=False)
    source_refs_json = Column(JSON, nullable=False)
