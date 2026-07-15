from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel


class LeaseContext(BaseModel):
    """Worker 每次写入必须携带的 ownership/fencing 上下文。"""

    execution_attempt: int
    lease_owner: str
    fencing_token: int
    lease_expires_at: datetime
    privacy_version: int
    authorization_version: int


class AgentRunResult(BaseModel):
    run_id: str
    status: str
    execution_attempt: int
    output_summary: dict[str, object] | None = None
    artifact_refs: list[str] = []
    error_code: str | None = None
    checkpoint_id: str | None = None


class RunExecutor(Protocol):
    def run(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult: ...
