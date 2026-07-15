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
        """状态迁移与安全 callback/outbox 在同一事务追加，业务侧可主动对账。"""
        run.last_event_seq += 1
        callback = CallbackEvent(
            event_id=str(uuid4()),
            run_id=run.run_id,
            event_seq=run.last_event_seq,
            status_version=run.status_version,
            event_type=event_type,
            payload_json={
                "run_id": run.run_id,
                "event_seq": run.last_event_seq,
                "status_version": run.status_version,
                "status": run.status,
                "error_code": run.error_code,
            },
            created_at=datetime.now(UTC),
        )
        event = RuntimeOutboxEvent(
            outbox_id=str(uuid4()),
            event_type="callback",
            aggregate_type="agent_run",
            aggregate_id=run.run_id,
            payload_json={"event_id": callback.event_id, "event_type": event_type},
            status="pending",
            retention_until=datetime.now(UTC) + timedelta(days=30),
        )
        self._session.add_all([callback, event])
        logging.info(
            "写入 callback outbox run_id=%s event_type=%s event_seq=%s",
            run.run_id,
            event_type,
            run.last_event_seq,
        )
        return event
