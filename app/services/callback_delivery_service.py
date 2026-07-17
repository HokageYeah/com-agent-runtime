"""callback Outbox 到预注册出站网关的最小适配器。"""

from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentRun, CallbackEvent, RuntimeOutboxEvent


class CallbackSender(Protocol):
    """出站网关的窄接口，方便 Dispatcher 注入与隔离 HTTP 细节。"""

    def send(self, target_id: str, payload: dict[str, object]) -> None: ...


class CallbackDeliveryService:
    """读取不可变事件事实并投递，outbox 状态仍由 Dispatcher 管理。"""

    def __init__(self, session: Session, sender: CallbackSender) -> None:
        self._session, self._sender = session, sender

    def send(self, outbox: RuntimeOutboxEvent) -> None:
        """使用原 event_id 与 payload 投递；缺失关联记录时明确失败等待人工处理。"""
        event_id, target_id = outbox.payload_json.get("event_id"), outbox.payload_json.get("target_id")
        if not isinstance(event_id, str) or not isinstance(target_id, str):
            raise ValueError("CALLBACK_OUTBOX_PAYLOAD_INVALID")
        callback = self._session.scalar(select(CallbackEvent).where(CallbackEvent.event_id == event_id))
        if callback is None or callback.run_id != outbox.aggregate_id:
            raise ValueError("CALLBACK_EVENT_NOT_FOUND")
        # Purge 是对外投递的最终隐私屏障：即使死信事件仍在保留期，也不得再次外发。
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == callback.run_id))
        if run is None or run.privacy_state != "active":
            logging.warning("callback 投递被私密状态拒绝 run_id=%s", callback.run_id)
            raise ValueError("CALLBACK_RUN_NOT_ACTIVE")
        self._sender.send(target_id, callback.payload_json)
        logging.info("callback Outbox 已发送 outbox_id=%s event_id=%s", outbox.outbox_id, event_id)
