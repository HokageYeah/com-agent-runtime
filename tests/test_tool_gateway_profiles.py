"""通过真实 Gateway 入口验证代码工具档案隔离；仅替换 HTTP 传输。"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

import app.runtime.tool_gateway as gateway_module
from app.models import AgentRun
from app.runtime.tool_gateway import BusinessConnector, ToolGateway
from app.schemas.agent_package import ToolManifest


def _run(agent_id: str = "demo_agent", business_type: str = "demo_task") -> AgentRun:
    return AgentRun(
        agent_id=agent_id, agent_version="1.0.0", run_id="run-1",
        business_type=business_type, business_id="job-1", trace_id="trace-1",
    )


def _manifest(name: str = "demo.read") -> ToolManifest:
    return ToolManifest(
        name=name, version="1.0.0", connector_id="demo_backend", method="POST",
        relative_path=f"/internal/{name}", input_from="input", output_to="snapshot",
    )


def _memory_manifest() -> ToolManifest:
    return ToolManifest(
        name="memory.get_snapshot", version="1.0.0",
        connector_id="couple_diary_backend", method="POST",
        relative_path="/api/v1/internal/agent-tools/memory.get_snapshot",
        input_from="input", output_to="snapshot",
    )


@pytest.fixture
def second_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """第二档案只在测试代码注入，不登记 QA，也不修改输出槽校验。"""
    def references(context: Mapping[str, Any]) -> dict[str, Any]:
        job_id, run_id = context.get("job_id"), context.get("run_id")
        if not isinstance(job_id, str) or not isinstance(run_id, str):
            raise ValueError("TOOL_RUNTIME_CONTEXT_INVALID")
        return {"job_id": job_id, "run_id": run_id}

    profile = gateway_module.AgentToolProfile(
        http_tools={
            "demo.read": ("demo_backend", "POST", "/internal/demo.read", "input", False, "cancellable"),
            "demo.submit": ("demo_backend", "POST", "/internal/demo.submit", "playback_document", True, "query_after_commit"),
        },
        tool_prefix="demo.", extract_references=references,
        business_id_field="job_id", wire_versions={"1.0.0": "1.1.0"},
    )
    from app.agents.registry import get_tool_profile

    monkeypatch.setattr(
        gateway_module, "get_tool_profile",
        lambda agent_id, business_type: profile
        if (agent_id, business_type) == ("demo_agent", "demo_task")
        else get_tool_profile(agent_id, business_type),
    )


@pytest.fixture
def gateway() -> Iterator[tuple[ToolGateway, list[httpx.Request]]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"output": {"revision": 1}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        connector = BusinessConnector("https://8.8.8.8", "runtime-test", "test-key", "test-secret")
        yield ToolGateway({"demo_backend": connector, "couple_diary_backend": connector}, client), requests


def _context(run: AgentRun | None = None) -> dict[str, Any]:
    return {
        "job_id": "job-1", "run_id": "run-1",
        "tool_context": ToolGateway.build_tool_context(run or _run(), "read"),
        # 不可信 input 和其它 Agent 的引用不能影响本档案的 wire 输入。
        "input": {"job_id": "forged"}, "archive_id": "other-archive",
    }


def test_second_profile_builds_context_and_uses_own_references_and_wire(second_profile, gateway) -> None:
    instance, requests = gateway
    context = _context()
    assert instance.call(_manifest(), context) == {"revision": 1}
    assert len(requests) == 1
    assert json.loads(requests[0].content) == {
        "input": {"job_id": "job-1", "run_id": "run-1"},
        "context": context["tool_context"],
    }
    assert requests[0].headers["X-Agent-Tool-Contract-Version"] == "1.1.0"


@pytest.mark.parametrize("direction", ["demo_to_memory", "memory_to_demo", "unregistered_tool"])
def test_profiles_cannot_call_each_others_tools(second_profile, gateway, direction: str) -> None:
    instance, requests = gateway
    context = _context()
    manifest = _memory_manifest() if direction == "demo_to_memory" else _manifest("demo.unknown")
    if direction == "memory_to_demo":
        context = _context(_run("memoir_agent", "couple_memory"))
        manifest = _manifest()
    with pytest.raises(ValueError, match="^TOOL_MANIFEST_NOT_ALLOWED$"):
        instance.call(manifest, context)
    assert requests == []


@pytest.mark.parametrize("method", ["get_snapshot", "get_publish_result"])
def test_memoir_methods_cannot_use_second_profile_context(second_profile, gateway, method: str) -> None:
    instance, requests = gateway
    context = ToolGateway.build_tool_context(_run(), "read")
    with pytest.raises(ValueError, match="^TOOL_CONTEXT_TRUST_INVALID$"):
        if method == "get_snapshot":
            instance.get_snapshot("demo_backend", "job-1", "snapshot-1", "run-1", 1, context)
        else:
            instance.get_publish_result("demo_backend", "job-1", "run-1", "key-1", tool_context=context)
    assert requests == []


@pytest.mark.parametrize("agent_id,business_type", [
    ("unknown_agent", "demo_task"), ("demo_agent", "couple_memory"),
    ("memoir_agent", "demo_task"),
])
def test_unregistered_identity_pair_is_rejected(second_profile, gateway, agent_id, business_type) -> None:
    instance, requests = gateway
    run = _run(agent_id, business_type)
    with pytest.raises(ValueError, match="^TOOL_CONTEXT_TRUST_INVALID$"):
        ToolGateway.build_tool_context(run, "read")
    # 即使跳过构造入口手工拼出 context，发送入口也必须拒绝。
    context = {
        **ToolGateway.build_tool_context(_run(), "read"),
        "agent_id": agent_id, "business_type": business_type,
    }
    with pytest.raises(ValueError, match="^TOOL_CONTEXT_TRUST_INVALID$"):
        instance.call(_manifest(), {"job_id": "job-1", "run_id": "run-1", "tool_context": context})
    assert requests == []


@pytest.mark.parametrize("field,value,error", [
    ("job_id", "forged", "TOOL_CONTEXT_BUSINESS_ID_MISMATCH"),
    ("run_id", "forged", "TOOL_CONTEXT_RUN_ID_MISMATCH"),
    ("job_id", 1, "TOOL_RUNTIME_CONTEXT_INVALID"),
])
def test_second_profile_rejects_forged_references(second_profile, gateway, field, value, error) -> None:
    instance, requests = gateway
    context = {**_context(), field: value}
    with pytest.raises(ValueError, match=f"^{error}$"):
        instance.call(_manifest(), context)
    assert requests == []


def test_second_profile_unknown_wire_version_cannot_use_memoir_map(second_profile, gateway) -> None:
    instance, requests = gateway
    run = _run()
    run.agent_version = "1.0.1"  # Memoir 已登记，本档案未登记。
    with pytest.raises(ValueError, match="^TOOL_WIRE_VERSION_INVALID$"):
        instance.call(_manifest(), _context(run))
    assert requests == []


def test_second_profile_write_uses_fixed_source_and_requires_idempotency(second_profile, gateway) -> None:
    instance, requests = gateway
    manifest = _manifest("demo.submit").model_copy(update={
        "input_from": "playback_document", "side_effect": True,
        "cancellation_behavior": "query_after_commit",
    })
    context = {**_context(), "playback_document": {"revision": 1}}
    with pytest.raises(ValueError, match="^TOOL_IDEMPOTENCY_KEY_REQUIRED$"):
        instance.call(manifest, context)
    assert requests == []
    instance.call(manifest, context, idempotency_key="write-1")
    assert json.loads(requests[0].content)["input"] == {
        "job_id": "job-1", "run_id": "run-1", "document": {"revision": 1},
    }
    assert requests[0].headers["Idempotency-Key"] == "write-1"


def test_memoir_and_qa_profiles_are_registered_in_production() -> None:
    from app.agents.registry import get_tool_profile

    assert get_tool_profile("memoir_agent", "couple_memory") is not None
    assert get_tool_profile("qa_agent", "business_qa") is not None


def test_gateway_does_not_import_concrete_agents_or_keep_private_profiles() -> None:
    """R1 公共网关只消费注册表，防止新增 Agent 又要修改公共内核。"""
    tree = ast.parse(Path("app/runtime/tool_gateway.py").read_text())
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    assert not any(
        name.startswith(("app.agents.memoir_agent", "app.agents.qa_agent")) for name in imports
    )
    assert not hasattr(gateway_module, "_AGENT_TOOL_PROFILES")
