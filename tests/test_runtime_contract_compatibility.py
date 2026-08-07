from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.contracts.api import CONTRACT_VERSION, AgentRunStatus, CreateAgentRunRequest
from app.contracts.errors import RuntimeErrorCode
from app.contracts.events import CallbackEventType, RuntimeEventType, callback_event_for
from app.contracts.schema_export import export_contract_schemas
from app.contracts.tools import ToolManifest
from app.services.agent_run_service import AgentRunService, AgentRunServiceError


def test_contract_schemas_are_stable_and_versioned() -> None:
    first = export_contract_schemas()
    second = export_contract_schemas()

    assert first == second
    assert first["contract_version"] == CONTRACT_VERSION == "1.0.0"
    assert "create_agent_run" in first["schemas"]
    assert "artifact_envelope" in first["schemas"]


def test_contract_fixture_freezes_version_and_error_taxonomy() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "runtime-contract-v1.0.0.json"
    fixture = json.loads(fixture_path.read_text())

    assert fixture["contract_version"] == CONTRACT_VERSION
    assert fixture["runtime_events"] == [event.value for event in RuntimeEventType]
    assert fixture["error_codes"] == [error.value for error in RuntimeErrorCode]


def test_runtime_events_have_safe_callback_mapping() -> None:
    assert (
        callback_event_for(RuntimeEventType.RUN_STARTED)
        is CallbackEventType.RUN_STARTED
    )
    assert (
        callback_event_for(RuntimeEventType.STEP_STARTED)
        is CallbackEventType.STEP_CHANGED
    )
    assert (
        callback_event_for(RuntimeEventType.HUMAN_REVIEW_REQUESTED)
        is CallbackEventType.WAITING_HUMAN
    )


def test_create_request_and_tool_manifest_keep_contract_extension_points() -> None:
    request = CreateAgentRunRequest(
        agent_id="memoir_agent",
        agent_version="1.0.0",
        business_type="couple_memory",
        business_id="archive_123",
        input={"snapshot_id": "snapshot_456"},
        callback_target_id="couple_diary_memory_callback",
        business_connector_id="couple_diary_backend",
    )
    tool = ToolManifest(
        name="memory.get_snapshot",
        version="1.0.0",
        connector_id="couple_diary_backend",
        method="POST",
        relative_path="/api/v1/internal/agent-tools/memory.get_snapshot",
        input_from="input",
        output_to="snapshot",
    )

    assert request.start_mode == "held"
    assert request.contract_version == CONTRACT_VERSION
    assert tool.mcp_server_id is None
    assert tool.enabled is True
    assert tool.model_dump(mode="json")["relative_path"].startswith("/")
    assert AgentRunStatus.PENDING.value == "pending"


def test_public_create_input_remains_generic_dict_without_business_specific_fields() -> None:
    """公共 CreateAgentRunRequest.input 必须保持通用 dict。

    业务字段(archive_id/snapshot_id/generation_epoch 等)只能由 AgentPackage 的
    input_schema 在创建时收紧；contract 层永远不能硬编码 memoir 字段，否则一旦
    后续接入新业务，contract 就会被破坏。本测试通过构造任意 dict 验证 contract
    层不区分业务负载。
    """
    # memoir 字段
    memoir_payload = {
        "archive_id": "archive-x",
        "snapshot_id": "snap-x",
        "generation_epoch": 3,
        "locale": "zh-CN",
    }
    # 假设的未来其它业务字段
    other_payload = {"diary_id": "d1", "tags": ["t1", "t2"], "weight": 0.5}

    memoir_request = CreateAgentRunRequest(
        agent_id="memoir_agent",
        agent_version="1.0.0",
        business_type="couple_memory",
        business_id="b1",
        input=memoir_payload,
        callback_target_id="cb",
        business_connector_id="couple_diary_backend",
    )
    other_request = CreateAgentRunRequest(
        agent_id="memoir_agent",
        agent_version="1.0.0",
        business_type="couple_memory",
        business_id="b2",
        input=other_payload,
        callback_target_id="cb",
        business_connector_id="couple_diary_backend",
    )

    # contract 层只把 input 当 dict 接收，不会挑字段。
    assert memoir_request.input == memoir_payload
    assert other_request.input == other_payload
    # 不带任何业务字段也能通过 contract 校验，证明 input 字段不耦合 memoir。
    assert CreateAgentRunRequest(
        agent_id="memoir_agent",
        agent_version="1.0.0",
        business_type="couple_memory",
        business_id="b3",
        input={},
        callback_target_id="cb",
        business_connector_id="couple_diary_backend",
    ).input == {}


def _load_memoir_input_schema() -> dict[str, object]:
    """读取 memoir_agent@1.0.0 注册时随包冻结的 input.schema.json。"""
    schema_path = (
        Path(__file__).parents[1]
        / "app"
        / "agents"
        / "memoir_agent"
        / "1.0.0"
        / "input.schema.json"
    )
    return json.loads(schema_path.read_text())


def test_memoir_package_input_schema_only_accepts_archive_snapshot_epoch_and_optional_locale() -> None:
    """memoir_agent@1.0.0 的 input.schema 必须与跨项目冻结契约严格对齐。

    包级 schema 才是收紧业务字段的边界，contract 层只验证 schema 自身。
    额外字段必须拒绝；generation_epoch 必须 >=1；locale 可省略。
    """
    schema = _load_memoir_input_schema()

    # 1. schema 自身冻结字段集合
    assert schema["type"] == "object"
    assert schema["required"] == ["archive_id", "snapshot_id", "generation_epoch"]
    assert schema.get("additionalProperties") is False
    properties = schema["properties"]
    assert set(properties) == {"archive_id", "snapshot_id", "generation_epoch", "locale"}
    assert properties["generation_epoch"]["minimum"] == 1

    # 2. 通过 service 层的 schema 校验模拟注册后调用
    validate = AgentRunService._validate_input_schema

    # 合法:只给必填字段
    validate(
        schema,
        {"archive_id": "a1", "snapshot_id": "s1", "generation_epoch": 1},
    )
    # 合法:locale 可选
    validate(
        schema,
        {
            "archive_id": "a1",
            "snapshot_id": "s1",
            "generation_epoch": 2,
            "locale": "zh-CN",
        },
    )

    # 缺失 archive_id
    with pytest.raises(AgentRunServiceError, match="input schema 校验失败"):
        validate(schema, {"snapshot_id": "s1", "generation_epoch": 1})
    # 缺失 generation_epoch
    with pytest.raises(AgentRunServiceError, match="input schema 校验失败"):
        validate(schema, {"archive_id": "a1", "snapshot_id": "s1"})
    # 额外字段必须拒绝
    with pytest.raises(AgentRunServiceError, match="input schema 校验失败"):
        validate(
            schema,
            {
                "archive_id": "a1",
                "snapshot_id": "s1",
                "generation_epoch": 1,
                "extra": "forbidden",
            },
        )
    # epoch 必须 >=1
    with pytest.raises(AgentRunServiceError, match="input schema 校验失败"):
        validate(
            schema,
            {"archive_id": "a1", "snapshot_id": "s1", "generation_epoch": 0},
        )
    # epoch 类型必须是整数(拒绝布尔)
    with pytest.raises(AgentRunServiceError, match="input schema 校验失败"):
        validate(
            schema,
            {"archive_id": "a1", "snapshot_id": "s1", "generation_epoch": True},
        )
