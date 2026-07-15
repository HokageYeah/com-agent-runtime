"""Workflow 的内存态定义；只有白名单安全摘要允许进入 checkpoint。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
