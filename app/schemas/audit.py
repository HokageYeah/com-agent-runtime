from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RuntimeAuditEvent(BaseModel):
    """追加写审计事件；metadata_summary 禁止存 prompt、正文、密钥或原始工具 payload。"""

    model_config = ConfigDict(extra="forbid")

    audit_id: str
    actor_type: str
    actor_id: str
    action: str
    resource_type: str
    resource_id: str
    reason_code: str | None = None
    outcome: str
    occurred_at: datetime
    trace_id: str | None = None
    metadata_summary: dict[str, str] = {}
