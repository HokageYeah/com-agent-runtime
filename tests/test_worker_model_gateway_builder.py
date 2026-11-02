import json
import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.worker as worker
from app.agents.memoir_agent import bootstrap as memoir_bootstrap
from app.db.sqlalchemy_db import Base
from app.models import AgentDefinition, AgentRun, AgentStep
from app.runtime.interfaces import LeaseContext
from app.runtime.model_gateway import ModelCallContext, ModelGateway
from app.runtime.prompt_registry import PromptDefinition
from app.runtime.test_harness import RuntimeDependencies
from tests.test_provider_traffic_controller import FakeRedis


@pytest.fixture
def model_settings(monkeypatch: pytest.MonkeyPatch):
    """独立受信配置故意不含有效 Memoir 节点表，公共装配不能读取该表。"""
    runtime_settings = worker.settings.model_copy(update={
        "RUNTIME_REDIS_URL": "redis://fixture",
        "MODEL_ROUTES_JSON": json.dumps([{
            "route_id": "fixture", "provider": "provider", "model": "model",
            "endpoint": "https://model.example.test/v1", "rate_limit_key": "fixture",
            "max_concurrency": 1, "rpm_limit": 30, "tpm_limit": 10000,
            "timeout_seconds": 1, "permit_ttl_seconds": 2, "settle_margin_seconds": 0,
            "price_unit": "usd_per_1k_tokens", "input_price": 0, "output_price": 0,
            "route_config_version": "v1", "pricing_config_version": "v1",
            "capabilities": ["structured_output", "private_residency"],
            "data_residency": "private", "max_context_tokens": 2048,
            "max_output_tokens": 1024, "enabled": True,
            "allowed_tenant_ids": ["*"], "allowed_model_policies": ["balanced"],
        }]),
        "MEMOIR_MODEL_NODE_ROUTES_JSON": "invalid-json",
        "RUNTIME_TRUSTED_CLIENTS_JSON": '{"caller":{"authorization_version":1}}',
    })

    class RedisFactory:
        @staticmethod
        def from_url(url: str) -> FakeRedis:
            assert url == "redis://fixture"
            return FakeRedis()

    monkeypatch.setattr(worker, "Redis", RedisFactory)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ])
    return runtime_settings


@pytest.mark.parametrize("boundary", ["draining", "authorization", "package"])
def test_builder_is_independent_of_memoir_and_keeps_live_guards(
    model_settings, boundary: str,
) -> None:
    """真实公共网关经注入 Provider 成功调用，边界变化后同一实例拒绝再次发送。"""
    class Provider:
        calls = 0

        def call(self, *args: object, **kwargs: object) -> object:
            self.calls += 1
            return {"ok": True}

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        now = datetime.now(UTC)
        session.add(AgentRun(
            run_id="builder-run", agent_id="fixture_agent", agent_version="1.0.0",
            package_digest="digest", contract_version="1.0.0", business_type="fixture",
            business_id="business", status="pending", dispatch_state="claimed",
            input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant",
            create_idempotency_key="key", callback_target_id="callback",
            business_connector_id="connector", trace_id="trace", execution_attempt=1,
            lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(minutes=1),
            capability_snapshot_json={"allowed_model_route_ids": ["fixture"]},
            run_deadline_at=now + timedelta(minutes=5),
        ))
        session.add(AgentStep(
            step_id="builder-step", run_id="builder-run", step_name="fixture_node",
            step_type="model", status="running", execution_attempt=1, step_attempt=1,
            input_summary={"estimated_input_tokens": 1},
        ))
        definition = AgentDefinition(
            agent_id="fixture_agent", version="1.0.0", runtime_type="workflow",
            definition_json={}, package_digest="digest", contract_version="1.0.0",
            status="active", status_changed_at=now, status_changed_by="test",
            status_change_reason="fixture",
        )
        session.add(definition)
        session.commit()
        lease = LeaseContext(
            execution_attempt=1, lease_owner="worker", fencing_token=1,
            lease_expires_at=now + timedelta(minutes=1), privacy_version=1,
            authorization_version=1,
        )
        provider = Provider()
        dependencies = RuntimeDependencies(
            settings=model_settings, session_factory=None, clock=None,
            callback_client=None, tool_client=None, transport_verifier=None,
            provider_adapter=provider,
        )
        draining = False
        gateway = worker.build_model_gateway(
            session, model_settings, is_draining=lambda: draining, dependencies=dependencies,
        )
        assert isinstance(gateway, ModelGateway)
        context = ModelCallContext.from_authoritative(session, "builder-run", "builder-step", lease)
        prompt = PromptDefinition(
            prompt_id="fixture", version="v1", owner_agent="fixture_agent",
            input_schema="fixture_input", output_schema="fixture_output",
            model_policy="balanced", guardrail_policy="private_first",
            status="active", template="",
        )
        assert gateway.call(context, "fixture", {}, prompt=prompt).status == "succeeded"
        assert provider.calls == 1

        if boundary == "draining":
            draining = True
        elif boundary == "authorization":
            model_settings.RUNTIME_TRUSTED_CLIENTS_JSON = '{"caller":{"authorization_version":2}}'
        else:
            definition.status = "revoked"
            session.commit()

        assert gateway.call(context, "fixture", {}, prompt=prompt).status == "aborted_before_send"
        assert provider.calls == 1
    engine.dispose()


@pytest.mark.parametrize("failure", ["missing_redis", "redis_init", "invalid_routes"])
def test_builder_disables_model_boundary_for_incomplete_configuration(
    model_settings, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    """公共配置不可用时不构建半成品网关，Memoir 调用方继续可退回模板。"""
    if failure == "missing_redis":
        model_settings.RUNTIME_REDIS_URL = ""
    elif failure == "redis_init":
        def unavailable(url: str) -> None:
            raise RuntimeError("synthetic")

        monkeypatch.setattr(worker, "Redis", SimpleNamespace(from_url=unavailable))
    else:
        model_settings.MODEL_ROUTES_JSON = "invalid-json"

    assert worker.build_model_gateway(object(), model_settings) is None


@pytest.mark.parametrize("node_routes", [
    {},
    {"extract_highlights": "fixture"},
    {node: "unknown" for node in (
        "extract_highlights", "plan_chapters", "generate_scenes",
        "generate_scene_batch", "repair_coverage_gaps",
    )},
])
def test_memoir_wrapper_keeps_its_node_route_gate(model_settings, node_routes) -> None:
    """公共路由存在仍不足以启用 Memoir；节点键及目标必须全部有效。"""
    model_settings.MEMOIR_MODEL_NODE_ROUTES_JSON = json.dumps(node_routes)
    dependencies = RuntimeDependencies(
        settings=model_settings, session_factory=None, clock=None,
        callback_client=None, tool_client=None, transport_verifier=None,
    )

    assert memoir_bootstrap.configured_model_gateway(object(), dependencies=dependencies) is None
