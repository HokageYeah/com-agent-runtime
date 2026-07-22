"""回忆录替代版本的最小宽限期回收服务。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.memory_action import MemoryAction
from app.models.memory_media_asset import MemoryMediaAsset
from app.models.memory_playback_document import MemoryPlaybackDocument
from app.models.memory_scene import MemoryScene
from app.models.memory_source_reference import MemorySourceReference


@dataclass(frozen=True)
class MemoryRevisionGcReport:
    """仅记录回收计数，避免把播放文档或素材内容写入日志。"""

    scanned_documents: int
    deleted_documents: int


class MemoryRevisionGcService:
    """只回收已过宽限期且不再发布的完整作品版本。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def purge_expired(self, now: datetime) -> MemoryRevisionGcReport:
        """幂等删除过期旧 revision，不处理隐私 purge 或任何 Snapshot 正文。"""
        current_time = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        documents = self._session.scalars(
            select(MemoryPlaybackDocument).where(
                MemoryPlaybackDocument.is_published.is_(False),
                MemoryPlaybackDocument.retain_until.is_not(None),
                MemoryPlaybackDocument.retain_until <= current_time,
            ).with_for_update()
        ).all()
        for document in documents:
            scene_ids = self._session.scalars(select(MemoryScene.scene_id).where(
                MemoryScene.document_id == document.document_id,
            )).all()
            self._session.execute(delete(MemorySourceReference).where(
                MemorySourceReference.document_id == document.document_id,
            ))
            self._session.execute(delete(MemoryMediaAsset).where(
                MemoryMediaAsset.document_id == document.document_id,
            ))
            if scene_ids:
                self._session.execute(delete(MemoryAction).where(MemoryAction.scene_id.in_(scene_ids)))
            self._session.execute(delete(MemoryScene).where(
                MemoryScene.document_id == document.document_id,
            ))
            self._session.delete(document)
        logging.info(
            "回忆录旧版本回收 scanned=%s deleted=%s code=MEMORY_REVISION_GC_COMPLETED",
            len(documents), len(documents),
        )
        return MemoryRevisionGcReport(
            scanned_documents=len(documents), deleted_documents=len(documents),
        )
