"""冻结且加密的回忆录输入快照模型。"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.sql import func

from app.db.sqlalchemy_db import Base


class MemorySnapshot(Base):
    """只保存冻结 manifest 与加密正文，禁止通用查询解密。"""

    __tablename__ = "memory_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "archive_id", "snapshot_version", name="uq_memory_snapshot_version"
        ),
    )

    id = Column(Integer, primary_key=True)
    snapshot_id = Column(String(64), unique=True, nullable=False, index=True)
    archive_id = Column(String(64), nullable=False, index=True)
    snapshot_version = Column(Integer, nullable=False, default=1)
    source_manifest_json = Column(JSON, nullable=False)
    source_manifest_hash = Column(String(128), nullable=False, index=True)
    privacy_filter_version = Column(String(40), nullable=False)
    snapshot_cutoff_at = Column(DateTime, nullable=False)
    encryption_key_id = Column(String(80), nullable=False)
    encrypted_payload = Column(LargeBinary, nullable=False)
    content_digest = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)
