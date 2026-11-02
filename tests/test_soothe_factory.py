"""SOOTHE 可信注册与配置边界：缺配置必须经真实队列收敛为无正文失败。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.worker as worker
from app.agents.registry import get_executor_factory, is_executable_agent
from app.agents.soothe_agent import bootstrap as soothe_bootstrap
from app.db.sqlalchemy_db import Base
from app.models import AgentDefinition, AgentRun, CallbackEvent
from app.runtime.executor import WorkflowExecutor
from app.runtime.interfaces import LeaseContext
from app.runtime.test_harness import RuntimeDependencies
from app.services.run_queue_service import RunQueueService
from tests.test_provider_traffic_controller import FakeRedis


@pytest.mark.parametrize("raw", [
    "{}", "[]", "invalid-json", '{"compose_turn":""}',
    '{"compose_turn":"fixture","extra":"fixture"}',
    '{"other":"fixture"}', '{"compose_turn":1}',
])
def test_soothe_model_routes_reject_incomplete_or_extra_nodes(soothe_run, monkeypatch, raw: str) -> None:
    """节点精确集合由 SOOTHE 装配校验；非法配置不能进入公共模型网关。"""
    session, run = soothe_run
    try:
        raw = json.dumps({"soothe_agent": json.loads(raw)})
    except json.JSONDecodeError:
        pass
    configuration = worker.settings.model_copy(update={
        "AGENT_MODEL_NODE_ROUTES_JSON": raw,
        "RUNTIME_BUSINESS_CONNECTORS_JSON": json.dumps({"couple_diary_backend": {
            "enabled": True, "base_url": "https://business.example.test",
            "runtime_id": "runtime", "key_id": "fixture", "secret": "fixture",
        }}),
    })

    def unexpected_model_build(*args, **kwargs):
        raise AssertionError("invalid SOOTHE nodes reached model assembly")

    monkeypatch.setattr(soothe_bootstrap, "build_model_gateway", unexpected_model_build)
    lease = LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        privacy_version=1, authorization_version=1,
    )
    executor = soothe_bootstrap.configured_soothe_executor(
        session, run.run_id, lease, dependencies=_dependencies(configuration),
    )
    result = executor.run(run.run_id, lease)
    assert (result.status, result.error_code) == ("failed", "SOOTHE_AGENT_NOT_CONFIGURED")


def test_soothe_model_routes_accept_only_compose_turn() -> None:
    configuration = worker.settings.model_copy(update={
        "AGENT_MODEL_NODE_ROUTES_JSON": '{"soothe_agent":{"compose_turn":"fixture"}}',
    })
    assert configuration.agent_model_node_routes("soothe_agent") == {"compose_turn": "fixture"}


def test_soothe_identity_has_trusted_factory() -> None:
    assert is_executable_agent("soothe_agent")
    assert callable(get_executor_factory("soothe_agent"))


@pytest.fixture
def soothe_run():
    """隔离 SQLite 中只保存业务引用和受信身份，绝不构造问题/回答正文。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        now = datetime.now(UTC)
        run = AgentRun(
            run_id="qa-factory-run", agent_id="soothe_agent", agent_version="1.0.0",
            package_digest="qa-digest", contract_version="1.0.0",
            business_type="soothe_reply", business_id="turn",
            status="pending", dispatch_state="queued",
            input_json={"session_id": "conversation", "turn_id": "turn"},
            authorization_version=1, caller_id="caller", tenant_id="tenant",
            create_idempotency_key="key", callback_target_id="callback",
            business_connector_id="couple_diary_backend", trace_id="qa-trace",
            execution_attempt=0, fencing_token=0,
            run_deadline_at=now + timedelta(minutes=5),
        )
        session.add_all([run, AgentDefinition(
            agent_id="soothe_agent", version="1.0.0", runtime_type="workflow",
            definition_json={}, package_digest="qa-digest", contract_version="1.0.0",
            status="active", status_changed_at=now,
            status_changed_by="test", status_change_reason="fixture",
        )])
        session.commit()
        yield session, run
    engine.dispose()


def _dependencies(configuration):
    return RuntimeDependencies(
        settings=configuration, session_factory=None, clock=None,
        callback_client=None, tool_client=None, transport_verifier=None,
    )


def _model_routes_json() -> str:
    """显式有效的部署路由让各缺配置用例真正触达对应门禁。"""
    return json.dumps([{
        "route_id": "fixture", "provider": "provider", "model": "model",
        "endpoint": "https://model.example.test/v1", "rate_limit_key": "qa-fixture",
        "max_concurrency": 1, "rpm_limit": 30, "tpm_limit": 10000,
        "timeout_seconds": 1, "permit_ttl_seconds": 2, "settle_margin_seconds": 0,
        "price_unit": "usd_per_1k_tokens", "input_price": 0, "output_price": 0,
        "route_config_version": "v1", "pricing_config_version": "v1",
        "capabilities": ["structured_output", "private_residency"],
        "data_residency": "private", "max_context_tokens": 2048,
        "max_output_tokens": 1024, "enabled": True,
        "allowed_tenant_ids": ["*"], "allowed_model_policies": ["balanced"],
    }])


@pytest.mark.parametrize("missing", [
    "connector", "connector_key", "connector_json", "node_routes", "unknown_route", "redis",
])
def test_missing_soothe_configuration_finishes_real_queue_in_failed_state(
    soothe_run, missing: str, caplog,
) -> None:
    session, run = soothe_run
    connectors = {
        "couple_diary_backend": {
            "enabled": True, "base_url": "https://business.example.test",
            "runtime_id": "runtime", "key_id": "test", "secret": "test-secret",
        },
    }
    configuration = worker.settings.model_copy(update={
        "RUNTIME_BUSINESS_CONNECTORS_JSON": json.dumps(connectors),
        "AGENT_MODEL_NODE_ROUTES_JSON": '{"soothe_agent":{"compose_turn":"fixture"}}',
        "MODEL_ROUTES_JSON": _model_routes_json(),
        "RUNTIME_REDIS_URL": "",
    })
    if missing == "connector":
        configuration.RUNTIME_BUSINESS_CONNECTORS_JSON = "{}"
    elif missing == "connector_key":
        connectors["couple_diary_backend"]["secret"] = ""
        configuration.RUNTIME_BUSINESS_CONNECTORS_JSON = json.dumps(connectors)
    elif missing == "connector_json":
        configuration.RUNTIME_BUSINESS_CONNECTORS_JSON = "invalid-json"
    elif missing == "node_routes":
        configuration.AGENT_MODEL_NODE_ROUTES_JSON = "{}"
    elif missing == "unknown_route":
        configuration.AGENT_MODEL_NODE_ROUTES_JSON = '{"soothe_agent":{"compose_turn":"unknown"}}'
    executor = worker.configured_executor(session, dependencies=_dependencies(configuration))

    assert RunQueueService(session, executor, "qa-worker").consume(run.run_id)
    session.refresh(run)
    assert (run.status, run.error_code, run.dispatch_state) == (
        "failed", "SOOTHE_AGENT_NOT_CONFIGURED", "finished",
    )
    assert run.lease_owner is None
    callback = session.scalar(select(CallbackEvent))
    assert callback is not None and callback.payload_json["status"] == "failed"
    assert "SOOTHE_AGENT_NOT_CONFIGURED" in caplog.text


@pytest.mark.parametrize("method", ["run", "resume"])
def test_missing_soothe_configuration_returns_terminal_executor_contract(soothe_run, method: str) -> None:
    session, run = soothe_run
    configuration = worker.settings.model_copy(update={"RUNTIME_BUSINESS_CONNECTORS_JSON": "{}"})
    lease = LeaseContext(
        execution_attempt=4, lease_owner="worker", fencing_token=3,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        privacy_version=1, authorization_version=1,
    )
    result = getattr(worker.configured_executor(
        session, dependencies=_dependencies(configuration),
    ), method)(run.run_id, lease)
    assert (result.status, result.error_code, result.execution_attempt) == (
        "failed", "SOOTHE_AGENT_NOT_CONFIGURED", 4,
    )
    assert result.artifact_refs == [] and result.output_summary is None


def test_soothe_factory_builds_fresh_workflow_with_complete_deployment_configuration(
    soothe_run, monkeypatch,
) -> None:
    """真实公共模型网关和 SOOTHE 适配器可装配；每次工厂调用均隔离内存工作状态。"""
    session, run = soothe_run
    configuration = worker.settings.model_copy(update={
        "RUNTIME_BUSINESS_CONNECTORS_JSON": json.dumps({"couple_diary_backend": {
            "enabled": True, "base_url": "https://business.example.test",
            "runtime_id": "runtime", "key_id": "test", "secret": "test-secret",
        }}),
        "MODEL_ROUTES_JSON": _model_routes_json(),
        "AGENT_MODEL_NODE_ROUTES_JSON": '{"soothe_agent":{"compose_turn":"fixture"}}',
        "RUNTIME_REDIS_URL": "redis://fixture",
    })

    class RedisFactory:
        @staticmethod
        def from_url(url: str) -> FakeRedis:
            assert url == "redis://fixture"
            return FakeRedis()

    monkeypatch.setattr(worker, "Redis", RedisFactory)
    lease = LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        privacy_version=1, authorization_version=1,
    )
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        dependencies = RuntimeDependencies(
            settings=configuration, session_factory=None, clock=None,
            callback_client=None, tool_client=client, transport_verifier=None,
            provider_adapter=object(),
        )
        factory = get_executor_factory("soothe_agent")
        assert factory is not None
        first = factory(session, run.run_id, lease, is_draining=lambda: False, dependencies=dependencies)
        second = factory(session, run.run_id, lease, is_draining=lambda: False, dependencies=dependencies)
        assert isinstance(first, WorkflowExecutor)
        assert isinstance(second, WorkflowExecutor)
        assert first is not second


@pytest.mark.parametrize(("base_url", "cipher_key"), [
    pytest.param(" ", None, id="blank-connector"),
    pytest.param("https://business.example.test/private-path", None, id="connector-path"),
    pytest.param("https://test-user:test-password@business.example.test", None, id="connector-credentials"),
    pytest.param("ftp://business.example.test", None, id="connector-scheme"),
    pytest.param("https://business.example.test", "invalid-key", id="invalid-cipher-key"),
])
def test_invalid_soothe_configuration_during_gateway_assembly_finishes_queue(
    soothe_run, monkeypatch, base_url: str, cipher_key: str | None,
) -> None:
    """模型网关可真实装配时，connector/cipher 配置错误仍须经 finish 安全终结。"""
    session, run = soothe_run
    configuration = worker.settings.model_copy(update={
        "RUNTIME_BUSINESS_CONNECTORS_JSON": json.dumps({"couple_diary_backend": {
            "enabled": True, "base_url": base_url,
            "runtime_id": "runtime", "key_id": "test", "secret": "test-secret",
        }}),
        "MODEL_ROUTES_JSON": _model_routes_json(),
        "AGENT_MODEL_NODE_ROUTES_JSON": '{"soothe_agent":{"compose_turn":"fixture"}}',
        "RUNTIME_REDIS_URL": "redis://fixture",
    })
    if cipher_key is not None:
        configuration.MEMORY_SNAPSHOT_FERNET_KEY = cipher_key

    class RedisFactory:
        @staticmethod
        def from_url(url: str) -> FakeRedis:
            assert url == "redis://fixture"
            return FakeRedis()

    monkeypatch.setattr(worker, "Redis", RedisFactory)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        dependencies = RuntimeDependencies(
            settings=configuration, session_factory=None, clock=None,
            callback_client=None, tool_client=client, transport_verifier=None,
            provider_adapter=object(),
        )
        assert RunQueueService(
            session, worker.configured_executor(session, dependencies=dependencies), "qa-worker",
        ).consume(run.run_id)
    session.refresh(run)
    assert (run.status, run.error_code, run.dispatch_state) == (
        "failed", "SOOTHE_AGENT_NOT_CONFIGURED", "finished",
    )
    assert run.lease_owner is None
