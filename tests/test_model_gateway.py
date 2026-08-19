from __future__ import annotations

import json
import logging
import socket
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.agents.memoir_agent.runner import MemoirNodeRunner
from app.db.sqlalchemy_db import Base
from app.models import AgentDefinition, AgentModelUsage, AgentRun, AgentStep
from app.runtime.interfaces import LeaseContext
from app.runtime.memoir_model_gateway import MemoirModelGatewayAdapter
from app.runtime.model_gateway import (
    ModelCallContext,
    ModelCapabilityEvaluator,
    ModelGateway,
    ModelPolicyRegistry,
    ModelRoute,
    ModelRouteRegistry,
    PermitResult,
    ProviderTrafficController,
)
from app.runtime.policy_engine import PolicyEngine
from app.runtime.prompt_registry import PromptDefinition
from app.runtime.state import AgentState
from app.schemas.agent_run import CreateRunCommand
from app.services.agent_run_service import AgentRunService
from app.services.lease_service import LeaseService
from app.services.model_usage_service import ModelUsageService
from tests.test_provider_traffic_controller import FakeRedis


@pytest.fixture(autouse=True)
def _resolve_mock_provider_to_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock Provider 使用虚拟域名；测试中显式模拟其部署 DNS 的公网结果。"""
    original_getaddrinfo = socket.getaddrinfo

    def resolve(host: object, port: object, *args: object, **kwargs: object) -> object:
        if host == "provider.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]
        return original_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


class RecordingProvider:
    def __init__(self, response: object = {"ok": True}) -> None:
        self.response = response
        self.calls = 0

    def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object:
        self.calls += 1
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class RevokingLease:
    def __init__(self, values: list[bool]) -> None:
        self._last = values[-1]
        self._values = iter(values)

    def can_write(self, run_id: str, context: LeaseContext) -> bool:
        try:
            self._last = next(self._values)
        except StopIteration:
            pass
        return self._last


def _route() -> ModelRoute:
    return ModelRoute(
        route_id="summary", provider="provider", model="small",
        endpoint="https://provider.example/v1/chat", rate_limit_key="provider:small",
        max_concurrency=2, rpm_limit=30, tpm_limit=10_000, timeout_seconds=5,
        permit_ttl_seconds=10, settle_margin_seconds=1,
        price_unit="usd_per_1k_tokens", input_price=1, output_price=2,
    )


def _private_route() -> ModelRoute:
    return ModelRoute(**{
        **_route().__dict__,
        "capabilities": frozenset({"structured_output", "private_residency"}),
        "data_residency": "private",
    })


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1/v1/chat",
        "https://10.0.0.8/v1/chat",
        "https://169.254.169.254/v1/chat",
        "https://[fd00::8]/v1/chat",
        "https://localhost/v1/chat",
    ],
    ids=("loopback", "private", "link_local", "ipv6_private", "localhost"),
)
def test_model_route_rejects_unsafe_endpoint_at_construction(endpoint: str) -> None:
    """模型路由配置不得指向本机、私网或链路本地地址。"""
    with pytest.raises(ValueError, match="MODEL_ENDPOINT_UNSAFE"):
        ModelRoute(**{**_route().__dict__, "endpoint": endpoint})


def test_model_gateway_rejects_domain_resolved_to_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """域名注册后解析到私网时，不得创建 usage、permit 或调用 Provider。"""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443))
        ],
    )
    session, lease = _run_session()
    provider = RecordingProvider()

    private_route = ModelRoute(**{
        **_route().__dict__,
        "capabilities": frozenset({"structured_output", "private_residency"}),
        "data_residency": "private",
    })
    result = _gateway(session, RevokingLease([True]), provider, route=private_route).call(
        _context(session, lease), "summary", {"message": "private"}
    )

    assert (result.status, result.error_code) == ("endpoint_rejected", "MODEL_ENDPOINT_UNSAFE")
    assert provider.calls == 0
    assert session.scalar(select(AgentModelUsage)) is None


def test_http_provider_adapter_rejects_connected_peer_not_in_preflight_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 Provider 的 TCP 对端与发送前 DNS 结果不一致时拒绝响应。"""
    from app.runtime.model_gateway import HttpProviderAdapter

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ],
    )
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ok": True}, request=request),
    ))
    adapter = HttpProviderAdapter(
        client,
        peer_ip_provider=lambda: "8.8.4.4",
        reset_peer_ip=lambda: None,
    )

    with pytest.raises(ValueError, match="MODEL_PROVIDER_PEER_MISMATCH"):
        adapter.call(_route(), {"message": "private"}, timeout_seconds=1)


def test_http_provider_adapter_fails_closed_without_peer_capture() -> None:
    """未注入真实 socket 对端读取器时，不得把 Provider 响应交给 Runtime。"""
    from app.runtime.model_gateway import HttpProviderAdapter

    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ok": True}, request=request),
    ))

    with pytest.raises(ValueError, match="MODEL_PROVIDER_PEER_UNVERIFIABLE"):
        HttpProviderAdapter(client).call(_route(), {"message": "private"}, timeout_seconds=1)


def test_http_provider_adapter_accepts_matching_peer_and_resets_previous_value() -> None:
    """仅本轮预检 DNS 中的公网对端可通过，发送前必须清除旧连接记录。"""
    from app.runtime.model_gateway import HttpProviderAdapter

    reset_calls = 0

    def reset_peer_ip() -> None:
        nonlocal reset_calls
        reset_calls += 1

    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ok": True}, request=request),
    ))
    result = HttpProviderAdapter(
        client,
        peer_ip_provider=lambda: "8.8.8.8",
        reset_peer_ip=reset_peer_ip,
    ).call(_route(), {"message": "private"}, timeout_seconds=1)

    assert result == {"ok": True}
    assert reset_calls == 1


def _run_session() -> tuple[object, LeaseContext]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(
        run_id="run-1", agent_id="agent", agent_version="1", package_digest="digest",
        contract_version="1.0.0", business_type="memoir", business_id="business", status="pending",
        dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller",
        tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace", execution_attempt=1,
        lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(minutes=1),
        capability_snapshot_json={"allowed_model_route_ids": ["summary"]},
        run_deadline_at=now + timedelta(days=1),
    ))
    session.add(AgentStep(
        step_id="step-1", run_id="run-1", step_name="summarize", step_type="model",
        status="running", execution_attempt=1, step_attempt=1,
        input_summary={"estimated_input_tokens": 20},
    ))
    session.commit()
    return session, LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=now + timedelta(minutes=1), privacy_version=1, authorization_version=1,
    )


def _gateway(
    session: object,
    lease: object,
    provider: RecordingProvider,
    route: ModelRoute | None = None,
) -> ModelGateway:
    return ModelGateway(
        ModelRouteRegistry([route or _route()]), ProviderTrafficController(FakeRedis()),
        ModelUsageService(session), lease, provider, PolicyEngine(session),
    )


def _context(session: object, lease: LeaseContext) -> ModelCallContext:
    return ModelCallContext.from_authoritative(session, "run-1", "step-1", lease)


def test_memoir_adapter_forwards_deployment_prompt_definition_to_usage_boundary() -> None:
    """适配器必须从部署内注册表获取 Prompt，不信任 request 自报的版本。"""
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    step = session.scalar(select(AgentStep).where(AgentStep.step_id == "step-1"))
    assert run is not None
    assert step is not None
    run.agent_id = "memoir_agent"
    run.agent_version = "1.0.0"
    step.step_name = "extract_highlights"
    session.commit()

    class RecordingGateway:
        def __init__(self) -> None:
            self.prompt: PromptDefinition | None = None

        @staticmethod
        def context_token_budget(_route_id: str, _prompt: PromptDefinition) -> int:
            return 300

        @staticmethod
        def capability_available(
            _route_id: str, _prompt: PromptDefinition, _estimated_input_tokens: int,
        ) -> bool:
            return True

        def call(
            self,
            context: ModelCallContext,
            route_id: str,
            request: object,
            *,
            prompt: PromptDefinition | None = None,
        ) -> object:
            assert context.run_id == "run-1"
            assert context.step_id == "step-1"
            assert route_id == "summary"
            assert isinstance(request, dict)
            assert request["messages"][0]["role"] == "system"
            assert request["messages"][1]["role"] == "human"
            assert "forged" not in str(request)
            self.prompt = prompt
            return type("Result", (), {"status": "succeeded", "data": {}})()

    gateway = RecordingGateway()
    result = MemoirModelGatewayAdapter(
        session, gateway, {"extract_highlights": "summary"}, lease,  # type: ignore[arg-type]
    ).call(
        "run-1", "extract_highlights",
        {"prompt_id": "forged", "prompt_version": "latest"},
    )

    assert result.status == "succeeded"
    assert gateway.prompt is not None
    assert (gateway.prompt.prompt_id, gateway.prompt.version) == ("highlight-extract", "v1")
    assert gateway.prompt.template not in str({
        "prompt_id": gateway.prompt.prompt_id,
        "prompt_version": gateway.prompt.version,
        "model_policy": gateway.prompt.model_policy,
    })


def test_memoir_adapter_uses_versioned_repair_prompt_and_untrusted_data_slot() -> None:
    """原始候选只能短暂进入 repair 的 human data 槽，不能覆盖可信 Prompt。"""
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    step = session.scalar(select(AgentStep).where(AgentStep.step_id == "step-1"))
    assert run is not None and step is not None
    run.agent_id, run.agent_version = "memoir_agent", "1.0.0"
    step.step_name = "extract_highlights"
    session.commit()

    class RecordingGateway:
        def __init__(self) -> None:
            self.prompt: PromptDefinition | None = None
            self.request: object = None

        @staticmethod
        def context_token_budget(_route_id: str, _prompt: PromptDefinition) -> int:
            return 300

        @staticmethod
        def capability_available(
            _route_id: str, _prompt: PromptDefinition, _estimated_input_tokens: int,
        ) -> bool:
            return True

        def call(
            self,
            context: ModelCallContext,
            route_id: str,
            request: object,
            *,
            prompt: PromptDefinition | None = None,
        ) -> object:
            self.prompt = prompt
            self.request = request
            return type(
                "Result",
                (),
                {"status": "succeeded", "data": {"source_refs": ["diary:d-1"]}},
            )()

    gateway = RecordingGateway()
    result = MemoirModelGatewayAdapter(
        session, gateway, {"extract_highlights": "summary"}, lease,  # type: ignore[arg-type]
    ).repair(
        "run-1",
        "extract_highlights",
        {
            "prompt_id": "highlight-extract",
            "prompt_version": "v1",
            "model_policy": "strict",
            "context": {},
            "input": {"source_refs": ["diary:d-1"]},
        },
        "not-json",
    )

    assert result.status == "succeeded"
    assert gateway.prompt is not None
    assert (gateway.prompt.prompt_id, gateway.prompt.version) == (
        "structured-output-repair",
        "v1",
    )
    assert isinstance(gateway.request, dict)
    messages = gateway.request["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "human"


def test_memoir_repair_creates_separate_usage_permit_and_prompt_attempt() -> None:
    """首次候选与 repair 必须是两个独立物理 attempt，且账本不保存候选正文。"""
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    step = session.scalar(select(AgentStep).where(AgentStep.step_id == "step-1"))
    assert run is not None and step is not None
    run.agent_id, run.agent_version = "memoir_agent", "1.0.0"
    step.step_name = "extract_highlights"
    session.commit()

    class SequenceProvider(RecordingProvider):
        def __init__(self) -> None:
            super().__init__()
            self._responses = iter(
                ["x" * 800, {"source_refs": ["diary:diary-1"]}],
            )

        def call(
            self, route: ModelRoute, request: object, *, timeout_seconds: float,
        ) -> object:
            self.calls += 1
            return next(self._responses)

    provider = SequenceProvider()
    redis = FakeRedis()
    adapter = MemoirModelGatewayAdapter(
        session,
        ModelGateway(
            ModelRouteRegistry([_private_route()]),
            ProviderTrafficController(redis),
            ModelUsageService(session),
            RevokingLease([True] * 20),
            provider,
            PolicyEngine(session),
        ),
        {"extract_highlights": "summary"},
        lease,
    )
    state = AgentState(
        sanitized_material={
            "materials": [
                {
                    "source_ref": "diary:diary-1",
                    "type": "diary",
                    "sensitive": False,
                    "summary": "摘要",
                },
            ],
        },
    )

    result = MemoirNodeRunner(object(), model_gateway=adapter).run_node(
        {"node_id": "extract_highlights"}, run, state,
    )

    usages = session.scalars(
        select(AgentModelUsage).order_by(AgentModelUsage.model_attempt),
    ).all()
    assert result == {"node_id": "extract_highlights", "fallback": False}
    assert provider.calls == 2
    assert [usage.model_attempt for usage in usages] == [1, 2]
    assert [usage.prompt_id for usage in usages] == [
        "highlight-extract",
        "structured-output-repair",
    ]
    assert len({usage.permit_id for usage in usages}) == 2
    assert usages[1].reserved_tokens > usages[0].reserved_tokens
    assert "not-json" not in str(
        [
            usage.capability_snapshot_json
            | {
                "prompt_id": usage.prompt_id,
                "prompt_version": usage.prompt_version,
                "thinking_summary": usage.thinking_summary_json,
            }
            for usage in usages
        ],
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda run: setattr(run, "cancel_requested_at", datetime.now(UTC)),
        lambda run: setattr(run, "privacy_state", "purge_requested"),
        lambda run: setattr(
            run,
            "authorization_version",
            run.authorization_version + 1,
        ),
        lambda run: setattr(run, "fencing_token", run.fencing_token + 1),
        lambda run: setattr(run, "tenant_id", "other-tenant"),
        lambda run: setattr(
            run,
            "capability_snapshot_json",
            {
                **run.capability_snapshot_json,
                "required_model_data_residency": "public",
            },
        ),
        lambda run: setattr(
            run,
            "capability_snapshot_json",
            {
                **run.capability_snapshot_json,
                "allowed_model_route_ids": [],
            },
        ),
    ],
    ids=(
        "cancel",
        "purge",
        "authorization",
        "old_lease",
        "tenant",
        "residency",
        "route",
    ),
)
def test_memoir_repair_rechecks_execution_boundary_before_second_attempt(
    change: object,
) -> None:
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    step = session.scalar(select(AgentStep).where(AgentStep.step_id == "step-1"))
    assert run is not None and step is not None
    run.agent_id, run.agent_version = "memoir_agent", "1.0.0"
    step.step_name = "extract_highlights"
    session.commit()

    class InvalidatingProvider(RecordingProvider):
        def call(
            self, route: ModelRoute, request: object, *, timeout_seconds: float,
        ) -> object:
            self.calls += 1
            current = session.scalar(
                select(AgentRun).where(AgentRun.run_id == "run-1"),
            )
            assert current is not None
            change(current)  # type: ignore[operator]
            session.commit()
            return "not-json"

    provider = InvalidatingProvider()
    governed_route = ModelRoute(**{
        **_private_route().__dict__,
        "allowed_tenant_ids": frozenset({"tenant"}),
        "allowed_model_policies": frozenset(
            {"balanced", "emotional_writing", "strict"},
        ),
    })
    adapter = MemoirModelGatewayAdapter(
        session,
        ModelGateway(
            ModelRouteRegistry([governed_route]),
            ProviderTrafficController(FakeRedis()),
            ModelUsageService(session),
            RevokingLease([True] * 20),
            provider,
            PolicyEngine(session),
        ),
        {"extract_highlights": "summary"},
        lease,
    )
    state = AgentState(
        sanitized_material={
            "materials": [
                {
                    "source_ref": "diary:diary-1",
                    "type": "diary",
                    "sensitive": False,
                    "summary": "摘要",
                },
            ],
        },
    )

    result = MemoirNodeRunner(object(), model_gateway=adapter).run_node(
        {"node_id": "extract_highlights"}, run, state,
    )

    assert result == {"node_id": "extract_highlights", "fallback": True}
    assert provider.calls == 1
    assert len(session.scalars(select(AgentModelUsage)).all()) == 1


def test_memoir_repair_respects_call_budget_before_second_permit() -> None:
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    step = session.scalar(select(AgentStep).where(AgentStep.step_id == "step-1"))
    assert run is not None and step is not None
    run.agent_id, run.agent_version = "memoir_agent", "1.0.0"
    run.capability_snapshot_json = {
        "allowed_model_route_ids": ["summary"],
        "model_policy": {"max_model_calls": 1},
    }
    step.step_name = "extract_highlights"
    session.commit()
    provider = RecordingProvider("not-json")
    redis = FakeRedis()
    adapter = MemoirModelGatewayAdapter(
        session,
        ModelGateway(
            ModelRouteRegistry([_private_route()]),
            ProviderTrafficController(redis),
            ModelUsageService(session),
            RevokingLease([True] * 20),
            provider,
            PolicyEngine(session),
        ),
        {"extract_highlights": "summary"},
        lease,
    )
    state = AgentState(
        sanitized_material={
            "materials": [
                {
                    "source_ref": "diary:diary-1",
                    "type": "diary",
                    "sensitive": False,
                    "summary": "摘要",
                },
            ],
        },
    )

    result = MemoirNodeRunner(object(), model_gateway=adapter).run_node(
        {"node_id": "extract_highlights"}, run, state,
    )

    assert result == {"node_id": "extract_highlights", "fallback": True}
    assert provider.calls == 1
    usages = session.scalars(select(AgentModelUsage)).all()
    assert len(usages) == 1
    assert redis.operations.count("acquire") == 1


def test_memoir_repair_fails_closed_when_redis_breaks_between_attempts() -> None:
    class BreakAfterFirstAttemptRedis(FakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.broken = False

        def eval(self, script: str, numkeys: int, *args: object) -> list[object]:
            if self.broken:
                raise ConnectionError("redis unavailable")
            result = super().eval(script, numkeys, *args)
            if args[0] == "settle":
                self.broken = True
            return result

    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    step = session.scalar(select(AgentStep).where(AgentStep.step_id == "step-1"))
    assert run is not None and step is not None
    run.agent_id, run.agent_version = "memoir_agent", "1.0.0"
    step.step_name = "extract_highlights"
    session.commit()
    provider = RecordingProvider("not-json")
    redis = BreakAfterFirstAttemptRedis()
    adapter = MemoirModelGatewayAdapter(
        session,
        ModelGateway(
            ModelRouteRegistry([_private_route()]),
            ProviderTrafficController(redis),
            ModelUsageService(session),
            RevokingLease([True] * 20),
            provider,
            PolicyEngine(session),
        ),
        {"extract_highlights": "summary"},
        lease,
    )
    state = AgentState(
        sanitized_material={
            "materials": [
                {
                    "source_ref": "diary:diary-1",
                    "type": "diary",
                    "sensitive": False,
                    "summary": "摘要",
                },
            ],
        },
    )

    result = MemoirNodeRunner(object(), model_gateway=adapter).run_node(
        {"node_id": "extract_highlights"}, run, state,
    )

    assert result == {"node_id": "extract_highlights", "fallback": True}
    assert provider.calls == 1
    assert len(session.scalars(select(AgentModelUsage)).all()) == 1


def test_memoir_adapter_rejects_ambiguous_authoritative_step() -> None:
    """同一执行边界内 Step 不唯一时必须 fail-closed，不能任意选一条。"""
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    first = session.scalar(select(AgentStep).where(AgentStep.step_id == "step-1"))
    assert run is not None
    assert first is not None
    run.agent_id = "memoir_agent"
    run.agent_version = "1.0.0"
    first.step_name = "extract_highlights"
    session.add(AgentStep(
        step_id="step-2", run_id="run-1", step_name="extract_highlights",
        step_type="model", status="running", execution_attempt=1, step_attempt=2,
        input_summary={"estimated_input_tokens": 20},
    ))
    session.commit()

    class RecordingGateway:
        calls = 0

        def call(self, *args: object, **kwargs: object) -> object:
            self.calls += 1
            return type("Result", (), {"status": "succeeded", "data": {}})()

    gateway = RecordingGateway()
    result = MemoirModelGatewayAdapter(
        session, gateway, {"extract_highlights": "summary"}, lease,  # type: ignore[arg-type]
    ).call("run-1", "extract_highlights", {})

    assert result.status == "aborted_before_send"
    assert gateway.calls == 0


def test_memoir_runner_uses_template_when_adapter_capability_is_unavailable_before_provider() -> None:
    """策略与 route 不匹配必须在适配器边界降级，Provider 不得收到任何请求。"""
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    step = session.scalar(select(AgentStep).where(AgentStep.step_id == "step-1"))
    assert run is not None and step is not None
    run.agent_id, run.agent_version = "memoir_agent", "1.0.0"
    step.step_name = "extract_highlights"
    session.commit()
    provider = RecordingProvider()
    # route 缺少 structured_output 能力，与 strict 模型策略不匹配；
    # 能力判定必须在适配器边界失败（不依赖护栏策略的具体取值）。
    mismatched_route = ModelRoute(**{**_route().__dict__, "capabilities": frozenset({"chat"})})
    adapter = MemoirModelGatewayAdapter(
        session,
        _gateway(session, RevokingLease([True]), provider, mismatched_route),
        {"extract_highlights": "summary"},
        lease,
    )
    state = AgentState(
        sanitized_material={"materials": [
            {"source_ref": "diary:diary-1", "type": "diary", "sensitive": False, "summary": "摘要"},
        ]},
    )

    result = MemoirNodeRunner(object(), model_gateway=adapter).run_node(
        {"node_id": "extract_highlights"}, run, state,
    )

    assert result == {"node_id": "extract_highlights", "fallback": True}
    assert state.highlights == {"source_refs": ["diary:diary-1"], "mode": "template"}
    assert provider.calls == 0


def test_cancellation_after_acquire_does_not_call_provider() -> None:
    session, lease = _run_session()
    provider = RecordingProvider()

    result = _gateway(session, RevokingLease([False]), provider).call(
        _context(session, lease), "summary", {"private": "must not persist"}
    )

    assert result.status == "aborted_before_send"
    assert provider.calls == 0


def test_draining_guard_rejects_before_policy_permit_usage_or_provider() -> None:
    class DrainingGuard:
        def permits_new_call(self, context: ModelCallContext) -> bool:
            return False

    session, lease = _run_session()
    provider = RecordingProvider()
    redis = FakeRedis()
    gateway = ModelGateway(
        ModelRouteRegistry([_route()]), ProviderTrafficController(redis),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
        call_guard=DrainingGuard(),
    )

    result = gateway.call(_context(session, lease), "summary", {"message": "private"})

    assert result.status == "aborted_before_send"
    assert provider.calls == 0
    assert session.scalar(select(AgentModelUsage)) is None
    assert "acquire" not in redis.operations


def test_draining_guard_after_permit_releases_usage_before_provider() -> None:
    class ChangesAfterPermit:
        def __init__(self) -> None:
            self.calls = 0

        def permits_new_call(self, context: ModelCallContext) -> bool:
            self.calls += 1
            return self.calls == 1

    session, lease = _run_session()
    provider = RecordingProvider()
    redis = FakeRedis()
    gateway = ModelGateway(
        ModelRouteRegistry([_route()]), ProviderTrafficController(redis),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
        call_guard=ChangesAfterPermit(),
    )

    result = gateway.call(_context(session, lease), "summary", {"message": "private"})

    usage = session.scalar(select(AgentModelUsage))
    assert result.status == "aborted_before_send"
    assert provider.calls == 0
    assert usage is not None and usage.status == "aborted_before_send"
    assert "acquire" in redis.operations
    assert "settle" in redis.operations


def test_revoked_before_send_marks_usage_aborted_without_cost() -> None:
    session, lease = _run_session()
    provider = RecordingProvider()

    result = _gateway(session, RevokingLease([True, False]), provider).call(
        _context(session, lease), "summary", {"private": "must not persist"}
    )

    usage = session.scalar(select(AgentModelUsage))
    assert result.status == "aborted_before_send"
    assert provider.calls == 0
    assert usage is not None and usage.status == "aborted_before_send"
    assert usage.estimated_cost == 0


def test_provider_429_settles_and_shares_retry_after() -> None:
    session, lease = _run_session()
    response = httpx.Response(429, headers={"Retry-After": "12"})
    provider = RecordingProvider(httpx.HTTPStatusError("rate limited", request=httpx.Request("POST", "https://p"), response=response))
    gateway = _gateway(session, RevokingLease([True, True]), provider)

    result = gateway.call(_context(session, lease), "summary", {"message": "private"})

    assert result.status == "rate_limited"
    assert provider.calls == 1
    assert session.scalar(select(AgentModelUsage)).status == "rate_limited"


def test_timeout_is_settled_as_unknown_not_zero_cost() -> None:
    session, lease = _run_session()
    provider = RecordingProvider(httpx.TimeoutException("timeout"))

    result = _gateway(session, RevokingLease([True, True]), provider).call(
        _context(session, lease), "summary", {"message": "private"}
    )

    usage = session.scalar(select(AgentModelUsage))
    assert result.status == "outcome_unknown"
    assert usage is not None and usage.status == "outcome_unknown"
    assert usage.estimated_cost == usage.reserved_estimated_cost
    assert usage.estimated_cost > 0


@pytest.mark.parametrize(
    "failure",
    [
        httpx.TimeoutException("timeout"),
        httpx.HTTPStatusError(
            "server error", request=httpx.Request("POST", "https://p"), response=httpx.Response(503)
        ),
    ],
    ids=("timeout", "http_5xx"),
)
def test_confirmed_provider_failure_opens_circuit_before_next_provider_call(failure: BaseException) -> None:
    session, lease = _run_session()
    guarded = ModelRoute(**{
        **_route().__dict__, "circuit_failure_threshold": 1, "circuit_open_seconds": 30,
    })
    provider = RecordingProvider(failure)
    gateway = ModelGateway(
        ModelRouteRegistry([guarded]), ProviderTrafficController(FakeRedis()),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
    )

    assert gateway.call(_context(session, lease), "summary", {"message": "private"}).status == "outcome_unknown"
    rejected = gateway.call(_context(session, lease), "summary", {"message": "private"})

    assert rejected.status == "circuit_open"
    assert rejected.retry_after_seconds > 0
    assert provider.calls == 1


def test_open_circuit_preflight_skips_policy_reservation_and_provider() -> None:
    class CountingPolicy(PolicyEngine):
        def __init__(self, session: object) -> None:
            super().__init__(session)
            self.reserve_calls = 0

        def reserve(self, context: ModelCallContext, route: ModelRoute) -> tuple[object, str | None]:
            self.reserve_calls += 1
            return super().reserve(context, route)

    session, lease = _run_session()
    guarded = ModelRoute(**{
        **_route().__dict__, "circuit_failure_threshold": 1, "circuit_open_seconds": 30,
    })
    redis = FakeRedis()
    traffic = ProviderTrafficController(redis)
    provider = RecordingProvider()
    policy = CountingPolicy(session)
    assert traffic.record_circuit_failure(guarded).status == "circuit_opened"
    gateway = ModelGateway(
        ModelRouteRegistry([guarded]), traffic, ModelUsageService(session),
        RevokingLease([True]), provider, policy,
    )

    rejected = gateway.call(_context(session, lease), "summary", {"message": "private"})

    assert rejected.status == "circuit_open"
    assert rejected.retry_after_seconds > 0
    assert provider.calls == 0
    assert policy.reserve_calls == 0
    assert session.scalar(select(AgentModelUsage)) is None


def test_circuit_opened_between_preflight_and_acquire_cancels_reservation() -> None:
    class OpensOnAcquireRedis(FakeRedis):
        def eval(self, script: str, numkeys: int, *args: object) -> list[object]:
            if args[0] == "acquire":
                self.circuit_open_until[str(args[10])] = float(args[4]) + 30
            return super().eval(script, numkeys, *args)

    session, lease = _run_session()
    guarded = ModelRoute(**{
        **_route().__dict__, "circuit_failure_threshold": 1, "circuit_open_seconds": 30,
    })
    provider = RecordingProvider()
    gateway = ModelGateway(
        ModelRouteRegistry([guarded]), ProviderTrafficController(OpensOnAcquireRedis()),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
    )

    rejected = gateway.call(_context(session, lease), "summary", {"message": "private"})

    assert rejected.status == "circuit_open"
    assert provider.calls == 0
    assert session.scalar(select(AgentModelUsage)) is None


def test_gateway_success_resets_circuit_and_429_does_not_open_it() -> None:
    class SequenceProvider(RecordingProvider):
        def __init__(self, responses: list[object]) -> None:
            super().__init__()
            self._responses = iter(responses)

        def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object:
            self.calls += 1
            response = next(self._responses)
            if isinstance(response, BaseException):
                raise response
            return response

    session, lease = _run_session()
    guarded = ModelRoute(**{
        **_route().__dict__, "circuit_failure_threshold": 2, "circuit_open_seconds": 30,
    })
    provider = SequenceProvider([
        httpx.TimeoutException("first"), {"ok": True}, httpx.TimeoutException("second"),
        httpx.HTTPStatusError(
            "rate limited", request=httpx.Request("POST", "https://p"), response=httpx.Response(429)
        ),
        httpx.TimeoutException("third"),
    ])
    gateway = ModelGateway(
        ModelRouteRegistry([guarded]), ProviderTrafficController(FakeRedis()),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
    )

    statuses = [
        gateway.call(_context(session, lease), "summary", {"message": "private"}).status
        for _ in range(5)
    ]

    assert statuses == ["outcome_unknown", "succeeded", "outcome_unknown", "rate_limited", "outcome_unknown"]
    assert gateway.call(_context(session, lease), "summary", {"message": "private"}).status == "circuit_open"
    assert provider.calls == 5


def test_usage_settlement_is_idempotent() -> None:
    session, lease = _run_session()
    usage = ModelUsageService(session).create_running(_context(session, lease), _route(), "permit-1")

    assert ModelUsageService(session).settle(usage, "aborted_before_send") == "settled"
    assert ModelUsageService(session).settle(usage, "outcome_unknown") == "already_settled"
    persisted = session.scalar(select(AgentModelUsage))
    assert persisted is not None and persisted.status == "aborted_before_send"


def test_reconciler_marks_expired_started_usage_as_outcome_unknown() -> None:
    """Worker 在已获发送权后崩溃时，不能遗留 started 计量或猜测零成本。"""
    session, lease = _run_session()
    usage_id = ModelUsageService(session).create_running(_context(session, lease), _route(), "permit-crash")
    assert ModelUsageService(session).mark_started(usage_id)
    usage = session.scalar(select(AgentModelUsage).where(AgentModelUsage.usage_id == usage_id))
    assert usage is not None
    usage.request_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    assert ModelUsageService(session).mark_expired_running_unknown() == 1
    session.refresh(usage)
    assert usage.status == "outcome_unknown"


def test_late_usage_monotonically_settles_unknown_attempt_once() -> None:
    """迟到计量必须同时匹配 usage 与已冻结的 Provider 请求身份。"""
    session, lease = _run_session()
    usage_service = ModelUsageService(session)
    usage_id = usage_service.create_running(_context(session, lease), _route(), "permit-late")
    assert usage_service.mark_started(usage_id)
    assert usage_service.settle(
        usage_id, "outcome_unknown", provider_request_id="provider-request-1",
    ) == "settled"

    assert usage_service.settle(
        usage_id, "succeeded", input_tokens=3, output_tokens=2, route=_route(),
        provider_request_id="wrong-provider-request",
    ) == "identity_mismatch"
    assert usage_service.settle(
        usage_id, "succeeded", input_tokens=3, output_tokens=2, route=_route(),
        provider_request_id="provider-request-1",
    ) == "settled"
    assert usage_service.settle(
        usage_id, "failed", provider_request_id="provider-request-1",
    ) == "already_settled"
    usage = session.scalar(select(AgentModelUsage).where(AgentModelUsage.usage_id == usage_id))
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert usage is not None
    assert (usage.status, usage.input_tokens, usage.output_tokens) == ("succeeded", 3, 2)
    # 迟到 usage 仅更新无内容账本，绝不借旧 execution 结算推进工作流状态。
    assert run is not None and (run.status, run.status_version) == ("pending", 1)


def test_unknown_usage_without_provider_identity_rejects_late_usage_recovery() -> None:
    """未记录 Provider 请求身份时，不能仅凭 usage_id 接受迟到计量。"""
    session, lease = _run_session()
    usage_service = ModelUsageService(session)
    usage_id = usage_service.create_running(_context(session, lease), _route(), "permit-late")
    assert usage_service.mark_started(usage_id)
    assert usage_service.mark_expired_running_unknown(datetime.now(UTC) + timedelta(days=1)) == 1

    assert usage_service.settle(
        usage_id, "succeeded", input_tokens=3, output_tokens=2, route=_route(),
        provider_request_id="provider-request-1",
    ) == "identity_mismatch"
    usage = session.scalar(select(AgentModelUsage).where(AgentModelUsage.usage_id == usage_id))
    assert usage is not None
    assert (usage.status, usage.input_tokens, usage.output_tokens, usage.estimated_cost) == (
        "outcome_unknown", None, None, usage.reserved_estimated_cost,
    )


def test_gateway_marks_usage_started_before_provider_call() -> None:
    """Provider 边界只接受已持久化 started usage，避免发送后无可对账账本。"""
    session, lease = _run_session()

    class InspectingProvider(RecordingProvider):
        def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object:
            usage = session.scalar(select(AgentModelUsage))
            assert usage is not None and usage.status == "started"
            return super().call(route, request, timeout_seconds=timeout_seconds)

    provider = InspectingProvider(
        {"ok": True, "usage": {"input_tokens": 1, "output_tokens": 1}}
    )
    result = _gateway(session, RevokingLease([True]), provider).call(
        _context(session, lease), "summary", {"private": "must not persist"},
    )

    assert result.status == "succeeded"
    assert provider.calls == 1


def test_gateway_does_not_send_when_permit_mark_started_is_rejected() -> None:
    """Redis permit 未进入 started 时，即使 usage 已存在也不得发送 Provider 请求。"""
    session, lease = _run_session()

    class RejectingTraffic(ProviderTrafficController):
        def mark_started(self, route: ModelRoute, permit_id: str) -> PermitResult:
            return PermitResult("expired")

    provider = RecordingProvider()
    result = ModelGateway(
        ModelRouteRegistry([_route()]), RejectingTraffic(FakeRedis()),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
    ).call(_context(session, lease), "summary", {"private": "must not persist"})

    usage = session.scalar(select(AgentModelUsage))
    assert result.status == "aborted_before_send"
    assert provider.calls == 0
    assert usage is not None and usage.status == "aborted_before_send"


def test_usage_settle_failure_still_releases_permit_and_hides_success() -> None:
    """usage 账本故障不能遗留 permit，也不能把 Provider 返回误报为成功。"""
    session, lease = _run_session()
    redis = FakeRedis()

    class FailingUsage(ModelUsageService):
        def settle(self, *args: object, **kwargs: object) -> str:
            raise RuntimeError("storage unavailable")

    result = ModelGateway(
        ModelRouteRegistry([_route()]), ProviderTrafficController(redis),
        FailingUsage(session), RevokingLease([True]), RecordingProvider(), PolicyEngine(session),
    ).call(_context(session, lease), "summary", {"private": "must not persist"})

    assert result.status == "outcome_unknown"
    assert "settle" in redis.operations


def test_permit_settle_failure_still_settles_usage_and_hides_success() -> None:
    """共享流控结算故障不能阻断 usage 账本，调用方只能得到未知结果。"""
    session, lease = _run_session()

    class FailingTraffic(ProviderTrafficController):
        def settle(
            self, route: ModelRoute, permit_id: str, *, retry_after_seconds: float = 0,
        ) -> PermitResult:
            raise RuntimeError("redis unavailable")

    result = ModelGateway(
        ModelRouteRegistry([_route()]), FailingTraffic(FakeRedis()),
        ModelUsageService(session), RevokingLease([True]), RecordingProvider(), PolicyEngine(session),
    ).call(_context(session, lease), "summary", {"private": "must not persist"})

    usage = session.scalar(select(AgentModelUsage))
    assert result.status == "outcome_unknown"
    assert usage is not None and usage.status == "succeeded"


def test_revocation_between_mark_started_and_http_send_does_not_call_provider() -> None:
    session, lease = _run_session()
    provider = RecordingProvider()

    result = _gateway(session, RevokingLease([True, True, False]), provider).call(
        _context(session, lease), "summary", {"message": "private"}
    )

    usage = session.scalar(select(AgentModelUsage))
    assert result.status == "aborted_before_send"
    assert provider.calls == 0
    assert usage is not None and usage.status == "aborted_before_send"


@pytest.mark.parametrize("change", ["cancel", "lease_lost"])
def test_change_after_permit_acquire_aborts_before_provider_send(change: str) -> None:
    """permit 已获批后发生取消或失租时，仍不得把请求发送给 Provider。"""
    session, lease = _run_session()
    provider = RecordingProvider()

    class ChangingTraffic(ProviderTrafficController):
        def acquire(
            self, route: ModelRoute, permit_id: str, *, estimated_tokens: int = 0,
        ) -> object:
            result = super().acquire(route, permit_id, estimated_tokens=estimated_tokens)
            if result.granted:
                run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
                assert run is not None
                if change == "cancel":
                    run.cancel_requested_at = datetime.now(UTC)
                else:
                    run.lease_owner = "replacement-worker"
                session.commit()
            return result

    result = ModelGateway(
        ModelRouteRegistry([_route()]), ChangingTraffic(FakeRedis()),
        ModelUsageService(session), RevokingLease([True] * 4), provider, PolicyEngine(session),
    ).call(_context(session, lease), "summary", {"request": "private"})

    usage = session.scalar(select(AgentModelUsage))
    assert result.status == "aborted_before_send"
    assert provider.calls == 0
    assert usage is not None and usage.status == "aborted_before_send"


def test_expired_request_deadline_does_not_acquire_or_call_provider() -> None:
    session, lease = _run_session()
    provider = RecordingProvider()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    run.run_deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    context = _context(session, lease)

    result = _gateway(session, RevokingLease([True]), provider).call(
        context, "summary", {"message": "private"}
    )

    assert result.status == "aborted_before_send"
    assert provider.calls == 0
    assert session.scalar(select(AgentModelUsage)) is None


def test_model_context_clamps_provider_window_to_remaining_active_budget() -> None:
    """活跃预算而非 created_at 必须成为 Provider permit/HTTP 的共同 deadline。"""
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    run.capability_snapshot_json = {
        "allowed_model_route_ids": ["summary"],
        "execution_policy": {"max_run_seconds": 5},
    }
    run.active_elapsed_ms = 3_000
    session.commit()

    context = _context(session, lease)

    assert context.request_deadline_at is not None
    deadline = context.request_deadline_at.replace(tzinfo=UTC) if context.request_deadline_at.tzinfo is None else context.request_deadline_at
    assert 0 < (deadline - datetime.now(UTC)).total_seconds() <= 2


def test_provider_timeout_is_clamped_to_trusted_deadline_and_lease_window() -> None:
    """Provider 只能获得 route、Run deadline 和 lease 中最短的同步窗口。"""
    class TimeoutRecordingProvider(RecordingProvider):
        timeout_seconds: float | None = None

        def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object:
            self.timeout_seconds = timeout_seconds
            return super().call(route, request, timeout_seconds=timeout_seconds)

    session, lease = _run_session()
    now = datetime.now(UTC)
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    # Run deadline 比 route 的 5 秒短，lease 又更短；两者均来自可信执行上下文。
    run.run_deadline_at = now + timedelta(seconds=2)
    lease.lease_expires_at = now + timedelta(seconds=1)
    session.commit()
    provider = TimeoutRecordingProvider()

    result = _gateway(session, RevokingLease([True, True, True]), provider).call(
        _context(session, lease), "summary", {"message": "private"}
    )

    assert result.status == "succeeded"
    assert provider.timeout_seconds is not None
    assert 0 < provider.timeout_seconds < _route().timeout_seconds
    assert provider.timeout_seconds <= 1


def test_success_settles_provider_tokens_with_frozen_route_prices() -> None:
    session, lease = _run_session()
    provider = RecordingProvider({
        "ok": True,
        "provider_request_id": "provider-request-success",
        "usage": {"input_tokens": 100, "output_tokens": 50},
    })

    result = _gateway(session, RevokingLease([True, True, True, True]), provider).call(
        _context(session, lease), "summary", {"message": "private"}
    )

    usage = session.scalar(select(AgentModelUsage))
    assert result.status == "succeeded"
    assert usage is not None
    assert usage.provider_request_id == "provider-request-success"
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.estimated_cost == 0.2


def test_private_context_metadata_is_not_persisted() -> None:
    session, lease = _run_session()
    secret = "prompt body: do not store"
    _gateway(session, RevokingLease([True, True, True, True]), RecordingProvider()).call(
        _context(session, lease), "summary", {"message": secret}
    )

    usage = session.scalar(select(AgentModelUsage))
    assert usage is not None
    assert usage.capability_snapshot_json == {
        "route_config_version": "v1",
        "capabilities": ["structured_output"],
        "data_residency": "public",
        "max_context_tokens": 8192,
        "max_output_tokens": 4096,
    }
    assert usage.pricing_config_version == "v1"
    assert usage.prompt_id is None
    assert usage.prompt_version is None


def test_context_can_only_be_built_from_authoritative_running_step() -> None:
    session, lease = _run_session()

    with pytest.raises(TypeError):
        ModelCallContext("run-1", "step-forged", 9, lease, 1)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="MODEL_CALL_CONTEXT_UNTRUSTED"):
        ModelCallContext.from_authoritative(session, "run-1", "step-forged", lease)

    assert session.scalar(select(AgentModelUsage)) is None


def test_expired_database_lease_does_not_call_provider() -> None:
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    provider = RecordingProvider()

    result = _gateway(session, LeaseService(session), provider).call(
        _context(session, lease), "summary", {"message": "private"}
    )

    assert (result.status, result.error_code) == (
        "policy_denied", "MODEL_RUN_NOT_EXECUTABLE",
    )
    assert provider.calls == 0


def test_missing_provider_usage_keeps_reserved_cost_and_unset_tokens() -> None:
    session, lease = _run_session()

    result = _gateway(session, RevokingLease([True]), RecordingProvider({"ok": True})).call(
        _context(session, lease), "summary", {"message": "private"}
    )

    usage = session.scalar(select(AgentModelUsage))
    assert result.status == "succeeded"
    assert usage is not None
    assert usage.input_tokens is None and usage.output_tokens is None
    assert usage.estimated_cost == usage.reserved_estimated_cost


def test_separate_session_cannot_overwrite_settled_usage(tmp_path: Path) -> None:
    database = str(tmp_path / "usage.db")
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    session_a, session_b = sessions(), sessions()
    now = datetime.now(UTC)
    session_a.add(AgentRun(
        run_id="run-atomic", agent_id="agent", agent_version="1", package_digest="digest",
        contract_version="1.0.0", business_type="memoir", business_id="business", status="pending",
        dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller",
        tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace", execution_attempt=1,
        lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(minutes=1),
        capability_snapshot_json={"allowed_model_route_ids": ["summary"]},
        run_deadline_at=now - timedelta(seconds=1),
    ))
    session_a.add(AgentStep(
        step_id="step-1", run_id="run-atomic", step_name="summarize", step_type="model",
        status="running", execution_attempt=1, step_attempt=1,
        input_summary={"estimated_input_tokens": 20},
    ))
    session_a.commit()
    lease = LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=now + timedelta(minutes=1), privacy_version=1,
        authorization_version=1,
    )
    usage_id = ModelUsageService(session_a).create_running(
        ModelCallContext.from_authoritative(session_a, "run-atomic", "step-1", lease),
        _route(), "permit-atomic",
    )
    session_a.commit()

    assert ModelUsageService(session_a).settle(usage_id, "aborted_before_send") == "settled"
    session_a.commit()
    assert ModelUsageService(session_b).settle(usage_id, "outcome_unknown") == "already_settled"
    persisted = session_b.scalar(select(AgentModelUsage).where(AgentModelUsage.usage_id == usage_id))
    assert persisted is not None
    assert persisted.status == "aborted_before_send"
    assert persisted.estimated_cost == 0


def test_reconciler_does_not_overwrite_usage_settled_by_another_session(tmp_path: Path) -> None:
    database = str(tmp_path / "reconcile.db")
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    session_a, session_b = sessions(), sessions()
    now = datetime.now(UTC)
    session_a.add(AgentRun(
        run_id="run-reconcile", agent_id="agent", agent_version="1", package_digest="digest",
        contract_version="1.0.0", business_type="memoir", business_id="business", status="pending",
        dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller",
        tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace", execution_attempt=1,
        lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(minutes=1),
        capability_snapshot_json={"allowed_model_route_ids": ["summary"]},
        run_deadline_at=now - timedelta(seconds=1),
    ))
    session_a.add(AgentStep(
        step_id="step-1", run_id="run-reconcile", step_name="summarize", step_type="model",
        status="running", execution_attempt=1, step_attempt=1, input_summary={},
    ))
    session_a.commit()
    lease = LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=now + timedelta(minutes=1), privacy_version=1,
        authorization_version=1,
    )
    context = ModelCallContext.from_authoritative(session_a, "run-reconcile", "step-1", lease)
    usage_id = ModelUsageService(session_a).create_running(context, _route(), "permit-reconcile")
    session_a.commit()

    assert ModelUsageService(session_b).settle(usage_id, "succeeded", input_tokens=1, output_tokens=1, route=_route()) == "settled"
    session_b.commit()
    assert ModelUsageService(session_a).mark_expired_running_unknown(now) == 0
    session_a.commit()
    persisted = session_a.scalar(select(AgentModelUsage).where(AgentModelUsage.usage_id == usage_id))
    assert persisted is not None and persisted.status == "succeeded"


def test_usage_creation_failure_releases_acquired_permit() -> None:
    session, lease = _run_session()
    redis = FakeRedis()
    traffic = ProviderTrafficController(redis)

    class FailingUsage:
        def activate_reservation(self, *args: object) -> bool:
            return False

    gateway = ModelGateway(
        ModelRouteRegistry([_route()]), traffic, FailingUsage(), RevokingLease([True]), RecordingProvider(), PolicyEngine(session)
    )
    result = gateway.call(_context(session, lease), "summary", {"message": "private"})

    assert result.status == "aborted_before_send"
    assert traffic.acquire(_route(), "permit-after-failure").granted


def test_route_not_frozen_in_run_capability_is_rejected_before_acquire() -> None:
    session, lease = _run_session()
    allowed = _route()
    forbidden = ModelRoute(**{**allowed.__dict__, "route_id": "other"})
    redis = FakeRedis()
    provider = RecordingProvider()
    gateway = ModelGateway(
        ModelRouteRegistry([allowed, forbidden]), ProviderTrafficController(redis),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
    )

    result = gateway.call(_context(session, lease), "other", {"message": "private"})

    assert result.status == "route_not_allowed"
    assert provider.calls == 0
    assert redis.permits == {}
    assert session.scalar(select(AgentModelUsage)) is None


def test_call_limit_denial_happens_before_permit_usage_and_provider() -> None:
    """已耗尽的冻结调用额度不得再占用 permit 或向 Provider 发送请求。"""
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    run.capability_snapshot_json = {
        "allowed_model_route_ids": ["summary"],
        "model_policy": {"max_model_calls": 1},
    }
    ModelUsageService(session).create_running(_context(session, lease), _route(), "existing")
    session.commit()
    provider = RecordingProvider()

    result = _gateway(session, RevokingLease([True]), provider).call(
        _context(session, lease), "summary", {"message": "private"}
    )

    assert (result.status, result.error_code) == (
        "policy_denied", "MODEL_CALL_LIMIT_EXCEEDED",
    )
    assert provider.calls == 0
    assert len(session.scalars(select(AgentModelUsage)).all()) == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda run: setattr(run, "cancel_requested_at", datetime.now(UTC)),
        lambda run: setattr(run, "privacy_state", "purge_requested"),
        lambda run: setattr(run, "authorization_version", run.authorization_version + 1),
        lambda run: setattr(run, "fencing_token", run.fencing_token + 1),
    ],
    ids=("cancel_requested", "privacy_not_active", "authorization_changed", "fencing_changed"),
)
def test_non_executable_run_is_policy_denied_before_traffic_usage_and_provider(change: object) -> None:
    """构造 Context 后 Run 失效时，准入必须在所有外部副作用之前拒绝。"""
    session, lease = _run_session()
    context = _context(session, lease)
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    change(run)  # type: ignore[operator]
    session.commit()
    provider = RecordingProvider()
    traffic = ProviderTrafficController(FakeRedis())
    acquire_calls = 0
    original_acquire = traffic.acquire

    def count_acquire(*args: object, **kwargs: object) -> object:
        nonlocal acquire_calls
        acquire_calls += 1
        return original_acquire(*args, **kwargs)

    traffic.acquire = count_acquire  # type: ignore[method-assign]
    gateway = ModelGateway(
        ModelRouteRegistry([_route()]), traffic, ModelUsageService(session),
        RevokingLease([True]), provider, PolicyEngine(session),
    )

    result = gateway.call(context, "summary", {"message": "private"})

    assert (result.status, result.error_code) == (
        "policy_denied", "MODEL_RUN_NOT_EXECUTABLE",
    )
    assert acquire_calls == 0
    assert provider.calls == 0
    assert session.scalar(select(AgentModelUsage)) is None


def test_policy_denial_does_not_acquire_traffic_permit() -> None:
    """已耗尽额度的拒绝不得调用 traffic.acquire。"""
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    run.capability_snapshot_json = {
        "allowed_model_route_ids": ["summary"],
        "model_policy": {"max_model_calls": 0},
    }
    session.commit()
    traffic = ProviderTrafficController(FakeRedis())
    acquire_calls = 0
    original_acquire = traffic.acquire

    def count_acquire(*args: object, **kwargs: object) -> object:
        nonlocal acquire_calls
        acquire_calls += 1
        return original_acquire(*args, **kwargs)

    traffic.acquire = count_acquire  # type: ignore[method-assign]
    result = ModelGateway(
        ModelRouteRegistry([_route()]), traffic, ModelUsageService(session),
        RevokingLease([True]), RecordingProvider(), PolicyEngine(session),
    ).call(_context(session, lease), "summary", {"message": "private"})

    assert (result.status, result.error_code) == (
        "policy_denied", "MODEL_CALL_LIMIT_EXCEEDED",
    )
    assert acquire_calls == 0


def test_second_session_cannot_pass_call_limit_after_first_reserves_budget(tmp_path: Path) -> None:
    """独立 Session 的后到调用必须看到先到调用已占用的同 Run 预算。"""
    database = str(tmp_path / "policy-budget.db")
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    session_a, session_b = sessions(), sessions()
    now = datetime.now(UTC)
    session_a.add(AgentRun(
        run_id="run-budget", agent_id="agent", agent_version="1", package_digest="digest",
        contract_version="1.0.0", business_type="memoir", business_id="business", status="pending",
        dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller",
        tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace", execution_attempt=1,
        lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(minutes=1),
        capability_snapshot_json={
            "allowed_model_route_ids": ["summary"], "model_policy": {"max_model_calls": 1},
        }, run_deadline_at=now + timedelta(days=1),
    ))
    session_a.add(AgentStep(
        step_id="step-budget", run_id="run-budget", step_name="summarize", step_type="model",
        status="running", execution_attempt=1, step_attempt=1,
        input_summary={"estimated_input_tokens": 20},
    ))
    session_a.commit()
    lease = LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=now + timedelta(minutes=1), privacy_version=1, authorization_version=1,
    )
    first = _gateway(session_a, RevokingLease([True, True, True]), RecordingProvider())
    assert first.call(
        ModelCallContext.from_authoritative(session_a, "run-budget", "step-budget", lease),
        "summary", {"message": "private"},
    ).status == "succeeded"
    session_a.commit()
    traffic_b = ProviderTrafficController(FakeRedis())
    acquire_calls = 0
    original_acquire = traffic_b.acquire

    def count_acquire(*args: object, **kwargs: object) -> object:
        nonlocal acquire_calls
        acquire_calls += 1
        return original_acquire(*args, **kwargs)

    traffic_b.acquire = count_acquire  # type: ignore[method-assign]
    second = ModelGateway(
        ModelRouteRegistry([_route()]), traffic_b, ModelUsageService(session_b),
        RevokingLease([True]), RecordingProvider(), PolicyEngine(session_b),
    ).call(
        ModelCallContext.from_authoritative(session_b, "run-budget", "step-budget", lease),
        "summary", {"message": "private"},
    )

    assert (second.status, second.error_code) == (
        "policy_denied", "MODEL_CALL_LIMIT_EXCEEDED",
    )
    assert acquire_calls == 0


@pytest.mark.parametrize(
    ("model_policy", "expected_code"),
    [
        ({"max_model_calls": 1}, "MODEL_CALL_LIMIT_EXCEEDED"),
        ({"max_model_cost": 0.03}, "MODEL_COST_LIMIT_EXCEEDED"),
    ],
    ids=("call_limit", "cost_limit"),
)
def test_concurrent_sessions_only_one_call_acquires_traffic_for_one_run_budget(
    tmp_path: Path, model_policy: dict[str, float | int], expected_code: str,
) -> None:
    """先到调用未返回 Provider 时，另一 Session 也不得越过同 Run 的次数或成本额度。"""
    database = str(tmp_path / "policy-concurrent-budget.db")
    engine = create_engine(f"sqlite:///{database}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    setup = sessions()
    now = datetime.now(UTC)
    setup.add(AgentRun(
        run_id="run-concurrent", agent_id="agent", agent_version="1", package_digest="digest",
        contract_version="1.0.0", business_type="memoir", business_id="business", status="pending",
        dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller",
        tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback",
        business_connector_id="connector", trace_id="trace", execution_attempt=1,
        lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(minutes=1),
        capability_snapshot_json={
            "allowed_model_route_ids": ["summary"], "model_policy": model_policy,
        }, run_deadline_at=now + timedelta(days=1),
    ))
    setup.add(AgentStep(
        step_id="step-concurrent", run_id="run-concurrent", step_name="summarize", step_type="model",
        status="running", execution_attempt=1, step_attempt=1,
        input_summary={"estimated_input_tokens": 20},
    ))
    setup.commit()
    setup.close()
    lease = LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=now + timedelta(minutes=1), privacy_version=1, authorization_version=1,
    )
    first_sent, release_first, second_acquired = Event(), Event(), Event()

    class BlockingProvider(RecordingProvider):
        def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object:
            self.calls += 1
            first_sent.set()
            assert release_first.wait(3)
            return {"ok": True}

    provider = BlockingProvider()
    traffic = ProviderTrafficController(FakeRedis())
    acquire_calls = 0
    original_acquire = traffic.acquire

    def count_acquire(*args: object, **kwargs: object) -> object:
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls == 2:
            second_acquired.set()
        return original_acquire(*args, **kwargs)

    traffic.acquire = count_acquire  # type: ignore[method-assign]
    results: list[object] = []

    def invoke() -> None:
        session = sessions()
        try:
            context = ModelCallContext.from_authoritative(
                session, "run-concurrent", "step-concurrent", lease,
            )
            results.append(ModelGateway(
                ModelRouteRegistry([_route()]), traffic, ModelUsageService(session),
                RevokingLease([True, True, True]), provider, PolicyEngine(session),
            ).call(context, "summary", {"message": "private"}))
            session.commit()
        finally:
            session.close()

    first, second = Thread(target=invoke), Thread(target=invoke)
    first.start()
    assert first_sent.wait(3)
    second.start()
    try:
        assert not second_acquired.wait(0.5)
    finally:
        release_first.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert acquire_calls == 1
    assert provider.calls == 1
    denied = [result for result in results if getattr(result, "status", None) == "policy_denied"]
    assert len(denied) == 1
    assert denied[0].error_code == expected_code


def test_cost_limit_counts_unknown_reserved_cost_and_current_input_reservation() -> None:
    """未知结果仍占保守成本，当前调用也必须先预留输入成本。"""
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    run.capability_snapshot_json = {
        "allowed_model_route_ids": ["summary"],
        "model_policy": {"max_model_cost": 1.0},
    }
    session.add(AgentModelUsage(
        id=101, usage_id="unknown-usage", run_id="run-1", step_id="step-1",
        execution_attempt=1, model_attempt=1, status="outcome_unknown",
        reserved_estimated_cost=0.99, estimated_cost=None,
    ))
    session.commit()
    provider = RecordingProvider()

    result = _gateway(session, RevokingLease([True]), provider).call(
        _context(session, lease), "summary", {"message": "private"}
    )

    assert (result.status, result.error_code) == (
        "policy_denied", "MODEL_COST_LIMIT_EXCEEDED",
    )
    assert provider.calls == 0
    assert len(session.scalars(select(AgentModelUsage)).all()) == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda step: setattr(step, "status", "succeeded"),
        lambda step: setattr(step, "execution_attempt", step.execution_attempt + 1),
        lambda step: setattr(step, "step_attempt", step.step_attempt + 1),
    ],
    ids=("step_not_running", "execution_attempt_changed", "model_attempt_changed"),
)
def test_stale_step_context_cannot_reserve_or_acquire(change: object) -> None:
    """Context 构造后 Step 改变时，预留与 permit 都必须保持零副作用。"""
    session, lease = _run_session()
    context = _context(session, lease)
    step = session.scalar(select(AgentStep).where(AgentStep.step_id == "step-1"))
    assert step is not None
    change(step)  # type: ignore[operator]
    session.commit()
    redis = FakeRedis()
    provider = RecordingProvider()

    result = ModelGateway(
        ModelRouteRegistry([_route()]), ProviderTrafficController(redis),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
    ).call(context, "summary", {"private": "must not persist"})

    assert (result.status, result.error_code) == (
        "policy_denied", "MODEL_RUN_NOT_EXECUTABLE",
    )
    assert session.scalar(select(AgentModelUsage)) is None
    assert "acquire" not in redis.operations
    assert provider.calls == 0


def test_step_revoked_after_reservation_cannot_reach_provider() -> None:
    """HTTP 紧邻检查必须重新验证权威 Step，而非只相信 lease mock。"""
    session, lease = _run_session()
    provider = RecordingProvider()

    class RevokingUsage(ModelUsageService):
        def activate_reservation(self, usage_id: str, permit_id: str) -> bool:
            activated = super().activate_reservation(usage_id, permit_id)
            step = self._session.scalar(select(AgentStep).where(AgentStep.step_id == "step-1"))
            assert step is not None
            step.status = "succeeded"
            self._session.commit()
            return activated

    result = ModelGateway(
        ModelRouteRegistry([_route()]), ProviderTrafficController(FakeRedis()),
        RevokingUsage(session), RevokingLease([True]), provider, PolicyEngine(session),
    ).call(_context(session, lease), "summary", {"private": "must not persist"})

    assert result.status == "aborted_before_send"
    assert provider.calls == 0


def test_gateway_records_registered_prompt_reference_without_template_body() -> None:
    session, lease = _run_session()
    provider = RecordingProvider()
    private_route = ModelRoute(**{
        **_route().__dict__,
        "capabilities": frozenset({"structured_output", "private_residency"}),
        "data_residency": "private",
    })

    result = _gateway(session, RevokingLease([True]), provider, route=private_route).call(
        _context(session, lease), "summary", {"request": "private"},
        prompt=PromptDefinition(
            prompt_id="highlight-extract", version="v1", owner_agent="memoir_agent",
            input_schema="input", output_schema="output", model_policy="strict",
            guardrail_policy="private_first", status="active", template="private template",
        ),
    )

    usage = session.scalar(select(AgentModelUsage))
    assert result.status == "succeeded"
    assert usage is not None
    assert usage.prompt_id == "highlight-extract"
    assert usage.prompt_version == "v1"
    assert usage.thinking_summary_json == {
        "thinking_enabled": False,
        "max_output_tokens": 512,
        "input_token_budget": 7680,
        "normalization_version": "v1",
    }
    assert "private template" not in str(usage.thinking_summary_json)


def test_thinking_summary_rejects_reasoning_text_before_persistence() -> None:
    session, lease = _run_session()
    usage_id = ModelUsageService(session).create_running(_context(session, lease), _route(), "permit")

    with pytest.raises(ValueError, match="THINKING_SUMMARY_INVALID"):
        ModelUsageService(session).attach_thinking_summary(usage_id, {
            "thinking_enabled": True,
            "max_output_tokens": 512,
            "input_token_budget": 7680,
            "normalization_version": "v1",
            "reasoning": "不得保存的隐藏推理",
        })

    usage = session.scalar(select(AgentModelUsage).where(AgentModelUsage.usage_id == usage_id))
    assert usage is not None and usage.thinking_summary_json is None


def test_gateway_disables_private_first_prompt_without_private_route_capability() -> None:
    """私有优先 Prompt 不得静默路由到未声明私有驻留的 Provider。"""
    session, lease = _run_session()
    provider = RecordingProvider()

    result = _gateway(session, RevokingLease([True]), provider).call(
        _context(session, lease),
        "summary",
        {"request": "private"},
        prompt=PromptDefinition(
            prompt_id="highlight-extract", version="v1", owner_agent="memoir_agent",
            input_schema="input", output_schema="output", model_policy="strict",
            guardrail_policy="private_first", status="active", template="private template",
        ),
    )

    assert (result.status, result.error_code) == ("capability_disabled", "MODEL_CAPABILITY_UNAVAILABLE")
    assert provider.calls == 0
    assert session.scalar(select(AgentModelUsage)) is None


def test_model_capability_evaluator_rejects_residency_window_and_redis_without_side_effects() -> None:
    """能力发现只计算可信配置；任一前置条件不满足都不能宣称模型可用。"""
    prompt = PromptDefinition(
        prompt_id="highlight-extract", version="v1", owner_agent="memoir_agent",
        input_schema="input", output_schema="output", model_policy="strict",
        guardrail_policy="private_first", status="active", template="trusted template",
    )
    evaluator = ModelCapabilityEvaluator(ModelPolicyRegistry.default())
    public_route = _route()
    private_route = ModelRoute(**{
        **public_route.__dict__,
        "capabilities": frozenset({"structured_output", "private_residency"}),
        "data_residency": "private",
        "max_context_tokens": 520,
        "max_output_tokens": 512,
    })

    assert evaluator.available(public_route, prompt, estimated_input_tokens=0, redis_available=True) is False
    assert evaluator.available(private_route, prompt, estimated_input_tokens=9, redis_available=True) is False
    assert evaluator.available(private_route, prompt, estimated_input_tokens=8, redis_available=False) is False


def test_gateway_disables_prompt_when_trusted_context_exceeds_route_window() -> None:
    """即使能力合规，可信输入预算加策略输出上限超过 route 窗口也不得发送。"""
    session, lease = _run_session()
    provider = RecordingProvider()
    narrow_private_route = ModelRoute(**{
        **_route().__dict__,
        "capabilities": frozenset({"structured_output", "private_residency"}),
        "data_residency": "private",
        "max_context_tokens": 520,
        "max_output_tokens": 512,
    })

    result = _gateway(session, RevokingLease([True]), provider, route=narrow_private_route).call(
        _context(session, lease),
        "summary",
        {"request": "private"},
        prompt=PromptDefinition(
            prompt_id="highlight-extract", version="v1", owner_agent="memoir_agent",
            input_schema="input", output_schema="output", model_policy="strict",
            guardrail_policy="private_first", status="active", template="private template",
        ),
    )

    assert (result.status, result.error_code) == ("capability_disabled", "MODEL_CAPABILITY_UNAVAILABLE")
    assert provider.calls == 0


def test_gateway_returns_capability_disabled_when_shared_traffic_control_is_unavailable() -> None:
    """Redis 失效不得暴露为可重试 Provider 调用，Runner 应直接走模板 fallback。"""
    class BrokenRedis:
        def eval(self, *args: object) -> object:
            raise ConnectionError("unavailable")

    session, lease = _run_session()
    result = ModelGateway(
        ModelRouteRegistry([_route()]),
        ProviderTrafficController(BrokenRedis()),
        ModelUsageService(session),
        RevokingLease([True]),
        RecordingProvider(),
        PolicyEngine(session),
    ).call(_context(session, lease), "summary", {"request": "private"})

    assert (result.status, result.error_code) == ("capability_disabled", "MODEL_TRAFFIC_UNAVAILABLE")
    assert session.scalar(select(AgentModelUsage)) is None


def test_gateway_uses_allowed_fallback_route_with_a_separate_permit_after_429() -> None:
    """主 route 429 后只能使用部署声明且 Run 快照允许的 fallback route。"""
    class FallbackProvider:
        def __init__(self) -> None:
            self.route_ids: list[str] = []

        def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object:
            self.route_ids.append(route.route_id)
            if route.route_id == "primary":
                request_obj = httpx.Request("POST", "https://provider.example/v1/chat")
                # 超出当前可信窗口时不得等待主 route，应直接尝试部署 fallback。
                response = httpx.Response(429, headers={"Retry-After": "999"}, request=request_obj)
                raise httpx.HTTPStatusError("rate limited", request=request_obj, response=response)
            return {"ok": True}

    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    run.capability_snapshot_json = {"allowed_model_route_ids": ["primary", "fallback"]}
    session.commit()
    primary = ModelRoute(**{**_route().__dict__, "route_id": "primary", "fallback_route_id": "fallback"})
    fallback = ModelRoute(**{**_route().__dict__, "route_id": "fallback", "rate_limit_key": "provider:fallback"})
    provider = FallbackProvider()
    redis = FakeRedis()

    result = ModelGateway(
        ModelRouteRegistry([primary, fallback]), ProviderTrafficController(redis),
        ModelUsageService(session), RevokingLease([True] * 8), provider, PolicyEngine(session),
    ).call(_context(session, lease), "primary", {"request": "private"})

    assert result.status == "succeeded"
    assert provider.route_ids == ["primary", "fallback"]
    assert len(redis.permits) == 2
    usages = list(session.scalars(select(AgentModelUsage)))
    assert sorted(usage.model_attempt for usage in usages) == [1, 2]


def test_route_governance_rejects_in_trusted_priority_before_provider() -> None:
    """紧急禁用必须先于租户、逻辑 policy 与部署 route allowlist 生效。"""
    prompt = PromptDefinition(
        prompt_id="highlight-extract", version="v1", owner_agent="memoir_agent",
        input_schema="input", output_schema="output", model_policy="balanced",
        guardrail_policy="strict", status="active", template="trusted template",
    )
    scenarios = [
        (
            {
                "enabled": False,
                "allowed_tenant_ids": frozenset({"other"}),
                "allowed_model_policies": frozenset({"strict"}),
            },
            {"allowed_model_route_ids": []},
            ("governance_denied", "MODEL_ROUTE_EMERGENCY_DISABLED"),
        ),
        (
            {
                "allowed_tenant_ids": frozenset({"other"}),
                "allowed_model_policies": frozenset({"strict"}),
            },
            {"allowed_model_route_ids": []},
            ("governance_denied", "MODEL_ROUTE_TENANT_DENIED"),
        ),
        (
            {
                "allowed_tenant_ids": frozenset({"tenant"}),
                "allowed_model_policies": frozenset({"strict"}),
            },
            {"allowed_model_route_ids": []},
            ("governance_denied", "MODEL_ROUTE_POLICY_DENIED"),
        ),
        (
            {
                "allowed_tenant_ids": frozenset({"tenant"}),
                "allowed_model_policies": frozenset({"balanced"}),
            },
            {"allowed_model_route_ids": []},
            ("route_not_allowed", None),
        ),
    ]

    for route_overrides, snapshot, expected in scenarios:
        session, lease = _run_session()
        run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
        assert run is not None
        run.capability_snapshot_json = snapshot
        session.commit()
        provider = RecordingProvider()
        route = ModelRoute(**{**_route().__dict__, **route_overrides})

        result = _gateway(
            session, RevokingLease([True]), provider, route=route
        ).call(
            _context(session, lease), "summary", {"request": "private"}, prompt=prompt
        )

        assert (result.status, result.error_code) == expected
        assert provider.calls == 0
        assert session.scalar(select(AgentModelUsage)) is None


def test_explicit_fallback_cannot_escape_tenant_or_policy_governance() -> None:
    """部署 fallback 即使在 Run allowlist 中，也必须重新经过同一治理链。"""
    class FallbackProvider:
        def __init__(self) -> None:
            self.route_ids: list[str] = []

        def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object:
            self.route_ids.append(route.route_id)
            request_obj = httpx.Request("POST", "https://provider.example/v1/chat")
            response = httpx.Response(
                429, headers={"Retry-After": "999"}, request=request_obj
            )
            raise httpx.HTTPStatusError(
                "rate limited", request=request_obj, response=response
            )

    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    run.capability_snapshot_json = {"allowed_model_route_ids": ["primary", "fallback"]}
    session.commit()
    primary = ModelRoute(
        **{
            **_route().__dict__,
            "route_id": "primary",
            "fallback_route_id": "fallback",
            "allowed_tenant_ids": frozenset({"tenant"}),
            "allowed_model_policies": frozenset({"balanced"}),
        }
    )
    fallback = ModelRoute(
        **{
            **_route().__dict__,
            "route_id": "fallback",
            "rate_limit_key": "provider:fallback",
            "allowed_tenant_ids": frozenset({"other"}),
            "allowed_model_policies": frozenset({"balanced"}),
        }
    )
    prompt = PromptDefinition(
        prompt_id="highlight-extract", version="v1", owner_agent="memoir_agent",
        input_schema="input", output_schema="output", model_policy="balanced",
        guardrail_policy="strict", status="active", template="trusted template",
    )
    provider = FallbackProvider()

    result = ModelGateway(
        ModelRouteRegistry([primary, fallback]), ProviderTrafficController(FakeRedis()),
        ModelUsageService(session), RevokingLease([True] * 8), provider, PolicyEngine(session),
    ).call(
        _context(session, lease), "primary", {"request": "private"}, prompt=prompt
    )

    assert (result.status, result.error_code) == (
        "governance_denied",
        "MODEL_ROUTE_TENANT_DENIED",
    )
    assert provider.route_ids == ["primary"]


def test_gateway_waits_once_within_deadline_then_reacquires_a_new_permit() -> None:
    """429 冷却未超过可信窗口时只等待一次，重试必须使用新的 permit。"""
    class RetryProvider:
        def __init__(self) -> None:
            self.calls = 0

        def call(self, route: ModelRoute, request: object, *, timeout_seconds: float) -> object:
            self.calls += 1
            if self.calls == 1:
                request_obj = httpx.Request("POST", "https://provider.example/v1/chat")
                response = httpx.Response(429, headers={"Retry-After": "0.1"}, request=request_obj)
                raise httpx.HTTPStatusError("rate limited", request=request_obj, response=response)
            return {"ok": True}

    session, lease = _run_session()
    slept: list[float] = []
    redis = FakeRedis()
    provider = RetryProvider()
    ticks = iter((0.0, 0.1))
    def wait(seconds: float) -> None:
        slept.append(seconds)
        time.sleep(seconds)

    result = ModelGateway(
        ModelRouteRegistry([_route()]), ProviderTrafficController(redis),
        ModelUsageService(session), RevokingLease([True] * 8), provider, PolicyEngine(session),
        sleep=wait,
        monotonic=lambda: next(ticks),
    ).call(_context(session, lease), "summary", {"request": "private"})

    assert result.status == "succeeded"
    assert provider.calls == 2
    assert slept == [0.1]
    assert len(redis.permits) == 2
    usages = list(session.scalars(select(AgentModelUsage)))
    assert sorted(usage.model_attempt for usage in usages) == [1, 2]
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    session.refresh(run)
    assert run.active_elapsed_ms == 100


def test_gateway_freezes_max_tokens_and_rejects_next_attempt_after_observed_usage() -> None:
    """实际 token 用量必须计入 Run 冻结上限，不能由请求参数扩大。"""
    session, lease = _run_session()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert run is not None
    run.capability_snapshot_json = {
        "allowed_model_route_ids": ["summary"],
        "model_policy": {"max_tokens": 25},
    }
    session.commit()
    provider = RecordingProvider(
        {"ok": True, "usage": {"input_tokens": 10, "output_tokens": 10}}
    )
    gateway = _gateway(session, RevokingLease([True] * 8), provider)

    assert (
        gateway.call(_context(session, lease), "summary", {"request": "private"}).status
        == "succeeded"
    )
    rejected = gateway.call(_context(session, lease), "summary", {"request": "private"})

    assert (rejected.status, rejected.error_code) == ("policy_denied", "MODEL_TOKEN_LIMIT_EXCEEDED")
    assert provider.calls == 1


def test_model_governance_denial_logs_exclude_request_body(caplog: pytest.LogCaptureFixture) -> None:
    """policy、熔断与 draining 的拒绝日志只含安全标识，不能回显请求正文。"""
    caplog.set_level(logging.INFO)
    request = {"prompt": "private-body-987", "token": "secret-token-456"}

    policy_session, policy_lease = _run_session()
    policy_run = policy_session.scalar(select(AgentRun).where(AgentRun.run_id == "run-1"))
    assert policy_run is not None
    policy_run.capability_snapshot_json = {
        "allowed_model_route_ids": ["summary"], "model_policy": {"max_model_calls": 0},
    }
    policy_session.commit()
    assert _gateway(policy_session, RevokingLease([True]), RecordingProvider()).call(
        _context(policy_session, policy_lease), "summary", request,
    ).status == "policy_denied"

    circuit_session, circuit_lease = _run_session()
    guarded = ModelRoute(**{
        **_route().__dict__, "circuit_failure_threshold": 1, "circuit_open_seconds": 30,
    })
    traffic = ProviderTrafficController(FakeRedis())
    assert traffic.record_circuit_failure(guarded).status == "circuit_opened"
    assert ModelGateway(
        ModelRouteRegistry([guarded]), traffic, ModelUsageService(circuit_session),
        RevokingLease([True]), RecordingProvider(), PolicyEngine(circuit_session),
    ).call(_context(circuit_session, circuit_lease), "summary", request).status == "circuit_open"

    class DrainingGuard:
        def permits_new_call(self, context: ModelCallContext) -> bool:
            return False

    drain_session, drain_lease = _run_session()
    assert ModelGateway(
        ModelRouteRegistry([_route()]), ProviderTrafficController(FakeRedis()),
        ModelUsageService(drain_session), RevokingLease([True]), RecordingProvider(),
        PolicyEngine(drain_session), call_guard=DrainingGuard(),
    ).call(_context(drain_session, drain_lease), "summary", request).status == "aborted_before_send"

    assert "模型策略拒绝" in caplog.text
    assert "模型熔断拒绝" in caplog.text
    assert "worker_draining" in caplog.text
    assert "private-body-987" not in caplog.text
    assert "secret-token-456" not in caplog.text


def test_created_run_freezes_server_route_and_residency_governance() -> None:
    session, _ = _run_session()
    session.add(AgentDefinition(
        agent_id="created-agent", version="1", runtime_type="workflow",
        definition_json={"allowed_business_types": ["memoir"], "workflow_nodes": []},
        package_digest="digest", contract_version="1.0.0", status="active",
        status_changed_at=datetime.now(UTC), status_changed_by="test", status_change_reason="fixture",
    ))
    created = AgentRunService(
        session,
        trusted_model_route_ids=("summary",),
        required_model_data_residency="private",
    ).create(
        CreateRunCommand(
            agent_id="created-agent", agent_version="1", business_type="memoir",
            business_id="business", start_mode="auto",
            input={
                "allowed_model_route_ids": ["other"],
                "required_model_data_residency": "public",
                "provider": "forged",
                "model": "forged",
                "base_url": "https://forged.invalid",
                "key": "forged",
            },
            callback_target_id="callback",
            business_connector_id="connector",
        ), "caller", "tenant", "create-server-routes",
    )
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == created.run_id))
    assert run is not None
    assert run.capability_snapshot_json is not None
    assert run.capability_snapshot_json["allowed_model_route_ids"] == ["summary"]
    assert run.capability_snapshot_json["required_model_data_residency"] == "private"
    assert all(
        key not in run.capability_snapshot_json
        for key in ("provider", "model", "base_url", "key")
    )
    run.dispatch_state = "claimed"
    run.execution_attempt = 1
    run.lease_owner = "worker"
    run.fencing_token = 1
    run.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    session.add(AgentStep(
        step_id="created-step", run_id=run.run_id, step_name="model", step_type="model",
        status="running", execution_attempt=1, step_attempt=1, input_summary={},
    ))
    session.commit()
    lease = LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1), privacy_version=1,
        authorization_version=1,
    )
    context = ModelCallContext.from_authoritative(session, run.run_id, "created-step", lease)
    allowed = ModelRoute(
        **{
            **_route().__dict__,
            "data_residency": "private",
            "capabilities": frozenset({"structured_output", "private_residency"}),
        }
    )
    forbidden = ModelRoute(**{**allowed.__dict__, "route_id": "other"})
    redis = FakeRedis()
    provider = RecordingProvider()
    gateway = ModelGateway(
        ModelRouteRegistry([allowed, forbidden]), ProviderTrafficController(redis),
        ModelUsageService(session), RevokingLease([True]), provider, PolicyEngine(session),
    )

    assert gateway.call(context, "summary", {"message": "private"}).status == "succeeded"
    assert gateway.call(context, "other", {"message": "private"}).status == "route_not_allowed"
    assert provider.calls == 1


def _openai_route() -> ModelRoute:
    """OpenAI 兼容路由：与默认 route 同一公网域名，仅 provider/model 不同。"""
    return ModelRoute(**{
        **_route().__dict__,
        "provider": "openai_compatible",
        "model": "deepseek-chat",
    })


def test_http_provider_adapter_openai_compatible_adds_bearer_and_model() -> None:
    """OpenAI 兼容调用必须注入部署 route 的 model 并携带 Bearer 密钥头。"""
    from app.runtime.model_gateway import HttpProviderAdapter

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{\"source_refs\": []}"}}]},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = HttpProviderAdapter(
        client,
        peer_ip_provider=lambda: "8.8.8.8",
        reset_peer_ip=lambda: None,
        # api_keys 按 route_id 索引；_openai_route() 继承默认 route_id="summary"。
        api_keys={"summary": "sk-test-placeholder"},
    ).call(_openai_route(), {"messages": []}, timeout_seconds=1)

    assert result == "{\"source_refs\": []}"
    assert captured["authorization"] == "Bearer sk-test-placeholder"
    # model 只来自部署 route 配置，请求侧无法覆盖 provider/model。
    assert captured["body"] == {"messages": [], "model": "deepseek-chat"}


def test_http_provider_adapter_openai_compatible_without_key_sends_no_auth() -> None:
    """api_keys 未覆盖该 route 时不携带 Authorization 头，仍按兼容格式解包。"""
    from app.runtime.model_gateway import HttpProviderAdapter

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = HttpProviderAdapter(
        client,
        peer_ip_provider=lambda: "8.8.8.8",
        reset_peer_ip=lambda: None,
    ).call(_openai_route(), {"messages": []}, timeout_seconds=1)

    assert result == "ok"
    assert captured["authorization"] is None


def test_http_provider_adapter_openai_compatible_rejects_malformed_choices() -> None:
    """choices 缺失或形状错误时按无效响应拒绝，不把 envelope 泄入 Runtime。"""
    from app.runtime.model_gateway import HttpProviderAdapter

    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": []}, request=request),
    ))

    with pytest.raises(ValueError, match="Provider JSON 响应格式无效"):
        HttpProviderAdapter(
            client,
            peer_ip_provider=lambda: "8.8.8.8",
            reset_peer_ip=lambda: None,
        ).call(_openai_route(), {"messages": []}, timeout_seconds=1)


def test_http_provider_adapter_plain_provider_keeps_legacy_contract() -> None:
    """非 openai_compatible provider 保持原契约：不注入 model、不解包 choices。"""
    from app.runtime.model_gateway import HttpProviderAdapter

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"source_refs": []}, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = HttpProviderAdapter(
        client,
        peer_ip_provider=lambda: "8.8.8.8",
        reset_peer_ip=lambda: None,
    ).call(_route(), {"messages": []}, timeout_seconds=1)

    assert result == {"source_refs": []}
    assert captured["body"] == {"messages": []}
