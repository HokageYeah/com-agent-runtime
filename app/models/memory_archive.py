"""回忆录归档权威容器；播放器只经由 published_revision 读取作品。"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from app.db.sqlalchemy_db import Base


class MemoryArchive(Base):
    """一段关系中、一个拥有者对应的一份独立回忆录归档。"""

    __tablename__ = "memory_archives"
    __table_args__ = (
        UniqueConstraint(
            "space_id", "relationship_segment_no", "owner_user_id",
            name="uq_memory_archive_owner_segment",
        ),
    )

    id = Column(Integer, primary_key=True)
    archive_id = Column(String(64), unique=True, nullable=False, index=True)
    relationship_id = Column(Integer, nullable=False, index=True)
    space_id = Column(String(64), nullable=False, index=True)
    relationship_segment_no = Column(Integer, nullable=False)
    owner_user_id = Column(Integer, nullable=False, index=True)
    partner_user_id = Column(Integer, nullable=False)
    content_status = Column(String(32), nullable=False, default="baseline_ready")
    enhancement_status = Column(String(32), nullable=False, default="not_started")
    generation_epoch = Column(Integer, nullable=False, default=0)
    active_run_id = Column(String(80), nullable=True, unique=True)
    published_revision = Column(Integer, nullable=False, default=0)
    summary = Column(Text, nullable=True)
    is_pinned = Column(Boolean, nullable=False, default=False)
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
