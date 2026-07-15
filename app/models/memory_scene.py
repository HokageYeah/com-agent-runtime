"""回忆录播放场景模型。"""

from __future__ import annotations

from sqlalchemy import Column, Integer, String, UniqueConstraint
from sqlalchemy.dialects.mysql import JSON

from app.db.sqlalchemy_db import Base


class MemoryScene(Base):
    __tablename__ = "memory_scenes"
    __table_args__ = (
        UniqueConstraint("document_id", "scene_order", name="uq_memory_scene_order"),
    )

    id = Column(Integer, primary_key=True)
    scene_id = Column(String(64), unique=True, nullable=False, index=True)
    document_id = Column(String(64), nullable=False, index=True)
    scene_order = Column(Integer, nullable=False)
    scene_type = Column(String(32), nullable=False)
    safety_level = Column(String(24), nullable=False, default="normal")
    payload_json = Column(JSON, nullable=False)
    source_refs_json = Column(JSON, nullable=False)
