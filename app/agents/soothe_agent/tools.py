"""AI 帮哄的固定工具权限；提交沿用公共审计与发送复核，载荷遵守 turn_result 契约。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models import AgentToolCall
    from app.runtime.tool_gateway import ToolGateway

# connector、方法、路径、输入槽、副作用与取消语义均由可信代码固定。
SOOTHE_HTTP_TOOLS: dict[str, tuple[str, str, str, str, bool, str]] = {
    'soothe.get_turn_context': (
        'couple_diary_backend', 'POST', '/api/v1/internal/agent-tools/soothe.get_turn_context',
        'input', False, 'cancellable',
    ),
    'soothe.submit_turn_result': (
        'couple_diary_backend', 'POST', '/api/v1/internal/agent-tools/soothe.submit_turn_result',
        'turn_result', True, 'query_after_commit',
    ),
    'soothe.get_turn_result': (
        'couple_diary_backend', 'POST', '/api/v1/internal/agent-tools/soothe.get_turn_result',
        'input', False, 'cancellable',
    ),
}
# 工具只填充私密上下文和安全回执；草稿不能通过工具覆盖。
SOOTHE_TOOL_OUTPUT_FIELDS = frozenset({'turn_context', 'submit_result'})


def soothe_trusted_references(runtime_context: Mapping[str, Any]) -> dict[str, Any]:
    """业务引用仅来自 Worker 已校验的运行上下文，不能从包或模型取值。"""
    names = ('session_id', 'turn_id', 'run_id')
    if any(not isinstance(runtime_context.get(name), str) or not runtime_context[name] for name in names):
        raise ValueError('TOOL_RUNTIME_CONTEXT_INVALID')
    return {name: runtime_context[name] for name in names}


def submit_turn_result(
    gateway: ToolGateway,
    runtime_context: Mapping[str, Any],
    idempotency_key: str,
    tool_call: AgentToolCall | None,
) -> dict[str, Any]:
    """提交绑定物理审计 attempt；私密结果只经公共 _call 进入业务端，不写日志。"""
    references = soothe_trusted_references(runtime_context)
    if tool_call is None:
        raise ValueError('TOOL_AUDIT_REQUIRED')
    if tool_call.run_id != references['run_id'] or tool_call.side_effect is not True:
        raise ValueError('TOOL_AUDIT_CONTEXT_INVALID')
    if not idempotency_key:
        raise ValueError('TOOL_IDEMPOTENCY_KEY_REQUIRED')
    result = runtime_context.get('turn_result')
    if not isinstance(result, dict):
        raise ValueError('TOOL_INPUT_SOURCE_INVALID')
    # 共享契约要求 turn_result，不能复用通用 call() 将非 input 槽包装为 document。
    return gateway._call(
        'couple_diary_backend', '/api/v1/internal/agent-tools/soothe.submit_turn_result',
        {**references, 'turn_result': result}, 'soothe.submit_turn_result', idempotency_key,
        tool_call=tool_call, tool_context=runtime_context.get('tool_context'),
    )
