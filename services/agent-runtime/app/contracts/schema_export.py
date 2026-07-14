from __future__ import annotations

import json
from typing import Any

from app.contracts.api import CONTRACT_VERSION, CreateAgentRunRequest
from app.contracts.artifacts import ArtifactEnvelope
from app.contracts.tools import ToolError, ToolRequest, ToolResult


def _stable_schema(model: type[Any]) -> dict[str, Any]:
    return json.loads(json.dumps(model.model_json_schema(), sort_keys=True))


def export_contract_schemas() -> dict[str, Any]:
    """Return deterministic JSON-schema payloads suitable for fixture comparison."""
    return {
        "contract_version": CONTRACT_VERSION,
        "schemas": {
            "artifact_envelope": _stable_schema(ArtifactEnvelope),
            "create_agent_run": _stable_schema(CreateAgentRunRequest),
            "tool_error": _stable_schema(ToolError),
            "tool_request": _stable_schema(ToolRequest),
            "tool_result": _stable_schema(ToolResult),
        },
    }
