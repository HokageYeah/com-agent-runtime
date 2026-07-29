"""回忆录归档权威容器；播放器只经由 published_revision 读取作品。"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
        CheckConstraint(
            "content_status IN "
            "('baseline','pending','running','waiting_human','succeeded',"
            "'failed','cancelled')",
            name="ck_memory_archive_content_status",
        ),
        CheckConstraint(
            "enhancement_status IN "
            "('disabled','pending','running','succeeded','partial','failed')",
            name="ck_memory_archive_enhancement_status",
        ),
    )

    id = Column(Integer, primary_key=True)
    archive_id = Column(String(64), unique=True, nullable=False, index=True)
    relationship_id = Column(Integer, nullable=False, index=True)
    space_id = Column(String(64), nullable=False, index=True)
    relationship_segment_no = Column(Integer, nullable=False)
    owner_user_id = Column(Integer, nullable=False, index=True)
    partner_user_id = Column(Integer, nullable=False)
    # 内容状态由原子发布工具与 callback adapter 分工写入，播放器只认 published_revision。
    content_status = Column(String(32), nullable=False, default="baseline")
    # 媒体增强由独立媒体 worker 独占写入；能力关闭时始终保持 disabled。
    enhancement_status = Column(String(32), nullable=False, default="disabled")
    partner_nickname_snapshot = Column(String(100), nullable=True)
    # 只保存受信任对象键/资产引用，禁止持久化 URL。
    partner_avatar_snapshot = Column(String(255), nullable=True)
    bound_at = Column(DateTime(timezone=True), nullable=True)
    unbound_at = Column(DateTime(timezone=True), nullable=True)
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
