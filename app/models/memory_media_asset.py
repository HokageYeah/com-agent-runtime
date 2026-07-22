"""媒体资产仅保存 storage key，不持久化签名 URL 或 prompt 原文。"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
)
from sqlalchemy.sql import func

from app.db.sqlalchemy_db import Base


class MemoryMediaAsset(Base):
    """媒体资产只保存私有 storage key；访问 URL 必须在鉴权接口临时生成。"""

    __tablename__ = "memory_media_assets"
    __table_args__ = (
        # 资产必须绑定 document 所属的同一 archive，不能借 ID 跨归档挂载。
        ForeignKeyConstraint(
            ("archive_id", "document_id"),
            ("memory_playback_documents.archive_id", "memory_playback_documents.document_id"),
            name="fk_memory_media_document_archive",
        ),
        CheckConstraint(
            "media_type IN ('image', 'audio', 'video')", name="ck_memory_media_type"
        ),
        CheckConstraint(
            "source_type IN ('diary_original', 'ai_generated', 'tts', 'default_asset')",
            name="ck_memory_media_source_type",
        ),
        CheckConstraint(
            "status IN ('ready', 'deleting', 'deleted')", name="ck_memory_media_status"
        ),
    )

    id = Column(Integer, primary_key=True)
    asset_id = Column(String(64), unique=True, nullable=False, index=True)
    # 复合外键的一部分，保证资产与作品版本所属 archive 一致。
    archive_id = Column(String(64), nullable=False, index=True)
    # 复合外键的一部分，资产不可跨 document/revision 复用。
    document_id = Column(String(64), nullable=False, index=True)
    # 媒体类型仅允许图片、音频、视频三类。
    media_type = Column(String(16), nullable=False)
    # 资产来源只记录受控枚举，不保存原始素材或 prompt。
    source_type = Column(String(24), nullable=False)
    # 删除流程使用 deleting/deleted，生成中状态由后续 MediaTask 管理。
    status = Column(String(24), nullable=False, default="ready")
    # 私有对象存储键，禁止存储签名访问 URL。
    storage_key = Column(String(512), nullable=False)
    # 仅存 prompt 哈希用于审计，不存敏感 prompt 原文。
    prompt_hash = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
