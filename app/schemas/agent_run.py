"""AgentRun API/Service 输入输出模型；字段仅含安全摘要。"""

from __future__ import annotations

from app.contracts.api import (
    AgentRunQuery,
    AgentRunResponse,
    CreateAgentRunRequest,
    HumanApprovalRequest,
)

# HTTP 与服务层共享冻结 contract，禁止再维护第二套字段集合。
CreateRunCommand = CreateAgentRunRequest
RunSummary = AgentRunResponse
RunDetail = AgentRunQuery
ApprovalCommand = HumanApprovalRequest
