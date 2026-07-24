"""Callback 出站 body 的严格安全契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.public_trace import PublicTraceItem


class CallbackError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=80)


class CallbackPayload(BaseModel):
    """不承载 prompt、正文、模型/工具 payload 或签名 URL。"""

    model_config = ConfigDict(extra="forbid")

    event: Literal["run_started", "step_changed", "waiting_human", "partial_succeeded", "run_succeeded", "run_failed", "run_cancelled"]
    event_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=80)
    event_seq: int = Field(ge=1)
    status_version: int = Field(ge=1)
    agent_id: str = Field(min_length=1, max_length=80)
    business_id: str = Field(min_length=1, max_length=120)
    status: str = Field(min_length=1, max_length=32)
    error: CallbackError | None = None
    public_trace: list[PublicTraceItem] = Field(default_factory=list, max_length=8)
