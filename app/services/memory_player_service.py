"""回忆录播放器读取边界：只按 archive 的 published_revision 返回完整文档。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memory_action import MemoryAction
from app.models.memory_archive import MemoryArchive
from app.models.memory_media_asset import MemoryMediaAsset
from app.models.memory_playback_document import MemoryPlaybackDocument
from app.models.memory_scene import MemoryScene


@dataclass(frozen=True)
class PublishedMemoryPlayback:
    """同一 published revision 的完整播放读取 DTO，禁止跨文档拼接。"""

    document: MemoryPlaybackDocument
    scenes: list[MemoryScene]
    actions: list[MemoryAction]
    media_assets: list[MemoryMediaAsset]


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

    def get_published_playback(self, archive_id: str) -> PublishedMemoryPlayback:
        """按发布指针锁定一个 document，再只读取它名下的播放子项。"""
        document = self.get_published_document(archive_id)
        scenes = self._session.scalars(
            select(MemoryScene).where(MemoryScene.document_id == document.document_id)
            .order_by(MemoryScene.scene_order)
        ).all()
        scene_ids = [scene.scene_id for scene in scenes]
        actions = self._session.scalars(
            select(MemoryAction).where(MemoryAction.scene_id.in_(scene_ids))
            .order_by(MemoryAction.scene_id, MemoryAction.action_order)
        ).all() if scene_ids else []
        media_assets = self._session.scalars(
            select(MemoryMediaAsset).where(
                MemoryMediaAsset.archive_id == archive_id,
                MemoryMediaAsset.document_id == document.document_id,
            )
        ).all()
        return PublishedMemoryPlayback(document, scenes, actions, media_assets)
