from __future__ import annotations

import json
from pathlib import Path

from app.contracts.api import CONTRACT_VERSION, AgentRunStatus, CreateAgentRunRequest
from app.contracts.errors import RuntimeErrorCode
from app.contracts.events import CallbackEventType, RuntimeEventType, callback_event_for
from app.contracts.schema_export import export_contract_schemas
from app.contracts.tools import ToolManifest


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
    assert tool.model_dump(mode="json")["relative_path"].startswith("/")
    assert AgentRunStatus.PENDING.value == "pending"
