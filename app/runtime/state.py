"""Workflow 的内存态定义；只有白名单安全摘要允许进入 checkpoint。"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Tool 的写入目标必须是以下运行期业务字段；身份、授权与代际信息不属于 AgentState。
SAFE_TOOL_OUTPUT_FIELDS = frozenset({
    "snapshot", "sanitized_material", "stats", "highlights", "chapter_plan",
    "scenes", "actions", "playback_document", "publish_result", "media_tasks", "safety_report",
})

# 这些字段可改变调用主体、连接位置或并发代际，任何嵌套层级均不能来自工具响应。
_SENSITIVE_TOOL_OUTPUT_KEYS = frozenset({
    "identity", "caller_id", "tenant_id", "user_id", "authorization", "authorization_version",
    "permission", "privacy", "privacy_version", "connector", "connector_id", "endpoint", "url",
    "generation", "generation_epoch", "execution_attempt", "fencing_token", "run_id", "step_id",
    "version", "contract_version", "package_digest", "callback_target", "idempotency_key",
    "secret", "token", "access_token", "api_key", "password",
})


class AgentState(BaseModel):
    """Task 6 统一状态容器，私密输入只在执行内存中保留。"""

    model_config = ConfigDict(extra="forbid")

    # 私密输入由受信任业务工具读取；checkpoint/public trace 永不写入该字段。
    run_input: dict[str, Any] = Field(default_factory=dict)
    snapshot: dict[str, Any] | None = None
    sanitized_material: dict[str, Any] | None = None
    stats: dict[str, Any] | None = None
    highlights: dict[str, Any] | None = None
    chapter_plan: dict[str, Any] | None = None
    scenes: list[dict[str, Any]] | None = None
    actions: list[dict[str, Any]] | None = None
    playback_document: dict[str, Any] | None = None
    publish_result: dict[str, Any] | None = None
    media_tasks: list[dict[str, Any]] | None = None
    safety_report: dict[str, Any] | None = None
    trust_metadata: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    fallback_flags: list[str] = Field(default_factory=list)
    completed_node_ids: list[str] = Field(default_factory=list)

    def checkpoint_summary(self) -> dict[str, list[str]]:
        """仅输出恢复路由所需的节点与 fallback 标记，不泄漏业务内容。"""
        return {
            "completed_node_ids": list(self.completed_node_ids),
            "fallback_flags": list(self.fallback_flags),
        }

    def apply_tool_output(self, output_to: str, output: object) -> None:
        """将已校验工具结果写入白名单字段，阻断控制面与凭据回流。"""
        if output_to not in SAFE_TOOL_OUTPUT_FIELDS:
            logging.warning("拒绝工具输出写入 error_code=%s target=%s", "TOOL_OUTPUT_TARGET_FORBIDDEN", output_to)
            raise ValueError("TOOL_OUTPUT_TARGET_FORBIDDEN")
        if _contains_sensitive_tool_key(output):
            logging.warning("拒绝工具敏感字段 error_code=%s target=%s", "TOOL_OUTPUT_SENSITIVE_FIELD", output_to)
            raise ValueError("TOOL_OUTPUT_SENSITIVE_FIELD")
        # 重新走 Pydantic 校验，避免绕过字段的既定类型约束。
        validated = type(self).model_validate({**self.model_dump(), output_to: output})
        setattr(self, output_to, getattr(validated, output_to))


def _contains_sensitive_tool_key(value: object) -> bool:
    """递归检查键名；只检查结构，不读取也不记录任何工具正文。"""
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str) and key.lower() in _SENSITIVE_TOOL_OUTPUT_KEYS:
                return True
            if _contains_sensitive_tool_key(nested):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_sensitive_tool_key(item) for item in value)
    return False
