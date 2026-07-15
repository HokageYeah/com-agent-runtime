"""回忆录播放器读取边界：只按 archive 的 published_revision 返回完整文档。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memory_archive import MemoryArchive
from app.models.memory_playback_document import MemoryPlaybackDocument


class MemoryPlayerService:
    """禁止把不同 revision 的 Scene/Action/Media 拼接给播放器。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_published_document(self, archive_id: str) -> MemoryPlaybackDocument:
        archive = self._session.scalar(
            select(MemoryArchive).where(
                MemoryArchive.archive_id == archive_id,
                MemoryArchive.deleted_at.is_(None),
            )
        )
        if archive is None:
            raise ValueError("回忆录归档不存在或已删除")
        document = self._session.scalar(
            select(MemoryPlaybackDocument).where(
                MemoryPlaybackDocument.archive_id == archive_id,
                MemoryPlaybackDocument.revision == archive.published_revision,
                MemoryPlaybackDocument.is_published.is_(True),
            )
        )
        if document is None:
            raise ValueError("回忆录已发布版本不存在")
        return document
