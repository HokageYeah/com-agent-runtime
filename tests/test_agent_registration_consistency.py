"""遍历可信登记与全部版本包，防止新增 Agent 时权限声明或装配遗漏。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.registry import REGISTRATIONS
from app.runtime.agent_registration import AgentRegistration
from app.schemas.agent_package import AgentPackage
from app.services.agent_package_service import AgentPackageService

AGENTS_ROOT = Path(__file__).resolve().parents[1] / "app" / "agents"


@pytest.fixture(scope="module", params=REGISTRATIONS, ids=lambda item: item.agent_id)
def registration(request: pytest.FixtureRequest) -> AgentRegistration:
    return request.param


@pytest.fixture(scope="module")
def packages(registration: AgentRegistration) -> list[AgentPackage]:
    # 版本目录以数字开头；不按 agent.yaml 筛选，否则缺少该文件的坏包会被漏过。
    versions = sorted(
        path.name for path in (AGENTS_ROOT / registration.agent_id).iterdir()
        if path.is_dir() and path.name[0].isdigit()
    )
    assert versions, f"{registration.agent_id} 至少需要一个版本包"
    service = AgentPackageService(AGENTS_ROOT)
    return [service.load(registration.agent_id, version) for version in versions]


def test_registered_agent_has_loadable_versions(packages: list[AgentPackage]) -> None:
    assert packages


def test_http_tools_match_registered_profiles_and_prefixes(
    registration: AgentRegistration, packages: list[AgentPackage],
) -> None:
    for package in packages:
        for business_type in package.allowed_business_types:
            for tool in package.tools:
                if tool.connector_id is None:
                    continue
                # 每种允许业务都必须独立授权，不能用多份档案的工具并集掩盖越权。
                profile = registration.tool_profiles.get(business_type)
                assert profile is not None, (package.version, business_type)
                assert tool.name.startswith(profile.tool_prefix), (package.version, tool.name)
                # ToolGateway.call() 先拒绝 manifest.enabled=False，再查询工具档案；
                # 禁用预留只豁免档案成员检查，前缀与下方 output_to 检查仍覆盖全部声明。
                if tool.enabled:
                    assert tool.name in profile.http_tools, (package.version, business_type, tool.name)


def test_output_targets_match_registered_fields(
    registration: AgentRegistration, packages: list[AgentPackage],
) -> None:
    for package in packages:
        for tool in package.tools:
            if tool.output_to is not None:
                assert tool.output_to in registration.tool_output_fields, (package.version, tool.name)


def test_executor_factory_loader_resolves_callable(registration: AgentRegistration) -> None:
    # 只解析工厂，不构建执行器，不连接数据库或外部业务/模型服务。
    assert callable(registration.executor_factory_loader())


def test_business_types_have_profiles_when_tools_are_declared(
    registration: AgentRegistration, packages: list[AgentPackage],
) -> None:
    for package in packages:
        if package.tools:
            assert package.allowed_business_types, package.version
            assert set(package.allowed_business_types) <= registration.tool_profiles.keys(), package.version
