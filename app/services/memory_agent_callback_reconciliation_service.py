"""按 Runtime 的无内容状态摘要补偿回忆录 callback 投影。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.models.memory_agent_run_ref import MemoryAgentRunRef
from app.models.memory_archive import MemoryArchive
from app.services.memory_agent_adapter import RuntimeRunState


class MemoryRunStateGateway(Protocol):
    """业务侧恢复所需的唯一 Runtime 查询，不暴露输入、步骤或正文。"""

    def get_run_state(self, run_id: str) -> RuntimeRunState | None: ...


class MemoryAgentCallbackReconciliationService:
    """以 Runtime 版本摘要修复 callback 缺失，不重建 callback 或公开轨迹。"""

    _REF_STATUSES = frozenset(
        {"pending", "running", "waiting_human", "succeeded", "partial", "failed", "cancelled"}
    )

    def __init__(self, session: Session, gateway: MemoryRunStateGateway) -> None:
        self._session = session
        self._gateway = gateway

    def reconcile_run(self, run_id: str) -> bool:
        """只在远端 event/status 版本单调前进时更新 RunRef。"""
        ref = self._session.scalar(
            select(MemoryAgentRunRef)
            .where(MemoryAgentRunRef.run_id == run_id)
            .with_for_update()
        )
        if ref is None:
            return False
        remote = self._gateway.get_run_state(run_id)
        if remote is None:
            self._mark_needed(ref, "MEMORY_RUNTIME_STATE_NOT_FOUND")
            return False
        if remote.run_id != ref.run_id or remote.status not in self._REF_STATUSES:
            self._mark_needed(ref, "MEMORY_RUNTIME_STATE_INVALID")
            return False
        archive = self._session.scalar(
            select(MemoryArchive)
            .where(MemoryArchive.archive_id == ref.archive_id)
            .with_for_update()
        )
        if (
            archive is None
            or archive.active_run_id != ref.run_id
            or archive.generation_epoch != ref.generation_epoch
        ):
            return False
        purge_updated = self._confirm_purge(ref, remote)
        if remote.last_event_seq <= ref.event_seq or remote.status_version < ref.status_version:
            return purge_updated
        if remote.status in {"succeeded", "partial"} and (
            archive.published_revision <= 0 or archive.content_status != "succeeded"
        ):
            self._mark_needed(ref, "MEMORY_RUNTIME_PUBLISH_RECONCILIATION_NEEDED")
            return purge_updated
        # row_version 与已读的两个 Runtime 版本共同组成条件写屏障，避免并发 callback
        # 或另一补偿实例覆盖新投影。兜底查询不修改 public_trace。
        updated = self._session.execute(
            update(MemoryAgentRunRef)
            .where(
                MemoryAgentRunRef.run_id == ref.run_id,
                MemoryAgentRunRef.row_version == ref.row_version,
                MemoryAgentRunRef.event_seq < remote.last_event_seq,
                MemoryAgentRunRef.status_version <= remote.status_version,
            )
            .values(
                status=remote.status,
                event_seq=remote.last_event_seq,
                status_version=remote.status_version,
                row_version=MemoryAgentRunRef.row_version + 1,
                reconciliation_status="not_needed",
            )
            .execution_options(synchronize_session=False)
        )
        if updated.rowcount != 1:  # type: ignore[attr-defined]
            return purge_updated
        self._session.refresh(ref)
        logging.info(
            "回忆录 Runtime 状态兜底已应用 run_id=%s event_seq=%s status_version=%s",
            run_id,
            remote.last_event_seq,
            remote.status_version,
        )
        return True

    def reconcile_pending(self, limit: int = 20) -> int:
        """扫描可能缺 callback 的活跃 RunRef，查询失败不把状态猜成终态。"""
        refs = self._session.scalars(
            select(MemoryAgentRunRef)
            .where(
                or_(
                    MemoryAgentRunRef.reconciliation_status == "needed",
                    MemoryAgentRunRef.status.in_(
                        ("pending_start", "pending", "running", "waiting_human")
                    ),
                )
            )
            .order_by(MemoryAgentRunRef.updated_at, MemoryAgentRunRef.id)
            .limit(limit)
        ).all()
        return sum(self.reconcile_run(ref.run_id) for ref in refs)

    @staticmethod
    def _mark_needed(ref: MemoryAgentRunRef, reason_code: str) -> None:
        ref.reconciliation_status = "needed"
        logging.warning("回忆录 Runtime 状态兜底待对账 run_id=%s code=%s", ref.run_id, reason_code)

    @staticmethod
    def _confirm_purge(ref: MemoryAgentRunRef, remote: RuntimeRunState) -> bool:
        if (
            remote.privacy_state != "purged"
            or remote.privacy_version < 1
            or ref.purge_state == "purged"
        ):
            return False
        ref.purge_state = "purged"
        ref.privacy_purge_completed_at = datetime.now(UTC)
        logging.info("回忆录 Runtime purge 已确认 run_id=%s", ref.run_id)
        return True
