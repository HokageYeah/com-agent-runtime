from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ToolContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


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


# Internal Business Tool 的非 2xx 合同按 wire version 封闭。v1.0.0 保留历史六
# 码/四字段形状；v1.1.0 才引入完整九码和显式 visibility 字段。Provider 和 consumer
# 必须同时更新对应 fixture；Runtime 绝不按裸 HTTP 状态或 FastAPI ``detail`` 推断语义。
TOOL_ERROR_SPECS_V1_0: dict[str, dict[str, object]] = {
    "MEMORY_SNAPSHOT_UNAVAILABLE": {"http_status": 403, "error_type": "snapshot_unavailable", "retryable": False, "safe_message": "回忆快照当前不可读取"},
    "GENERATION_SUPERSEDED": {"http_status": 403, "error_type": "generation_superseded", "retryable": False, "safe_message": "当前生成已被更新版本取代"},
    "MEMORY_RUN_NOT_ACTIVE": {"http_status": 403, "error_type": "run_not_active", "retryable": False, "safe_message": "该回忆录运行当前不可执行"},
    "MEMORY_DOCUMENT_INVALID": {"http_status": 403, "error_type": "document_invalid", "retryable": False, "safe_message": "播放文档不满足发布要求"},
    "IDEMPOTENCY_CONFLICT": {"http_status": 409, "error_type": "idempotency_conflict", "retryable": False, "safe_message": "请求与既有幂等操作冲突"},
    "PUBLISH_NOT_YET_OBSERVED": {"http_status": 404, "error_type": "publish_not_observed", "retryable": False, "safe_message": "尚未观察到发布结果"},
}


TOOL_ERROR_SPECS_V1_1: dict[str, dict[str, object]] = {
    "IDEMPOTENCY_CONFLICT": {"http_status": 409, "error_type": "idempotency_conflict", "retryable": False, "safe_message": "请求与既有幂等操作冲突"},
    "GENERATION_SUPERSEDED": {"http_status": 409, "error_type": "generation_superseded", "retryable": False, "safe_message": "当前生成已被更新版本取代"},
    "AUTHORIZATION_REVOKED": {"http_status": 403, "error_type": "authorization_revoked", "retryable": False, "safe_message": "该运行授权已失效"},
    "BUSINESS_DATA_INVALID": {"http_status": 422, "error_type": "business_data_invalid", "retryable": False, "safe_message": "业务数据不满足工具要求"},
    "MEMORY_SNAPSHOT_UNAVAILABLE": {"http_status": 403, "error_type": "snapshot_unavailable", "retryable": False, "safe_message": "回忆快照当前不可读取"},
    "MEMORY_RUN_NOT_ACTIVE": {"http_status": 409, "error_type": "run_not_active", "retryable": False, "safe_message": "该回忆录运行当前不可执行"},
    "MEMORY_DOCUMENT_INVALID": {"http_status": 422, "error_type": "document_invalid", "retryable": False, "safe_message": "播放文档不满足发布要求"},
    "PUBLISH_NOT_YET_OBSERVED": {"http_status": 404, "error_type": "publish_not_observed", "retryable": False, "safe_message": "尚未观察到发布结果"},
    "RUNTIME_SERVICE_UNAVAILABLE": {"http_status": 503, "error_type": "service_unavailable", "retryable": True, "safe_message": "业务工具服务暂时不可用"},
}

# 当前 Runtime 的新 Run 默认使用 v1.1.0；保留别名避免既有内部导入发生漂移。
TOOL_ERROR_SPECS = TOOL_ERROR_SPECS_V1_1

TOOL_ERROR_SPECS_BY_WIRE_VERSION: dict[str, dict[str, dict[str, object]]] = {
    "1.0.0": TOOL_ERROR_SPECS_V1_0,
    "1.1.0": TOOL_ERROR_SPECS_V1_1,
}

# 保留该投影供既有调用方使用；语义校验一律取 ``TOOL_ERROR_SPECS`` 的完整合同。
TOOL_ERROR_HTTP_STATUS: dict[str, int] = {
    code: cast(int, spec["http_status"]) for code, spec in TOOL_ERROR_SPECS.items()
}
