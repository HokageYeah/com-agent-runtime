from pathlib import Path

import yaml

from app.services.agent_package_service import AgentPackageService

ROOT = Path(__file__).resolve().parents[1] / "app" / "agents"


def test_package_load_and_contract():
    service = AgentPackageService(ROOT)
    package = service.load("soothe_agent", "1.0.0")
    assert (
        package.package_digest == service.load("soothe_agent", "1.0.0").package_digest
    )
    assert set(package.allowed_business_types) == {"soothe_reply", "soothe_simulator"}
    assert len(package.evals) >= 8
    for field, value in {
        "max_model_calls": 2,
        "max_tokens": 16000,
        "max_model_cost": 0.1,
        "max_steps": 8,
        "max_tool_calls": 6,
        "max_run_seconds": 90,
        "max_auto_retry_per_step": 1,
    }.items():
        assert getattr(package.policy, field) == value
    manifest = yaml.safe_load(
        (ROOT / "soothe_agent/1.0.0/prompts/manifest.yaml").read_text()
    )
    assert {p["file"] for p in manifest["prompts"]} == {
        "compose-reply.v1.md",
        "compose-simulator.v1.md",
    }
    for item in manifest["prompts"]:
        assert (ROOT / "soothe_agent/1.0.0/prompts" / item["file"]).is_file()
        assert item["owner_agent"] == "soothe_agent"


def test_reference_only_schemas_and_workflow():
    package = AgentPackageService(ROOT).load("soothe_agent", "1.0.0")
    assert package.input_schema["additionalProperties"] is False
    assert set(package.input_schema["required"]) == {"session_id", "turn_id"}
    for field in ["session_id", "turn_id"]:
        assert package.input_schema["properties"][field]["minLength"] == 1
        assert package.input_schema["properties"][field]["type"] == "string"
    assert package.output_schema["additionalProperties"] is False
    assert set(package.output_schema["properties"]) == {"submitted", "revision"}
    assert package.output_schema["properties"]["revision"]["minimum"] == 1
    assert [node.node_id for node in package.workflow_nodes] == [
        "load_turn_context",
        "compose_turn",
        "validate_turn",
        "submit_turn_result",
    ]
    assert all(node.safe_to_rerun for node in package.workflow_nodes)
    assert package.ui_trace.mode == "status_only"
    assert len(package.tools) == 3
    assert {tool.connector_id for tool in package.tools} == {"couple_diary_backend"}
