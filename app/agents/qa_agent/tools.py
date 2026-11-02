"""QA 业务工具的代码常量；包声明不能扩展 connector、路径或输出权限。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models import AgentToolCall
    from app.runtime.tool_gateway import ToolGateway

# 三种工具共享固定 connector；元组依次是 connector、方法、路径、输入槽、副作用与取消语义。
QA_HTTP_TOOLS: dict[str, tuple[str, str, str, str, bool, str]] = {
    'qa.get_question_context': (
        'qa_business_backend', 'POST', '/api/v1/internal/agent-tools/qa.get_question_context',
        'input', False, 'cancellable',
    ),
    'qa.submit_answer': (
        'qa_business_backend', 'POST', '/api/v1/internal/agent-tools/qa.submit_answer',
        'answer_draft', True, 'query_after_commit',
    ),
    'qa.get_answer_result': (
        'qa_business_backend', 'POST', '/api/v1/internal/agent-tools/qa.get_answer_result',
        'input', False, 'cancellable',
    ),
}
# 工具只能填充私有问题上下文与安全提交回执，模型草稿只由 Runner 写入。
QA_TOOL_OUTPUT_FIELDS = frozenset({'question_context', 'submit_result'})


def qa_trusted_references(runtime_context: Mapping[str, Any]) -> dict[str, Any]:
    """只接受 Worker 可信运行上下文中的引用，不读取请求自报 input。"""
    names = ('conversation_id', 'turn_id', 'run_id')
    if any(not isinstance(runtime_context.get(name), str) or not runtime_context[name] for name in names):
        raise ValueError('TOOL_RUNTIME_CONTEXT_INVALID')
    return {name: runtime_context[name] for name in names}


def submit_answer(
    gateway: ToolGateway,
    runtime_context: Mapping[str, Any],
    idempotency_key: str,
    tool_call: AgentToolCall | None,
) -> dict[str, Any]:
    """提交必须绑定已登记物理 attempt，随后复用公共发送复核与输出校验。"""
    references = qa_trusted_references(runtime_context)
    if tool_call is None:
        raise ValueError('TOOL_AUDIT_REQUIRED')
    if tool_call.run_id != references['run_id'] or tool_call.side_effect is not True:
        raise ValueError('TOOL_AUDIT_CONTEXT_INVALID')
    if not idempotency_key:
        raise ValueError('TOOL_IDEMPOTENCY_KEY_REQUIRED')
    document = runtime_context.get('answer_draft')
    if not isinstance(document, dict):
        raise ValueError('TOOL_INPUT_SOURCE_INVALID')
    # 路由固定在代码，ToolGateway._call 继续校验七字段身份、授权、lease 和敏感输出。
    return gateway._call(
        'qa_business_backend', '/api/v1/internal/agent-tools/qa.submit_answer',
        {**references, 'document': document}, 'qa.submit_answer', idempotency_key,
        tool_call=tool_call, tool_context=runtime_context.get('tool_context'),
    )
