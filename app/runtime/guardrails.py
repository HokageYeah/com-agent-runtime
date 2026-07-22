"""回忆录发布前的确定性内容护栏，所有结果均为不含原文的错误码。"""

from __future__ import annotations

import re

from app.runtime.native_tools import contains_sensitive_identifier


class MemoirGuardrails:
    """拦截敏感标识和产品明确禁止的情绪操控表达。"""

    # 仅匹配已确认的高风险关系措辞，避免把正常的情绪叙述过度拦截。
    _EMOTIONAL_RISK = re.compile(r"关系失败|都怪你|(?:都是|全是|你).{0,8}错|复合|重新在一起")

    @classmethod
    def violations(cls, body: object) -> tuple[str, ...]:
        """返回受控违规码；绝不返回、记录或持久化待审正文。"""
        if not isinstance(body, str):
            return ()
        violations: list[str] = []
        if contains_sensitive_identifier(body):
            violations.append("SENSITIVE_IDENTIFIER_BLOCKED")
        if cls._EMOTIONAL_RISK.search(body) is not None:
            violations.append("EMOTIONAL_LANGUAGE_BLOCKED")
        return tuple(violations)
