from __future__ import annotations

import logging

import pytest

from app.runtime.semantic_validation import SemanticValidator
from app.runtime.state import AgentState
from app.schemas.agent_package import ToolManifest


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
