"""Soothe 身份、工具范围与版本允许表；不在 API 导入时加载执行器依赖。"""
from app.agents.soothe_agent.tools import (
    SOOTHE_HTTP_TOOLS,
    SOOTHE_TOOL_OUTPUT_FIELDS,
    soothe_trusted_references,
)
from app.runtime.agent_registration import (
    AgentRegistration,
    AgentToolProfile,
    ExecutorFactory,
)


def _load_executor_factory() -> ExecutorFactory:
    """每次实际派发时才解析装配函数，执行实例不跨 Run 缓存。"""
    from app.agents.soothe_agent.bootstrap import configured_soothe_executor

    return configured_soothe_executor


# 两种业务类型共享工具权限，版本只在本 Agent 档案内解释。
SOOTHE_PROFILE = AgentToolProfile(
    http_tools=SOOTHE_HTTP_TOOLS, tool_prefix='soothe.',
    extract_references=soothe_trusted_references, business_id_field='turn_id',
    wire_versions={'1.0.0': '1.1.0'},
)
REGISTRATION = AgentRegistration(
    agent_id='soothe_agent',
    tool_profiles={'soothe_reply': SOOTHE_PROFILE, 'soothe_simulator': SOOTHE_PROFILE},
    tool_output_fields=SOOTHE_TOOL_OUTPUT_FIELDS,
    executor_factory_loader=_load_executor_factory,
)
