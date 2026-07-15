"""Task 6：静态计划执行、步骤审计与安全 checkpoint 回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.base import Base
from app.models import AgentCheckpoint, AgentPlan, AgentRun, AgentStep
from app.runtime.executor import WorkflowExecutor
from app.runtime.interfaces import LeaseContext
from app.runtime.state import AgentState


class DeterministicNodeRunner:
    """不访问网络或业务数据的测试节点执行器。"""

    def run_node(self, node: dict[str, object], run: AgentRun) -> dict[str, object]:
        return {"node_id": node["node_id"], "result": "ok"}


class RecordingNodeRunner(DeterministicNodeRunner):
    def __init__(self) -> None:
        self.node_ids: list[str] = []

    def run_node(self, node: dict[str, object], run: AgentRun) -> dict[str, object]:
        self.node_ids.append(str(node["node_id"]))
        return super().run_node(node, run)


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

    result = WorkflowExecutor(session, DeterministicNodeRunner()).run(
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
    assert all(checkpoint.encrypted_state_blob is None for checkpoint in checkpoints)
    assert [checkpoint.state_summary["completed_steps"] for checkpoint in checkpoints] == [1, 2]
    assert checkpoints[-1].state_summary["completed_node_ids"] == [
        "compute_stats",
        "load_snapshot",
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

    result = WorkflowExecutor(session, DeterministicNodeRunner()).run(
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
    session.add(
        AgentCheckpoint(
            checkpoint_id="resume-checkpoint", run_id="resume-run", checkpoint_key="attempt:1:step:1",
            state_schema_version="1.0.0", data_classification="runtime_internal", privacy_version=1,
            state_summary={"completed_node_ids": ["load_snapshot"]}, content_digest="digest",
            expires_at=now + timedelta(days=1), created_at=now,
        )
    )
    session.commit()
    runner = RecordingNodeRunner()

    result = WorkflowExecutor(session, runner).resume(
        "resume-run",
        LeaseContext(
            execution_attempt=2, lease_owner="worker-b", fencing_token=2,
            lease_expires_at=now + timedelta(seconds=60), privacy_version=1,
            authorization_version=1,
        ),
    )

    assert result.status == "succeeded"
    assert runner.node_ids == ["compute_stats"]


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
