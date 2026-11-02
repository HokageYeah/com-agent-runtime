"""T5：真实 QA 工厂/队列/网关的进程内验收，不绑定端口、不保存业务正文。"""

from __future__ import annotations

import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.worker as worker
from app.db.sqlalchemy_db import Base
from app.models import (
    AgentArtifact,
    AgentCheckpoint,
    AgentDefinition,
    AgentModelUsage,
    AgentRun,
    AgentStep,
    AgentToolCall,
    CallbackEvent,
    RuntimeAuditRecord,
    RuntimeOutboxEvent,
)
from app.runtime.checkpoint import FernetCheckpointCipher
from app.runtime.executor import _SAFE_CHECKPOINT_KEYS
from app.runtime.interfaces import AgentRunResult, LeaseContext
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
from app.services.callback_service import CallbackDeliveryService
from app.services.lease_service import LeaseService
from app.services.run_queue_service import RunQueueService
from tests.test_provider_traffic_controller import FakeRedis

_ORIGIN = "http://127.0.0.1:18765"
_NODES = ["load_question_context", "compose_answer", "validate_answer", "submit_answer"]


class _WorkerInterrupted(BaseException):
    """模拟进程退出：不让生产代码的 Exception 分支提前终结 Run。"""


class _MemorySocket:
    """把原始签名 HTTP 请求交给真实 mock handler，完全不创建监听 socket。"""

    def __init__(self, wire: bytes) -> None:
        self.request = io.BytesIO(wire)
        self.response = bytearray()

    def makefile(self, *_args, **_kwargs):
        return self.request

    def sendall(self, data: bytes) -> None:
        self.response.extend(data)


class _BusinessBoundary:
    """只留安全计数/键和布尔验证结果；问题、证据与回答仅在调用栈中流转。"""

    def __init__(self) -> None:
        self.markers = tuple(
            "private-" + uuid4().hex.translate(str.maketrans("0123456789", "ghijklmnop"))
            for _ in range(3)
        )
        self.evidence = True
        self.after_query = lambda: None
        self.fault = "none"
        self.sent: list[tuple[str, str | None]] = []
        self.document_checked = False
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.blocked_response = None
        boundary = self

        class Handler(_handler("qa-t5")):
            def _json(self, status, payload):
                # 保留真实 handler 的验签、授权引用校验和计数，只替换动态测试正文。
                if "question" in payload.get("output", {}):
                    payload = {"output": {
                        "question": boundary.markers[0],
                        "evidence": [{"source_ref": "qa-evidence-1", "text": boundary.markers[1]}]
                        if boundary.evidence else [],
                    }}
                super()._json(status, payload)

        self.handler = Handler

    def exchange(self, request: httpx.Request) -> httpx.Response:
        """请求字节保持不变，因而签名和 wire envelope 校验仍由业务服务执行。"""
        wire = f"{request.method} {request.url.raw_path.decode()} HTTP/1.0\r\n"
        wire += "".join(f"{key}: {value}\r\n" for key, value in request.headers.items())
        sock = _MemorySocket(wire.encode() + b"\r\n" + request.content)
        self.handler(sock, ("127.0.0.1", 1), object())
        head, body = bytes(sock.response).split(b"\r\n\r\n", 1)
        return httpx.Response(int(head.split()[1]), content=body, request=request)

    def control(self, operation: str) -> None:
        response = self.exchange(httpx.Request(
            "POST", f"{_ORIGIN}/__harness__/{operation}",
            headers={"X-Harness-Control": "qa-t5"},
        ))
        assert response.status_code == 202

    def state(self):
        return self.exchange(httpx.Request("GET", f"{_ORIGIN}/state")).json()

    def handle(self, request: httpx.Request) -> httpx.Response:
        name = request.headers.get("X-Agent-Tool-Name", "callback")
        self.sent.append((name, request.headers.get("Idempotency-Key")))
        if name == "qa.submit_answer":
            document = json.loads(request.content)["input"]["document"]
            self.document_checked = (
                document == {"answer": self.markers[2],
                             "source_refs": ["qa-evidence-1"] if self.evidence else [],
                             "no_evidence": not self.evidence}
            )
            if self.fault == "interrupt_after_commit":
                self.fault = "none"
                self.blocked_response = self.pool.submit(self.exchange, request)
                deadline = monotonic() + 2
                while monotonic() < deadline:
                    if self.state()["qa_submit_started"]:
                        break
                    sleep(0.001)
                assert self.state()["qa_submit_started"], "业务 mock 未到达提交后阻塞点"
                assert not self.blocked_response.done()
                raise _WorkerInterrupted()
            if self.fault in {"submit_error", "interrupt_before_commit"}:
                fault, self.fault = self.fault, "none"
                if fault == "interrupt_before_commit":
                    raise _WorkerInterrupted()
                raise httpx.ReadError(" ".join(self.markers), request=request)
        response = self.exchange(request)
        if name == "qa.get_question_context" and self.fault == "load_error":
            raise ValueError(" ".join(self.markers))
        if name == "qa.get_answer_result":
            self.after_query()
        return response

    def close(self) -> None:
        # 即使断言失败也释放业务线程；测试不能遗留阻塞线程或待处理响应。
        if self.blocked_response is not None:
            self.control("release-submit")
            self.blocked_response.result(timeout=2)
        self.pool.shutdown(wait=True)


class _ProviderBoundary:
    """通过真实 HttpProviderAdapter 进入模型网关；不保留 Provider 请求正文。"""

    def __init__(self, business: _BusinessBoundary) -> None:
        self.business = business
        self.calls = 0
        self.mode = "normal"
        self.slots_checked = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        messages = json.loads(request.content)["messages"]
        # 只保留断言结果；失败时不让 pytest 把模型消息或哨兵打印出来。
        instructions, data = messages[0]["content"], messages[1]["content"]
        self.slots_checked = (
            all(marker not in instructions for marker in self.business.markers)
            and self.business.markers[0] in data
            and (not self.business.evidence or self.business.markers[1] in data)
        )
        if self.mode == "provider_error":
            raise RuntimeError(" ".join(self.business.markers))
        return httpx.Response(200, json={
            "answer": self.business.markers[2],
            "source_refs": ["forged-source"] if self.mode == "invalid_ref"
            else (["qa-evidence-1"] if self.business.evidence else []),
            "no_evidence": not self.business.evidence,
        })


class _Scenario:
    def __init__(self, session, run, configuration, dependencies, business, provider):
        self.session, self.run = session, run
        self.configuration, self.dependencies = configuration, dependencies
        self.business, self.provider = business, provider

    def consume(self, owner="qa-worker") -> None:
        # 每次派发都走真实登记工厂，恢复不能复用上轮的私密内存工作态。
        assert RunQueueService(
            self.session,
            worker.configured_executor(self.session, dependencies=self.dependencies),
            owner,
        ).consume(self.run.run_id)
        self.session.refresh(self.run)

    def rows(self, model):
        return self.session.scalars(select(model)).all()

    def checkpoints(self):
        cipher = FernetCheckpointCipher(self.configuration.MEMORY_SNAPSHOT_FERNET_KEY.encode())
        rows = self.session.scalars(select(AgentCheckpoint).order_by(AgentCheckpoint.created_at, AgentCheckpoint.id))
        return [cipher.decrypt(row.encrypted_state_blob) for row in rows]

    def deliver_callbacks(self):
        gateway = worker.configured_callback_gateway(self.dependencies)
        assert gateway is not None
        delivery = CallbackDeliveryService(self.session, gateway)
        for event in self.rows(RuntimeOutboxEvent):
            if event.event_type == "callback":
                delivery.deliver(event)
        self.session.commit()


@pytest.fixture
def scenario(monkeypatch):
    """SQLite 服务层创建 Run 并冻结真实 QA 包；仅在外部依赖边界注入 mock。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    business = _BusinessBoundary()
    provider = _ProviderBoundary(business)
    redis = FakeRedis()

    class RedisFactory:
        @staticmethod
        def from_url(url):
            assert url == "redis://qa-t5"
            return redis

    monkeypatch.setattr(worker, "Redis", RedisFactory)
    credential = {"runtime_id": "agent-runtime-harness", "key_id": "test", "secret": "harness-only-qa-t5"}
    configuration = worker.settings.model_copy(update={
        "RUNTIME_BUSINESS_CONNECTORS_JSON": json.dumps({"qa_business_backend": {
            "enabled": True, "base_url": _ORIGIN, **credential,
        }}),
        "RUNTIME_CALLBACK_TARGETS_JSON": json.dumps({"qa_callback": {
            "enabled": True, "url": f"{_ORIGIN}/callbacks", **credential,
        }}),
        "RUNTIME_TRUSTED_CLIENTS_JSON": '{"qa-caller":{"authorization_version":1}}',
        "RUNTIME_REDIS_URL": "redis://qa-t5",
        "AGENT_MODEL_NODE_ROUTES_JSON": '{"qa_agent":{"compose_answer":"qa-small"}}',
        "MEMORY_SNAPSHOT_FERNET_KEY": Fernet.generate_key().decode(),
        "MODEL_ROUTES_JSON": json.dumps([{
            "route_id": "qa-small", "provider": "provider", "model": "qa-model",
            "endpoint": "https://8.8.8.8/v1/model", "rate_limit_key": "qa-t5",
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
    harness = RuntimeHarnessConfig(None, {"qa-caller": {}}, "agent-runtime-harness", _ORIGIN)
    with (
        Session(engine) as session,
        httpx.Client(transport=httpx.MockTransport(business.handle)) as business_client,
        httpx.Client(transport=httpx.MockTransport(provider.handle)) as provider_client,
    ):
        package = AgentPackageService(Path("app/agents")).load("qa_agent", "1.0.0")
        session.add(AgentDefinition(
            agent_id=package.agent_id, version=package.version, runtime_type="workflow",
            definition_json=package.model_dump(mode="json"), package_digest=package.package_digest,
            contract_version=package.contract_version, status="active",
            status_changed_at=datetime.now(UTC), status_changed_by="qa-t5", status_change_reason="test",
        ))
        session.commit()
        summary = AgentRunService(session, trusted_model_route_ids=["qa-small"]).create(
            CreateRunCommand(
                agent_id="qa_agent", agent_version="1.0.0", business_type="business_qa",
                business_id="qa-turn", start_mode="auto",
                input={"conversation_id": "qa-conversation", "turn_id": "qa-turn"},
                callback_target_id="qa_callback", business_connector_id="qa_business_backend",
                data_domain="business_qa",
            ), "qa-caller", "qa-tenant", "qa-create",
        )
        session.commit()
        run = session.scalar(select(AgentRun).where(AgentRun.run_id == summary.run_id))
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


def test_e1_normal_question_submits_once_and_delivers_safe_callbacks(scenario):
    scenario.consume()
    assert (scenario.run.status, scenario.run.dispatch_state) == ("succeeded", "finished")
    assert scenario.run.lease_owner is None
    assert scenario.business.state()["qa_submissions"] == 1
    assert scenario.business.document_checked and scenario.provider.slots_checked
    assert scenario.provider.calls == 1
    assert len(scenario.rows(AgentModelUsage)) == 1
    assert scenario.rows(AgentModelUsage)[0].reserved_tokens > 0
    assert [step.step_name for step in scenario.rows(AgentStep)] == _NODES
    assert all(step.status == "succeeded" for step in scenario.rows(AgentStep))
    scenario.deliver_callbacks()
    events = scenario.rows(CallbackEvent)
    assert len(events) >= 2 and events[-1].payload_json["status"] == "succeeded"
    assert scenario.business.state()["callback_count"] == len(events)
    for event in events:
        assert set(event.payload_json) == {
            "event", "event_id", "run_id", "event_seq", "status_version", "agent_id",
            "business_id", "status", "error", "public_trace",
        }
        assert event.payload_json["error"] is None
        assert all(set(item) <= {"step", "status", "label"} for item in event.payload_json["public_trace"])
    # 重复通知不得重新取得已完成 Run，更不得重复提交。
    assert not RunQueueService(
        scenario.session, worker.configured_executor(scenario.session, dependencies=scenario.dependencies), "duplicate",
    ).consume(scenario.run.run_id)
    assert scenario.business.state()["qa_submissions"] == 1


def test_e2_no_evidence_submits_controlled_answer_and_succeeds(scenario):
    scenario.business.evidence = False
    scenario.consume()
    assert scenario.run.status == "succeeded" and scenario.run.error_code is None
    assert scenario.business.document_checked
    assert scenario.business.state()["qa_submissions"] == 1


def test_e3_nonexistent_source_fails_without_submit(scenario):
    scenario.provider.mode = "invalid_ref"
    scenario.consume()
    assert (scenario.run.status, scenario.run.error_code) == ("failed", "STRUCTURED_OUTPUT_INVALID")
    assert scenario.business.state()["qa_submissions"] == 0
    assert not scenario.rows(AgentToolCall)
    assert all(name != "qa.submit_answer" for name, _ in scenario.business.sent)


@pytest.mark.parametrize("committed", [True, False], ids=["committed-response-lost", "unknown-not-committed"])
def test_e4_worker_recovery_queries_original_key_without_resubmit(scenario, committed):
    business = scenario.business
    if committed:
        business.control("block-next-submit")
    business.fault = "interrupt_after_commit" if committed else "interrupt_before_commit"
    with pytest.raises(_WorkerInterrupted):
        scenario.consume("lost-worker")
    scenario.session.refresh(scenario.run)
    assert (scenario.run.status, scenario.run.dispatch_state) == ("running", "claimed")
    old_lease = LeaseContext(
        execution_attempt=scenario.run.execution_attempt, lease_owner=scenario.run.lease_owner,
        fencing_token=scenario.run.fencing_token, lease_expires_at=scenario.run.lease_expires_at,
        privacy_version=scenario.run.privacy_version, authorization_version=scenario.run.authorization_version,
    )
    first_attempt = scenario.rows(AgentToolCall)[0]
    assert first_attempt.status == "running"
    assert business.state()["qa_submissions"] == int(committed)
    assert len(scenario.rows(AgentToolCall)) == 1
    assert len(scenario.checkpoints()) == 3
    # 不改业务终态或伪造新键：只推进失联租约到过期，由真实 reaper 接管。
    scenario.run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    scenario.session.commit()
    assert LeaseService(scenario.session).reap_expired() == [scenario.run.run_id]
    scenario.consume("recovered-worker")
    assert scenario.run.execution_attempt == 2 and scenario.run.fencing_token == 2
    assert scenario.run.dispatch_state == "finished"
    expected = ("succeeded", None) if committed else ("failed", "TOOL_OUTCOME_UNKNOWN")
    assert (scenario.run.status, scenario.run.error_code) == expected
    assert business.state()["qa_submissions"] == int(committed)
    assert len(scenario.rows(AgentToolCall)) == 1
    assert first_attempt.status == ("succeeded" if committed else "running")
    keys = [key for name, key in business.sent if name in {"qa.get_answer_result", "qa.submit_answer"}]
    assert len(keys) == 3 and len(set(keys)) == 1
    assert keys[0] == first_attempt.logical_operation_key == first_attempt.idempotency_key
    assert sum(name == "qa.submit_answer" for name, _ in business.sent) == 1
    assert scenario.provider.calls == 2
    assert not LeaseService(scenario.session).finish(AgentRunResult(
        run_id=scenario.run.run_id, status="succeeded", execution_attempt=old_lease.execution_attempt,
    ), old_lease)
    if committed:
        assert business.blocked_response is not None and not business.blocked_response.done()
        business.control("release-submit")
        assert business.blocked_response.result(timeout=2).status_code == 200
        # 旧 Worker 即使收到迟到响应也已经失去所有权，不能改变新终态。
        assert not business.state()["qa_submit_blocked"]


def test_e4_unknown_result_and_uncommitted_query_fail_without_resending(scenario):
    """明确记录 outcome_unknown 后人工重试：查询未提交仍失败，不能当成重发许可。"""
    scenario.business.fault = "submit_error"
    scenario.consume()
    assert (scenario.run.status, scenario.run.error_code) == ("failed", "TOOL_OUTCOME_UNKNOWN")
    audit = scenario.rows(AgentToolCall)[0]
    assert audit.status == "outcome_unknown"
    AgentRunService(scenario.session).retry(scenario.run.run_id, "qa-caller")
    scenario.session.commit()
    scenario.consume("retry-worker")
    assert (scenario.run.status, scenario.run.error_code) == ("failed", "TOOL_OUTCOME_UNKNOWN")
    assert scenario.run.execution_attempt == 2 and scenario.run.dispatch_state == "finished"
    assert scenario.business.state()["qa_submissions"] == 0
    assert len(scenario.rows(AgentToolCall)) == 1 and audit.status == "outcome_unknown"
    sent = scenario.business.sent
    assert sum(name == "qa.submit_answer" for name, _ in sent) == 1
    keys = [key for name, key in sent if name in {"qa.get_answer_result", "qa.submit_answer"}]
    assert len(keys) == 3 and set(keys) == {audit.idempotency_key}


def test_e5_authorization_revoked_inside_submit_node_blocks_physical_send(scenario, caplog):
    def revoke():
        scenario.configuration.RUNTIME_TRUSTED_CLIENTS_JSON = '{"qa-caller":{"authorization_version":2}}'

    scenario.business.after_query = revoke
    with caplog.at_level(logging.INFO):
        scenario.consume()
    assert scenario.business.state()["qa_submissions"] == 0
    assert all(name != "qa.submit_answer" for name, _ in scenario.business.sent)
    assert scenario.run.status == "failed" and scenario.run.dispatch_state == "finished"
    assert "TOOL_AUTHORIZATION_REVOKED" in caplog.text
    assert any(row.action == "tool_gateway_rejected" and row.reason_code == "AUTHORIZATION_VERSION_CHANGED"
               for row in scenario.rows(RuntimeAuditRecord))


def test_e6_cancel_during_submit_node_finishes_cancelled_without_submit(scenario):
    def cancel():
        AgentRunService(scenario.session).cancel(scenario.run.run_id, "qa-caller")
        scenario.session.commit()

    scenario.business.after_query = cancel
    scenario.consume()
    assert (scenario.run.status, scenario.run.dispatch_state) == ("cancelled", "finished")
    assert scenario.run.lease_owner is None and scenario.run.cancel_requested_at is not None
    assert scenario.business.state()["qa_submissions"] == 0
    assert all(name != "qa.submit_answer" for name, _ in scenario.business.sent)
    assert scenario.rows(CallbackEvent)[-1].payload_json["status"] == "cancelled"


@pytest.mark.parametrize("limit", ["max_model_calls", "max_tokens", "max_model_cost"])
def test_e7_model_budget_exhausted_fails_before_provider_or_submit(scenario, caplog, limit):
    # 冻结快照的剩余额度置零，必须触发真实 PolicyEngine，而不是伪造网关失败结果。
    snapshot = scenario.run.capability_snapshot_json
    scenario.run.capability_snapshot_json = {
        **snapshot, "model_policy": {**snapshot["model_policy"], limit: 0},
    }
    scenario.session.commit()
    scenario.consume()
    assert (scenario.run.status, scenario.run.error_code) == ("failed", "MODEL_BUDGET_EXCEEDED")
    assert scenario.provider.calls == 0 and scenario.business.state()["qa_submissions"] == 0
    assert not scenario.rows(AgentModelUsage)


@pytest.mark.parametrize("missing", ["connector", "model_routes"])
def test_e8_missing_qa_configuration_finishes_failed_queue(scenario, missing):
    if missing == "connector":
        scenario.configuration.RUNTIME_BUSINESS_CONNECTORS_JSON = "{}"
    else:
        scenario.configuration.AGENT_MODEL_NODE_ROUTES_JSON = "{}"
    scenario.consume()
    assert (scenario.run.status, scenario.run.error_code, scenario.run.dispatch_state) == (
        "failed", "QA_AGENT_NOT_CONFIGURED", "finished",
    )
    assert scenario.run.lease_owner is None and scenario.provider.calls == 0
    assert not scenario.business.sent
    assert scenario.rows(CallbackEvent)[-1].payload_json["error"] == {"code": "QA_AGENT_NOT_CONFIGURED"}


def test_e9_every_decrypted_checkpoint_contains_only_safe_routing_metadata(scenario):
    scenario.consume()
    checkpoints = scenario.checkpoints()
    assert len(checkpoints) == 4
    for index, payload in enumerate(checkpoints, 1):
        assert set(payload) <= _SAFE_CHECKPOINT_KEYS
        assert payload["completed_node_ids"] == sorted(_NODES[:index])
        assert payload["completed_steps"] == index
    assert all(row.encrypted_state_blob and row.storage_ref is None for row in scenario.rows(AgentCheckpoint))


@pytest.mark.parametrize("outcome", ["normal", "invalid_ref", "provider_error", "load_error", "submit_error"])
def test_e10_private_sentinels_never_escape_success_or_failure(scenario, caplog, capsys, outcome):
    if outcome in {"invalid_ref", "provider_error"}:
        scenario.provider.mode = outcome
    elif outcome != "normal":
        scenario.business.fault = outcome
    with caplog.at_level(logging.INFO):
        scenario.consume()
        scenario.deliver_callbacks()
    expected = {
        "normal": ("succeeded", None),
        "invalid_ref": ("failed", "STRUCTURED_OUTPUT_INVALID"),
        "provider_error": ("failed", "MODEL_UNAVAILABLE"),
        "load_error": ("failed", "TOOL_CALL_FAILED"),
        "submit_error": ("failed", "TOOL_OUTCOME_UNKNOWN"),
    }
    assert (scenario.run.status, scenario.run.error_code) == expected[outcome]
    # 逐表扫描包含审计、callback、artifact、step/run 的所有错误和摘要字段；密文另解密扫描。
    persisted = str([scenario.session.execute(table.select()).all() for table in Base.metadata.sorted_tables])
    captured = capsys.readouterr()
    surfaces = persisted + json.dumps(scenario.checkpoints()) + caplog.text + captured.out + captured.err
    surfaces += AgentRunService(scenario.session).get(scenario.run.run_id, "qa-caller").model_dump_json()
    private_free = not any(marker in surfaces for marker in scenario.business.markers)
    assert private_free, "私密哨兵逃逸到 Runtime 持久化、日志或测试输出"
    assert scenario.business.state()["callback_count"] == len(scenario.rows(CallbackEvent))
    if outcome == "normal":
        assert scenario.business.document_checked and len(scenario.rows(AgentArtifact)) == 4


def test_t5_package_digests_match_reviewed_t0_and_t4_records():
    """将审核记录直接锁为回归断言；不更新包或基线文件。"""
    service = AgentPackageService(Path("app/agents"))
    memoir = [
        "a6e2f53e223658fb648026335373d23f548232e5dd2c4c67a2c774df6e67833e",
        "e92ae977220e02f3956d821b0c5ff6adc2320359970d9989863324ab11349c06",
        "bd20469b0d205242b2639aaa039858495e42dd704036ec8be8b82bfb8ac2f379",
        "c9b3936c9ca11e3e388a8cdba3f10e04a43bdaa998775c47da512c378c379f9c",
        "c99d171acf1def1dc1eecbebe62e24643904e849d4d0544248ee3fcea48908bb",
        "7cb0c8d7457e7ccc643108079a3fcc725ebee5e2557fa2b7f065454dc411dbe2",
        "94456a80237bafd81b802a9eccf981ad9f0a46dd96083b6c916dbbab29c318bf",
        "b5b06cbfae816372efcd567d2fe2182bccb7013cd0fc0d666c75c4b83b9502ba",
        "da05c6c7f83f209f76f0ecbf5fc555fbeb7d90d4cd119f7d3be3b938b645848d",
    ]
    for index, digest in enumerate(memoir):
        assert service.load("memoir_agent", f"1.0.{index}").package_digest == f"sha256:{digest}"
    assert service.load("qa_agent", "1.0.0").package_digest == (
        "sha256:9513dc262c64832ad2d4c6fa564459be79d47bdeeb6bf42797efe48f51ef6466"
    )
