from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models import (
    AdmissionBucket,
    AgentDefinition,
    AgentPlan,
    AgentRun,
    CallbackEvent,
)
from app.runtime.artifact import ArtifactStore
from app.runtime.checkpoint import CheckpointStore, FernetCheckpointCipher
from app.runtime.executor import WorkflowExecutor
from app.runtime.interfaces import AgentRunResult, LeaseContext
from app.services.run_queue_service import RunQueueService


class FakeExecutor:
    """Task 5.5 专用执行器，不导入后续 Task 6 的 WorkflowExecutor。"""

    called = 0

    def run(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
        self.called += 1
        return AgentRunResult(
            run_id=run_id,
            status="succeeded",
            execution_attempt=lease_context.execution_attempt,
        )


def _add_active_test_package(session, changed_at: datetime) -> None:
    """为正常消费链路装配与 Run 冻结身份完全匹配的可执行 Package。"""
    session.add(
        AgentDefinition(
            agent_id="memoir_agent",
            version="1.0.0",
            runtime_type="workflow",
            definition_json={},
            package_digest="sha256:test",
            contract_version="1.0.0",
            status="active",
            status_changed_at=changed_at,
            status_changed_by="test",
            status_change_reason="fixture",
        )
    )


def test_draining_blocks_claim_and_normal_queue_executes_once() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run = AgentRun(
        run_id="queue_run",
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
    session.add(run)
    _add_active_test_package(session, datetime.now(UTC))
    session.commit()
    executor = FakeExecutor()
    assert (
        RunQueueService(session, executor, "worker", is_draining=lambda: True).consume(
            "queue_run"
        )
        is False
    )
    assert executor.called == 0
    assert RunQueueService(session, executor, "worker").consume("queue_run") is True
    assert executor.called == 1
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "queue_run"))
    assert run is not None
    assert run.status == "succeeded"
    assert run.dispatch_state == "finished"
    assert all(
        bucket.running_count == 0 for bucket in session.scalars(select(AdmissionBucket))
    )


def test_late_executor_result_releases_cancelled_claim_for_purge() -> None:
    """cancel/purge 在外部请求途中到达时，旧 Worker 必须释放 claimed 归属。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(
        AgentRun(
            run_id="late-result-run",
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
    _add_active_test_package(session, datetime.now(UTC))
    session.commit()

    class LateExecutor:
        def run(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
            run = session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
            assert run is not None
            run.cancel_requested_at = datetime.now(UTC)
            run.privacy_state = "purge_requested"
            run.privacy_version += 1
            session.commit()
            return AgentRunResult(
                run_id=run_id,
                status="failed",
                execution_attempt=lease_context.execution_attempt,
            )

    assert RunQueueService(session, LateExecutor(), "worker").consume("late-result-run")
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "late-result-run"))
    assert run is not None
    assert (run.status, run.dispatch_state, run.lease_owner, run.lease_expires_at) == (
        "cancelled",
        "finished",
        None,
        None,
    )


def test_queue_cancels_deprecated_package_and_releases_lease_once() -> None:
    """deprecated Package 的真实消费链路必须取消、释放 lease 且只回调一次。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = AgentRun(
        run_id="deprecated-queue-run",
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
        run_deadline_at=now + timedelta(days=1),
    )
    session.add_all(
        [
            run,
            AgentDefinition(
                agent_id=run.agent_id,
                version=run.agent_version,
                runtime_type="workflow",
                definition_json={},
                package_digest=run.package_digest,
                contract_version="1.0.0",
                status="deprecated",
                status_changed_at=now,
                status_changed_by="test",
                status_change_reason="fixture",
            ),
            AgentPlan(
                plan_id="deprecated-queue-plan",
                run_id=run.run_id,
                strategy="static_workflow",
                steps_json=[
                    {"node_id": "load_snapshot", "node_type": "tool"},
                    {"node_id": "compute_stats", "node_type": "deterministic"},
                ],
                stop_conditions_json={},
                fallback_policy_json={},
                status="planned",
            ),
        ]
    )
    session.commit()

    class RecordingNodeRunner:
        def __init__(self) -> None:
            self.calls = 0

        def run_node(
            self, node: object, run: object, state: object
        ) -> dict[str, object]:
            del node, run, state
            self.calls += 1
            return {"result": "unexpected"}

    runner = RecordingNodeRunner()
    executor = WorkflowExecutor(
        session,
        runner,
        CheckpointStore(session, FernetCheckpointCipher.generate()),
        ArtifactStore(session),
    )

    assert RunQueueService(session, executor, "worker-a").consume(run.run_id) is True

    refreshed = session.scalar(select(AgentRun).where(AgentRun.run_id == run.run_id))
    callbacks = session.scalars(
        select(CallbackEvent).where(CallbackEvent.run_id == run.run_id)
    ).all()
    assert runner.calls == 0
    assert refreshed is not None
    assert (refreshed.status, refreshed.dispatch_state) == ("cancelled", "finished")
    assert (refreshed.lease_owner, refreshed.lease_expires_at) == (None, None)
    assert [callback.event_type for callback in callbacks] == ["run_cancelled"]
