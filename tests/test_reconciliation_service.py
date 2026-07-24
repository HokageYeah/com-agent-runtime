"""P0 对账器只处理可由权威状态确定的安全恢复动作。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
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
    RuntimeAuditRecord,
    RuntimeOutboxEvent,
)
from app.runtime.planner import StaticPlanner
from app.services.admission_service import AdmissionService
from app.services.agent_run_service import AgentRunService, AgentRunServiceError
from app.services.lease_service import LeaseService
from app.services.reconciliation_service import ReconciliationService


def _run(run_id: str, *, status: str, dispatch_state: str) -> AgentRun:
    now = datetime.now(UTC)
    return AgentRun(
        run_id=run_id, agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status=status, dispatch_state=dispatch_state, input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        run_deadline_at=now + timedelta(days=1),
    )


def test_reconciler_fails_expired_waiting_human_and_reports_dead_letter() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    waiting = _run("waiting-run", status="waiting_human", dispatch_state="finished")
    waiting.waiting_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.add(waiting)
    session.add(_run("finished-run", status="succeeded", dispatch_state="finished"))
    session.add(CallbackEvent(event_id="event-1", run_id="finished-run", event_seq=1, status_version=2, event_type="run_succeeded", payload_json={"event": "run_succeeded"}, created_at=datetime.now(UTC)))
    session.add(RuntimeOutboxEvent(outbox_id="dead-callback", event_type="callback", aggregate_type="agent_run", aggregate_id="finished-run", payload_json={"event_id": "event-1"}, status="dead_letter", retention_until=datetime.now(UTC) + timedelta(days=1)))
    session.commit()

    report = ReconciliationService(session).run_once()

    refreshed = session.scalar(select(AgentRun).where(AgentRun.run_id == "waiting-run"))
    # scanned 仅统计本轮可处理的 waiting_human 任务，避免把终态任务误认为待修复对象。
    assert report.scanned == 1
    assert (report.repaired, report.dead_letter_callbacks, report.failures) == (1, 1, 0)
    assert refreshed is not None and (refreshed.status, refreshed.dispatch_state, refreshed.error_code) == ("failed", "finished", "WAITING_HUMAN_TIMEOUT")


def test_reconciliation_report_logs_only_safe_aggregates(caplog: pytest.LogCaptureFixture) -> None:
    """报告和日志只含计数与固定动作码，绝不回显 Run 输入或 callback 正文。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run = _run("safe-report-run", status="pending", dispatch_state="held")
    run.input_json = {"prompt": "private prompt", "diary": "private diary"}
    run.held_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.add(run)
    session.commit()

    with caplog.at_level("INFO"):
        report = ReconciliationService(session).run_once()

    assert report.as_dict() == {
        "scanned": 1,
        "repaired": 1,
        "dead_letter_callbacks": 0,
        "failures": 0,
        "alerts": 0,
        "action_counts": {},
        "memory_deletion_delivered_events": 0,
        "memory_deletion_confirmed_purges": 0,
        "memory_deletion_deleted_revisions": 0,
        "memory_deletion_aborted": False,
    }
    assert "private prompt" not in caplog.text
    assert "private diary" not in caplog.text


def test_reconciler_cancels_expired_waiting_human_when_package_requires_it() -> None:
    """已冻结的 cancelled 策略必须在对账时产生对应终态 callback。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    waiting = _run("cancelled-waiting-run", status="waiting_human", dispatch_state="finished")
    waiting.waiting_expires_at = now - timedelta(seconds=1)
    session.add(waiting)
    session.add(
        AgentPlan(
            plan_id="cancelled-waiting-plan", run_id=waiting.run_id,
            strategy="static_workflow", steps_json=[], stop_conditions_json={},
            fallback_policy_json={"waiting_human_timeout_action": "cancelled"}, status="planned",
        )
    )
    session.commit()

    report = ReconciliationService(session).run_once(now=now)

    refreshed = session.scalar(select(AgentRun).where(AgentRun.run_id == waiting.run_id))
    callback = session.scalar(select(CallbackEvent).where(CallbackEvent.run_id == waiting.run_id))
    assert report.repaired == 1
    assert refreshed is not None and refreshed.status == "cancelled"
    assert callback is not None and callback.event_type == "run_cancelled"


def test_reconciler_requeues_timeout_fallback_from_frozen_plan() -> None:
    """超时 fallback 只服从已冻结 Plan，不得重读可变 AgentDefinition。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(
        AgentDefinition(
            agent_id="memoir_agent", version="1.0.0", runtime_type="workflow",
            definition_json={"policy": {"waiting_human_timeout_action": "failed"}},
            package_digest="sha256:changed", contract_version="1.0.0", status="active",
            status_changed_at=now, status_changed_by="test", status_change_reason="changed",
        )
    )
    waiting = _run("timeout-fallback-run", status="waiting_human", dispatch_state="finished")
    waiting.waiting_expires_at = now - timedelta(seconds=1)
    session.add(waiting)
    session.add(
        AgentPlan(
            plan_id="timeout-fallback-plan", run_id=waiting.run_id,
            strategy="static_workflow", steps_json=[{"node_id": "fallback", "node_type": "deterministic"}],
            stop_conditions_json={},
            fallback_policy_json={"waiting_human_timeout_action": "fallback", "waiting_human_fallback_node": "fallback"},
            status="planned",
        )
    )
    session.commit()

    report = ReconciliationService(session).run_once(now=now)

    refreshed = session.scalar(select(AgentRun).where(AgentRun.run_id == waiting.run_id))
    dispatches = session.scalars(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.aggregate_id == waiting.run_id,
            RuntimeOutboxEvent.event_type == "run_dispatch",
        )
    ).all()
    assert report.repaired == 1
    assert refreshed is not None and (refreshed.status, refreshed.dispatch_state) == ("waiting_human", "queued")
    assert refreshed.queued_at is not None
    assert len(dispatches) == 1 and dispatches[0].payload_json["reason"] == "waiting_human_timeout_fallback"


def test_static_planner_freezes_waiting_human_timeout_action() -> None:
    plan = StaticPlanner().create_plan_from_definition(
        "frozen-policy-run",
        {
            "workflow_nodes": [{"node_id": "fallback", "node_type": "deterministic"}],
            "policy": {
                "waiting_human_timeout_action": "fallback",
                "waiting_human_fallback_node": "fallback",
            },
        },
    )

    assert plan.fallback_policy["waiting_human_timeout_action"] == "fallback"
    assert plan.fallback_policy["waiting_human_fallback_node"] == "fallback"


def test_reconciler_fails_timeout_fallback_with_target_outside_frozen_steps() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    waiting = _run("invalid-fallback-target-run", status="waiting_human", dispatch_state="finished")
    waiting.waiting_expires_at = now - timedelta(seconds=1)
    session.add(waiting)
    session.add(
        AgentPlan(
            plan_id="invalid-fallback-target-plan", run_id=waiting.run_id,
            strategy="static_workflow", steps_json=[{"node_id": "review", "node_type": "guardrail"}],
            stop_conditions_json={}, fallback_policy_json={"waiting_human_timeout_action": "fallback", "waiting_human_fallback_node": "not-a-step"},
            status="planned",
        )
    )
    session.commit()

    ReconciliationService(session).run_once(now=now)

    refreshed = session.scalar(select(AgentRun).where(AgentRun.run_id == waiting.run_id))
    assert refreshed is not None and (refreshed.status, refreshed.dispatch_state, refreshed.error_code) == (
        "failed", "finished", "FALLBACK_NODE_INVALID"
    )


def test_reconciler_cancels_expired_held_run() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    held = _run("expired-held", status="pending", dispatch_state="held")
    held.held_expires_at = now - timedelta(seconds=1)
    session.add(held)
    session.commit()

    report = ReconciliationService(session).run_once(now=now)

    refreshed = session.scalar(select(AgentRun).where(AgentRun.run_id == held.run_id))
    callback = session.scalar(select(CallbackEvent).where(CallbackEvent.run_id == held.run_id))
    assert report.repaired == 1
    assert refreshed is not None and (refreshed.status, refreshed.dispatch_state, refreshed.error_code) == (
        "cancelled", "finished", "HELD_TIMEOUT"
    )
    assert callback is not None and callback.event_type == "run_cancelled"


def test_reconciler_fails_queued_run_past_frozen_queue_ttl() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    queued = _run("expired-queued", status="pending", dispatch_state="queued")
    queued.queued_at = now - timedelta(seconds=61)
    session.add(queued)
    session.add(AgentPlan(
        plan_id="expired-queued-plan", run_id=queued.run_id, strategy="static_workflow",
        steps_json=[], stop_conditions_json={"queue_ttl_seconds": 60},
        fallback_policy_json={}, status="planned",
    ))
    session.commit()

    report = ReconciliationService(session).run_once(now=now)

    refreshed = session.scalar(select(AgentRun).where(AgentRun.run_id == queued.run_id))
    assert report.repaired == 1
    assert refreshed is not None and (refreshed.status, refreshed.dispatch_state, refreshed.error_code) == (
        "failed", "finished", "QUEUE_TIMEOUT"
    )


def test_reconciler_reaps_expired_lease_to_queued() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    reaped = _run("expired-lease", status="pending", dispatch_state="claimed")
    reaped.lease_owner = "lost-worker"
    reaped.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.add(reaped)
    session.commit()

    report = ReconciliationService(session).run_once()

    refreshed = session.scalar(select(AgentRun).where(AgentRun.run_id == reaped.run_id))
    assert report.repaired == 1
    assert refreshed is not None and refreshed.dispatch_state == "queued"


def test_reconciler_reaps_expired_running_lease_into_a_new_claimable_attempt() -> None:
    """失联中的 running Run 必须回到 pending，避免 queued 但永远无法再次 claim。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = _run("expired-running-lease", status="running", dispatch_state="claimed")
    run.execution_attempt = 1
    run.fencing_token = 1
    run.lease_owner = "lost-worker"
    run.lease_expires_at = now - timedelta(seconds=1)
    session.add(run)
    session.commit()

    assert ReconciliationService(session).run_once(now=now).repaired == 1
    session.refresh(run)
    assert (run.status, run.dispatch_state, run.execution_attempt, run.fencing_token) == (
        "pending", "queued", 1, 1,
    )

    context = LeaseService(session).claim(run.run_id, "recovery-worker")
    assert context is not None
    assert (context.execution_attempt, context.fencing_token) == (2, 2)


def test_reconciler_enforces_active_and_wall_clock_deadlines_without_side_effect_replay() -> None:
    """未被 Worker 持有的耗尽 Run 条件终止；claimed Run 仅写取消请求。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    active = _run("active-timeout", status="pending", dispatch_state="queued")
    active.active_elapsed_ms = 60_000
    wall = _run("wall-timeout", status="pending", dispatch_state="held")
    wall.run_deadline_at = now - timedelta(seconds=1)
    claimed = _run("claimed-wall-timeout", status="running", dispatch_state="claimed")
    claimed.lease_owner, claimed.fencing_token = "worker", 1
    claimed.lease_expires_at = now + timedelta(minutes=1)
    claimed.run_deadline_at = now - timedelta(seconds=1)
    session.add_all([
        active,
        wall,
        claimed,
        AgentPlan(
            plan_id="active-timeout-plan", run_id=active.run_id,
            strategy="static_workflow", steps_json=[],
            stop_conditions_json={"max_run_seconds": 60}, fallback_policy_json={}, status="planned",
        ),
    ])
    session.commit()

    report = ReconciliationService(session).run_once(now=now)

    session.refresh(active)
    session.refresh(wall)
    session.refresh(claimed)
    assert (active.status, active.dispatch_state, active.error_code) == (
        "failed", "finished", "ACTIVE_TIME_LIMIT_EXCEEDED",
    )
    assert (wall.status, wall.dispatch_state, wall.error_code) == (
        "failed", "finished", "RUN_DEADLINE_EXCEEDED",
    )
    assert claimed.cancel_requested_at is not None
    assert report.action_counts == {
        "active_time_limit_terminated": 1,
        "run_deadline_cancel_requested": 1,
        "run_deadline_terminated": 1,
    }


def test_reconciler_terminates_pending_queued_run_for_dead_dispatch() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    dead_dispatch = _run("dead-dispatch", status="pending", dispatch_state="queued")
    session.add(dead_dispatch)
    session.add(RuntimeOutboxEvent(
        outbox_id="dead-dispatch-event", event_type="run_dispatch", aggregate_type="agent_run",
        aggregate_id=dead_dispatch.run_id, payload_json={"run_id": dead_dispatch.run_id},
        status="dead_letter", retention_until=datetime.now(UTC) + timedelta(days=1),
    ))
    session.add_all([
        AdmissionBucket(scope_type="global", scope_key="*", queued_count=1),
        AdmissionBucket(scope_type="caller", scope_key="caller", queued_count=1),
        AdmissionBucket(scope_type="tenant", scope_key="tenant", queued_count=1),
        AdmissionBucket(scope_type="agent", scope_key="memoir_agent", queued_count=1),
    ])
    session.commit()

    report = ReconciliationService(session).run_once()

    refreshed = session.scalar(select(AgentRun).where(AgentRun.run_id == dead_dispatch.run_id))
    assert report.repaired == 1
    assert refreshed is not None and (refreshed.status, refreshed.dispatch_state, refreshed.error_code) == (
        "failed", "finished", "DISPATCH_FAILED"
    )
    assert all(bucket.queued_count == 0 for bucket in session.scalars(select(AdmissionBucket)))


def test_reconciler_restores_missing_or_lost_run_dispatch_without_duplicate_identity() -> None:
    """queued Run 缺少 outbox 时补建；已投递但丢失通知时复用原 outbox 身份。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    missing = _run("missing-dispatch", status="pending", dispatch_state="queued")
    delivered = _run("lost-dispatch", status="pending", dispatch_state="queued")
    expired_delivery = _run("expired-delivery", status="pending", dispatch_state="queued")
    pending = _run("active-dispatch", status="pending", dispatch_state="queued")
    session.add_all([
        missing,
        delivered,
        expired_delivery,
        pending,
        RuntimeOutboxEvent(
            outbox_id="expired-delivering-dispatch", event_type="run_dispatch",
            aggregate_type="agent_run", aggregate_id=expired_delivery.run_id,
            payload_json={"run_id": expired_delivery.run_id, "reason": "auto_create"},
            status="delivering", lease_owner="lost-worker",
            lease_expires_at=now - timedelta(seconds=1),
            retention_until=now + timedelta(days=1),
        ),
        RuntimeOutboxEvent(
            outbox_id="delivered-dispatch", event_type="run_dispatch",
            aggregate_type="agent_run", aggregate_id=delivered.run_id,
            payload_json={"run_id": delivered.run_id, "reason": "auto_create"},
            status="delivered", delivered_at=now - timedelta(minutes=10),
            retention_until=now + timedelta(days=1),
        ),
        RuntimeOutboxEvent(
            outbox_id="pending-dispatch", event_type="run_dispatch",
            aggregate_type="agent_run", aggregate_id=pending.run_id,
            payload_json={"run_id": pending.run_id, "reason": "auto_create"},
            status="pending", retention_until=now + timedelta(days=1),
        ),
    ])
    session.commit()

    report = ReconciliationService(session).run_once(now=now)

    events = session.scalars(
        select(RuntimeOutboxEvent)
        .where(RuntimeOutboxEvent.event_type == "run_dispatch")
        .order_by(RuntimeOutboxEvent.outbox_id)
    ).all()
    by_run = {event.aggregate_id: event for event in events}
    assert by_run[missing.run_id].status == "pending"
    assert by_run[missing.run_id].payload_json["reason"] == "reconciliation_missing_dispatch"
    assert by_run[delivered.run_id].outbox_id == "delivered-dispatch"
    assert by_run[delivered.run_id].status == "pending"
    assert by_run[expired_delivery.run_id].outbox_id == "expired-delivering-dispatch"
    assert by_run[expired_delivery.run_id].status == "pending"
    assert by_run[pending.run_id].outbox_id == "pending-dispatch"
    assert len(events) == 4
    assert report.action_counts == {"run_dispatch_repaired": 3}


def test_reconciler_cancels_unclaimed_runs_for_revoked_definition_and_requests_claimed_cancel() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentDefinition(
        agent_id="memoir_agent", version="1.0.0", runtime_type="workflow",
        definition_json={}, package_digest="sha256:test", contract_version="1.0.0",
        status="revoked", status_changed_at=now, status_changed_by="admin",
        status_change_reason="security", revoked_at=now, revocation_reason="security",
    ))
    held = _run("revoked-held", status="pending", dispatch_state="held")
    queued = _run("revoked-queued", status="pending", dispatch_state="queued")
    waiting = _run("revoked-waiting", status="waiting_human", dispatch_state="finished")
    claimed = _run("revoked-claimed", status="pending", dispatch_state="claimed")
    session.add_all([held, queued, waiting, claimed])
    session.commit()

    report = ReconciliationService(session).run_once(now=now)

    cancelled = session.scalars(select(AgentRun).where(AgentRun.run_id.in_((held.run_id, queued.run_id, waiting.run_id)))).all()
    refreshed_claimed = session.scalar(select(AgentRun).where(AgentRun.run_id == claimed.run_id))
    assert report.repaired == 4
    assert all(run.status == "cancelled" and run.dispatch_state == "finished" and run.error_code == "PACKAGE_REVOKED" for run in cancelled)
    assert refreshed_claimed is not None and refreshed_claimed.cancel_requested_at is not None


def test_reconciler_loser_after_worker_claim_writes_no_terminal_callback_or_admission() -> None:
    """对账器读到过期 queued Run 后，Worker claim 获胜时不得用旧快照终结它。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    reconciler_session, worker_session = sessions(), sessions()
    now = datetime.now(UTC)
    queued = _run("claim-race", status="pending", dispatch_state="queued")
    queued.queued_at = now - timedelta(seconds=61)
    reconciler_session.add_all([
        queued,
        AgentPlan(
            plan_id="claim-race-plan", run_id=queued.run_id, strategy="static_workflow",
            steps_json=[], stop_conditions_json={"queue_ttl_seconds": 60},
            fallback_policy_json={}, status="planned",
        ),
    ])
    AdmissionService(reconciler_session).transition_run(queued, "none", "queued")
    reconciler_session.commit()

    stale_run = reconciler_session.scalar(select(AgentRun).where(AgentRun.run_id == queued.run_id))
    assert stale_run is not None
    assert LeaseService(worker_session).claim(queued.run_id, "winning-worker") is not None

    assert not ReconciliationService(reconciler_session)._repair_queued_timeout(stale_run, now)
    reconciler_session.commit()

    refreshed = worker_session.scalar(select(AgentRun).where(AgentRun.run_id == queued.run_id))
    buckets = worker_session.scalars(select(AdmissionBucket)).all()
    callbacks = worker_session.scalars(select(CallbackEvent).where(CallbackEvent.run_id == queued.run_id)).all()
    assert refreshed is not None and refreshed.dispatch_state == "claimed"
    assert len(callbacks) == 0
    assert len(buckets) == 4 and all(bucket.queued_count == 0 and bucket.running_count == 1 for bucket in buckets)


def test_reconciler_loser_after_approval_writes_no_timeout_callback_or_extra_admission() -> None:
    """审批已把 waiting_human 移回 queued 后，对账器不能按旧超时快照终结它。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    reconciler_session, approval_session = sessions(), sessions()
    now = datetime.now(UTC)
    waiting = _run("approval-race", status="waiting_human", dispatch_state="finished")
    waiting.waiting_expires_at = now - timedelta(seconds=1)
    reconciler_session.add(waiting)
    reconciler_session.commit()

    stale_run = reconciler_session.scalar(select(AgentRun).where(AgentRun.run_id == waiting.run_id))
    assert stale_run is not None
    AgentRunService(approval_session).approve(waiting.run_id, "caller", "approve", 1)
    approval_session.commit()

    assert not ReconciliationService(reconciler_session)._repair_waiting_human_timeout(stale_run, now)
    reconciler_session.commit()

    refreshed = approval_session.scalar(select(AgentRun).where(AgentRun.run_id == waiting.run_id))
    buckets = approval_session.scalars(select(AdmissionBucket)).all()
    callbacks = approval_session.scalars(select(CallbackEvent).where(CallbackEvent.run_id == waiting.run_id)).all()
    dispatches = approval_session.scalars(select(RuntimeOutboxEvent).where(RuntimeOutboxEvent.aggregate_id == waiting.run_id, RuntimeOutboxEvent.event_type == "run_dispatch")).all()
    assert refreshed is not None and (refreshed.status, refreshed.dispatch_state) == ("waiting_human", "queued")
    assert len(callbacks) == 0 and len(dispatches) == 1
    assert len(buckets) == 4 and all(bucket.queued_count == 1 and bucket.running_count == 0 for bucket in buckets)


def test_approval_loser_after_reconciler_termination_writes_no_outbox_or_admission() -> None:
    """审批持有旧快照时，对账先终结后不得复活 Run 或写入任何审批副作用。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    approval_session, reconciler_session = sessions(), sessions()
    now = datetime.now(UTC)
    waiting = _run("reconciler-wins", status="waiting_human", dispatch_state="finished")
    waiting.waiting_expires_at = now - timedelta(seconds=1)
    approval_session.add(waiting)
    approval_session.commit()

    stale_run = approval_session.scalar(select(AgentRun).where(AgentRun.run_id == waiting.run_id))
    assert stale_run is not None
    winning_run = reconciler_session.scalar(select(AgentRun).where(AgentRun.run_id == waiting.run_id))
    assert winning_run is not None
    assert ReconciliationService(reconciler_session)._repair_waiting_human_timeout(winning_run, now)
    reconciler_session.commit()

    with pytest.raises(AgentRunServiceError, match="状态版本冲突"):
        AgentRunService(approval_session).approve(waiting.run_id, "caller", "approve", 1)
    approval_session.rollback()

    refreshed = reconciler_session.scalar(select(AgentRun).where(AgentRun.run_id == waiting.run_id))
    callbacks = reconciler_session.scalars(select(CallbackEvent).where(CallbackEvent.run_id == waiting.run_id)).all()
    dispatches = reconciler_session.scalars(select(RuntimeOutboxEvent).where(RuntimeOutboxEvent.aggregate_id == waiting.run_id, RuntimeOutboxEvent.event_type == "run_dispatch")).all()
    audits = reconciler_session.scalars(select(RuntimeAuditRecord).where(RuntimeAuditRecord.resource_id == waiting.run_id)).all()
    buckets = reconciler_session.scalars(select(AdmissionBucket)).all()
    assert refreshed is not None and (refreshed.status, refreshed.dispatch_state) == ("failed", "finished")
    assert len(callbacks) == 1 and len(dispatches) == 0 and len(audits) == 0
    assert len(buckets) == 4 and all(bucket.queued_count == 0 and bucket.running_count == 0 for bucket in buckets)
