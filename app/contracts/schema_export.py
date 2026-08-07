from __future__ import annotations

import json
from typing import Any, cast

from app.contracts.api import (
    CONTRACT_VERSION,
    AgentRunQuery,
    CancelAgentRunRequest,
    CreateAgentRunRequest,
    HumanApprovalRequest,
    PurgePrivateDataRequest,
    RetryAgentRunRequest,
    StartAgentRunRequest,
)
from app.contracts.artifacts import ArtifactEnvelope
from app.contracts.tools import ToolError, ToolRequest, ToolResult


def _stable_schema(model: type[Any]) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(json.dumps(model.model_json_schema(), sort_keys=True)))


def export_contract_schemas() -> dict[str, Any]:
    """Return deterministic JSON-schema payloads suitable for fixture comparison."""
    return {
        "contract_version": CONTRACT_VERSION,
        "schemas": {
            "artifact_envelope": _stable_schema(ArtifactEnvelope),
            "create_agent_run": _stable_schema(CreateAgentRunRequest),
            "agent_run_query": _stable_schema(AgentRunQuery),
            "start_agent_run": _stable_schema(StartAgentRunRequest),
            "retry_agent_run": _stable_schema(RetryAgentRunRequest),
            "cancel_agent_run": _stable_schema(CancelAgentRunRequest),
            "human_approval": _stable_schema(HumanApprovalRequest),
            "purge_private_data": _stable_schema(PurgePrivateDataRequest),
            "tool_error": _stable_schema(ToolError),
            "tool_request": _stable_schema(ToolRequest),
            "tool_result": _stable_schema(ToolResult),
        },
    }
