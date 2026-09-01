from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.runtime.state import SAFE_TOOL_OUTPUT_FIELDS


class PackageSchema(BaseModel):
    """文件包的严格 schema 基类，拒绝未知配置以避免隐式行为漂移。"""

    model_config = ConfigDict(extra="forbid")


class LoopPolicySchema(PackageSchema):
    """M7 受控循环策略：全部取值 Literal 冻结，Package 不得自带任意预算语义。"""

    # 预算策略：唯一取值，循环额度继承 Run 级限额（缺失/零值由 executor fail closed）。
    budget_strategy: Literal["inherit_run_limits_v1"]
    # 合并策略：迭代产物按键去重后追加，禁止覆盖式合并。
    merge_strategy: Literal["append_unique_by_key"]
    # 去重键：当前唯一合法键为场景 ID。
    merge_key: Literal["scene_id"]
    # 迭代级错误处理：只允许跳过该迭代继续。
    on_iteration_error: Literal["continue"]
    # 额度耗尽策略：允许部分发布（partial）或整体失败（failed）。
    on_budget_exhausted: Literal["partial", "failed"]
    # 循环体节点引用：必须指向同 workflow 内 deterministic/model 节点。
    body_node_ids: list[str] = Field(min_length=1)


class WorkflowNodeDefinition(PackageSchema):
    node_id: str = Field(min_length=1)
    node_type: Literal[
        "deterministic", "tool", "model", "guardrail", "fallback", "bounded_loop"
    ]
    next_nodes: list[str] = Field(default_factory=list)
    prompt_ref: str | None = None
    can_wait_for_human: bool = False
    # partial 只允许由发布完成后的非关键后处理节点触发；默认始终为主链节点。
    optional: bool = False
    # resume 分类恢复：已完成节点是否允许安全重算。True 表示该节点无副作用，或
    # 副作用由 runner 层 query-after-commit 保证幂等（如 memoir load_snapshot/
    # 内容节点/publish_document），resume 时强制重跑以按当前 epoch/隐私/授权重读
    # 与重算；False（默认）表示副作用幂等性未知，resume 时已完成则跳过，只执行
    # 未完成节点，避免对非幂等副作用盲目重放（保护非 memoir Agent）。
    safe_to_rerun: bool = False
    # 受控循环策略：仅 bounded_loop 节点可声明；与 node_type 的双向强制
    # （⟺）由下方 validator 保证，其它节点类型携带即拒绝。
    loop_policy: LoopPolicySchema | None = None

    @model_validator(mode="after")
    def loop_policy_requires_bounded_loop(self) -> WorkflowNodeDefinition:
        # 双向校验：非 bounded_loop 带 loop_policy 拒绝（策略语义只属于受控循环）；
        # bounded_loop 缺 loop_policy 拒绝（循环额度语义必须可静态审计）。
        if self.loop_policy is not None and self.node_type != "bounded_loop":
            raise ValueError("loop_policy 仅允许出现在 bounded_loop 节点上")
        if self.node_type == "bounded_loop" and self.loop_policy is None:
            raise ValueError("bounded_loop 节点必须声明 loop_policy")
        # bounded_loop 必须 safe_to_rerun=True：循环中间正文/场景/图片不落
        # checkpoint，崩溃后只能整节点重算（resume 强制重算语义）；False 意味着
        # resume 可能跳过半途循环，与"崩溃后重算"契约冲突。safe_to_rerun 默认
        # False，故 bounded_loop 缺省声明该键会被补成 False 而拒绝——这是期望的
        # fail closed，不允许静默依赖默认值获得 False 语义。
        if self.node_type == "bounded_loop" and not self.safe_to_rerun:
            raise ValueError(
                "bounded_loop 节点必须 safe_to_rerun=True（崩溃后整节点重算）"
            )
        return self


class ToolManifest(PackageSchema):
    """受信任工具声明；不允许 Package 将请求重定向到任意 URL。"""

    name: str
    version: str
    # disabled 契约可随 Package 预留，但 Gateway 必须在解析网络注册前拒绝调用。
    enabled: bool = True
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
    # 单个 Run 的模型 token 总量上限；实际计量未知时使用输入 token 预留保守计入。
    max_tokens: int | None = None
    # 以下额度由 Runtime 在创建 Run 时冻结，避免请求方在执行期扩大资源权限。
    max_steps: int | None = None
    max_tool_calls: int | None = None
    max_run_seconds: int | None = None
    max_auto_retry_per_step: int | None = None

    @field_validator("max_model_calls", "max_tokens", mode="before")
    @classmethod
    def validate_max_model_calls(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("max_model_calls/max_tokens 必须为非负整数")
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
