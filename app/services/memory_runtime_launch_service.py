"""回忆录归档到 Runtime held/start 握手的最小可靠 outbox。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memory_agent_run_ref import MemoryAgentRunRef
from app.models.memory_archive import MemoryArchive
from app.models.memory_runtime_launch_event import MemoryRuntimeLaunchEvent
from app.models.memory_snapshot import MemorySnapshot
from app.services.memory_agent_binding_service import MemoryAgentBindingService

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeHeldRun:
    """Runtime create-held 响应中允许业务侧持久化的安全摘要。"""

    run_id: str
    contract_version: str
    package_digest: str
    authorization_version: int


class MemoryRuntimeGateway(Protocol):
    """业务后端调用 Runtime 的窄接口；实现层负责鉴权与 HTTP 签名。"""

    def create_held(
        self,
        *,
        archive_id: str,
        snapshot_id: str,
        generation_epoch: int,
        idempotency_key: str,
    ) -> RuntimeHeldRun:
        """创建未调度的 held Run，输入只能包含业务资源标识。"""

    def start_held(self, *, run_id: str, idempotency_key: str) -> None:
        """显式允许已绑定的 held Run 入队执行。"""

    def get_run_summary(self, run_id: str) -> RuntimeHeldRun | None:
        """查询 held Run 的安全身份摘要；不存在时返回 None。"""

    def cancel_run(self, run_id: str, idempotency_key: str) -> None:
        """取消已被业务归档代次淘汰的 held Run。"""


class MemoryRuntimeLaunchService:
    """将外部 Runtime 调用分解为可重试的 create/start 两个业务 outbox 事件。"""

    def __init__(
        self, session: Session, gateway: MemoryRuntimeGateway | None = None,
    ) -> None:
        self._session = session
        self._gateway = gateway

    def enqueue(self, archive_id: str) -> MemoryRuntimeLaunchEvent:
        """在归档事务中登记 create-held 意图；不执行网络调用也不改变 baseline。"""
        archive = self._session.scalar(select(MemoryArchive).where(
            MemoryArchive.archive_id == archive_id,
            MemoryArchive.deleted_at.is_(None),
        ).with_for_update())
        if archive is None:
            raise ValueError("MEMORY_ARCHIVE_UNAVAILABLE")
        snapshot = self._session.scalar(select(MemorySnapshot).where(
            MemorySnapshot.archive_id == archive_id,
        ).order_by(MemorySnapshot.snapshot_version.desc()))
        if snapshot is None:
            raise ValueError("MEMORY_SNAPSHOT_UNAVAILABLE")
        existing = self._event(archive_id, archive.generation_epoch, "create_held")
        if existing is not None:
            return existing
        event = MemoryRuntimeLaunchEvent(
            event_id=str(uuid4()), archive_id=archive_id, snapshot_id=snapshot.snapshot_id,
            generation_epoch=archive.generation_epoch, phase="create_held",
            idempotency_key=_idempotency_key("create", archive_id, archive.generation_epoch),
            status="pending",
        )
        self._session.add(event)
        LOGGER.info(
            "已登记回忆录 Runtime 创建意图 archive_id=%s generation_epoch=%s",
            archive_id, archive.generation_epoch,
        )
        return event

    def deliver(self, event_id: str) -> bool:
        """投递一个事件；成功返回 True，失败保持 pending 以便补偿任务重试。"""
        if self._gateway is None:
            raise ValueError("MEMORY_RUNTIME_GATEWAY_UNAVAILABLE")
        event = self._session.scalar(select(MemoryRuntimeLaunchEvent).where(
            MemoryRuntimeLaunchEvent.event_id == event_id,
        ).with_for_update())
        if event is None:
            raise ValueError("MEMORY_RUNTIME_LAUNCH_EVENT_UNAVAILABLE")
        if event.status == "delivered":
            return False
        if event.phase == "create_held":
            return self._deliver_create(event)
        if event.phase == "start_held":
            return self._deliver_start(event)
        raise ValueError("MEMORY_RUNTIME_LAUNCH_PHASE_INVALID")

    def deliver_pending(self, limit: int = 20) -> int:
        """按创建顺序投递有限数量的 pending 事件，供轻量定时补偿调用。"""
        events = self._session.scalars(select(MemoryRuntimeLaunchEvent).where(
            MemoryRuntimeLaunchEvent.status == "pending",
        ).order_by(MemoryRuntimeLaunchEvent.id).limit(limit)).all()
        return sum(self.deliver(event.event_id) for event in events)

    def reconcile_pending_start(self, now: datetime) -> int:
        """超过 600 秒仍 pending_start 时，仅重放已有 start 事件及其稳定幂等键。"""
        repaired = 0
        refs = self._session.scalars(select(MemoryAgentRunRef).where(
            MemoryAgentRunRef.status == "pending_start",
        )).all()
        for ref in refs:
            updated_at = _as_utc(ref.updated_at)
            if (now - updated_at).total_seconds() <= 600:
                continue
            event = self._event(ref.archive_id, ref.generation_epoch, "start_held")
            if event is None or event.run_id != ref.run_id:
                ref.reconciliation_status = "needed"
                continue
            event.status = "pending"
            repaired += int(self.deliver(event.event_id))
        return repaired

    def reconcile_orphaned_create(self, now: datetime) -> int:
        """恢复已创建但未绑定的陈旧 held Run，或取消已失效归档对应的孤儿。

        create 事件保留原始稳定键，补偿只查询已知 ``run_id``，绝不重新生成 Run。
        600 秒窗口避免正常 create/bind 提交间隙被并发补偿任务错误介入。
        """
        if self._gateway is None:
            raise ValueError("MEMORY_RUNTIME_GATEWAY_UNAVAILABLE")
        repaired = 0
        events = self._session.scalars(select(MemoryRuntimeLaunchEvent).where(
            MemoryRuntimeLaunchEvent.phase == "create_held",
            MemoryRuntimeLaunchEvent.status == "delivered",
            MemoryRuntimeLaunchEvent.run_id.is_not(None),
        )).all()
        for event in events:
            observed_at = _as_utc(event.delivered_at or event.updated_at)
            if (now - observed_at).total_seconds() <= 600:
                continue
            existing = self._session.scalar(select(MemoryAgentRunRef).where(
                MemoryAgentRunRef.run_id == event.run_id,
            ))
            if existing is not None:
                continue
            runtime_run = self._gateway.get_run_summary(event.run_id)  # type: ignore[union-attr,arg-type]
            if runtime_run is None:
                event.last_error_code = "MEMORY_RUNTIME_ORPHAN_NOT_FOUND"
                LOGGER.warning(
                    "回忆录孤儿 held Run 不存在 event_id=%s archive_id=%s",
                    event.event_id, event.archive_id,
                )
                repaired += 1
                continue
            if runtime_run.run_id != event.run_id:
                event.last_error_code = "MEMORY_RUNTIME_ORPHAN_ID_MISMATCH"
                LOGGER.warning(
                    "回忆录孤儿 held Run 查询标识不匹配 event_id=%s archive_id=%s",
                    event.event_id, event.archive_id,
                )
                continue
            if self._archive_is_current(event):
                try:
                    MemoryAgentBindingService(self._session).bind(
                        event.archive_id, runtime_run.run_id, event.generation_epoch,
                        snapshot_id=event.snapshot_id,
                        create_idempotency_key=event.idempotency_key,
                        contract_version=runtime_run.contract_version,
                        package_digest=runtime_run.package_digest,
                        authorization_version=runtime_run.authorization_version,
                    )
                    self._enqueue_start(event, runtime_run.run_id)
                    event.last_error_code = None
                    LOGGER.info(
                        "回忆录孤儿 held Run 已恢复绑定 event_id=%s archive_id=%s run_id=%s",
                        event.event_id, event.archive_id, runtime_run.run_id,
                    )
                    repaired += 1
                    continue
                except ValueError:
                    # active Run 的并发绑定等竞争视为代次已淘汰，走安全取消而不覆盖引用。
                    pass
            try:
                self._gateway.cancel_run(  # type: ignore[union-attr]
                    runtime_run.run_id,
                    _idempotency_key("cancel", event.archive_id, event.generation_epoch),
                )
                event.last_error_code = "MEMORY_RUNTIME_ORPHAN_CANCELLED"
                LOGGER.warning(
                    "已取消回忆录孤儿 held Run event_id=%s archive_id=%s run_id=%s epoch=%s",
                    event.event_id, event.archive_id, runtime_run.run_id,
                    event.generation_epoch,
                )
                repaired += 1
            except Exception:
                # 事件保持 delivered，下一轮仍可使用同一 cancel 键再次收敛，避免回到 create 投递。
                event.last_error_code = "MEMORY_RUNTIME_ORPHAN_CANCEL_FAILED"
                LOGGER.warning(
                    "取消回忆录孤儿 held Run 失败 event_id=%s archive_id=%s",
                    event.event_id, event.archive_id,
                )
        return repaired

    def _deliver_create(self, event: MemoryRuntimeLaunchEvent) -> bool:
        """调用 create-held，成功后在本地事务绑定 RunRef 并追加 start 意图。"""
        try:
            held_run = self._gateway.create_held(  # type: ignore[union-attr]
                archive_id=event.archive_id, snapshot_id=event.snapshot_id,
                generation_epoch=event.generation_epoch,
                idempotency_key=event.idempotency_key,
            )
            # 远端已接受 create 后先保留 Run 标识；本地绑定异常不能再被误当作
            # “尚未创建”而走新的 create 语义，后续由孤儿补偿使用此标识收敛。
            event.run_id = held_run.run_id
            MemoryAgentBindingService(self._session).bind(
                event.archive_id, held_run.run_id, event.generation_epoch,
                snapshot_id=event.snapshot_id,
                create_idempotency_key=event.idempotency_key,
                contract_version=held_run.contract_version,
                package_digest=held_run.package_digest,
                authorization_version=held_run.authorization_version,
            )
            self._mark_delivered(event)
            self._enqueue_start(event, held_run.run_id)
            LOGGER.info("回忆录 Runtime held Run 已绑定 archive_id=%s", event.archive_id)
            return True
        except Exception:  # 外部网络失败可通过同一 create key 安全重试。
            if event.run_id is not None:
                # Runtime 已创建而绑定未完成时，禁止 deliver_pending 再次进入 create。
                # 保持 held 状态，等待 600 秒孤儿补偿查询/恢复或取消。
                event.status = "delivered"
                event.attempt_count += 1
                event.last_error_code = "MEMORY_RUNTIME_BINDING_PENDING"
                event.delivered_at = datetime.now(UTC)
                LOGGER.warning(
                    "回忆录 Runtime held Run 等待绑定补偿 event_id=%s archive_id=%s",
                    event.event_id, event.archive_id,
                )
                return False
            self._mark_pending(event, "MEMORY_RUNTIME_CREATE_FAILED")
            return False

    def _deliver_start(self, event: MemoryRuntimeLaunchEvent) -> bool:
        """启动已绑定 Run；callback 若已推进状态，不得被该步骤回退。"""
        if event.run_id is None:
            raise ValueError("MEMORY_RUNTIME_START_RUN_UNAVAILABLE")
        try:
            self._gateway.start_held(  # type: ignore[union-attr]
                run_id=event.run_id, idempotency_key=event.idempotency_key,
            )
            # 复用绑定服务只补齐缺失 start 键，禁止覆盖 create 时冻结的版本摘要。
            ref = MemoryAgentBindingService(self._session).bind(
                event.archive_id, event.run_id, event.generation_epoch,
                start_idempotency_key=event.idempotency_key,
            )
            if ref.status == "pending_start":
                ref.status = "pending"
                ref.row_version += 1
            self._mark_delivered(event)
            LOGGER.info("回忆录 Runtime held Run 已启动 archive_id=%s", event.archive_id)
            return True
        except Exception:
            self._mark_pending(event, "MEMORY_RUNTIME_START_FAILED")
            return False

    def _enqueue_start(
        self, create_event: MemoryRuntimeLaunchEvent, run_id: str,
    ) -> MemoryRuntimeLaunchEvent:
        """create 成功后追加唯一 start 事件，避免 create 重放重复调度。"""
        existing = self._event(
            create_event.archive_id, create_event.generation_epoch, "start_held",
        )
        if existing is not None:
            return existing
        event = MemoryRuntimeLaunchEvent(
            event_id=str(uuid4()), archive_id=create_event.archive_id,
            snapshot_id=create_event.snapshot_id,
            generation_epoch=create_event.generation_epoch, phase="start_held",
            idempotency_key=_idempotency_key(
                "start", create_event.archive_id, create_event.generation_epoch,
            ),
            run_id=run_id, status="pending",
        )
        self._session.add(event)
        return event

    def _event(
        self, archive_id: str, generation_epoch: int, phase: str,
    ) -> MemoryRuntimeLaunchEvent | None:
        """按归档代次和副作用阶段查找已存在意图。"""
        return self._session.scalar(select(MemoryRuntimeLaunchEvent).where(
            MemoryRuntimeLaunchEvent.archive_id == archive_id,
            MemoryRuntimeLaunchEvent.generation_epoch == generation_epoch,
            MemoryRuntimeLaunchEvent.phase == phase,
        ))

    def _archive_is_current(self, event: MemoryRuntimeLaunchEvent) -> bool:
        """确认归档未删除、代次未切换且没有其他 active Run 可写入。"""
        archive = self._session.scalar(select(MemoryArchive).where(
            MemoryArchive.archive_id == event.archive_id,
            MemoryArchive.deleted_at.is_(None),
        ).with_for_update())
        return bool(
            archive is not None
            and archive.generation_epoch == event.generation_epoch
            and archive.active_run_id in {None, event.run_id}
        )

    @staticmethod
    def _mark_delivered(event: MemoryRuntimeLaunchEvent) -> None:
        """只记录成功时间，不把 Runtime 响应正文写入业务库。"""
        event.status = "delivered"
        event.last_error_code = None
        event.delivered_at = datetime.now(UTC)

    @staticmethod
    def _mark_pending(event: MemoryRuntimeLaunchEvent, error_code: str) -> None:
        """失败保持可重试，并以标准码支持安全监控。"""
        event.status = "pending"
        event.attempt_count += 1
        event.last_error_code = error_code
        LOGGER.warning(
            "回忆录 Runtime 投递失败 event_id=%s phase=%s error_code=%s",
            event.event_id, event.phase, error_code,
        )


def _idempotency_key(phase: str, archive_id: str, generation_epoch: int) -> str:
    """由业务资源和代次确定稳定键，不能掺入时间戳或私密素材。"""
    return f"memory:{phase}:{archive_id}:{generation_epoch}"


def _as_utc(value: datetime) -> datetime:
    """兼容 SQLite 的 naive 时间，避免补偿窗口被数据库方言扩大。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
