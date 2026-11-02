"""R7：真实登记工厂、队列、工具和模型网关的进程内端到端隐私验收。"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.worker as worker
from app.core.authorization import AuthorizationService
from app.db.sqlalchemy_db import Base
from app.models import (
    AgentArtifact,
    AgentDefinition,
    AgentModelUsage,
    AgentRun,
    AgentStep,
    AgentToolCall,
    CallbackEvent,
)
from app.runtime.mock_business_server import _handler
from app.runtime.model_gateway import HttpProviderAdapter
from app.runtime.test_harness import (
    LoopbackTestTransport,
    RuntimeDependencies,
    RuntimeHarnessConfig,
)
from app.schemas.agent_run import CreateRunCommand
from app.services.agent_package_service import AgentPackageService
from app.services.agent_run_service import AgentRunService
from tests.test_provider_traffic_controller import FakeRedis
from tests.test_qa_end_to_end import _Scenario
from tests.test_soothe_package_tools_mock import _exchange
from tests.test_soothe_runner import (
    private_text,
    reply_context,
    reply_draft,
    simulator_context,
    simulator_draft,
)

_ORIGIN = "http://127.0.0.1:18765"


class _BusinessBoundary:
    def __init__(self, mode):
        self.business_type = "soothe_" + mode
        self.turn_id = mode + "-" + private_text()
        self.context = reply_context() if mode == "reply" else simulator_context()
        if mode == "reply":
            self.context.update(background=private_text(), previous_replies=[private_text()])
        else:
            self.context.update(partner_name=private_text()[:8], history=[
                {"user_message": private_text(), "reply": private_text()}])
        self.draft = reply_draft() if mode == "reply" else simulator_draft()
        self.bound_references = None
        self.submission_checked = False
        self.sent = []
        boundary = self

        class Handler(_handler("soothe-r7")):
            def _json(self, status, payload):
                # 只替换内存正文，验签、引用授权、幂等和计数仍由真实 handler 负责。
                if "turn_context" in payload.get("output", {}):
                    payload = {**payload, "output": {"turn_context": boundary.context}}
                super()._json(status, payload)

        self.handler = Handler

    def handle(self, request):
        name = request.headers.get("X-Agent-Tool-Name", "callback")
        self.sent.append(name)
        if name.startswith("soothe."):
            references = json.loads(request.content)["input"]
            assert (references["session_id"], references["turn_id"], references["run_id"]) == self.bound_references
        if name == "soothe.submit_turn_result":
            # 只保存比较结果，断言失败不打印任何动态业务正文。
            self.submission_checked = json.loads(request.content)["input"]["turn_result"] == self.draft
        return _exchange(self.handler, request)

    def state(self):
        return _exchange(self.handler, httpx.Request("GET", _ORIGIN + "/state")).json()

    def close(self):
        pass


class _ProviderBoundary:
    def __init__(self, business):
        self.business = business
        self.calls = 0
        self.slots_checked = False
        self.roles_checked = False

    def handle(self, request):
        self.calls += 1
        messages = json.loads(request.content)["messages"]
        self.roles_checked = all(message["role"] in {"system", "user", "assistant", "tool"} for message in messages)
        if not self.roles_checked:
            return httpx.Response(400, json={"error": "invalid role"})
        instructions, data = messages[0]["content"], messages[1]["content"]
        context = self.business.context
        material = context.get("chat_text", context.get("user_message"))
        self.slots_checked = material not in instructions and material in data
        if "scenario" in context:
            self.slots_checked &= context["scenario"]["hidden_need"] in instructions
        return httpx.Response(200, json=self.business.draft)


@pytest.fixture
def scenario(monkeypatch, request):
    """SQLite 服务层创建 Run 并冻结真实 SOOTHE 包；仅在外部依赖边界注入 mock。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    business = _BusinessBoundary(request.param)
    provider = _ProviderBoundary(business)
    redis = FakeRedis()

    class RedisFactory:
        @staticmethod
        def from_url(url):
            assert url == "redis://soothe-r7"
            return redis

    monkeypatch.setattr(worker, "Redis", RedisFactory)
    trusted_clients = {"soothe-caller": {
        "tenant_id": "soothe-tenant", "authorization_version": 1,
        "agent_ids": ["soothe_agent"],
        "business_types": ["soothe_reply", "soothe_simulator"],
        "callback_target_ids": ["soothe_callback"],
        "connector_ids": ["couple_diary_backend"], "data_domains": ["couple_soothe"],
    }}
    credential = {"runtime_id": "agent-runtime-harness", "key_id": "test", "secret": "harness-only-soothe-r7"}
    configuration = worker.settings.model_copy(update={
        "RUNTIME_BUSINESS_CONNECTORS_JSON": json.dumps({"couple_diary_backend": {
            "enabled": True, "base_url": _ORIGIN, **credential,
        }}),
        "RUNTIME_CALLBACK_TARGETS_JSON": json.dumps({"soothe_callback": {
            "enabled": True, "url": f"{_ORIGIN}/callbacks", **credential,
        }}),
        "RUNTIME_TRUSTED_CLIENTS_JSON": json.dumps(trusted_clients),
        "RUNTIME_REDIS_URL": "redis://soothe-r7",
        "AGENT_MODEL_NODE_ROUTES_JSON": '{"soothe_agent":{"compose_turn":"soothe-small"}}',
        "MEMORY_SNAPSHOT_FERNET_KEY": Fernet.generate_key().decode(),
        "MODEL_ROUTES_JSON": json.dumps([{
            "route_id": "soothe-small", "provider": "provider", "model": "soothe-model",
            "endpoint": "https://8.8.8.8/v1/model", "rate_limit_key": "soothe-r7",
            "max_concurrency": 1, "rpm_limit": 30, "tpm_limit": 100000,
            "timeout_seconds": 1, "permit_ttl_seconds": 2, "settle_margin_seconds": 0,
            "price_unit": "usd_per_1k_tokens", "input_price": 0, "output_price": 0,
            "route_config_version": "v1", "pricing_config_version": "v1",
            "capabilities": ["structured_output", "private_residency"],
            "data_residency": "private", "max_context_tokens": 8192,
            "max_output_tokens": 4096, "enabled": True,
            "allowed_tenant_ids": ["*"], "allowed_model_policies": ["strict"],
        }]),
    })
    harness = RuntimeHarnessConfig(None, trusted_clients, "agent-runtime-harness", _ORIGIN)
    with (
        Session(engine) as session,
        httpx.Client(transport=httpx.MockTransport(business.handle)) as business_client,
        httpx.Client(transport=httpx.MockTransport(provider.handle)) as provider_client,
    ):
        package = AgentPackageService(Path("app/agents")).load("soothe_agent", "1.0.0")
        session.add(AgentDefinition(
            agent_id=package.agent_id, version=package.version, runtime_type="workflow",
            definition_json=package.model_dump(mode="json"), package_digest=package.package_digest,
            contract_version=package.contract_version, status="active",
            status_changed_at=datetime.now(UTC), status_changed_by="soothe-r7", status_change_reason="test",
        ))
        session.commit()
        AuthorizationService(trusted_clients).authorize_create(
            client_id="soothe-caller", agent_id="soothe_agent", business_type=business.business_type,
            callback_target_id="soothe_callback", connector_id="couple_diary_backend", data_domain="couple_soothe",
        )
        summary = AgentRunService(session, trusted_model_route_ids=["soothe-small"]).create(
            CreateRunCommand(
                agent_id="soothe_agent", agent_version="1.0.0", business_type=business.business_type,
                business_id=business.turn_id, start_mode="held",
                input={"session_id": "soothe-session", "turn_id": business.turn_id},
                callback_target_id="soothe_callback", business_connector_id="couple_diary_backend",
                data_domain="couple_soothe",
            ), "soothe-caller", "soothe-tenant", "soothe-create",
        )
        session.commit()
        run = session.scalar(select(AgentRun).where(AgentRun.run_id == summary.run_id))
        assert (run.status, run.dispatch_state) == ("pending", "held")
        assert not business.sent and provider.calls == 0
        # 模拟业务端完成 session/turn → Run 引用绑定后，再发起真实 start 握手。
        business.bound_references = ("soothe-session", business.turn_id, run.run_id)
        AgentRunService(session).start(
            run.run_id, "soothe-caller", "soothe-start", expected_status_version=run.status_version,
        )
        session.commit()
        assert run.dispatch_state == "queued"
        dependencies = RuntimeDependencies(
            settings=configuration, session_factory=None, clock=None,
            callback_client=business_client, tool_client=business_client,
            transport_verifier=LoopbackTestTransport(harness),
            provider_adapter=HttpProviderAdapter(provider_client, peer_ip_provider=lambda: "8.8.8.8"),
        )
        try:
            yield _Scenario(session, run, configuration, dependencies, business, provider)
        finally:
            business.close()
    engine.dispose()


def _assert_private_free(scenario, caplog, capsys):
    # 扫描所有表和解密 checkpoint，覆盖 Run/Step、审计、回调与 Artifact。
    surfaces = str([scenario.session.execute(table.select()).all() for table in Base.metadata.sorted_tables])
    surfaces += json.dumps(scenario.checkpoints()) + caplog.text
    captured = capsys.readouterr()
    surfaces += captured.out + captured.err
    surfaces += AgentRunService(scenario.session).get(scenario.run.run_id, "soothe-caller").model_dump_json()
    context = scenario.business.context
    markers = [context.get("chat_text", context.get("user_message"))]
    if "scenario" in context:
        markers.extend(context["scenario"][key] for key in ("title", "anger_reason", "hidden_need"))
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
    # 仅检查随机正文，不将结构枚举当成私密哨兵。
    markers.extend(value for value in strings(context) if len(value) == 32)
    if "partner_name" in context:
        markers.append(context["partner_name"])
    markers.extend(value for value in strings(scenario.business.draft) if len(value) == 32)
    private_free = not any(marker in surfaces for marker in markers)
    assert private_free, "私密正文逃逸到持久化、checkpoint、日志或输出"


@pytest.mark.parametrize("scenario", ["reply", "simulator"], indirect=True)
def test_full_run_submits_actual_provider_output_and_keeps_private_data_in_memory(scenario, caplog, capsys):
    with caplog.at_level(logging.INFO):
        scenario.consume("soothe-r7-worker")
        scenario.deliver_callbacks()
    assert (scenario.run.status, scenario.run.dispatch_state) == ("succeeded", "finished")
    assert scenario.business.submission_checked, "提交内容未匹配实际 Provider 输出"
    assert scenario.business.state()["soothe_submissions"] == 1
    assert scenario.provider.calls == 1
    assert scenario.provider.slots_checked and scenario.provider.roles_checked
    assert [step.step_name for step in scenario.rows(AgentStep)] == [
        "load_turn_context", "compose_turn", "validate_turn", "submit_turn_result"]
    assert all(step.status == "succeeded" for step in scenario.rows(AgentStep))
    assert len(scenario.rows(AgentModelUsage)) == 1
    assert len(scenario.rows(AgentArtifact)) == 4
    assert scenario.business.state()["callback_count"] == len(scenario.rows(CallbackEvent))
    _assert_private_free(scenario, caplog, capsys)


@pytest.mark.parametrize("scenario,case_id,error_code", [
    ("reply", "reply_banned_phrase", "ANSWER_VALIDATION_FAILED"),
    ("simulator", "simulator_control_field", "STRUCTURED_OUTPUT_INVALID"),
], indirect=["scenario"])
def test_minimal_eval_rejections_never_submit(scenario, case_id, error_code, caplog, capsys):
    cases = {case["case_id"]: case for case in map(json.loads, Path(
        "app/agents/soothe_agent/1.0.0/evals/minimal.jsonl").read_text().splitlines())}
    assert case_id in cases
    if case_id == "reply_banned_phrase":
        assert cases[case_id]["expected"] == "answer_validation_failed"
        scenario.business.draft["replies"][0]["text"] = "别生气了"
    else:
        assert cases[case_id]["expected"] == "schema_validation_failed"
        scenario.business.draft["forgiveness"] = 100
    with caplog.at_level(logging.INFO):
        scenario.consume("soothe-r7-worker")
        scenario.deliver_callbacks()
    assert (scenario.run.status, scenario.run.error_code) == ("failed", error_code)
    assert scenario.provider.calls == 1 and scenario.provider.roles_checked
    assert scenario.business.state()["soothe_submissions"] == 0
    assert "soothe.submit_turn_result" not in scenario.business.sent
    assert not scenario.rows(AgentToolCall)
    _assert_private_free(scenario, caplog, capsys)
