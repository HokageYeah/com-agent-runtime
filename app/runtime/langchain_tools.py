"""受限的 LangChain Tool 包装；业务执行始终回流 ``ToolGateway``。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol

from langchain_core.tools import StructuredTool
from pydantic import ConfigDict, create_model

from app.schemas.agent_package import ToolManifest

# 需求文档中的 UnifiedToolDefinition 在当前文件包实现中由 ToolManifest 承载。
type UnifiedToolDefinition = ToolManifest

_CONTROL_FIELDS = frozenset(
    {
        "archive_id",
        "snapshot_id",
        "run_id",
        "generation_epoch",
        "connector_id",
        "authorization_version",
        "privacy_version",
        "fencing_token",
        "lease_owner",
        "execution_attempt",
        "credential",
        "credentials",
        "idempotency_key",
    }
)
_SCHEMA_TYPES: dict[str, type[object]] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "object": dict[str, Any],
    "array": list[Any],
    "null": type(None),
}


class _ToolGateway(Protocol):
    """适配器唯一允许触达的业务执行边界。"""

    def call(
        self,
        manifest: ToolManifest,
        runtime_context: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...


def build_langchain_tool(
    definition: UnifiedToolDefinition,
    gateway: _ToolGateway,
    runtime_context: Mapping[str, Any],
    *,
    idempotency_key: str | None = None,
) -> StructuredTool:
    """把受信任的统一工具定义转换成 LangChain ``StructuredTool``。

    该函数不接收 connector、客户端或 URL。模型给出的字段仅会写到 manifest
    冻结的 ``input_from`` 槽；归档、运行、租约和授权等控制字段一律由
    ``runtime_context`` 保持权威，最终执行与输出校验都委托给 ToolGateway。
    """
    args_schema = _build_args_schema(definition)

    def invoke_gateway(**tool_input: Any) -> dict[str, Any]:
        # 新字典避免 LangChain 调用参数原地影响 Worker 持有的可信上下文。
        gateway_context = dict(runtime_context)
        if definition.input_from is None:
            raise ValueError("LANGCHAIN_TOOL_INPUT_SOURCE_INVALID")
        gateway_context[definition.input_from] = tool_input
        return gateway.call(
            definition,
            gateway_context,
            idempotency_key=idempotency_key,
        )

    return StructuredTool.from_function(
        invoke_gateway,
        name=definition.name,
        description=f"Runtime tool {definition.name} ({definition.version})",
        args_schema=args_schema,
        infer_schema=False,
    )


def _build_args_schema(definition: UnifiedToolDefinition) -> type[object]:
    """将受限 object JSON Schema 转成严格 Pydantic 入参模型。

    不支持组合 schema、引用或动态额外字段，避免框架包装层扩大 Package 可表达的
    行为；ToolGateway 仍是业务输出、授权、幂等与审计的唯一权威边界。
    """
    schema = definition.input_schema
    if not schema:
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
    if (
        not isinstance(schema, dict)
        or schema.get("type") != "object"
        or set(schema) - {"type", "properties", "required", "additionalProperties"}
    ):
        raise ValueError("LANGCHAIN_TOOL_INPUT_SCHEMA_INVALID")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    additional_properties = schema.get("additionalProperties", False)
    if (
        not isinstance(properties, dict)
        or not isinstance(required, list)
        or not isinstance(additional_properties, bool)
        or additional_properties
        or any(not isinstance(name, str) for name in required)
        or any(name not in properties for name in required)
    ):
        raise ValueError("LANGCHAIN_TOOL_INPUT_SCHEMA_INVALID")
    if _CONTROL_FIELDS.intersection(properties):
        raise ValueError("LANGCHAIN_TOOL_INPUT_SCHEMA_UNSAFE")

    fields: dict[str, tuple[type[object], object]] = {}
    for name, rule in properties.items():
        if not isinstance(name, str) or not isinstance(rule, dict) or set(rule) != {"type"}:
            raise ValueError("LANGCHAIN_TOOL_INPUT_SCHEMA_INVALID")
        type_name = rule["type"]
        if not isinstance(type_name, str):
            raise ValueError("LANGCHAIN_TOOL_INPUT_SCHEMA_INVALID")
        field_type = _SCHEMA_TYPES.get(type_name)
        if field_type is None:
            raise ValueError("LANGCHAIN_TOOL_INPUT_SCHEMA_INVALID")
        # Pydantic 将 bool 视作 int 的子类；严格模式避免模型输入借此越过 schema。
        fields[name] = (field_type, ... if name in required else None)

    model_name = "LangChainToolArgs_" + re.sub(r"[^A-Za-z0-9_]", "_", definition.name)
    return create_model(
        model_name,
        __config__=ConfigDict(extra="forbid", strict=True),
        **fields,
    )
