from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.models import RuntimeAuditRecord
from app.schemas.audit import RuntimeAuditEvent


class AuditService:
    """追加写审计服务；可在当前事务内同时写数据库与外部审计 sink。"""

    def __init__(
        self,
        append: Callable[[RuntimeAuditEvent], None] | None = None,
        session: Session | None = None,
    ) -> None:
        self._append = append or (lambda _: None)
        self._session = session

    def append(self, event: RuntimeAuditEvent) -> None:
        # 审计日志只输出可公开的定位字段；metadata_summary 已由调用方脱敏。
        logging.info(
            "写入 Runtime 审计事件 audit_id=%s action=%s resource_type=%s "
            "resource_id=%s outcome=%s",
            event.audit_id,
            event.action,
            event.resource_type,
            event.resource_id,
            event.outcome,
        )
        if self._session is not None:
            # 不 commit：让审计与同一业务状态迁移同事务提交或回滚。
            self._session.add(
                RuntimeAuditRecord(
                    audit_id=event.audit_id,
                    actor_type=event.actor_type,
                    actor_id=event.actor_id,
                    action=event.action,
                    resource_type=event.resource_type,
                    resource_id=event.resource_id,
                    reason_code=event.reason_code,
                    outcome=event.outcome,
                    trace_id=event.trace_id,
                    metadata_summary=event.metadata_summary,
                    occurred_at=event.occurred_at,
                )
            )
        self._append(event)
