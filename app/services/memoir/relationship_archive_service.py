"""将关系解绑与回忆录归档放入同一业务事务。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memoir.couple_relationship import CoupleRelationship
from app.models.memoir.memory_archive import MemoryArchive
from app.services.memoir.memory_archive_service import (
    FernetSnapshotCipher,
    MemoryArchiveService,
)
from app.services.memoir.memory_runtime_launch_service import MemoryRuntimeLaunchService

LOGGER = logging.getLogger(__name__)


class RelationshipArchiveService:
    """唯一负责将已绑定关系收敛为可归档关系段的最小服务。"""

    def __init__(self, session: Session, cipher: FernetSnapshotCipher) -> None:
        self._session = session
        self._cipher = cipher

    def archive_after_unbind(
        self, relationship_id: int, *, actor_user_id: int, reason: str,
    ) -> list[MemoryArchive]:
        """锁定关系后原子标记解绑并冻结双方归档；调用方统一 commit 或 rollback。"""
        if reason not in {"peaceful", "blocked"}:
            raise ValueError("RELATIONSHIP_UNBOUND_REASON_INVALID")
        relationship = self._session.scalar(select(CoupleRelationship).where(
            CoupleRelationship.id == relationship_id
        ).with_for_update())
        if relationship is None or actor_user_id not in {
            relationship.user_a_id, relationship.user_b_id,
        }:
            raise ValueError("RELATIONSHIP_UNBOUND_FORBIDDEN")
        if relationship.status == "BOUND":
            relationship.status = "UNBOUND_ARCHIVED"
            relationship.unbound_at = datetime.now(UTC)
            relationship.unbound_by_user_id = actor_user_id
            relationship.unbound_reason = reason
            # 日志仅保留关系标识与原因，禁止写入日记正文、提示词或完整播放文档。
            LOGGER.info(
                "情侣关系已归档解绑 relationship_id=%s reason=%s", relationship_id, reason
            )
        elif relationship.status != "UNBOUND_ARCHIVED":
            raise ValueError("RELATIONSHIP_UNBOUND_STATE_INVALID")
        archives = MemoryArchiveService(
            self._session, self._cipher
        ).create_archives_for_unbound_relationship(relationship_id)
        # 同一事务写入启动意图；Runtime 不可用时 revision 0 baseline 仍可正常提交。
        launch_service = MemoryRuntimeLaunchService(self._session)
        for archive in archives:
            launch_service.enqueue(archive.archive_id)
        return archives
