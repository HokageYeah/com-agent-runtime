from __future__ import annotations

import logging

import pytest

from app.runtime.interfaces import LeaseContext
from app.runtime.semantic_validation import SemanticValidator
from app.runtime.state import AgentState
from app.runtime.tool_gateway import ToolGateway
from app.schemas.agent_package import ToolManifest


class _InvalidLease:
    """模拟已失效 lease，确保状态写入前被拒绝。"""

    def can_write(self, run_id: str, context: LeaseContext) -> bool:
        """统一返回不可写，模拟 fencing/privacy/authorization 失效。"""
        return False


def _lease_context() -> LeaseContext:
    """构造无须访问数据库的测试租约上下文。"""
    from datetime import UTC, datetime, timedelta

    return LeaseContext(
        execution_attempt=1,
        lease_owner="worker-a",
        fencing_token=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        privacy_version=1,
        authorization_version=1,
    )


def test_tool_output_can_only_write_declared_runtime_memory_field() -> None:
    """工具响应只能写入 AgentState 已声明的业务字段。"""
    state = AgentState()

    state.apply_tool_output("snapshot", {"source_refs": ["diary:d-1"]})

    assert state.snapshot == {"source_refs": ["diary:d-1"]}
    with pytest.raises(ValueError, match="TOOL_OUTPUT_TARGET_FORBIDDEN"):
        state.apply_tool_output("authorization_version", {"value": 2})
    with pytest.raises(ValueError, match="受控的 AgentState 业务字段"):
        ToolManifest(
            name="unsafe.output", version="1", connector_id="memory", method="POST",
            relative_path="/internal/tool", input_from="input",
            output_to="authorization_version",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"authorization_version": 2},
        {"result": {"connector": "evil"}},
        {"items": [{"generation_epoch": 99}]},
        {"access_token": "private-token"},
    ],
)
def test_tool_output_rejects_nested_control_or_credential_fields(payload: dict[str, object]) -> None:
    """工具不得借业务 payload 覆盖控制面或传递认证材料。"""
    with pytest.raises(ValueError, match="TOOL_OUTPUT_SENSITIVE_FIELD"):
        AgentState().apply_tool_output("snapshot", payload)


def test_tool_output_security_log_does_not_contain_private_payload(caplog: pytest.LogCaptureFixture) -> None:
    """拒绝日志只记录目标和错误码，不能泄漏工具响应正文。"""
    secret = "private-token-should-never-log"
    with caplog.at_level(logging.WARNING), pytest.raises(ValueError):
        AgentState().apply_tool_output("snapshot", {"access_token": secret})

    assert secret not in caplog.text
    assert "TOOL_OUTPUT_SENSITIVE_FIELD" in caplog.text


def test_semantic_validator_rejects_nested_runtime_control_field() -> None:
    """结构化输出同样不能通过嵌套对象携带 Runtime 控制字段。"""
    result = SemanticValidator().validate(
        {"source_refs": [], "document": {"connector_id": "untrusted"}},
        trusted_refs=set(),
    )

    assert result.valid is False
    assert result.error_codes == ("FORBIDDEN_CONTROL_FIELD",)


def test_gateway_apply_result_rejects_schema_mismatch_without_state_mutation() -> None:
    """缺少 manifest 要求字段的工具结果不能进入 AgentState。"""
    manifest = ToolManifest(
        name="memory.get_snapshot", version="1.0.0", connector_id="couple_diary_backend",
        method="POST", relative_path="/api/v1/internal/agent-tools/memory.get_snapshot",
        input_from="input", output_to="snapshot",
        output_schema={
            "type": "object", "required": ["snapshot_digest"],
            "properties": {"snapshot_digest": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    state = AgentState()

    with pytest.raises(ValueError, match="TOOL_OUTPUT_SCHEMA_INVALID"):
        ToolGateway.apply_result(
            manifest, {"other": "value"}, state, "run-1", _lease_context(), _AlwaysWritableLease()
        )

    assert state.snapshot is None


def test_gateway_apply_result_rejects_sensitive_field_without_state_mutation() -> None:
    """即使 schema 允许，控制面敏感字段也不能写入 AgentState。"""
    manifest = ToolManifest(
        name="memory.get_snapshot", version="1.0.0", connector_id="couple_diary_backend",
        method="POST", relative_path="/api/v1/internal/agent-tools/memory.get_snapshot",
        input_from="input", output_to="snapshot",
        output_schema={"type": "object", "properties": {"access_token": {"type": "string"}}},
    )
    state = AgentState()

    with pytest.raises(ValueError, match="TOOL_OUTPUT_SENSITIVE_FIELD"):
        ToolGateway.apply_result(
            manifest, {"access_token": "secret"}, state, "run-1", _lease_context(), _AlwaysWritableLease()
        )

    assert state.snapshot is None


class _AlwaysWritableLease:
    """模拟有效 lease，供内容安全边界测试使用。"""

    def can_write(self, run_id: str, context: LeaseContext) -> bool:
        """允许进入 schema 和状态安全检查。"""
        return True


def test_gateway_apply_result_rejects_invalid_lease_without_state_mutation() -> None:
    """fencing/privacy/authorization 失效时绝不改动状态。"""
    manifest = ToolManifest(
        name="memory.get_snapshot", version="1.0.0", connector_id="couple_diary_backend",
        method="POST", relative_path="/api/v1/internal/agent-tools/memory.get_snapshot",
        input_from="input", output_to="snapshot",
        output_schema={"type": "object", "properties": {"snapshot_digest": {"type": "string"}}},
    )
    state = AgentState()

    with pytest.raises(ValueError, match="TOOL_RESULT_LEASE_INVALID"):
        ToolGateway.apply_result(
            manifest, {"snapshot_digest": "safe"}, state, "run-1", _lease_context(), _InvalidLease()
        )

    assert state.snapshot is None
