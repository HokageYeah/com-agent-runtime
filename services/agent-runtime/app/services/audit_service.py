from __future__ import annotations

import logging
from collections.abc import Callable

from app.schemas.audit import RuntimeAuditEvent


class AuditService:
    """Append-only audit facade; a database sink is introduced with Task 2."""

    def __init__(
        self, append: Callable[[RuntimeAuditEvent], None] | None = None
    ) -> None:
        self._append = append or (lambda _: None)

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
        self._append(event)
