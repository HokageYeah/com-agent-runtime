"""回忆录素材引用反查服务；只返回删除补偿所需的安全摘要。"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memoir.memory_archive import MemoryArchive
from app.models.memoir.memory_source_reference import MemorySourceReference


@dataclass(frozen=True)
class PublishedSourceReference:
    """仍是当前公开版本的 archive/revision 定位结果，不携带素材或播放正文。"""

    archive_id: str
    revision: int


class MemorySourceReferenceService:
    """为素材正式删除补偿提供当前已发布 revision 的最小反查入口。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_published_revisions_by_source(
        self, source_type: str, source_id: int | str,
    ) -> list[PublishedSourceReference]:
        """仅返回未删除 archive 的当前发布指针，忽略宽限期旧 revision。"""
        if source_type not in {"diary", "completed_bet"} or isinstance(source_id, bool):
            raise ValueError("MEMORY_SOURCE_REF_INVALID")
        normalized_id = str(source_id)
        if not normalized_id:
            raise ValueError("MEMORY_SOURCE_REF_INVALID")
        rows = self._session.execute(
            select(MemorySourceReference.archive_id, MemorySourceReference.revision)
            .join(MemoryArchive, MemoryArchive.archive_id == MemorySourceReference.archive_id)
            .where(
                MemorySourceReference.source_type == source_type,
                MemorySourceReference.source_id == normalized_id,
                MemoryArchive.deleted_at.is_(None),
                MemoryArchive.published_revision == MemorySourceReference.revision,
            )
            .order_by(MemorySourceReference.archive_id)
        ).all()
        result = [PublishedSourceReference(archive_id, revision) for archive_id, revision in rows]
        logger.info(
            "回忆录素材反查 source_type={} match_count={}", source_type, len(result)
        )
        return result
