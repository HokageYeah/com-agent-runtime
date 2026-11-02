"""受信执行身份只来自代码；查询身份不能加载 Worker 的运行依赖。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


def test_memoir_and_qa_are_registered_as_executable() -> None:
    from app.agents.registry import EXECUTABLE_AGENT_IDS, is_executable_agent

    assert EXECUTABLE_AGENT_IDS == frozenset({"memoir_agent", "qa_agent", "soothe_agent"})
    assert is_executable_agent("memoir_agent") is True
    assert is_executable_agent("qa_agent") is True
    assert is_executable_agent("soothe_agent") is True


@pytest.mark.parametrize("agent_id", ["agent", "unknown_agent"])
def test_unknown_agent_has_no_executable_identity_or_factory(agent_id: str) -> None:
    from app.agents.registry import get_executor_factory, is_executable_agent

    assert is_executable_agent(agent_id) is False
    assert get_executor_factory(agent_id) is None


def test_identity_lookup_does_not_import_worker_dependencies() -> None:
    # 独立解释器避免其他测试预先导入 Worker，锁住 API/注册脚本的轻量导入边界。
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; "
            "from app.agents.registry import "
            "is_executable_agent, get_executor_factory, get_tool_profile, get_tool_output_fields; "
            "assert is_executable_agent('memoir_agent'); "
            "assert get_tool_profile('qa_agent', 'business_qa') is not None; "
            "assert get_tool_output_fields('qa_agent') == {'question_context', 'submit_result'}; "
            "assert get_executor_factory('unknown_agent') is None; "
            "assert not {'app.worker', 'app.runtime.executor', 'httpx', 'sqlalchemy'} "
            ".intersection(sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("agent_id,business_type,prefix,business_id,outputs", [
    ("memoir_agent", "couple_memory", "memory.", "archive_id", frozenset({
        "snapshot", "sanitized_material", "stats", "highlights", "chapter_plan",
        "scenes", "actions", "playback_document", "publish_result", "media_tasks", "safety_report",
    })),
    ("qa_agent", "business_qa", "qa.", "turn_id", frozenset({"question_context", "submit_result"})),
    ("soothe_agent", "soothe_reply", "soothe.", "turn_id", frozenset({"turn_context", "submit_result"})),
    ("soothe_agent", "soothe_simulator", "soothe.", "turn_id", frozenset({"turn_context", "submit_result"})),
])
def test_registration_exposes_existing_tool_permissions(
    agent_id: str, business_type: str, prefix: str, business_id: str, outputs: frozenset[str],
) -> None:
    from app.agents.registry import (
        REGISTRATIONS,
        get_tool_output_fields,
        get_tool_profile,
    )
    from app.runtime.agent_registration import AgentToolProfile
    from app.runtime.tool_gateway import AgentToolProfile as LegacyProfile

    assert {item.agent_id for item in REGISTRATIONS} == {"memoir_agent", "qa_agent", "soothe_agent"}
    profile = get_tool_profile(agent_id, business_type)
    assert isinstance(profile, AgentToolProfile)
    assert LegacyProfile is AgentToolProfile
    assert profile.tool_prefix == prefix
    assert profile.business_id_field == business_id
    assert get_tool_output_fields(agent_id) == outputs
    assert get_tool_profile(agent_id, "unknown_business") is None
    assert get_tool_profile("unknown_agent", business_type) is None
    assert get_tool_output_fields("unknown_agent") is None

    # 历史输出目标路径仍是同一份派生映射，避免兼容路径变成第二份权限来源。
    from app.agents.output_targets import AGENT_TOOL_OUTPUT_FIELDS as legacy_outputs
    from app.agents.registry import AGENT_TOOL_OUTPUT_FIELDS

    assert legacy_outputs is AGENT_TOOL_OUTPUT_FIELDS
    registration = next(item for item in REGISTRATIONS if item.agent_id == agent_id)
    assert registration.tool_profiles[business_type] is profile
    assert registration.tool_output_fields == outputs


def test_registered_factories_still_resolve_to_worker() -> None:
    from app.agents.memoir_agent.bootstrap import configured_memoir_executor
    from app.agents.qa_agent.bootstrap import configured_qa_executor
    from app.agents.registry import get_executor_factory

    assert get_executor_factory("memoir_agent") is configured_memoir_executor
    assert get_executor_factory("qa_agent") is configured_qa_executor


def test_duplicate_agent_id_is_rejected_during_registry_import() -> None:
    # 在全新进程中模拟可信清单重复身份，不能因字典覆盖而静默授予另一份权限。
    result = subprocess.run(
        [sys.executable, "-c", """
from dataclasses import replace
import app.agents.memoir_agent.registration as memoir
memoir.REGISTRATION = replace(memoir.REGISTRATION, agent_id='qa_agent')
try:
    import app.agents.registry
except ValueError as error:
    assert str(error) == 'AGENT_REGISTRATION_DUPLICATE'
else:
    raise AssertionError('duplicate agent_id was accepted')
"""],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_neutral_descriptor_does_not_import_agents_or_worker() -> None:
    import ast

    tree = ast.parse(Path("app/runtime/agent_registration.py").read_text())
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    assert not any(name == "app.worker" or name.startswith("app.agents") for name in imports)


@pytest.mark.parametrize("first_module", [
    "app.runtime.agent_registration", "app.agents.registry",
    "app.schemas.agent_package", "app.runtime.tool_gateway",
])
def test_registration_consumers_support_cold_import_in_any_order(first_module: str) -> None:
    # 不依赖 pytest 已加载模块，覆盖 schema/网关/注册描述之间的循环导入风险。
    result = subprocess.run(
        [sys.executable, "-c", f"""
import importlib
import sys
importlib.import_module({first_module!r})
from app.agents.registry import get_tool_profile
from app.schemas.agent_package import ToolManifest
from app.runtime.tool_gateway import AgentToolProfile
assert isinstance(get_tool_profile('memoir_agent', 'couple_memory'), AgentToolProfile)
assert 'app.worker' not in sys.modules
assert 'app.runtime.executor' not in sys.modules
"""],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
