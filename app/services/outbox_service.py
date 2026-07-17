from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import AgentRun, CallbackEvent, RuntimeOutboxEvent


class OutboxService:
    """将状态变更和异步投递意图写入同一数据库事务。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append_run_dispatch(self, run_id: str, reason: str) -> RuntimeOutboxEvent:
        event = RuntimeOutboxEvent(
            outbox_id=str(uuid4()),
            event_type="run_dispatch",
            aggregate_type="agent_run",
            aggregate_id=run_id,
            payload_json={"run_id": run_id, "reason": reason},
            status="pending",
            retention_until=datetime.now(UTC) + timedelta(days=30),
        )
        logging.info("写入 run_dispatch outbox run_id=%s reason=%s", run_id, reason)
        self._session.add(event)
        return event

    def append_callback(self, run: AgentRun, event_type: str) -> RuntimeOutboxEvent:
        """兼容终态调用方，将 Runtime 状态映射为冻结 callback 事件。"""
        return self.append_callback_event(run, self._terminal_callback_event(event_type))

    def append_callback_event(
        self,
        run: AgentRun,
        callback_event: str,
        public_trace: list[dict[str, str]] | None = None,
    ) -> RuntimeOutboxEvent:
        """同事务写入安全 callback 事实与 outbox，不保存私密运行数据。"""
        if callback_event not in {
            "run_started", "step_changed", "waiting_human", "partial_succeeded",
            "run_succeeded", "run_failed", "run_cancelled",
        }:
            raise ValueError("CALLBACK_EVENT_TYPE_INVALID")
        run.last_event_seq += 1
        callback = CallbackEvent(
            event_id=str(uuid4()),
            run_id=run.run_id,
            event_seq=run.last_event_seq,
            status_version=run.status_version,
            event_type=callback_event,
            payload_json={
                "event": callback_event,
                "event_id": None,  # 创建后立即回填，保证 body 与 event 主键一致。
                "run_id": run.run_id,
                "event_seq": run.last_event_seq,
                "status_version": run.status_version,
                "agent_id": run.agent_id,
                "business_id": run.business_id,
                "status": run.status,
                "error": {"code": run.error_code} if run.error_code else None,
                "public_trace": public_trace or [],
            },
            created_at=datetime.now(UTC),
        )
        callback.payload_json["event_id"] = callback.event_id
        event = RuntimeOutboxEvent(
            outbox_id=str(uuid4()),
            event_type="callback",
            aggregate_type="agent_run",
            aggregate_id=run.run_id,
            payload_json={
                "event_id": callback.event_id,
                "event_type": callback_event,
                "target_id": run.callback_target_id,
            },
            status="pending",
            retention_until=datetime.now(UTC) + timedelta(days=30),
        )
        self._session.add_all([callback, event])
        logging.info(
            "写入 callback outbox run_id=%s event_type=%s event_seq=%s",
            run.run_id,
            callback_event,
            run.last_event_seq,
        )
        return event

    @staticmethod
    def _terminal_callback_event(status: str) -> str:
        """将 Runtime 终态映射为冻结的业务 callback 事件名。"""
        event_by_status = {
            "succeeded": "run_succeeded",
            "partial": "partial_succeeded",
            "failed": "run_failed",
            "cancelled": "run_cancelled",
        }
        event = event_by_status.get(status)
        if event is None:
            raise ValueError("CALLBACK_EVENT_TYPE_INVALID")
        return event
