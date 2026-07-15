from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PackageSchema(BaseModel):
    """文件包的严格 schema 基类，拒绝未知配置以避免隐式行为漂移。"""

    model_config = ConfigDict(extra="forbid")


class WorkflowNodeDefinition(PackageSchema):
    node_id: str = Field(min_length=1)
    node_type: Literal["deterministic", "tool", "model", "guardrail", "fallback"]
    next_nodes: list[str] = Field(default_factory=list)
    prompt_ref: str | None = None
    can_wait_for_human: bool = False


class ToolManifest(PackageSchema):
    """受信任工具声明；不允许 Package 将请求重定向到任意 URL。"""

    name: str
    version: str
    connector_id: str | None = None
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] | None = None
    relative_path: str | None = None
    input_from: str | None = None
    output_to: str | None = None
    side_effect: bool = False
    mcp_server_id: str | None = None
    mcp_tool_name: str | None = None
    mcp_resource_uri: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_transport(self) -> ToolManifest:
        is_http = self.connector_id is not None
        if is_http and not all(
            [self.method, self.relative_path, self.input_from, self.output_to]
        ):
            raise ValueError(
                "HTTP 工具必须声明 connector、method、relative_path、input_from、output_to"
            )
        if self.relative_path and (
            not self.relative_path.startswith("/") or "://" in self.relative_path
        ):
            raise ValueError("relative_path 只能是以 / 开头的相对路径")
        trusted_prefixes = ("identity.", "authorization.", "connector.", "generation.")
        if self.output_to and self.output_to.startswith(trusted_prefixes):
            raise ValueError("output_to 不得覆盖 trusted 控制字段")
        return self


class CallbackConfig(PackageSchema):
    enabled_events: list[str] = Field(default_factory=list)

    @property
    def waiting_human_enabled(self) -> bool:
        return "waiting_human" in self.enabled_events


class UiTraceConfig(PackageSchema):
    mode: Literal["none", "status_only", "public_summary"] = "status_only"
    step_labels: dict[str, str] = Field(default_factory=dict)


class PackagePolicy(PackageSchema):
    waiting_human_timeout_action: Literal["fallback", "failed", "cancelled"] = "failed"
    waiting_human_fallback_node: str | None = None

    @model_validator(mode="after")
    def fallback_requires_node(self) -> PackagePolicy:
        if (
            self.waiting_human_timeout_action == "fallback"
            and not self.waiting_human_fallback_node
        ):
            raise ValueError("waiting_human fallback 必须指定恢复节点")
        return self


class AgentPackage(PackageSchema):
    """加载后的不可变 Package 视图，digest 覆盖所有受管文件内容。"""

    agent_id: str
    version: str
    contract_version: str
    status: Literal["active", "deprecated", "revoked"]
    allowed_business_types: list[str]
    policy: PackagePolicy
    workflow_nodes: list[WorkflowNodeDefinition]
    tools: list[ToolManifest]
    callbacks: CallbackConfig
    ui_trace: UiTraceConfig
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    prompts: list[str]
    guardrails: dict[str, Any]
    evals: list[dict[str, Any]]
    package_digest: str

    @field_validator("workflow_nodes")
    @classmethod
    def workflow_must_not_be_empty(
        cls, value: list[WorkflowNodeDefinition]
    ) -> list[WorkflowNodeDefinition]:
        if not value:
            raise ValueError("workflow 不能为空")
        return value
