"""Memoir 的可信工具权限与工厂登记；版本包不能改写此处的权限。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.runtime.agent_registration import (
    AgentRegistration,
    AgentToolProfile,
    ExecutorFactory,
)
from app.runtime.state import SAFE_TOOL_OUTPUT_FIELDS

# 工具注册表属于 Runtime 代码，而非可变 AgentPackage。即使 package 文件被错误更新，
# 也不能改变业务 host、接口路径或运行上下文的取值方式。
_MEMOIR_HTTP_TOOLS: dict[str, tuple[str, str, str, str, bool, str]] = {
    "memory.get_snapshot": (
        "couple_diary_backend",
        "POST",
        "/api/v1/internal/agent-tools/memory.get_snapshot",
        "input",
        False,
        "cancellable",
    ),
    "memory.publish_playback_document": (
        "couple_diary_backend",
        "POST",
        "/api/v1/internal/agent-tools/memory.publish_playback_document",
        "playback_document",
        True,
        "query_after_commit",
    ),
}

# agent_version → Tool wire 合同的显式允许表：1.0.0 走四字段/六码 v1，
# 1.0.1 起走 v1.1 完整错误合同；1.0.2 只改了 prompt manifest 的 guardrail
# 策略（redacted_only），Tool 合同与 1.0.1 完全一致，沿用 v1.1.0。
# 1.0.3（M6 媒体通道）只新增图节点与媒体生成，Tool 合同零变更，沿用 v1.1.0。
# 1.0.4（每场景配图 + body 不设字数上限）同为媒体与内容侧放宽，Tool 合同零变更。
# 新版本包注册时若忘记在此登记，Worker 会在发包前以 TOOL_WIRE_VERSION_INVALID
# 瞬时失败（无日志、无 HTTP），表现为 load_snapshot 节点 WORKFLOW_NODE_FAILED。
_MEMOIR_WIRE_VERSIONS = {
    "1.0.0": "1.0.0",
    "1.0.1": "1.1.0",
    "1.0.2": "1.1.0",
    "1.0.3": "1.1.0",
    "1.0.4": "1.1.0",
    # 1.0.5（M7 受控循环）沿用 1.1.0 工具 wire 契约：仅新增 bounded_loop 工作流语义。
    "1.0.5": "1.1.0",
    # 1.0.6（批次候选游标 + 首/末批结构硬校验）只改 runner 循环语义与 prompt，
    # Tool 合同零变更，沿用 v1.1.0；未登记会导致 load_snapshot 无日志瞬时失败。
    "1.0.6": "1.1.0",
    # 1.0.7（预算扩容）只调 agent.yaml 额度，循环语义与 1.0.6 一致，
    # Tool 合同零变更，沿用 v1.1.0；同样必须登记，否则 load_snapshot 瞬时失败。
    "1.0.7": "1.1.0",
    # 1.0.8（M8 语音与配乐）只新增 Memoir 专属音频服务编排与 2.0.0 业务
    # 发布文档（audio 键为 publish_playback_document 负载演进，非 Tool wire
    # 变更），Tool/Snapshot 合同零变更，沿用 v1.1.0；未登记同样会导致
    # load_snapshot 无日志瞬时失败（历史事故口径，B18 已冻结该版本号）。
    "1.0.8": "1.1.0",
}


def _memoir_trusted_references(runtime_context: Mapping[str, Any]) -> dict[str, Any]:
    """保持 Memoir 归档引用的既有提取、类型检查与错误码。"""
    archive_id = runtime_context.get("archive_id")
    snapshot_id = runtime_context.get("snapshot_id")
    run_id = runtime_context.get("run_id")
    generation_epoch = runtime_context.get("generation_epoch")
    if (
        not isinstance(archive_id, str)
        or not isinstance(snapshot_id, str)
        or not isinstance(run_id, str)
        or isinstance(generation_epoch, bool)
        or not isinstance(generation_epoch, int)
    ):
        raise ValueError("TOOL_RUNTIME_CONTEXT_INVALID")
    return {
        "archive_id": archive_id,
        "snapshot_id": snapshot_id,
        "run_id": run_id,
        "generation_epoch": generation_epoch,
    }


def _load_executor_factory() -> ExecutorFactory:
    """仅实际派发时加载本 Agent 装配，API 查询登记不加载 Worker 依赖。"""
    from app.agents.memoir_agent.bootstrap import configured_memoir_executor

    return configured_memoir_executor


REGISTRATION = AgentRegistration(
    agent_id="memoir_agent",
    tool_profiles={
        "couple_memory": AgentToolProfile(
            http_tools=_MEMOIR_HTTP_TOOLS,
            tool_prefix="memory.",
            extract_references=_memoir_trusted_references,
            business_id_field="archive_id",
            wire_versions=_MEMOIR_WIRE_VERSIONS,
        ),
    },
    tool_output_fields=SAFE_TOOL_OUTPUT_FIELDS,
    executor_factory_loader=_load_executor_factory,
)
