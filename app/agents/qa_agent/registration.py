"""QA 的可信工具权限与工厂登记；沿用现有工具常量和 wire 契约。"""

from app.agents.qa_agent.tools import (
    QA_HTTP_TOOLS,
    QA_TOOL_OUTPUT_FIELDS,
    qa_trusted_references,
)
from app.runtime.agent_registration import (
    AgentRegistration,
    AgentToolProfile,
    ExecutorFactory,
)


def _load_executor_factory() -> ExecutorFactory:
    """仅实际派发时加载本 Agent 装配，API 查询登记不加载 Worker 依赖。"""
    from app.agents.qa_agent.bootstrap import configured_qa_executor

    return configured_qa_executor


REGISTRATION = AgentRegistration(
    agent_id="qa_agent",
    tool_profiles={
        "business_qa": AgentToolProfile(
            http_tools=QA_HTTP_TOOLS,
            tool_prefix="qa.",
            extract_references=qa_trusted_references,
            business_id_field="turn_id",
            wire_versions={"1.0.0": "1.1.0"},
        ),
    },
    tool_output_fields=QA_TOOL_OUTPUT_FIELDS,
    executor_factory_loader=_load_executor_factory,
)
