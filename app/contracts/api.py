from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1.0.0"


class AgentRunStatus(StrEnum):
    """运行状态是业务回调和 Worker 都必须遵守的冻结枚举。"""

    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    EVALUATING = "evaluating"
    WAITING_HUMAN = "waiting_human"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DispatchState(StrEnum):
    """调度状态与业务执行状态分离，用于 lease 与配额账本对账。"""

    HELD = "held"
    QUEUED = "queued"
    CLAIMED = "claimed"
    FINISHED = "finished"


class ContractModel(BaseModel):
    """所有外部契约拒绝未声明字段，避免调用方悄悄扩展控制参数。"""

    model_config = ConfigDict(extra="forbid")


class CreateAgentRunRequest(ContractModel):
    """创建 Run 的短请求；真正工作流只能由后续 Worker 异步执行。"""

    agent_id: str = Field(min_length=1, max_length=80)
    agent_version: str = Field(min_length=1, max_length=40)
    business_type: str = Field(min_length=1, max_length=80)
    business_id: str = Field(min_length=1, max_length=120)
    start_mode: Literal["held", "auto"] = "held"
    # input 是业务 Agent 输入；后续只允许进入加密、短 TTL 的私密存储。
    input: dict[str, Any]
    callback_target_id: str = Field(min_length=1, max_length=120)
    business_connector_id: str = Field(min_length=1, max_length=120)
    contract_version: Literal["1.0.0"] = "1.0.0"


class AgentRunResponse(ContractModel):
    """创建成功的安全摘要，不返回 connector 地址或任何私密 payload。"""

    run_id: str
    status: AgentRunStatus
    dispatch_state: DispatchState
    contract_version: Literal["1.0.0"] = "1.0.0"
    package_digest: str
    authorization_version: int = Field(ge=1)


class StartAgentRunRequest(ContractModel):
    expected_status_version: int | None = Field(default=None, ge=1)


class RetryAgentRunRequest(ContractModel):
    expected_status_version: int | None = Field(default=None, ge=1)


class CancelAgentRunRequest(ContractModel):
    reason_code: str = Field(min_length=1, max_length=80)


class HumanApprovalRequest(ContractModel):
    decision: Literal["approve", "reject"]
    expected_status_version: int = Field(ge=1)


class PurgePrivateDataRequest(ContractModel):
    reason_code: str = Field(min_length=1, max_length=80)


class AgentRunQuery(ContractModel):
    run_id: str
    status: AgentRunStatus
    dispatch_state: DispatchState
    status_version: int = Field(ge=1)
    last_event_seq: int = Field(ge=0)
    execution_attempt: int = Field(ge=0)
    privacy_state: Literal["active", "purge_requested", "purged"]
