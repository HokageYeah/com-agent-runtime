"""代码内受信执行身份、工具权限与延迟工厂；包声明不能授予执行能力。"""

from app.agents.memoir_agent.registration import REGISTRATION as MEMOIR_REGISTRATION
from app.agents.qa_agent.registration import REGISTRATION as QA_REGISTRATION
from app.agents.soothe_agent.registration import REGISTRATION as SOOTHE_REGISTRATION
from app.runtime.agent_registration import (
    AgentRegistration,
    AgentToolProfile,
    ExecutorFactory,
)

# 新 Agent 只在此可信清单加一行；不可从包、请求或模型输出解析注册描述。
REGISTRATIONS: tuple[AgentRegistration, ...] = (
    MEMOIR_REGISTRATION,
    QA_REGISTRATION,
    SOOTHE_REGISTRATION,
)

# 导入时检查身份唯一性，禁止字典覆盖悄悄改变同一 Agent 的执行权限。
_REGISTRATIONS_BY_ID: dict[str, AgentRegistration] = {}
for registration in REGISTRATIONS:
    if registration.agent_id in _REGISTRATIONS_BY_ID:
        raise ValueError("AGENT_REGISTRATION_DUPLICATE")
    _REGISTRATIONS_BY_ID[registration.agent_id] = registration

# 历史导入路径仍有调用方：只从可信清单派生，避免维护第二份权限登记。
EXECUTABLE_AGENT_IDS: frozenset[str] = frozenset(_REGISTRATIONS_BY_ID)
AGENT_TOOL_OUTPUT_FIELDS: dict[str, frozenset[str]] = {
    item.agent_id: item.tool_output_fields for item in REGISTRATIONS
}


def is_executable_agent(agent_id: str) -> bool:
    """供注册入口查询可信身份，不加载 Worker 的执行依赖。"""
    return agent_id in _REGISTRATIONS_BY_ID


def get_executor_factory(agent_id: str) -> ExecutorFactory | None:
    """只在派发已登记身份时调用固定加载器；未知身份不触发任何导入。"""
    registration = _REGISTRATIONS_BY_ID.get(agent_id)
    return registration.executor_factory_loader() if registration is not None else None


def get_tool_profile(agent_id: str, business_type: str) -> AgentToolProfile | None:
    """身份和业务类型必须联合命中，未知组合不继承任何默认档案。"""
    registration = _REGISTRATIONS_BY_ID.get(agent_id)
    return registration.tool_profiles.get(business_type) if registration is not None else None


def get_tool_output_fields(agent_id: str) -> frozenset[str] | None:
    """返回代码登记的工具写入白名单；未知身份由包 schema 沿用既有校验。"""
    return AGENT_TOOL_OUTPUT_FIELDS.get(agent_id)
