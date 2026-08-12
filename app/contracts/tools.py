from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ToolContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolManifest(ToolContractModel):
    """受注册表约束的业务工具描述；Runtime 不接受调用方自带完整 URL。"""

    name: str
    version: str
    enabled: bool = True
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
    # 默认绝不让业务错误详情进入模型上下文；只有未来冻结 policy 明确允许的
    # 受控枚举才能改变此结论，不能由 Provider/Tool 响应自行声明。
    details_visible_to_model: bool = False


# P3 冻结：业务 HTTP Tool 非 2xx 响应若自称 ToolError，error_code 必须落在以下 allowlist，
# 且其 HTTP 状态码必须等于此处冻结的期望值。码值与状态码精确取自业务后端实际抛出点
# (memory_publish_service.py 的 _CODE_* 与对应 HTTPException status_code)，不臆造。
# retryable 不单独冻结，统一由 `http_status >= 500` 派生，与 runner.py 捕获 HTTPStatusError
# 时的重试判定完全一致；当前 6 个码均为 4xx，故 retryable 全为 False。
# 业务端未来若要新增码，必须先在此 allowlist 与双方 fixture 同步冻结，否则 Runtime fail closed。
TOOL_ERROR_HTTP_STATUS: dict[str, int] = {
    "MEMORY_SNAPSHOT_UNAVAILABLE": 403,  # 快照不可读（权限/缺失）
    "GENERATION_SUPERSEDED": 403,  # 生成被更高 epoch 取代
    "MEMORY_RUN_NOT_ACTIVE": 403,  # Run 已终结，拒绝写入
    "MEMORY_DOCUMENT_INVALID": 403,  # 文档校验失败
    "IDEMPOTENCY_CONFLICT": 409,  # 幂等键冲突
    "PUBLISH_NOT_YET_OBSERVED": 404,  # 尚无可观测的发布结果（轮询信号）
}
