from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.agent_package import AgentPackage, ToolManifest
from app.services.agent_package_service import AgentPackageService


@pytest.mark.parametrize("agent_id", ["memoir_agent", "unregistered_agent"])
def test_package_rejects_another_registered_agents_output_target(
    monkeypatch: pytest.MonkeyPatch, agent_id: str,
) -> None:
    """单独 manifest 可识别代码登记的字段，包仍须按身份拒绝跨 Agent 写入。"""
    from app.agents.output_targets import AGENT_TOOL_OUTPUT_FIELDS

    monkeypatch.setitem(
        AGENT_TOOL_OUTPUT_FIELDS, "fixture_agent", frozenset({"fixture_result"}),
    )
    package = AgentPackageService(Path("app/agents")).load("memoir_agent", "1.0.0")
    manifest_data = package.tools[0].model_dump()
    manifest_data["output_to"] = "fixture_result"
    manifest = ToolManifest.model_validate(manifest_data)
    package_data = {**package.model_dump(), "agent_id": agent_id, "tools": [manifest]}

    with pytest.raises(ValidationError, match="受控的 AgentState 业务字段"):
        AgentPackage.model_validate(package_data)


def test_registered_package_uses_its_own_output_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第二档案只接收自己登记的字段，不继承 Memoir 的输出权限。"""
    from app.agents.output_targets import AGENT_TOOL_OUTPUT_FIELDS

    monkeypatch.setitem(
        AGENT_TOOL_OUTPUT_FIELDS, "fixture_agent", frozenset({"fixture_result"}),
    )
    package = AgentPackageService(Path("app/agents")).load("memoir_agent", "1.0.0")
    package_data = {**package.model_dump(), "agent_id": "fixture_agent"}
    with pytest.raises(ValidationError, match="受控的 AgentState 业务字段"):
        AgentPackage.model_validate(package_data)

    package_data["tools"] = [
        {**package.tools[0].model_dump(), "output_to": "fixture_result"},
    ]
    assert AgentPackage.model_validate(package_data).tools[0].output_to == "fixture_result"


def test_unregistered_package_keeps_original_output_allowlist() -> None:
    """未登记身份保留旧白名单，控制字段仍在 manifest 构造时被拒绝。"""
    package = AgentPackageService(Path("app/agents")).load("memoir_agent", "1.0.0")
    package_data = {**package.model_dump(), "agent_id": "unregistered_agent"}
    assert AgentPackage.model_validate(package_data).tools[0].output_to == "snapshot"

    with pytest.raises(ValidationError, match="output_to 必须是受控的 AgentState 业务字段"):
        ToolManifest.model_validate({**package.tools[0].model_dump(), "output_to": "authorization_version"})


def test_memoir_1_0_8_keeps_t0_digest() -> None:
    """补齐既有历史 digest 测试之外的最新冻结版本。"""
    package = AgentPackageService(Path("app/agents")).load("memoir_agent", "1.0.8")
    assert package.package_digest == "sha256:da05c6c7f83f209f76f0ecbf5fc555fbeb7d90d4cd119f7d3be3b938b645848d"
