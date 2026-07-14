from __future__ import annotations

from enum import StrEnum


class RuntimeEventType(StrEnum):
    """内部详细事件；它们绝不能直接泄露给小程序或业务前端。"""

    RUN_STARTED = "run_started"
    STEP_STARTED = "step_started"
    MODEL_CALL_STARTED = "model_call_started"
    MODEL_CALL_FINISHED = "model_call_finished"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_FINISHED = "tool_call_finished"
    STEP_FAILED = "step_failed"
    FALLBACK_USED = "fallback_used"
    HUMAN_REVIEW_REQUESTED = "human_review_requested"
    PARTIAL_SUCCEEDED = "partial_succeeded"
    RUN_SUCCEEDED = "run_succeeded"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"


class CallbackEventType(StrEnum):
    """业务后端允许接收的脱敏 callback 事件集合。"""

    RUN_STARTED = "run_started"
    STEP_CHANGED = "step_changed"
    WAITING_HUMAN = "waiting_human"
    PARTIAL_SUCCEEDED = "partial_succeeded"
    RUN_SUCCEEDED = "run_succeeded"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"


_CALLBACK_EVENT_MAP = {
    # 模型与工具的内部过程都压缩成 step_changed，避免透传模型/工具私密信息。
    RuntimeEventType.RUN_STARTED: CallbackEventType.RUN_STARTED,
    RuntimeEventType.STEP_STARTED: CallbackEventType.STEP_CHANGED,
    RuntimeEventType.MODEL_CALL_STARTED: CallbackEventType.STEP_CHANGED,
    RuntimeEventType.MODEL_CALL_FINISHED: CallbackEventType.STEP_CHANGED,
    RuntimeEventType.TOOL_CALL_STARTED: CallbackEventType.STEP_CHANGED,
    RuntimeEventType.TOOL_CALL_FINISHED: CallbackEventType.STEP_CHANGED,
    RuntimeEventType.STEP_FAILED: CallbackEventType.STEP_CHANGED,
    RuntimeEventType.FALLBACK_USED: CallbackEventType.STEP_CHANGED,
    RuntimeEventType.HUMAN_REVIEW_REQUESTED: CallbackEventType.WAITING_HUMAN,
    RuntimeEventType.PARTIAL_SUCCEEDED: CallbackEventType.PARTIAL_SUCCEEDED,
    RuntimeEventType.RUN_SUCCEEDED: CallbackEventType.RUN_SUCCEEDED,
    RuntimeEventType.RUN_FAILED: CallbackEventType.RUN_FAILED,
    RuntimeEventType.RUN_CANCELLED: CallbackEventType.RUN_CANCELLED,
}


def callback_event_for(event: RuntimeEventType) -> CallbackEventType:
    """Map internal events to the only callback events visible to a business app."""
    return _CALLBACK_EVENT_MAP[event]
