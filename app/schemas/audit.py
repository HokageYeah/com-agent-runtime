from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


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

    @field_validator("metadata_summary")
    @classmethod
    def validate_safe_metadata_summary(cls, value: dict[str, str]) -> dict[str, str]:
        """审计仅保留固定定位字段，拒绝 prompt、正文与原始载荷等自由键。"""
        safe_fields = {
            "content_digest_prefix",
            "decision",
            "dispatch_state",
            "manual_retry_count",
            "privacy_version",
            "run_id",
            "status",
        }
        if any(field not in safe_fields for field in value):
            raise ValueError("AUDIT_METADATA_FIELD_FORBIDDEN")
        return value
