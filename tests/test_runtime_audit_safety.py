"""RuntimeAuditEvent 只允许固定安全摘要，避免调试路径泄露私密内容。"""

from datetime import UTC, datetime

import pytest

from app.schemas.audit import RuntimeAuditEvent


def test_runtime_audit_event_rejects_sensitive_metadata_field() -> None:
    """审计边界拒绝 prompt 等原始内容，调用方必须改用受控错误码或计数。"""
    with pytest.raises(ValueError, match="AUDIT_METADATA_FIELD_FORBIDDEN"):
        RuntimeAuditEvent(
            audit_id="audit-1", actor_type="worker", actor_id="worker-1",
            action="debug", resource_type="agent_run", resource_id="run-1",
            outcome="rejected", occurred_at=datetime.now(UTC),
            metadata_summary={"prompt": "private-marker"},
        )
