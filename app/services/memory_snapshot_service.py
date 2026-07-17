"""回忆录内部工具专用的冻结快照读取服务。"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memory_archive import MemoryArchive
from app.models.memory_snapshot import MemorySnapshot
from app.services.memory_archive_service import FernetSnapshotCipher


class MemorySnapshotService:
    """只允许已绑定 archive 的内部工具读取加密快照。"""

    def __init__(self, session: Session, cipher: FernetSnapshotCipher) -> None:
        self._session, self._cipher = session, cipher

    def read_for_runtime(self, archive_id: str, snapshot_id: str) -> dict[str, Any]:
        archive = self._session.scalar(select(MemoryArchive).where(
            MemoryArchive.archive_id == archive_id, MemoryArchive.deleted_at.is_(None)
        ))
        snapshot = self._session.scalar(select(MemorySnapshot).where(
            MemorySnapshot.snapshot_id == snapshot_id, MemorySnapshot.archive_id == archive_id
        ))
        if archive is None or snapshot is None:
            logging.warning("Runtime 快照读取被拒绝 archive_id=%s snapshot_id=%s", archive_id, snapshot_id)
            raise ValueError("MEMORY_SNAPSHOT_UNAVAILABLE")
        payload = self._cipher.decrypt_json(snapshot.encrypted_payload)
        logging.info("Runtime 已读取冻结快照 archive_id=%s snapshot_id=%s", archive_id, snapshot_id)
        return payload
