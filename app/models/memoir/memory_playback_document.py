"""可播放文档 revision；不与草稿或其他 revision 混读。"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.sql import func

from app.db.sqlalchemy_db import Base


class MemoryPlaybackDocument(Base):
    """完整作品的版本容器，revision 0 是 Runtime 不可用时的 baseline。"""

    __tablename__ = "memory_playback_documents"
    __table_args__ = (
        UniqueConstraint("archive_id", "revision", name="uq_memory_document_revision"),
        # 供 MediaAsset 的复合外键确认 document 不会跨 archive 挂载。
        UniqueConstraint("archive_id", "document_id", name="uq_memory_document_archive_id"),
        # revision 0 为不可用 Runtime 时的 baseline，schema major 必须为正。
        CheckConstraint("revision >= 0", name="ck_memory_document_revision_nonnegative"),
        CheckConstraint("schema_major >= 1", name="ck_memory_document_schema_major_positive"),
    )

    id = Column(Integer, primary_key=True)
    document_id = Column(String(64), unique=True, nullable=False, index=True)
    # 归档删除前必须先按 Task 10.5 执行撤权，不能留下悬空作品版本。
    archive_id = Column(
        String(64),
        ForeignKey("memory_archives.archive_id", name="fk_memory_document_archive"),
        nullable=False,
        index=True,
    )
    revision = Column(Integer, nullable=False)
    # 播放文档版本主号，读取与发布均拒绝未知未来主版本。
    schema_major = Column(Integer, nullable=False, default=1)
    document_json = Column(JSON, nullable=False)
    content_digest = Column(String(128), nullable=False)
    is_published = Column(Boolean, nullable=False, default=False)
    retain_until = Column(DateTime, nullable=True)
    published_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
