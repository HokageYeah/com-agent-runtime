"""AgentRun API/Service 输入输出模型；字段仅含安全摘要。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class RunSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateRunCommand(RunSchema):
    agent_id: str
    agent_version: str
    business_type: str
    business_id: str
    start_mode: Literal["held", "auto"] = "held"
    # 私密输入只写 AgentRun；查询 DTO 永远不会回显该字段。
    input: dict[str, Any]
    callback_target_id: str
    business_connector_id: str
    # 数据域由受信任业务服务声明，Runtime 会再按服务身份 allowlist 校验。
    data_domain: str = "couple_memory"


class RunSummary(RunSchema):
    run_id: str
    status: str
    dispatch_state: str
    contract_version: str
    package_digest: str
    authorization_version: int


class StepSummary(RunSchema):
    step_id: str
    step_name: str
    step_type: str
    status: str
    execution_attempt: int
    step_attempt: int
    error_code: str | None = None
    error_message: str | None = None


class RunDetail(RunSummary):
    status_version: int
    last_event_seq: int
    execution_attempt: int
    privacy_state: str
    privacy_version: int
    # 进度仅基于已落库的步骤状态计算，不能从模型/工具私密输出推导。
    progress: int = 0
    current_step: StepSummary | None = None
    error_code: str | None = None
    error_message: str | None = None
    privacy_purge_requested_at: datetime | None = None
    private_data_purged_at: datetime | None = None
    updated_at: datetime | None = None
    # 第一版只暴露状态/节点摘要，永不携带 prompt、原始工具 payload 或私密素材。
    public_trace: list[dict[str, Any]] = []


class ApprovalCommand(RunSchema):
    decision: Literal["approve", "reject"]
    expected_status_version: int


class ReasonCommand(RunSchema):
    """取消和隐私清理使用标准原因码，避免把自由文本写入审计日志。"""

    reason_code: str
