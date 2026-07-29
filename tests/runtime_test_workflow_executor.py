"""Task 6：静态计划执行、步骤审计与安全 checkpoint 回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pytest import MonkeyPatch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models import (
    AgentArtifact,
    AgentCheckpoint,
    AgentDefinition,
    AgentPlan,
    AgentRun,
    AgentStep,
    CallbackEvent,
)
from app.runtime.artifact import ArtifactStore
from app.runtime.checkpoint import CheckpointStore, FernetCheckpointCipher
from app.runtime.executor import RetryableWorkflowNodeError, WorkflowExecutor
from app.runtime.interfaces import LeaseContext
from app.runtime.state import AgentState
from app.services.lease_service import LeaseService


class DeterministicNodeRunner:
    """不访问网络或业务数据的测试节点执行器。"""

    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> dict[str, object]:
        return {"node_id": node["node_id"], "result": "ok"}


class RecordingNodeRunner(DeterministicNodeRunner):
    def __init__(self) -> None:
        self.node_ids: list[str] = []

    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> dict[str, object]:
        self.node_ids.append(str(node["node_id"]))
        return super().run_node(node, run, state)


class RetryOnceNodeRunner(DeterministicNodeRunner):
    """首轮返回受控临时失败，第二轮才产生节点结果。"""

    def __init__(self) -> None:
        self.calls = 0

    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            raise RetryableWorkflowNodeError("TRANSIENT_NODE_FAILURE")
        return super().run_node(node, run, state)


class OptionalFailureThenSuccessRunner(RecordingNodeRunner):
    """仅模拟发布后的可选媒体步骤首次失败。"""

    def __init__(self) -> None:
        super().__init__()
        self.optional_calls = 0

    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> dict[str, object]:
        self.node_ids.append(str(node["node_id"]))
        if node["node_id"] == "enqueue_media":
            self.optional_calls += 1
            if self.optional_calls == 1:
                raise RuntimeError("MEDIA_QUEUE_UNAVAILABLE")
        return {"node_id": node["node_id"], "result": "ok"}


class SnapshotNodeRunner(DeterministicNodeRunner):
    """模拟受信任工具返回私密快照，验证其只进入加密状态。"""

    def run_node(
        self, node: dict[str, object], run: AgentRun, state: AgentState
    ) -> dict[str, object]:
        state.snapshot = {"diaries": [{"content": "私密正文"}]}
        return {"node_id": node["node_id"], "snapshot_loaded": True}


def _executor(session, node_runner: DeterministicNodeRunner) -> WorkflowExecutor:
    """为每个测试注入独立密钥，避免依赖环境中的生产私钥。"""
    return WorkflowExecutor(
        session,
        node_runner,
        CheckpointStore(session, FernetCheckpointCipher.generate()),
        ArtifactStore(session),
    )


def _add_claimed_two_node_run(session, run_id: str) -> datetime:
    now = datetime.now(UTC)
    session.add(
        AgentRun(
            run_id=run_id,
            agent_id="memoir_agent",
            agent_version="1.0.0",
            package_digest="sha256:test",
            contract_version="1.0.0",
            business_type="couple_memory",
            business_id="archive",
            status="pending",
            dispatch_state="claimed",
            input_json={},
            authorization_version=1,
            caller_id="caller",
            tenant_id="tenant",
            create_idempotency_key="key",
            callback_target_id="callback",
            business_connector_id="connector",
            trace_id="trace",
            execution_attempt=1,
            lease_owner="worker-a",
            fencing_token=1,
            lease_expires_at=now + timedelta(seconds=60),
            run_deadline_at=now + timedelta(days=1),
        )
    )
    session.add(
        AgentPlan(
            plan_id=f"{run_id}-plan",
            run_id=run_id,
            strategy="static_workflow",
            steps_json=[
                {"node_id": "load_snapshot", "node_type": "tool"},
                {"node_id": "compute_stats", "node_type": "deterministic"},
            ],
            stop_conditions_json={},
            fallback_policy_json={},
            status="planned",
        )
    )
    session.commit()
    return now


def _lease_context(now: datetime) -> LeaseContext:
    return LeaseContext(
        execution_attempt=1,
        lease_owner="worker-a",
        fencing_token=1,
        lease_expires_at=now + timedelta(seconds=60),
        privacy_version=1,
        authorization_version=1,
    )


def test_executor_writes_step_and_checkpoint_for_every_static_plan_node() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = AgentRun(
        run_id="executor-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="pending", dispatch_state="claimed", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        execution_attempt=1, lease_owner="worker-a", fencing_token=1,
        lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
    )
    session.add(run)
    session.add(
        AgentPlan(
            plan_id="executor-plan", run_id="executor-run", strategy="static_workflow",
            steps_json=[
                {"node_id": "load_snapshot", "node_type": "tool"},
                {"node_id": "compute_stats", "node_type": "deterministic"},
            ],
            stop_conditions_json={}, fallback_policy_json={}, status="planned",
        )
    )
    session.commit()

    result = _executor(session, DeterministicNodeRunner()).run(
        "executor-run",
        LeaseContext(
            execution_attempt=1, lease_owner="worker-a", fencing_token=1,
            lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
            authorization_version=1,
        ),
    )

    assert result.status == "succeeded"
    assert result.output_summary == {"completed_steps": 2}
    assert [step.status for step in session.scalars(select(AgentStep)).all()] == [
        "succeeded",
        "succeeded",
    ]
    checkpoints = session.scalars(select(AgentCheckpoint)).all()
    assert len(checkpoints) == 2
    # 恢复状态必须是密文；安全摘要只保留节点进度，不能暴露运行私密数据。
    assert all(checkpoint.encrypted_state_blob is not None for checkpoint in checkpoints)
    assert [checkpoint.state_summary["completed_steps"] for checkpoint in checkpoints] == [1, 2]
    assert checkpoints[-1].state_summary["completed_node_ids"] == [
        "compute_stats",
        "load_snapshot",
    ]
    artifacts = session.scalars(select(AgentArtifact)).all()
    assert len(artifacts) == 2
    assert artifacts[0].summary_json == {
        "node_id": "load_snapshot",
        "result_keys": ["node_id", "result"],
    }
    assert artifacts[0].business_resource_ref == "business://couple_memory/archive"
    callbacks = session.scalars(select(CallbackEvent).order_by(CallbackEvent.event_seq)).all()
    assert [(event.event_type, event.payload_json["public_trace"]) for event in callbacks] == [
        ("run_started", []),
        ("step_changed", [{"step": "load_snapshot", "status": "succeeded"}]),
        ("step_changed", [{"step": "compute_stats", "status": "succeeded"}]),
    ]


def test_executor_accumulates_only_node_execution_time(monkeypatch: MonkeyPatch) -> None:
    """活跃预算只在节点运行区间累加，恢复/排队前后的间隔不参与计算。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(session, "active-time-run")
    ticks = iter((0.0, 0.0, 1.25, 1.25, 1.25, 3.0, 3.0))
    monkeypatch.setattr("app.runtime.executor.monotonic", lambda: next(ticks))

    result = _executor(session, DeterministicNodeRunner()).run(
        "active-time-run", _lease_context(now)
    )

    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "active-time-run"))
    assert result.status == "succeeded"
    assert run is not None and run.active_elapsed_ms == 3_000


def test_executor_retries_retryable_node_with_frozen_step_budget() -> None:
    """节点自动重试必须按 step_id 计数，且只消耗冻结的节点额度。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(session, "node-retry-run")
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "node-retry-run"))
    assert run is not None
    run.capability_snapshot_json = {"execution_policy": {"max_auto_retry_per_step": 1}}
    session.commit()
    runner = RetryOnceNodeRunner()

    result = _executor(session, runner).run("node-retry-run", _lease_context(now))

    steps = session.scalars(select(AgentStep).order_by(AgentStep.created_at)).all()
    assert result.status == "succeeded"
    assert runner.calls == 3
    assert run.auto_retry_count == 1
    assert steps[0].step_attempt == 2


def test_executor_refuses_revoked_package_before_starting_any_node() -> None:
    """已撤销 Package 不能在旧 lease 下继续启动模型、工具或其它节点。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(session, "revoked-package-run")
    session.add(
        AgentDefinition(
            agent_id="memoir_agent", version="1.0.0", runtime_type="workflow",
            definition_json={}, package_digest="sha256:test", contract_version="1.0.0",
            status="revoked", status_changed_at=now, status_changed_by="test",
            status_change_reason="fixture",
        )
    )
    session.commit()
    runner = RecordingNodeRunner()

    result = _executor(session, runner).run("revoked-package-run", _lease_context(now))

    assert (result.status, result.error_code) == ("cancelled", "PACKAGE_REVOKED")
    assert runner.node_ids == []


def test_executor_partial_resume_retries_only_failed_optional_node() -> None:
    """partial 只能发生在主发布后；恢复不得再次执行发布副作用。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = AgentRun(
        run_id="partial-retry-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="pending", dispatch_state="claimed", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        execution_attempt=1, lease_owner="worker-a", fencing_token=1,
        lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
    )
    session.add_all([
        run,
        AgentPlan(
            plan_id="partial-retry-plan", run_id=run.run_id, strategy="static_workflow",
            steps_json=[
                {"node_id": "load_snapshot", "node_type": "tool"},
                {"node_id": "publish_document", "node_type": "tool"},
                {"node_id": "enqueue_media", "node_type": "tool", "optional": True},
            ], stop_conditions_json={}, fallback_policy_json={}, status="planned",
        ),
    ])
    session.commit()
    context = _lease_context(now)
    runner = OptionalFailureThenSuccessRunner()
    store = CheckpointStore(session, FernetCheckpointCipher.generate())
    executor = WorkflowExecutor(session, runner, store, ArtifactStore(session))

    first = executor.run(run.run_id, context)
    assert first.status == "partial"
    assert runner.node_ids == ["load_snapshot", "publish_document", "enqueue_media"]
    failed = session.scalar(select(AgentStep).where(AgentStep.step_name == "enqueue_media"))
    assert failed is not None and failed.status == "failed"

    # 模拟 retry 已重新认领的新 execution attempt；checkpoint 仍是权威恢复边界。
    run.status, run.dispatch_state = "pending", "claimed"
    run.execution_attempt, run.fencing_token = 2, 2
    run.lease_owner = "worker-b"
    run.lease_expires_at = now + timedelta(seconds=60)
    session.commit()
    second_context = LeaseContext(
        execution_attempt=2, lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
        authorization_version=1,
    )
    second = executor.resume(run.run_id, second_context)

    assert second.status == "succeeded"
    assert runner.node_ids == ["load_snapshot", "publish_document", "enqueue_media", "enqueue_media"]


def test_executor_heartbeats_same_context_before_and_after_every_node(
    monkeypatch: MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(session, "heartbeat-boundary-run")
    context = _lease_context(now)
    runner = RecordingNodeRunner()
    heartbeat_boundaries: list[tuple[tuple[str, ...], int]] = []
    original_heartbeat = LeaseService.heartbeat

    def record_heartbeat(
        lease: LeaseService, run_id: str, current_context: LeaseContext
    ) -> bool:
        heartbeat_boundaries.append((tuple(runner.node_ids), id(current_context)))
        return original_heartbeat(lease, run_id, current_context)

    monkeypatch.setattr(LeaseService, "heartbeat", record_heartbeat)

    result = _executor(session, runner).run("heartbeat-boundary-run", context)

    assert result.status == "succeeded"
    assert [nodes for nodes, _ in heartbeat_boundaries] == [
        (),
        ("load_snapshot",),
        ("load_snapshot",),
        ("load_snapshot", "compute_stats"),
    ]
    assert {context_id for _, context_id in heartbeat_boundaries} == {id(context)}


def test_executor_does_not_start_node_when_pre_node_heartbeat_is_fenced(
    monkeypatch: MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(session, "pre-heartbeat-fenced-run")
    runner = RecordingNodeRunner()
    monkeypatch.setattr(LeaseService, "heartbeat", lambda *_args: False)

    result = _executor(session, runner).run(
        "pre-heartbeat-fenced-run", _lease_context(now)
    )

    assert (result.status, result.error_code) == ("failed", "LEASE_CONTEXT_INVALID")
    assert runner.node_ids == []
    assert session.scalars(select(AgentStep)).all() == []
    assert session.scalars(select(AgentArtifact)).all() == []
    assert session.scalars(select(AgentCheckpoint)).all() == []


def test_executor_does_not_persist_node_when_post_node_heartbeat_is_fenced(
    monkeypatch: MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(session, "post-heartbeat-fenced-run")
    runner = RecordingNodeRunner()
    heartbeat_calls = 0

    def reject_post_node_heartbeat(*_args: object) -> bool:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        return heartbeat_calls == 1

    monkeypatch.setattr(LeaseService, "heartbeat", reject_post_node_heartbeat)

    result = _executor(session, runner).run(
        "post-heartbeat-fenced-run", _lease_context(now)
    )

    assert (result.status, result.error_code) == ("failed", "LEASE_CONTEXT_INVALID")
    assert runner.node_ids == ["load_snapshot"]
    assert session.scalars(select(AgentArtifact)).all() == []
    assert session.scalars(select(AgentCheckpoint)).all() == []


def test_executor_rejects_stale_fencing_context_before_creating_step() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(
        AgentRun(
            run_id="fenced-run", agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="pending", dispatch_state="claimed", input_json={},
            authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            execution_attempt=2, lease_owner="worker-b", fencing_token=2,
            lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
        )
    )
    session.add(
        AgentPlan(
            plan_id="fenced-plan", run_id="fenced-run", strategy="static_workflow",
            steps_json=[{"node_id": "load_snapshot", "node_type": "tool"}],
            stop_conditions_json={}, fallback_policy_json={}, status="planned",
        )
    )
    session.commit()

    result = _executor(session, DeterministicNodeRunner()).run(
        "fenced-run",
        LeaseContext(
            execution_attempt=1, lease_owner="worker-a", fencing_token=1,
            lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
            authorization_version=1,
        ),
    )

    assert result.status == "failed"
    assert result.error_code == "LEASE_CONTEXT_INVALID"
    assert session.scalars(select(AgentStep)).all() == []


def test_executor_resume_skips_nodes_already_recorded_by_checkpoint() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(
        AgentRun(
            run_id="resume-run", agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="pending", dispatch_state="claimed", input_json={},
            authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            execution_attempt=2, lease_owner="worker-b", fencing_token=2,
            lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
        )
    )
    session.add(
        AgentPlan(
            plan_id="resume-plan", run_id="resume-run", strategy="static_workflow",
            steps_json=[
                {"node_id": "load_snapshot", "node_type": "tool"},
                {"node_id": "compute_stats", "node_type": "deterministic"},
            ],
            stop_conditions_json={}, fallback_policy_json={}, status="planned",
        )
    )
    session.commit()
    context = LeaseContext(
        execution_attempt=2, lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
        authorization_version=1,
    )
    checkpoint_store = CheckpointStore(session, FernetCheckpointCipher.generate())
    checkpoint_store.save(
        "resume-run",
        "attempt:1:step:1",
        {"completed_node_ids": ["load_snapshot"]},
        context,
    )
    session.commit()
    runner = RecordingNodeRunner()

    result = WorkflowExecutor(
        session, runner, checkpoint_store, ArtifactStore(session)
    ).resume(
        "resume-run",
        context,
    )

    assert result.status == "succeeded"
    assert runner.node_ids == ["compute_stats"]


def test_executor_resume_from_fallback_checkpoint_starts_at_fallback_node() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(
        AgentRun(
            run_id="fallback-run", agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="pending", dispatch_state="claimed", input_json={},
            authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            execution_attempt=2, lease_owner="worker-b", fencing_token=2,
            lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
        )
    )
    session.add(
        AgentPlan(
            plan_id="fallback-plan", run_id="fallback-run", strategy="static_workflow",
            steps_json=[
                {"node_id": "prepare", "node_type": "tool"},
                {"node_id": "review", "node_type": "guardrail"},
                {"node_id": "fallback", "node_type": "deterministic"},
            ],
            stop_conditions_json={}, fallback_policy_json={"waiting_human_fallback_node": "fallback"},
            status="planned",
        )
    )
    session.commit()
    context = LeaseContext(
        execution_attempt=2, lease_owner="worker-b", fencing_token=2,
        lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
        authorization_version=1,
    )
    checkpoint_store = CheckpointStore(session, FernetCheckpointCipher.generate())
    checkpoint_store.save(
        "fallback-run", "attempt:1:step:2",
        {"completed_node_ids": ["prepare", "review"], "resume_from_node_id": "fallback"},
        context,
    )
    session.commit()
    runner = RecordingNodeRunner()

    result = WorkflowExecutor(
        session, runner, checkpoint_store, ArtifactStore(session)
    ).resume("fallback-run", context)

    assert runner.node_ids == ["fallback"]
    assert result.status == "succeeded"


def test_agent_state_checkpoint_summary_never_contains_private_run_input() -> None:
    state = AgentState(
        run_input={"snapshot_id": "snapshot-1", "diary_content": "私密正文"},
        completed_node_ids=["load_snapshot"],
        fallback_flags=["template_highlights"],
    )

    summary = state.checkpoint_summary()

    assert summary == {
        "completed_node_ids": ["load_snapshot"],
        "fallback_flags": ["template_highlights"],
    }
    assert "diary_content" not in summary


def test_executor_encrypts_node_state_without_leaking_snapshot_to_artifact() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(run_id="snapshot-run", agent_id="memoir_agent", agent_version="1.0.0", package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory", business_id="archive", status="pending", dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback", business_connector_id="connector", trace_id="trace", execution_attempt=1, lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1)))
    session.add(AgentPlan(plan_id="snapshot-plan", run_id="snapshot-run", strategy="static_workflow", steps_json=[{"node_id": "load_snapshot", "node_type": "tool"}], stop_conditions_json={}, fallback_policy_json={}, status="planned"))
    session.commit()
    context = LeaseContext(execution_attempt=1, lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1)
    cipher = FernetCheckpointCipher.generate()
    store = CheckpointStore(session, cipher)
    assert WorkflowExecutor(session, SnapshotNodeRunner(), store, ArtifactStore(session)).run("snapshot-run", context).status == "succeeded"
    assert store.load_latest("snapshot-run", context)["snapshot"] == {"diaries": [{"content": "私密正文"}]}
    assert "私密正文" not in str(session.scalar(select(AgentArtifact)).summary_json)


def test_executor_pauses_for_human_after_checkpoint_and_emits_callback() -> None:
    """人工等待必须先持久化节点 checkpoint，再释放 Worker lease 与 Admission。"""
    class HumanReviewRunner(DeterministicNodeRunner):
        def run_node(
            self, node: dict[str, object], run: AgentRun, state: AgentState
        ) -> dict[str, object]:
            return {"node_id": node["node_id"], "waiting_human": True}

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(run_id="human-run", agent_id="memoir_agent", agent_version="1.0.0", package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory", business_id="archive", status="pending", dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback", business_connector_id="connector", trace_id="trace", execution_attempt=1, lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1)))
    session.add(AgentPlan(plan_id="human-plan", run_id="human-run", strategy="static_workflow", steps_json=[{"node_id": "review", "node_type": "guardrail"}, {"node_id": "fallback", "node_type": "deterministic"}], stop_conditions_json={"approval_ttl_seconds": 60}, fallback_policy_json={"waiting_human_fallback_node": "fallback"}, status="planned"))
    session.commit()
    context = LeaseContext(execution_attempt=1, lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1)
    cipher = FernetCheckpointCipher.generate()
    store = CheckpointStore(session, cipher)

    result = WorkflowExecutor(session, HumanReviewRunner(), store, ArtifactStore(session)).run("human-run", context)

    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "human-run"))
    callback = session.scalar(select(CallbackEvent).where(CallbackEvent.run_id == "human-run", CallbackEvent.event_type == "waiting_human"))
    assert result.status == "waiting_human"
    assert run is not None and (run.status, run.dispatch_state, run.lease_owner) == ("waiting_human", "finished", None)
    assert run.waiting_expires_at is not None
    assert callback is not None
    checkpoint = session.scalar(select(AgentCheckpoint).where(AgentCheckpoint.run_id == "human-run"))
    assert checkpoint is not None and checkpoint.state_summary["completed_node_ids"] == ["review"]
    assert checkpoint.encrypted_state_blob is not None
    assert cipher.decrypt(checkpoint.encrypted_state_blob)["resume_from_node_id"] == "fallback"


def test_executor_rejects_checkpoint_fallback_missing_from_frozen_plan() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(run_id="invalid-fallback-run", agent_id="memoir_agent", agent_version="1.0.0", package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory", business_id="archive", status="waiting_human", dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback", business_connector_id="connector", trace_id="trace", error_code="WAITING_HUMAN_FALLBACK", execution_attempt=2, lease_owner="worker", fencing_token=2, lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1)))
    session.add(AgentPlan(plan_id="invalid-fallback-plan", run_id="invalid-fallback-run", strategy="static_workflow", steps_json=[{"node_id": "review", "node_type": "guardrail"}], stop_conditions_json={}, fallback_policy_json={"waiting_human_fallback_node": "fallback"}, status="planned"))
    session.commit()
    context = LeaseContext(execution_attempt=2, lease_owner="worker", fencing_token=2, lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1)
    store = CheckpointStore(session, FernetCheckpointCipher.generate())
    store.save("invalid-fallback-run", "attempt:1:step:1", {"completed_node_ids": ["review"], "resume_from_node_id": "fallback"}, context)
    session.commit()

    result = WorkflowExecutor(session, RecordingNodeRunner(), store, ArtifactStore(session)).resume("invalid-fallback-run", context)

    assert (result.status, result.error_code) == ("failed", "FALLBACK_NODE_INVALID")


def test_executor_stops_before_next_node_when_authorization_changes() -> None:
    """节点一完成即撤销授权时，Executor 不得启动后续节点。"""
    class RevokingRunner(RecordingNodeRunner):
        def run_node(
            self, node: dict[str, object], run: AgentRun, state: AgentState
        ) -> dict[str, object]:
            result = super().run_node(node, run, state)
            if node["node_id"] == "load_snapshot":
                run.authorization_version += 1
            return result

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(run_id="authorization-run", agent_id="memoir_agent", agent_version="1.0.0", package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory", business_id="archive", status="pending", dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key", callback_target_id="callback", business_connector_id="connector", trace_id="trace", execution_attempt=1, lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1)))
    session.add(AgentPlan(plan_id="authorization-plan", run_id="authorization-run", strategy="static_workflow", steps_json=[{"node_id": "load_snapshot", "node_type": "tool"}, {"node_id": "compute_stats", "node_type": "deterministic"}], stop_conditions_json={}, fallback_policy_json={}, status="planned"))
    session.commit()
    runner = RevokingRunner()
    result = _executor(session, runner).run("authorization-run", LeaseContext(execution_attempt=1, lease_owner="worker", fencing_token=1, lease_expires_at=now + timedelta(seconds=60), privacy_version=1, authorization_version=1))

    assert result.error_code == "LEASE_CONTEXT_INVALID"
    assert runner.node_ids == ["load_snapshot"]
    assert session.scalars(select(AgentArtifact)).all() == []
    assert session.scalars(select(AgentCheckpoint)).all() == []


def test_executor_does_not_persist_tool_result_when_privacy_version_changes() -> None:
    """工具返回后隐私版本变化时，heartbeat 不得绕过统一写前复核。"""

    class PrivacyRevokingRunner(RecordingNodeRunner):
        def run_node(
            self, node: dict[str, object], run: AgentRun, state: AgentState
        ) -> dict[str, object]:
            result = super().run_node(node, run, state)
            if node["node_id"] == "load_snapshot":
                run.privacy_version += 1
            return result

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = _add_claimed_two_node_run(session, "privacy-version-run")
    runner = PrivacyRevokingRunner()

    result = _executor(session, runner).run(
        "privacy-version-run", _lease_context(now)
    )

    assert (result.status, result.error_code) == ("failed", "LEASE_CONTEXT_INVALID")
    assert runner.node_ids == ["load_snapshot"]
    assert session.scalars(select(AgentArtifact)).all() == []
    assert session.scalars(select(AgentCheckpoint)).all() == []


def test_executor_draining_before_first_node_returns_safe_nonterminal_result() -> None:
    """draining 已开始时不得启动首个节点或工具调用。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(
        AgentRun(
            run_id="draining-before-run", agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="pending", dispatch_state="claimed", input_json={},
            authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            execution_attempt=1, lease_owner="worker", fencing_token=1,
            lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
        )
    )
    session.add(
        AgentPlan(
            plan_id="draining-before-plan", run_id="draining-before-run", strategy="static_workflow",
            steps_json=[{"node_id": "load_snapshot", "node_type": "tool"}],
            stop_conditions_json={}, fallback_policy_json={}, status="planned",
        )
    )
    session.commit()
    runner = RecordingNodeRunner()

    result = WorkflowExecutor(
        session,
        runner,
        CheckpointStore(session, FernetCheckpointCipher.generate()),
        ArtifactStore(session),
        is_draining=lambda: True,
    ).run(
        "draining-before-run",
        LeaseContext(
            execution_attempt=1, lease_owner="worker", fencing_token=1,
            lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
            authorization_version=1,
        ),
    )

    assert (result.status, result.error_code) == ("pending", "WORKFLOW_DRAINING")
    assert runner.node_ids == []
    assert session.scalars(select(AgentStep)).all() == []
    assert session.scalars(select(AgentArtifact)).all() == []
    assert session.scalars(select(AgentCheckpoint)).all() == []


def test_executor_draining_after_checkpoint_does_not_start_next_node() -> None:
    """已完成节点安全 checkpoint 后，draining 不得继续启动下一工具节点。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(
        AgentRun(
            run_id="draining-after-run", agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="pending", dispatch_state="claimed", input_json={},
            authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            execution_attempt=1, lease_owner="worker", fencing_token=1,
            lease_expires_at=now + timedelta(seconds=60), run_deadline_at=now + timedelta(days=1),
        )
    )
    session.add(
        AgentPlan(
            plan_id="draining-after-plan", run_id="draining-after-run", strategy="static_workflow",
            steps_json=[
                {"node_id": "load_snapshot", "node_type": "tool"},
                {"node_id": "compute_stats", "node_type": "deterministic"},
            ],
            stop_conditions_json={}, fallback_policy_json={}, status="planned",
        )
    )
    session.commit()
    draining = {"value": False}

    class DrainingRunner(RecordingNodeRunner):
        def run_node(
            self, node: dict[str, object], run: AgentRun, state: AgentState
        ) -> dict[str, object]:
            result = super().run_node(node, run, state)
            draining["value"] = True
            return result

    runner = DrainingRunner()
    result = WorkflowExecutor(
        session,
        runner,
        CheckpointStore(session, FernetCheckpointCipher.generate()),
        ArtifactStore(session),
        is_draining=lambda: draining["value"],
    ).run(
        "draining-after-run",
        LeaseContext(
            execution_attempt=1, lease_owner="worker", fencing_token=1,
            lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
            authorization_version=1,
        ),
    )

    assert (result.status, result.error_code) == ("pending", "WORKFLOW_DRAINING")
    assert runner.node_ids == ["load_snapshot"]
    checkpoint = session.scalar(
        select(AgentCheckpoint).where(AgentCheckpoint.run_id == "draining-after-run")
    )
    assert checkpoint is not None
    assert checkpoint.state_summary["completed_node_ids"] == ["load_snapshot"]
    assert len(session.scalars(select(AgentArtifact)).all()) == 1
