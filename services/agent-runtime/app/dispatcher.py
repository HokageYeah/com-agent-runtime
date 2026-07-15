"""持久 outbox dispatcher：通知可重复/丢失，数据库 Run 状态才是权威。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import RuntimeOutboxEvent


class Dispatcher:
    """按事件类型显式注册处理器，未启用事件绝不被误认领。"""

    def __init__(
        self,
        session: Session,
        *,
        owner: str = "runtime-dispatcher",
        notify_run: Callable[[str], None] | None = None,
        lease_seconds: int = 30,
    ) -> None:
        self._session = session
        self._owner = owner
        self._lease_seconds = lease_seconds
        self._notify_run = notify_run or self._log_run_notification
        self._handlers: dict[str, Callable[[RuntimeOutboxEvent], None]] = {
            "run_dispatch": self._deliver_run_dispatch
        }

    @property
    def enabled_event_types(self) -> set[str]:
        return set(self._handlers)

    def dispatch_pending(self) -> int:
        """逐条条件 lease；callback 等未注册事件保持 pending 且 attempt 不变。"""
        now = datetime.now(UTC)
        candidate_ids = self._session.scalars(
            select(RuntimeOutboxEvent.outbox_id).where(
                RuntimeOutboxEvent.status == "pending",
                RuntimeOutboxEvent.event_type.in_(tuple(self._handlers)),
                (RuntimeOutboxEvent.next_attempt_at.is_(None))
                | (RuntimeOutboxEvent.next_attempt_at <= now),
            )
        ).all()
        delivered = 0
        for outbox_id in candidate_ids:
            event = self._claim(outbox_id, now)
            if event is None:
                continue
            handler = self._handlers.get(event.event_type)
            if handler is None:  # 防御性分支，确保以后扩展不会错误累计 attempt。
                continue
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - dispatcher 必须把失败安全留在 outbox。
                event.status = "pending"
                event.lease_owner = None
                event.lease_expires_at = None
                event.attempt_count += 1
                event.next_attempt_at = now + timedelta(seconds=min(60, 2**event.attempt_count))
                event.last_error_code = "DISPATCH_DELIVERY_FAILED"
                logging.exception("outbox 投递失败 outbox_id=%s", event.outbox_id)
            else:
                event.status = "delivered"
                event.lease_owner = None
                event.lease_expires_at = None
                event.delivered_at = now
                delivered += 1
        self._session.commit()
        return delivered

    def _claim(self, outbox_id: str, now: datetime) -> RuntimeOutboxEvent | None:
        """使用条件更新取得 outbox lease，多个 dispatcher 只能有一个成功。"""
        result = self._session.execute(
            update(RuntimeOutboxEvent)
            .where(
                RuntimeOutboxEvent.outbox_id == outbox_id,
                RuntimeOutboxEvent.status == "pending",
                RuntimeOutboxEvent.event_type.in_(tuple(self._handlers)),
                (RuntimeOutboxEvent.lease_expires_at.is_(None))
                | (RuntimeOutboxEvent.lease_expires_at < now),
            )
            .values(
                status="delivering",
                lease_owner=self._owner,
                lease_expires_at=now + timedelta(seconds=self._lease_seconds),
            )
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            return None
        return self._session.scalar(
            select(RuntimeOutboxEvent).where(RuntimeOutboxEvent.outbox_id == outbox_id)
        )

    def _deliver_run_dispatch(self, event: RuntimeOutboxEvent) -> None:
        run_id = event.payload_json.get("run_id", event.aggregate_id)
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_dispatch 缺少 run_id")
        logging.info(
            "dispatcher 投递 run_dispatch outbox_id=%s run_id=%s",
            event.outbox_id,
            run_id,
        )
        self._notify_run(run_id)

    @staticmethod
    def _log_run_notification(run_id: str) -> None:
        """没有 Redis/Arq 时仅记录唤醒意图；Worker 可通过数据库补偿扫描恢复。"""
        logging.info("run_dispatch 通知已记录 run_id=%s", run_id)
