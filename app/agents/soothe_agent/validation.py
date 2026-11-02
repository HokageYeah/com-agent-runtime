"""解析后和提交前共享的确定性语义闸门；错误只含固定码。"""

from pydantic import BaseModel

from app.agents.soothe_agent.state import (
    ReplyContext,
    ReplyDraft,
    SimulatorContext,
    SimulatorDraft,
)
from app.runtime.executor import WorkflowNodeError, WorkflowNodeErrorCode

BANNED_PHRASES = (
    "别生气了",
    "你想多了",
    "至于吗",
    "你太敏感",
    "但是你也",
    "可是你也",
    "你也有错",
    "没有你我活不下去",
    "你是不是生理期",
    "女生都这样",
    "男人都这样",
)


def _has_blank(value: object) -> bool:
    # 遍历所有嵌套字段，包括上下文，防止空白正文绕过最小长度约束。
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, BaseModel):
        return _has_blank(value.model_dump())
    if isinstance(value, dict):
        return any(_has_blank(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_blank(item) for item in value)
    return False


def validate_draft(
    business_type: str,
    context: ReplyContext | SimulatorContext,
    draft: ReplyDraft | SimulatorDraft,
) -> None:
    """失败立即关闭提交路径，不把正文或上游异常带入错误。"""
    valid = not (_has_blank(context) or _has_blank(draft))
    if (
        business_type == "soothe_reply"
        and isinstance(context, ReplyContext)
        and isinstance(draft, ReplyDraft)
    ):
        valid &= draft.safety_flag == (draft.conflict_type == "safety")
        valid &= (
            not draft.replies
            if draft.safety_flag
            else (
                len(draft.replies) == 3
                and len({item.style for item in draft.replies}) == 3
            )
        )
        texts = [item.text for item in draft.replies]
        valid &= not any(text in context.previous_replies for text in texts)
    elif (
        business_type == "soothe_simulator"
        and isinstance(context, SimulatorContext)
        and isinstance(draft, SimulatorDraft)
    ):
        texts = [draft.reply]
    else:
        valid, texts = False, []
    # 安全场景与普通场景使用同一禁用表；模拟器也必须提供平和收尾。
    valid &= not any(phrase in text for text in texts for phrase in BANNED_PHRASES)
    if not valid:
        raise WorkflowNodeError(WorkflowNodeErrorCode.ANSWER_VALIDATION_FAILED)
