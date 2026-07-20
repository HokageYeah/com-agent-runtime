"""Task 6：静态计划执行、步骤审计与安全 checkpoint 回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models import (
    AgentArtifact,
    AgentCheckpoint,
    AgentPlan,
    AgentRun,
    AgentStep,
    CallbackEvent,
)
from app.runtime.artifact import ArtifactStore
from app.runtime.checkpoint import CheckpointStore, FernetCheckpointCipher
from app.runtime.executor import WorkflowExecutor
from app.runtime.interfaces import LeaseContext
from app.runtime.state import AgentState


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
