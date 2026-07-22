"""回忆录隐私删除与素材删除的持久补偿闭环。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models.memory_agent_run_ref import MemoryAgentRunRef
from app.models.memory_archive import MemoryArchive
from app.models.memory_media_asset import MemoryMediaAsset
from app.models.memory_playback_document import MemoryPlaybackDocument
from app.models.memory_runtime_compensation_event import MemoryRuntimeCompensationEvent
from app.models.memory_snapshot import MemorySnapshot
from app.models.memory_source_reference import MemorySourceReference
from app.services.memory_revision_gc_service import MemoryRevisionGcService
from app.services.memory_source_reference_service import MemorySourceReferenceService


class MemoryDeletionRuntimeGateway(Protocol):
    """删除补偿需要的最小 Runtime 能力；实现层负责服务鉴权与 HTTP 细节。"""

    def request_private_purge(self, run_id: str, idempotency_key: str) -> None:
        """请求 Runtime 建立隐私写屏障并异步执行物理清理。"""

    def cancel_run(self, run_id: str, idempotency_key: str) -> None:
        """取消已被来源删除淘汰的旧 Run。"""

    def get_privacy_state(self, run_id: str) -> str | None:
        """查询 Runtime 安全摘要中的 privacy_state；不存在返回 None。"""


@dataclass(frozen=True)
class MemoryDeletionMaintenanceReport:
    """删除补偿单轮维护的安全计数，不含 archive、播放或私密素材内容。"""

    delivered_events: int
    confirmed_purges: int
    deleted_revisions: int
    # True 表示调度 lease 已失效；调用方必须回滚本轮尚未提交的状态变更。
    aborted: bool = False


class MemoryDeletionCompensationService:
    """先提交业务侧撤权，再按稳定键投递 Runtime purge/cancel 并对账。"""

    def __init__(
        self, session: Session, gateway: MemoryDeletionRuntimeGateway | None,
    ) -> None:
        """创建补偿服务；纯本地撤权可在未创建 HTTP 客户端时安全完成。"""
        self._session = session
        self._gateway = gateway

    def request_archive_privacy_purge(self, archive_id: str) -> int:
        """删除 archive 并登记所有关联 Run 的 privacy purge 意图。

        重复请求不会再次递增 generation，也不会创建第二个外部副作用；调用方在
        本地事务提交后再调用 :meth:`deliver_pending`，因此网络失败可安全补偿。
        """
        archive = self._session.scalar(
            select(MemoryArchive)
            .where(MemoryArchive.archive_id == archive_id)
            .with_for_update()
        )
        if archive is None:
            raise ValueError("MEMORY_ARCHIVE_UNAVAILABLE")
        if archive.deleted_at is not None:
            return 0
        now = datetime.now(UTC)
        archive.deleted_at = now
        archive.generation_epoch += 1
        archive.active_run_id = None
        archive.content_status = "cancelled"
        archive.enhancement_status = "disabled"
        created = 0
        for ref in self._session.scalars(
            select(MemoryAgentRunRef).where(MemoryAgentRunRef.archive_id == archive_id)
        ):
            if ref.purge_state == "purged":
                continue
            key = ref.privacy_purge_idempotency_key or _purge_key(
                archive_id, ref.run_id, ref.generation_epoch
            )
            ref.privacy_purge_idempotency_key = key
            ref.purge_state = "requested"
            ref.privacy_purge_requested_at = now
            created += int(self._enqueue_event(ref, "privacy_purge", key) is not None)
        logging.warning(
            "回忆录 archive 已撤权并登记隐私清理 archive_id=%s purge_events=%s code=MEMORY_PRIVACY_PURGE_REQUESTED",
            archive_id,
            created,
        )
        return created

    def invalidate_deleted_source(self, source_type: str, source_id: int | str) -> int:
        """来源素材正式删除后立即切换到 baseline，并登记旧 active Run 取消。

        第一版没有安全的按场景重写器，故仅使用已经存在且无来源引用的 baseline；
        不尝试猜测性地编辑旧播放文档。播放器先切换指针，随后在同一事务清理被
        淘汰 revision 的引用、媒体与快照，确保旧素材立即不可读。
        """
        matches = MemorySourceReferenceService(self._session).find_published_revisions_by_source(
            source_type, source_id
        )
        now = datetime.now(UTC)
        invalidated = 0
        for match in matches:
            archive = self._session.scalar(
                select(MemoryArchive)
                .where(
                    MemoryArchive.archive_id == match.archive_id,
                    MemoryArchive.deleted_at.is_(None),
                )
                .with_for_update()
            )
            if archive is None or archive.published_revision != match.revision:
                continue
            baseline = self._session.scalar(
                select(MemoryPlaybackDocument).where(
                    MemoryPlaybackDocument.archive_id == archive.archive_id,
                    MemoryPlaybackDocument.revision == 0,
                )
            )
            current = self._session.scalar(
                select(MemoryPlaybackDocument).where(
                    MemoryPlaybackDocument.archive_id == archive.archive_id,
                    MemoryPlaybackDocument.revision == match.revision,
                )
            )
            if baseline is None or current is None:
                logging.warning(
                    "来源删除无法安全回退 baseline archive_id=%s code=MEMORY_SOURCE_DELETE_BASELINE_MISSING",
                    archive.archive_id,
                )
                continue
            prior_run_id = archive.active_run_id
            archive.generation_epoch += 1
            archive.active_run_id = None
            archive.published_revision = 0
            archive.content_status = "baseline"
            archive.enhancement_status = "disabled"
            baseline.is_published = True
            baseline.retain_until = None
            if current.document_id != baseline.document_id:
                current.is_published = False
                # 立即撤销播放指针后交由幂等 GC 清理旧 revision 与媒体。
                current.retain_until = now
                self._session.execute(
                    update(MemoryMediaAsset)
                    .where(MemoryMediaAsset.document_id == current.document_id)
                    .values(status="deleting")
                )
                self._session.execute(
                    delete(MemorySourceReference).where(
                        MemorySourceReference.document_id == current.document_id
                    )
                )
            # 冻结快照可能含已删除来源，不能留作下一轮生成输入。
            self._session.execute(
                delete(MemorySnapshot).where(MemorySnapshot.archive_id == archive.archive_id)
            )
            if prior_run_id:
                self._enqueue_event(
                    MemoryAgentRunRef(
                        run_id=prior_run_id,
                        archive_id=archive.archive_id,
                        generation_epoch=archive.generation_epoch,
                    ),
                    "cancel",
                    _cancel_key(archive.archive_id, prior_run_id, archive.generation_epoch),
                )
            # 来源已正式删除，不能等待普通七天宽限；新指针已经指向 baseline 后，
            # 在同一事务物理回收旧 revision、其 source-ref 与媒体记录。
            MemoryRevisionGcService(self._session).purge_expired(now)
            invalidated += 1
        logging.warning(
            "回忆录来源删除补偿 source_type=%s affected_archives=%s code=MEMORY_SOURCE_DELETE_INVALIDATED",
            source_type,
            invalidated,
        )
        return invalidated

    def deliver_pending(
        self,
        limit: int = 20,
        *,
        lease_guard: Callable[[], bool] | None = None,
    ) -> int:
        """投递尚未确认接收的外部副作用；失败保持 pending 并复用稳定键。"""
        events = self._session.scalars(
            select(MemoryRuntimeCompensationEvent)
            .where(MemoryRuntimeCompensationEvent.status == "pending")
            .order_by(MemoryRuntimeCompensationEvent.id)
            .limit(limit)
        ).all()
        delivered = 0
        for event in events:
            if not self._has_lease(lease_guard):
                logging.warning("回忆录删除补偿投递中止 operation=lease_lost")
                break
            try:
                if self._gateway is None:
                    raise RuntimeError("MEMORY_RUNTIME_GATEWAY_UNAVAILABLE")
                if event.action == "privacy_purge":
                    self._gateway.request_private_purge(event.run_id, event.idempotency_key)
                else:
                    self._gateway.cancel_run(event.run_id, event.idempotency_key)
            except Exception:
                event.attempt_count += 1
                event.last_error_code = "MEMORY_RUNTIME_COMPENSATION_FAILED"
                logging.warning(
                    "回忆录删除补偿投递失败 event_id=%s action=%s code=%s",
                    event.event_id,
                    event.action,
                    event.last_error_code,
                )
                continue
            event.status = "delivered"
            event.delivered_at = datetime.now(UTC)
            event.last_error_code = None
            delivered += 1
        return delivered

    def reconcile_purges(self, *, lease_guard: Callable[[], bool] | None = None) -> int:
        """仅在 Runtime 查询明确返回 purged 后更新业务侧完成状态。"""
        completed = 0
        events = self._session.scalars(
            select(MemoryRuntimeCompensationEvent).where(
                MemoryRuntimeCompensationEvent.action == "privacy_purge",
                MemoryRuntimeCompensationEvent.status == "delivered",
            )
        ).all()
        for event in events:
            if not self._has_lease(lease_guard):
                logging.warning("回忆录隐私清理对账中止 operation=lease_lost")
                break
            ref = self._session.scalar(
                select(MemoryAgentRunRef).where(MemoryAgentRunRef.run_id == event.run_id)
            )
            if ref is None or ref.purge_state == "purged":
                continue
            if self._gateway is None:
                logging.warning(
                    "回忆录隐私清理对账跳过 event_id=%s code=MEMORY_RUNTIME_GATEWAY_UNAVAILABLE",
                    event.event_id,
                )
                continue
            if self._gateway.get_privacy_state(event.run_id) != "purged":
                continue
            ref.purge_state = "purged"
            ref.privacy_purge_completed_at = datetime.now(UTC)
            completed += 1
        if completed:
            logging.warning(
                "回忆录隐私清理对账完成 count=%s code=MEMORY_PRIVACY_PURGE_CONFIRMED",
                completed,
            )
        return completed

    def run_maintenance(
        self,
        now: datetime,
        *,
        lease_guard: Callable[[], bool] | None = None,
    ) -> MemoryDeletionMaintenanceReport:
        """执行受租约保护的投递、确认与旧版本清理，失租立即停止。"""
        if not self._has_lease(lease_guard):
            return self._aborted_report()
        delivered = self.deliver_pending(lease_guard=lease_guard)
        if not self._has_lease(lease_guard):
            return self._aborted_report(delivered_events=delivered)
        confirmed = self.reconcile_purges(lease_guard=lease_guard)
        if not self._has_lease(lease_guard):
            return self._aborted_report(delivered_events=delivered, confirmed_purges=confirmed)
        gc_report = MemoryRevisionGcService(self._session).purge_expired(now)
        report = MemoryDeletionMaintenanceReport(
            delivered_events=delivered,
            confirmed_purges=confirmed,
            deleted_revisions=gc_report.deleted_documents,
        )
        logging.info(
            "回忆录删除维护完成 delivered=%s confirmed=%s deleted_revisions=%s",
            report.delivered_events,
            report.confirmed_purges,
            report.deleted_revisions,
        )
        return report

    @staticmethod
    def _has_lease(lease_guard: Callable[[], bool] | None) -> bool:
        """未传入 guard 时保留给独立 launcher 的既有兼容行为。"""
        return lease_guard is None or lease_guard()

    @staticmethod
    def _aborted_report(
        *, delivered_events: int = 0, confirmed_purges: int = 0
    ) -> MemoryDeletionMaintenanceReport:
        """只返回已完成的安全计数，明确通知上层回滚本轮事务。"""
        return MemoryDeletionMaintenanceReport(
            delivered_events=delivered_events,
            confirmed_purges=confirmed_purges,
            deleted_revisions=0,
            aborted=True,
        )

    def _enqueue_event(
        self, ref: MemoryAgentRunRef, action: str, idempotency_key: str
    ) -> MemoryRuntimeCompensationEvent | None:
        """按 archive/run/代次/动作去重，避免重试创建第二个外部副作用。"""
        existing = self._session.scalar(
            select(MemoryRuntimeCompensationEvent).where(
                MemoryRuntimeCompensationEvent.archive_id == ref.archive_id,
                MemoryRuntimeCompensationEvent.run_id == ref.run_id,
                MemoryRuntimeCompensationEvent.generation_epoch == ref.generation_epoch,
                MemoryRuntimeCompensationEvent.action == action,
            )
        )
        if existing is not None:
            return None
        event = MemoryRuntimeCompensationEvent(
            event_id=str(uuid4()),
            archive_id=ref.archive_id,
            run_id=ref.run_id,
            generation_epoch=ref.generation_epoch,
            action=action,
            idempotency_key=idempotency_key,
            status="pending",
        )
        self._session.add(event)
        return event


def _purge_key(archive_id: str, run_id: str, generation_epoch: int) -> str:
    """隐私 purge 键固定到原 Run 与其创建代次，不能混入时间或请求次数。"""
    return f"memory:purge:{archive_id}:{run_id}:{generation_epoch}"


def _cancel_key(archive_id: str, run_id: str, generation_epoch: int) -> str:
    """素材删除取消键固定到失效后的 archive 代次，重复补偿复用同一键。"""
    return f"memory:cancel:{archive_id}:{run_id}:{generation_epoch}"
