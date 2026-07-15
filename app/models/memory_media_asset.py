"""媒体资产仅保存 storage key，不持久化签名 URL 或 prompt 原文。"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.sql import func

from app.db.sqlalchemy_db import Base


class MemoryMediaAsset(Base):
    __tablename__ = "memory_media_assets"

    id = Column(Integer, primary_key=True)
    asset_id = Column(String(64), unique=True, nullable=False, index=True)
    archive_id = Column(String(64), nullable=False, index=True)
    document_id = Column(String(64), nullable=False, index=True)
    media_type = Column(String(16), nullable=False)
    source_type = Column(String(24), nullable=False)
    status = Column(String(24), nullable=False, default="ready")
    storage_key = Column(String(512), nullable=False)
    prompt_hash = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
