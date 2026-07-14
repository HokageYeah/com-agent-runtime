from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ToolContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolManifest(ToolContractModel):
    """受注册表约束的业务工具描述；Runtime 不接受调用方自带完整 URL。"""

    name: str
    version: str
    connector_id: str | None = None
    method: str | None = None
    relative_path: str | None = None
    input_from: str | None = None
    output_to: str | None = None
    side_effect: bool = False
    mcp_server_id: str | None = None
    mcp_tool_name: str | None = None
    mcp_resource_uri: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)

    @field_validator("relative_path")
    @classmethod
    def relative_path_must_not_be_full_url(cls, value: str | None) -> str | None:
        # 阻断完整 URL，后续 ToolGateway 只能从可信 connector 拼接 host。
        if value is not None and (not value.startswith("/") or "://" in value):
            raise ValueError("relative_path must be a relative absolute path")
        return value


class ToolRequest(ToolContractModel):
    input: dict[str, Any]
    context: dict[str, str]


class ToolResult(ToolContractModel):
    output: dict[str, Any]
    schema_version: str = "1.0.0"


class ToolError(ToolContractModel):
    error_code: str
    error_type: str
    retryable: bool
    safe_message: str
