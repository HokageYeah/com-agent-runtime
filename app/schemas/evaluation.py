"""Evaluator 对外与持久化之间共用的安全决策 DTO。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EvaluationDecisionDTO(BaseModel):
    """评价结论只包含枚举、受控错误码和计数，不能携带素材或播放正文。"""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["pass", "retry", "fallback", "human_review", "fail"]
    reasons: tuple[str, ...] = ()
    scores: dict[str, int | float] = Field(default_factory=dict)
    next_node: str | None = None
    safe_summary: dict[str, int] = Field(default_factory=dict)
