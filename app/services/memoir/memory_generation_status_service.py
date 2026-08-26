"""回忆录生成状态的安全查询服务。"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memoir.memory_agent_run_ref import MemoryAgentRunRef
from app.models.memoir.memory_archive import MemoryArchive


class MemoryGenerationStatusService:
    """汇总 Archive 与当前 RunRef，供业务服务向前端返回生成进度。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, archive_id: str) -> dict[str, Any]:
        """返回不含快照、日记正文和播放文档的状态摘要。"""
        archive = self._session.scalar(
            select(MemoryArchive).where(
                MemoryArchive.archive_id == archive_id,
                MemoryArchive.deleted_at.is_(None),
            )
        )
        if archive is None:
            raise ValueError("MEMORY_ARCHIVE_UNAVAILABLE")
        active_run: dict[str, Any] | None = None
        status_version = 0
        updated_at = archive.updated_at
        retry_after_ms = 0
        if archive.active_run_id:
            ref = self._session.scalar(
                select(MemoryAgentRunRef).where(MemoryAgentRunRef.run_id == archive.active_run_id)
            )
            if ref is not None:
                status_version = ref.status_version
                updated_at = ref.updated_at
                # 轮询仅在未终态 Run 存在时继续；页面依据 status_version 不变自行退避。
                retry_after_ms = 2000 if ref.status in {"pending", "running", "waiting_human"} else 0
                active_run = {
                    "run_id": ref.run_id,
                    "status": ref.status,
                    "event_seq": ref.event_seq,
                    "status_version": ref.status_version,
                    "reconciliation_status": ref.reconciliation_status,
                    "public_trace": ref.public_trace_json,
                }
        logging.info("查询回忆录生成状态 archive_id=%s active_run=%s", archive_id, bool(active_run))
        return {
            "archive_id": archive.archive_id,
            "content_status": archive.content_status,
            "enhancement_status": archive.enhancement_status,
            "generation_epoch": archive.generation_epoch,
            "published_revision": archive.published_revision,
            "status_version": status_version,
            "updated_at": updated_at.isoformat(),
            "retry_after_ms": retry_after_ms,
            "active_run": active_run,
        }
