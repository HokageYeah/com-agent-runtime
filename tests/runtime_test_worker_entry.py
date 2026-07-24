"""Worker 入口通过 dispatcher 通知认领 Run，执行器仍可注入 Task 6 实现。"""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.agents.memoir_agent.runner import MemoirNodeRunner
from app.db.sqlalchemy_db import Base
from app.models import (
    AgentPlan,
    AgentRun,
    AgentStep,
    AgentToolCall,
    CallbackEvent,
    RuntimeOutboxEvent,
)
from app.runtime.artifact import ArtifactStore
from app.runtime.checkpoint import CheckpointStore, FernetCheckpointCipher
from app.runtime.executor import WorkflowExecutor
from app.runtime.interfaces import AgentRunResult, LeaseContext
from app.services.agent_run_service import AgentRunService
from app.services.reconciliation_service import ReconciliationService
from app.services.tool_call_audit_service import ToolCallAuditService
from app.worker import WorkerLoop, configured_model_gateway


class FakeExecutor:
    def __init__(self) -> None:
        self.run_ids: list[str] = []
        self.resume_ids: list[str] = []

    def run(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
        self.run_ids.append(run_id)
        return AgentRunResult(
            run_id=run_id,
            status="succeeded",
            execution_attempt=lease_context.execution_attempt,
        )

    def resume(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
        self.resume_ids.append(run_id)
        return AgentRunResult(
            run_id=run_id,
            status="succeeded",
            execution_attempt=lease_context.execution_attempt,
        )


def test_configured_model_gateway_uses_only_trusted_settings(monkeypatch) -> None:
    class FakeRedis:
        @classmethod
        def from_url(cls, url: str) -> FakeRedis:
            assert url == "redis://trusted"
            return cls()

        def eval(self, *args: object) -> object:
            return ["acquired", 0]

    import app.worker as worker

    monkeypatch.setattr(worker, "Redis", FakeRedis)
    monkeypatch.setattr(
        worker.settings,
        "MODEL_ROUTES_JSON",
        '[{"route_id":"memoir","provider":"provider","model":"model",'
        '"endpoint":"https://model.example.test/v1","rate_limit_key":"memoir",'
            '"max_concurrency":1,"rpm_limit":1,"tpm_limit":1,"timeout_seconds":1,'
            '"permit_ttl_seconds":2,"settle_margin_seconds":0,"price_unit":"usd_per_1k_tokens",'
            '"input_price":0,"output_price":0,"route_config_version":"v1",'
            '"pricing_config_version":"v1","capabilities":["structured_output","private_residency"],'
            '"data_residency":"private","max_context_tokens":2048,"max_output_tokens":512}]',
    )
    monkeypatch.setattr(worker.settings, "RUNTIME_REDIS_URL", "redis://trusted", raising=False)
    monkeypatch.setattr(
        worker.settings,
        "MEMOIR_MODEL_NODE_ROUTES_JSON",
        '{"extract_highlights":"memoir","plan_chapters":"memoir","generate_scenes":"memoir"}',
        raising=False,
    )

    gateway = configured_model_gateway(object())

    assert gateway is not None
    assert gateway._route_ids == {
        "extract_highlights": "memoir",
        "plan_chapters": "memoir",
        "generate_scenes": "memoir",
    }


def test_configured_model_gateway_honors_live_draining_guard(monkeypatch) -> None:
    class FakeRedis:
        @classmethod
        def from_url(cls, url: str) -> FakeRedis:
            return cls()

        def eval(self, *args: object) -> object:
            from tests.test_provider_traffic_controller import FakeRedis as TrafficRedis

            if not hasattr(self, "delegate"):
                self.delegate = TrafficRedis()
            return self.delegate.eval(*args)

    class RecordingProvider:
        calls = 0

        def __init__(self, client: object, **kwargs: object) -> None:
            """测试替身接受生产装配注入的 TCP 对端校验回调。"""

        def call(self, *args: object, **kwargs: object) -> object:
            type(self).calls += 1
            return {"ok": True}

    import app.worker as worker

    # 该用例只验证 draining；Provider 虚拟域名按部署公网地址解析。
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        ],
    )

    monkeypatch.setattr(worker, "Redis", FakeRedis)
    monkeypatch.setattr(worker, "HttpProviderAdapter", RecordingProvider)
    monkeypatch.setattr(worker.settings, "MODEL_ROUTES_JSON", '[{"route_id":"memoir","provider":"provider","model":"model","endpoint":"https://model.example.test/v1","rate_limit_key":"memoir","max_concurrency":1,"rpm_limit":1,"tpm_limit":1,"timeout_seconds":1,"permit_ttl_seconds":2,"settle_margin_seconds":0,"price_unit":"usd_per_1k_tokens","input_price":0,"output_price":0,"route_config_version":"v1","pricing_config_version":"v1","capabilities":["structured_output","private_residency"],"data_residency":"private","max_context_tokens":2048,"max_output_tokens":512}]')
    monkeypatch.setattr(worker.settings, "RUNTIME_REDIS_URL", "redis://trusted", raising=False)
    monkeypatch.setattr(worker.settings, "MEMOIR_MODEL_NODE_ROUTES_JSON", '{"extract_highlights":"memoir","plan_chapters":"memoir","generate_scenes":"memoir"}', raising=False)
    monkeypatch.setattr(
        worker.settings,
        "RUNTIME_TRUSTED_CLIENTS_JSON",
        '{"caller":{"authorization_version":1}}',
        raising=False,
    )

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(run_id="guarded-run", agent_id="memoir_agent", agent_version="1.0.0", package_digest="digest", contract_version="1", business_type="memoir", business_id="business", status="pending", dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback", business_connector_id="connector", trace_id="trace", execution_attempt=1, lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(minutes=1), capability_snapshot_json={"allowed_model_route_ids": ["memoir"]}, run_deadline_at=now + timedelta(days=1)))
    session.add(AgentStep(step_id="guarded-step", run_id="guarded-run", step_name="extract_highlights", step_type="model", status="running", execution_attempt=1, step_attempt=1, input_summary={"estimated_input_tokens": 1}))
    session.commit()
    lease = LeaseContext(execution_attempt=1, lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(minutes=1), privacy_version=1, authorization_version=1)

    draining = configured_model_gateway(session, is_draining=lambda: True)
    assert draining is not None
    draining.bind_lease(lease)
    assert draining.call("guarded-run", "extract_highlights", {"source_refs": []}).status == "aborted_before_send"
    assert RecordingProvider.calls == 0

    active = configured_model_gateway(session, is_draining=lambda: False)
    assert active is not None
    active.bind_lease(lease)
    assert active.call("guarded-run", "extract_highlights", {"source_refs": []}).status == "succeeded"
    assert RecordingProvider.calls == 1

    # 同一 Run 冻结的版本与当前权威授权不一致时，模型边界必须在触网前拒绝。
    monkeypatch.setattr(
        worker.settings,
        "RUNTIME_TRUSTED_CLIENTS_JSON",
        '{"caller":{"authorization_version":2}}',
        raising=False,
    )
    assert active.call("guarded-run", "extract_highlights", {"source_refs": []}).status == "aborted_before_send"
    assert RecordingProvider.calls == 1


def test_configured_model_gateway_is_disabled_for_incomplete_settings(monkeypatch) -> None:
    import app.worker as worker

    monkeypatch.setattr(worker.settings, "MODEL_ROUTES_JSON", "[]")
    monkeypatch.setattr(worker.settings, "RUNTIME_REDIS_URL", "", raising=False)
    monkeypatch.setattr(worker.settings, "MEMOIR_MODEL_NODE_ROUTES_JSON", "{}", raising=False)

    assert configured_model_gateway(object()) is None


def test_worker_once_dispatches_and_claims_run_with_injected_executor() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    session.add(
        AgentRun(
            run_id="worker-run",
            agent_id="memoir_agent",
            agent_version="1.0.0",
            package_digest="sha256:test",
            contract_version="1.0.0",
            business_type="couple_memory",
            business_id="archive",
            status="pending",
            dispatch_state="queued",
            input_json={},
            authorization_version=1,
            caller_id="caller",
            tenant_id="tenant",
            create_idempotency_key="key",
            callback_target_id="callback",
            business_connector_id="connector",
            trace_id="trace",
            run_deadline_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    session.add(
        RuntimeOutboxEvent(
            outbox_id="worker-dispatch",
            event_type="run_dispatch",
            aggregate_type="agent_run",
            aggregate_id="worker-run",
            payload_json={"run_id": "worker-run"},
            status="pending",
            retention_until=datetime.now(UTC) + timedelta(days=1),
        )
    )
    session.commit()
    executor = FakeExecutor()

    assert WorkerLoop(factory, executor, worker_id="worker-1").run_once() == 1
    assert executor.run_ids == ["worker-run"]


def test_worker_passes_live_draining_callable_to_keyword_executor_factory() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    session.add(
        AgentRun(
            run_id="worker-draining-factory-run",
            agent_id="memoir_agent",
            agent_version="1.0.0",
            package_digest="sha256:test",
            contract_version="1.0.0",
            business_type="couple_memory",
            business_id="archive",
            status="pending",
            dispatch_state="queued",
            input_json={},
            authorization_version=1,
            caller_id="caller",
            tenant_id="tenant",
            create_idempotency_key="key",
            callback_target_id="callback",
            business_connector_id="connector",
            trace_id="trace",
            run_deadline_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    session.add(
        RuntimeOutboxEvent(
            outbox_id="worker-draining-factory-dispatch",
            event_type="run_dispatch",
            aggregate_type="agent_run",
            aggregate_id="worker-draining-factory-run",
            payload_json={"run_id": "worker-draining-factory-run"},
            status="pending",
            retention_until=datetime.now(UTC) + timedelta(days=1),
        )
    )
    session.commit()
    state = {"draining": False}
    received: list[object] = []
    executor = FakeExecutor()

    def is_draining() -> bool:
        return state["draining"]

    def executor_factory(worker_session, *, is_draining):
        received.append(is_draining)
        return executor

    assert WorkerLoop(
        factory, executor_factory, worker_id="worker-1", is_draining=is_draining
    ).run_once() == 1
    assert received == [is_draining]
    state["draining"] = True
    assert received[0]() is True


def test_worker_resumes_reconciled_waiting_human_run() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    session.add(
        AgentRun(
            run_id="fallback-run", agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="waiting_human", dispatch_state="queued", input_json={},
            authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            run_deadline_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    session.add(
        RuntimeOutboxEvent(
            outbox_id="fallback-dispatch", event_type="run_dispatch", aggregate_type="agent_run",
            aggregate_id="fallback-run", payload_json={"run_id": "fallback-run"},
            status="pending", retention_until=datetime.now(UTC) + timedelta(days=1),
        )
    )
    session.commit()
    executor = FakeExecutor()

    assert WorkerLoop(factory, executor, worker_id="worker-1").run_once() == 1
    assert executor.resume_ids == ["fallback-run"]
    assert executor.run_ids == []


def test_worker_resumes_timeout_fallback_requeued_by_reconciler() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    now = datetime.now(UTC)
    run = AgentRun(run_id="timeout-fallback-run", agent_id="memoir_agent", agent_version="1.0.0", package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory", business_id="archive", status="waiting_human", dispatch_state="finished", input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback", business_connector_id="connector", trace_id="trace", waiting_expires_at=now - timedelta(seconds=1), run_deadline_at=now + timedelta(days=1))
    session.add(run)
    session.add(AgentPlan(plan_id="timeout-fallback-plan", run_id=run.run_id, strategy="static_workflow", steps_json=[{"node_id": "fallback", "node_type": "deterministic"}], stop_conditions_json={}, fallback_policy_json={"waiting_human_timeout_action": "fallback", "waiting_human_fallback_node": "fallback"}, status="planned"))
    session.commit()
    ReconciliationService(session).run_once(now=now)
    executor = FakeExecutor()

    assert WorkerLoop(factory, executor, worker_id="worker-1").run_once() == 1
    assert executor.resume_ids == ["timeout-fallback-run"]
    assert executor.run_ids == []


def test_worker_approve_resumes_from_completed_checkpoint_not_fallback_target() -> None:
    class HumanThenContinueRunner:
        def __init__(self) -> None:
            self.node_ids: list[str] = []

        def run_node(self, node, run, state):
            node_id = str(node["node_id"])
            self.node_ids.append(node_id)
            return {"node_id": node_id, "waiting_human": node_id == "review"}

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    now = datetime.now(UTC)
    session.add(AgentRun(run_id="approve-resume-run", agent_id="memoir_agent", agent_version="1.0.0", package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory", business_id="archive", status="pending", dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback", business_connector_id="connector", trace_id="trace", execution_attempt=1, lease_owner="worker-a", fencing_token=1, lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1)))
    session.add(AgentPlan(plan_id="approve-resume-plan", run_id="approve-resume-run", strategy="static_workflow", steps_json=[{"node_id": "review", "node_type": "guardrail"}, {"node_id": "continue", "node_type": "deterministic"}, {"node_id": "fallback", "node_type": "deterministic"}], stop_conditions_json={"approval_ttl_seconds": 60}, fallback_policy_json={"waiting_human_fallback_node": "fallback"}, status="planned"))
    session.commit()
    cipher = FernetCheckpointCipher.generate()
    runner = HumanThenContinueRunner()
    initial_context = LeaseContext(execution_attempt=1, lease_owner="worker-a", fencing_token=1, lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1)
    executor = WorkflowExecutor(session, runner, CheckpointStore(session, cipher), ArtifactStore(session))
    assert executor.run("approve-resume-run", initial_context).status == "waiting_human"
    waiting = session.scalar(select(AgentRun).where(AgentRun.run_id == "approve-resume-run"))
    assert waiting is not None
    AgentRunService(session).approve("approve-resume-run", "caller", "approve", waiting.status_version)
    session.commit()

    def resumed_executor(worker_session):
        return WorkflowExecutor(worker_session, runner, CheckpointStore(worker_session, cipher), ArtifactStore(worker_session))

    assert WorkerLoop(factory, resumed_executor, worker_id="worker-b").run_once() == 1
    assert runner.node_ids == ["review", "continue", "fallback"]


def test_worker_completes_template_memoir_workflow_and_publishes_document() -> None:
    """真实 Worker 路径必须从 outbox、lease 到发布节点一次闭环。"""
    published: list[tuple[object, ...]] = []

    class Gateway:
        """模拟受信任业务工具；断言发布载荷不需要日记正文。"""

        def get_snapshot(self, *args: object) -> dict[str, object]:
            return {
                "diaries": [
                    {"id": "diary-1", "content": "这是可公开的回忆摘要。"},
                    {
                        "id": "diary-private",
                        "content": "绝不能发布的私密日记正文",
                        "sensitive": True,
                    },
                ],
                "bets": [],
            }

        def publish_playback_document(self, *args: object) -> dict[str, object]:
            published.append(args)
            return {"revision": 1, "content_digest": "published-digest"}

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    session.add(
        AgentRun(
            run_id="memoir-worker-run", agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive-1", status="pending", dispatch_state="queued",
            input_json={"archive_id": "archive-1", "snapshot_id": "snapshot-1", "generation_epoch": 0},
            authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            run_deadline_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    session.add(
        AgentPlan(
            plan_id="memoir-worker-plan", run_id="memoir-worker-run", strategy="static_workflow",
            steps_json=[
                {"node_id": node_id, "node_type": "workflow"}
                for node_id in ("load_snapshot", "sanitize_materials", "compute_stats", "extract_highlights", "plan_chapters", "generate_scenes", "generate_actions", "safety_review", "publish_document")
            ],
            stop_conditions_json={}, fallback_policy_json={}, status="planned",
        )
    )
    session.add(
        RuntimeOutboxEvent(
            outbox_id="memoir-worker-dispatch", event_type="run_dispatch", aggregate_type="agent_run",
            aggregate_id="memoir-worker-run", payload_json={"run_id": "memoir-worker-run"},
            status="pending", retention_until=datetime.now(UTC) + timedelta(days=1),
        )
    )
    session.commit()

    def executor_factory(worker_session):
        return WorkflowExecutor(
            worker_session,
            MemoirNodeRunner(Gateway(), ToolCallAuditService(worker_session)),
            CheckpointStore(worker_session, FernetCheckpointCipher.generate()),
            ArtifactStore(worker_session),
        )

    assert WorkerLoop(factory, executor_factory, worker_id="worker-1").run_once() == 1
    run = factory().scalar(select(AgentRun).where(AgentRun.run_id == "memoir-worker-run"))
    assert run is not None and (run.status, run.dispatch_state) == ("succeeded", "finished")
    assert len(factory().scalars(select(AgentStep).where(AgentStep.run_id == run.run_id)).all()) == 9
    assert factory().scalar(select(AgentToolCall).where(AgentToolCall.run_id == run.run_id)).output_summary == {"revision": 1, "content_digest": "published-digest"}
    assert published[0][1:5] == ("archive-1", "memoir-worker-run", "snapshot-1", 0)
    document = published[0][5]
    assert isinstance(document, dict)
    scenes = document["scenes"]
    assert isinstance(scenes, list) and 3 <= len(scenes) <= 8
    assert document["media_manifest"] == []
    assert "绝不能发布的私密日记正文" not in str(document)
    assert all("diary:diary-private" not in scene["source_refs"] for scene in scenes)


def test_worker_dispatches_callback_outbox_when_callback_gateway_is_configured() -> None:
    """Worker 只在明确注入 callback 网关时消费 callback Outbox。"""
    sent: list[tuple[str, dict[str, object]]] = []

    class Gateway:
        def send(self, target_id: str, payload: dict[str, object]) -> None:
            sent.append((target_id, payload))

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    payload = {"event": "run_cancelled", "event_id": "event-worker", "event_seq": 1, "run_id": "run-worker", "business_id": "archive-worker"}
    session.add(AgentRun(run_id="run-worker", agent_id="memoir_agent", agent_version="1.0.0", package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory", business_id="archive-worker", status="cancelled", dispatch_state="finished", input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key", callback_target_id="memory", business_connector_id="connector", trace_id="trace", run_deadline_at=datetime.now(UTC) + timedelta(days=1)))
    session.add(CallbackEvent(event_id="event-worker", run_id="run-worker", event_seq=1, status_version=2, event_type="run_cancelled", payload_json=payload, created_at=datetime.now(UTC)))
    session.add(RuntimeOutboxEvent(outbox_id="callback-worker", event_type="callback", aggregate_type="agent_run", aggregate_id="run-worker", payload_json={"event_id": "event-worker", "target_id": "memory"}, status="pending", retention_until=datetime.now(UTC) + timedelta(days=1)))
    session.commit()

    assert WorkerLoop(factory, FakeExecutor(), worker_id="worker-1", callback_gateway=Gateway()).run_once() == 0
    assert sent == [("memory", payload)]
