from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class AgentPlanDTO(BaseModel):
    """不含私密 state 的静态计划 DTO，可安全用于审计和后续落库。"""

    plan_id: str
    run_id: str
    strategy: Literal["static_workflow"]
    steps: list[dict[str, Any]]
    stop_conditions: dict[str, Any]
    fallback_policy: dict[str, Any]
    status: str
