"""回忆录作品到冻结素材的最小反查映射，不保存素材正文。"""

from __future__ import annotations

from sqlalchemy import Column, Integer, String, UniqueConstraint

from app.db.sqlalchemy_db import Base


class MemorySourceReference(Base):
    """供素材删除补偿定位 archive/revision 的发布期引用记录。"""

    __tablename__ = "memory_source_references"
    __table_args__ = (
        # 同一文档不重复写入相同来源，重试发布不扩大引用集合。
        UniqueConstraint(
            "document_id", "source_type", "source_id",
            name="uq_memory_source_reference_document_source",
        ),
    )

    id = Column(Integer, primary_key=True)
    # 归档和 revision 作为反查安全摘要，避免反向解析完整 document JSON。
    archive_id = Column(String(64), nullable=False, index=True)
    document_id = Column(String(64), nullable=False, index=True)
    revision = Column(Integer, nullable=False, index=True)
    source_type = Column(String(24), nullable=False, index=True)
    source_id = Column(String(80), nullable=False, index=True)
