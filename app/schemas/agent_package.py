from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.runtime.state import SAFE_TOOL_OUTPUT_FIELDS


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
    # 取消语义是 Runtime 固定工具契约的一部分，Package 不能借此改变调用时机。
    cancellation_behavior: Literal[
        "cancellable", "non_cancellable", "query_after_commit"
    ] = "cancellable"
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
        if self.output_to and self.output_to not in SAFE_TOOL_OUTPUT_FIELDS:
            raise ValueError("output_to 必须是受控的 AgentState 业务字段")
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
    # 缺失表示不设额度，不能被解释为零额度。
    max_model_calls: int | None = None
    max_model_cost: float | None = None
    # 以下额度由 Runtime 在创建 Run 时冻结，避免请求方在执行期扩大资源权限。
    max_steps: int | None = None
    max_tool_calls: int | None = None
    max_run_seconds: int | None = None
    max_auto_retry_per_step: int | None = None

    @field_validator("max_model_calls", mode="before")
    @classmethod
    def validate_max_model_calls(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("max_model_calls 必须为非负整数")
        return value

    @field_validator("max_steps", "max_tool_calls", "max_run_seconds", "max_auto_retry_per_step", mode="before")
    @classmethod
    def validate_execution_limits(cls, value: object) -> object:
        """执行期次数与秒数只能是非负整数，布尔值不能伪装为额度。"""
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("执行期额度必须为非负整数")
        return value

    @field_validator("max_model_cost", mode="before")
    @classmethod
    def validate_max_model_cost(cls, value: object) -> object:
        if value is None:
            return value
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("max_model_cost 必须为非负有限数")
        return value

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
