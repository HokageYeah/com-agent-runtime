"""callback 的事实读取、授权复核和死信原身份重放。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentRun, CallbackEvent, RuntimeOutboxEvent
from app.schemas.audit import (
    AUTHORIZATION_REVOKED,
    CALLBACK_TARGET_MISSING,
    RUNTIME_REJECTION_REASON_CODES,
    RuntimeAuditEvent,
)
from app.services.audit_service import AuditService


class CallbackSender(Protocol):
    """隔离 HTTP 细节的最小出站边界。"""

    def send(self, target_id: str, payload: dict[str, object]) -> None: ...


class CallbackDeliveryService:
    """只读取不可变 CallbackEvent；outbox 状态仍由 Dispatcher 管理。"""

    def __init__(
        self,
        session: Session,
        sender: CallbackSender,
        *,
        authorize_target: Callable[[AgentRun], str | bool | None] | None = None,
    ) -> None:
        self._session, self._sender = session, sender
        self._authorize_target = authorize_target or (lambda run: None)

    def send(self, outbox: RuntimeOutboxEvent) -> None:
        """兼容既有 Dispatcher handler 名称。"""
        self.deliver(outbox)

    def deliver(self, outbox: RuntimeOutboxEvent) -> None:
        """发送前从权威 Run 复核 callback target，绝不重建事件。"""
        event_id, target_id = self._outbox_identity(outbox)
        callback = self._session.scalar(
            select(CallbackEvent).where(CallbackEvent.event_id == event_id)
        )
        if callback is None or callback.run_id != outbox.aggregate_id:
            raise ValueError("CALLBACK_EVENT_UNAVAILABLE")
        # payload 是不可变事件的安全快照；关联键必须一致，避免 outbox 被串接到另一事件。
        if not self._matches_callback(callback):
            raise ValueError("CALLBACK_EVENT_INTEGRITY_INVALID")
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == callback.run_id))
        if run is None or run.privacy_state != "active":
            logging.warning("callback 投递被私密状态拒绝 run_id=%s", callback.run_id)
            raise ValueError("CALLBACK_RUN_NOT_ACTIVE")
        authorization_rejection = self._authorize_target(run)
        if target_id != run.callback_target_id:
            reason_code = CALLBACK_TARGET_MISSING
        elif authorization_rejection is None or authorization_rejection is True:
            reason_code = None
        elif authorization_rejection in RUNTIME_REJECTION_REASON_CODES:
            reason_code = str(authorization_rejection)
        else:
            reason_code = AUTHORIZATION_REVOKED
        if reason_code is not None:
            # 授权撤销不是普通网络错误：Dispatcher 将其留在 outbox，待管理员恢复授权后按原身份投递。
            AuditService(session=self._session).append(
                RuntimeAuditEvent(
                    audit_id=str(uuid4()), actor_type="system", actor_id="callback_dispatcher",
                    action="callback_authorization_rejected", resource_type="agent_run",
                    resource_id=run.run_id, reason_code=reason_code, outcome="rejected",
                    occurred_at=datetime.now(UTC), trace_id=run.trace_id,
                    metadata_summary={"run_id": run.run_id, "status": run.status},
                )
            )
            raise ValueError("CALLBACK_TARGET_REVOKED")
        self._sender.send(target_id, callback.payload_json)
        logging.info("callback Outbox 已发送 outbox_id=%s event_id=%s", outbox.outbox_id, event_id)

    def replay_dead_letter(self, outbox_id: str) -> bool:
        """仅把既有 callback outbox 放回 pending；不生成新 CallbackEvent。"""
        outbox = self._session.scalar(
            select(RuntimeOutboxEvent).where(RuntimeOutboxEvent.outbox_id == outbox_id)
        )
        if outbox is None or outbox.event_type != "callback" or outbox.status != "dead_letter":
            return False
        self._outbox_identity(outbox)
        outbox.status = "pending"
        outbox.attempt_count = 0
        outbox.next_attempt_at = None
        outbox.lease_owner = None
        outbox.lease_expires_at = None
        outbox.last_error_code = None
        return True

    @staticmethod
    def _outbox_identity(outbox: RuntimeOutboxEvent) -> tuple[str, str]:
        event_id, target_id = outbox.payload_json.get("event_id"), outbox.payload_json.get("target_id")
        if not isinstance(event_id, str) or not isinstance(target_id, str):
            raise ValueError("CALLBACK_OUTBOX_PAYLOAD_INVALID")
        return event_id, target_id

    @staticmethod
    def _matches_callback(callback: CallbackEvent) -> bool:
        payload = callback.payload_json
        return (
            payload.get("event_id") == callback.event_id
            and payload.get("run_id") == callback.run_id
            and payload.get("event_seq") == callback.event_seq
            and payload.get("status_version") == callback.status_version
            and payload.get("event") == callback.event_type
        )
