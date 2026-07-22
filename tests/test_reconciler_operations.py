"""对账器的进程级租约与周期入口测试。"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
import app.reconciler as reconciler_module
from app.db.sqlalchemy_db import Base
from app.models import AdmissionBucket, AgentModelUsage, AgentRun, RuntimeOutboxEvent
from app.reconciler import ReconcilerRunner
from app.services.memory_deletion_compensation_service import (
    MemoryDeletionMaintenanceReport,
)
from app.services.reconciliation_lease_service import ReconciliationLeaseService
from app.services.reconciliation_service import (
    ReconciliationReport,
    ReconciliationService,
)


def _sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_only_one_instance_can_hold_reconciliation_lease() -> None:
    sessions = _sessions()
    now = datetime.now(UTC)
    first, second = sessions(), sessions()

    assert ReconciliationLeaseService(first).acquire("instance-a", now=now)
    assert not ReconciliationLeaseService(second).acquire("instance-b", now=now)


def test_expired_reconciliation_lease_can_be_taken_over() -> None:
    sessions = _sessions()
    now = datetime.now(UTC)
    first, second = sessions(), sessions()

    assert ReconciliationLeaseService(first, ttl_seconds=10).acquire("instance-a", now=now)
    assert ReconciliationLeaseService(second, ttl_seconds=10).acquire(
        "instance-b", now=now + timedelta(seconds=11)
    )


def test_old_fencing_token_cannot_release_a_reused_owner_lease() -> None:
    sessions = _sessions()
    now = datetime.now(UTC)
    first, replacement, stale = sessions(), sessions(), sessions()
    first_lease = ReconciliationLeaseService(first, ttl_seconds=10)
    assert first_lease.acquire("instance-a", now=now)
    assert first_lease.fencing_token is not None

    replacement_lease = ReconciliationLeaseService(replacement, ttl_seconds=10)
    assert replacement_lease.acquire("instance-a", now=now + timedelta(seconds=11))
    assert replacement_lease.fencing_token is not None

    assert not ReconciliationLeaseService(stale, ttl_seconds=10).release(
        "instance-a", first_lease.fencing_token, now=now + timedelta(seconds=12)
    )


def test_runner_once_skips_scan_when_another_instance_owns_lease() -> None:
    sessions = _sessions()
    now = datetime.now(UTC)
    holder = sessions()
    assert ReconciliationLeaseService(holder).acquire("instance-a", now=now)
    scanned: list[str] = []

    class RecordingReconciler:
        def __init__(self, session: object) -> None:
            self._session = session

        def run_once(self) -> str:
            scanned.append("scan")
            return "report"

    result = ReconcilerRunner(
        sessions, "instance-b", reconciler_factory=RecordingReconciler, clock=lambda: now
    ).run_once()

    assert result is None
    assert scanned == []


def test_runner_forever_uses_injected_300_second_interval() -> None:
    sessions = _sessions()
    pauses: list[int] = []
    runner = ReconcilerRunner(sessions, "instance-a", interval_seconds=300, sleep=pauses.append)
    runs: list[int] = []
    runner.run_once = lambda: runs.append(1)  # type: ignore[method-assign]

    runner.run_forever(max_cycles=2)

    assert runs == [1, 1]
    assert pauses == [300]


def test_runner_merges_memory_deletion_maintenance_under_the_same_lease() -> None:
    """删除补偿仅可在对账器持有 fencing lease 时执行，报告只汇总计数。"""
    sessions = _sessions()
    calls: list[str] = []

    class ReportingReconciler:
        def __init__(self, session: object) -> None:
            self._session = session

        def run_once(self, *, lease_guard) -> ReconciliationReport:
            assert lease_guard()
            return ReconciliationReport(scanned=1, repaired=0, dead_letter_callbacks=0, failures=0)

    def maintain(session: object, now: datetime, *, lease_guard) -> MemoryDeletionMaintenanceReport:
        assert lease_guard()
        calls.append("maintenance")
        return MemoryDeletionMaintenanceReport(
            delivered_events=2,
            confirmed_purges=1,
            deleted_revisions=3,
        )

    report = ReconcilerRunner(
        sessions,
        "instance-a",
        reconciler_factory=ReportingReconciler,
        maintenance_runner=maintain,
    ).run_once()

    assert calls == ["maintenance"]
    assert report is not None
    assert (
        report.memory_deletion_delivered_events,
        report.memory_deletion_confirmed_purges,
        report.memory_deletion_deleted_revisions,
        report.memory_deletion_aborted,
    ) == (2, 1, 3, False)


def test_taken_over_runner_stops_before_a_later_scan_side_effect() -> None:
    sessions = _sessions()
    started_at = datetime.now(UTC)
    current = started_at
    repaired: list[str] = []

    class TakeoverAwareReconciler:
        def __init__(self, session: object) -> None:
            self._session = session

        def run_once(self, *, lease_guard) -> None:
            nonlocal current
            assert lease_guard()
            repaired.append("before-takeover")
            current = started_at + timedelta(seconds=11)
            assert ReconciliationLeaseService(sessions(), ttl_seconds=10).acquire(
                "instance-b", now=current
            )
            if lease_guard():
                repaired.append("after-takeover")

    ReconcilerRunner(
        sessions,
        "instance-a",
        reconciler_factory=TakeoverAwareReconciler,
        lease_ttl_seconds=10,
        clock=lambda: current,
    ).run_once()

    assert repaired == ["before-takeover"]


def test_takeover_rolls_back_pending_run_admission_and_outbox_writes(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    started_at = datetime.now(UTC)
    current = started_at
    seed = sessions()
    seed.add(
        AgentRun(
            run_id="stale-run",
            agent_id="memoir_agent",
            agent_version="1.0.0",
            package_digest="sha256:test",
            contract_version="1.0.0",
            business_type="couple_memory",
            business_id="archive",
            status="pending",
            dispatch_state="held",
            input_json={},
            authorization_version=1,
            caller_id="caller",
            tenant_id="tenant",
            create_idempotency_key="key",
            callback_target_id="callback",
            business_connector_id="connector",
            trace_id="trace",
            run_deadline_at=started_at + timedelta(days=1),
        )
    )
    seed.commit()
    seed.close()

    class PendingWriteReconciler:
        def __init__(self, session) -> None:
            self._session = session

        def run_once(self, *, lease_guard) -> None:
            nonlocal current
            assert lease_guard()
            run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == "stale-run"))
            assert run is not None
            run.status = "failed"
            self._session.add_all(
                [
                    AdmissionBucket(scope_type="global", scope_key="*"),
                    RuntimeOutboxEvent(
                        outbox_id="stale-run-dispatch",
                        event_type="run_dispatch",
                        aggregate_type="agent_run",
                        aggregate_id=run.run_id,
                        payload_json={"run_id": run.run_id},
                        status="pending",
                        retention_until=started_at + timedelta(days=1),
                    ),
                ]
            )
            current = started_at + timedelta(seconds=11)
            assert ReconciliationLeaseService(sessions(), ttl_seconds=10).acquire(
                "instance-b", now=current
            )
            assert not lease_guard()

    ReconcilerRunner(
        sessions,
        "instance-a",
        reconciler_factory=PendingWriteReconciler,
        lease_ttl_seconds=10,
        clock=lambda: current,
    ).run_once()

    verify = sessions()
    run = verify.scalar(select(AgentRun).where(AgentRun.run_id == "stale-run"))
    assert run is not None and run.status == "pending"
    assert verify.scalars(select(AdmissionBucket)).all() == []
    assert verify.scalars(select(RuntimeOutboxEvent)).all() == []


def test_main_once_calls_a_single_runner_cycle(monkeypatch) -> None:
    calls: list[object] = []

    class RecordingRunner:
        def __init__(self, session_factory, owner_id, *, interval_seconds, maintenance_runner) -> None:
            calls.append((session_factory, owner_id, interval_seconds, maintenance_runner))

        def run_once(self) -> None:
            calls.append("once")

        def run_forever(self) -> None:
            calls.append("forever")

    monkeypatch.setattr(reconciler_module, "ReconcilerRunner", RecordingRunner)
    monkeypatch.setattr(reconciler_module.database, "connect", lambda: calls.append("connect"))
    monkeypatch.setattr(reconciler_module.database, "close", lambda: calls.append("close"))
    monkeypatch.setattr(reconciler_module.database, "get_session_factory", lambda: "sessions")
    monkeypatch.setattr(reconciler_module, "setup_logging", lambda: None)
    monkeypatch.setattr(sys, "argv", ["reconciler", "--once"])

    reconciler_module.main()

    assert "once" in calls
    assert "forever" not in calls
    runner_call = next(call for call in calls if isinstance(call, tuple))
    assert runner_call[0] == "sessions"
    assert runner_call[2] == 300
    assert callable(runner_call[3])


def test_main_uses_300_second_interval_by_default(monkeypatch) -> None:
    intervals: list[int] = []

    class RecordingRunner:
        def __init__(self, session_factory, owner_id, *, interval_seconds, maintenance_runner) -> None:
            intervals.append(interval_seconds)

        def run_once(self) -> None:
            raise AssertionError("default entry must run forever")

        def run_forever(self) -> None:
            pass

    monkeypatch.setattr(reconciler_module, "ReconcilerRunner", RecordingRunner)
    monkeypatch.setattr(reconciler_module.database, "connect", lambda: None)
    monkeypatch.setattr(reconciler_module.database, "close", lambda: None)
    monkeypatch.setattr(reconciler_module.database, "get_session_factory", lambda: "sessions")
    monkeypatch.setattr(reconciler_module, "setup_logging", lambda: None)
    monkeypatch.setattr(sys, "argv", ["reconciler"])

    reconciler_module.main()

    assert intervals == [300]


def _run(run_id: str, *, dispatch_state: str) -> AgentRun:
    return AgentRun(
        run_id=run_id, agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0",
        business_type="couple_memory", business_id="archive", status="pending",
        dispatch_state=dispatch_state, input_json={}, authorization_version=1,
        caller_id="caller", tenant_id="tenant", create_idempotency_key=f"key-{run_id}",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        run_deadline_at=datetime.now(UTC) + timedelta(days=1),
    )


def test_reconciler_marks_only_expired_running_usage_unknown_and_reports_safe_action() -> None:
    sessions = _sessions()
    session = sessions()
    now = datetime.now(UTC)
    session.add_all([
        AgentModelUsage(
            id=1, usage_id="expired-running", run_id="run", step_id="step",
            execution_attempt=1, model_attempt=1, status="running",
            reserved_estimated_cost=1.25, estimated_cost=0.1,
            request_deadline_at=now - timedelta(seconds=1),
        ),
        AgentModelUsage(
            id=2, usage_id="expired-started", run_id="run", step_id="step",
            execution_attempt=1, model_attempt=2, status="started",
            reserved_estimated_cost=2.5, estimated_cost=0.2,
            request_deadline_at=now - timedelta(seconds=1),
        ),
    ])
    session.commit()

    report = ReconciliationService(session).run_once(now=now)

    usages = {usage.usage_id: usage for usage in session.scalars(select(AgentModelUsage)).all()}
    assert usages["expired-running"].status == "outcome_unknown"
    assert usages["expired-running"].estimated_cost == 1.25
    assert usages["expired-started"].status == "started"
    assert report.action_counts == {"model_usage_outcome_unknown": 1}
    assert report.scanned == 1


def test_reconciler_repairs_admission_bucket_from_dispatch_aggregation_with_version_guard() -> None:
    sessions = _sessions()
    session = sessions()
    session.add_all([
        _run("queued", dispatch_state="queued"),
        _run("claimed", dispatch_state="claimed"),
        AdmissionBucket(
            scope_type="global", scope_key="*", held_count=9, queued_count=9,
            running_count=9, version=7,
        ),
    ])
    session.commit()

    report = ReconciliationService(session).run_once()

    bucket = session.scalar(select(AdmissionBucket).where(AdmissionBucket.scope_type == "global"))
    assert bucket is not None
    assert (bucket.held_count, bucket.queued_count, bucket.running_count, bucket.version) == (0, 1, 1, 8)
    assert report.action_counts == {"admission_bucket_repaired": 1}


def test_admission_repair_does_not_overwrite_a_bucket_with_newer_version() -> None:
    sessions = _sessions()
    seed = sessions()
    seed.add(AdmissionBucket(
        scope_type="global", scope_key="*", held_count=0, queued_count=9,
        running_count=0, version=1,
    ))
    seed.commit()
    stale_session, writer = sessions(), sessions()
    stale_bucket = stale_session.scalar(select(AdmissionBucket))
    assert stale_bucket is not None
    writer.execute(
        update(AdmissionBucket)
        .where(AdmissionBucket.id == stale_bucket.id)
        .values(queued_count=4, version=2)
    )
    writer.commit()

    assert not ReconciliationService(stale_session)._repair_admission_bucket(
        stale_bucket, (0, 1, 0)
    )
    stale_session.rollback()

    refreshed = sessions().scalar(select(AdmissionBucket))
    assert refreshed is not None and (refreshed.queued_count, refreshed.version) == (4, 2)


def test_admission_conflict_does_not_flush_stale_bucket_during_later_run_repair() -> None:
    """同轮 bucket 冲突后，其他 Run 修复不得用 ORM 陈旧计数覆盖并发迁移。"""
    sessions = _sessions()
    now = datetime.now(UTC)
    seed = sessions()
    timed_out = _run("timed-out", dispatch_state="queued")
    timed_out.queued_at = now - timedelta(seconds=61)
    seed.add_all([
        timed_out,
        AdmissionBucket(
            scope_type="global", scope_key="*", held_count=0, queued_count=9,
            running_count=0, version=1,
        ),
    ])
    seed.commit()

    scan_session, writer = sessions(), sessions()
    stale_run = scan_session.scalar(select(AgentRun).where(AgentRun.run_id == timed_out.run_id))
    stale_bucket = scan_session.scalar(select(AdmissionBucket))
    assert stale_run is not None and stale_bucket is not None
    writer.execute(
        update(AdmissionBucket)
        .where(AdmissionBucket.id == stale_bucket.id)
        .values(queued_count=4, running_count=1, version=2)
    )
    writer.commit()

    reconciler = ReconciliationService(scan_session)
    assert not reconciler._repair_admission_bucket(stale_bucket, (0, 1, 0))
    assert reconciler._terminate(stale_run, now, "failed", "TEST_TIMEOUT")
    scan_session.commit()

    refreshed = sessions().scalar(select(AdmissionBucket))
    assert refreshed is not None
    assert (refreshed.queued_count, refreshed.running_count, refreshed.version) == (3, 1, 3)


def test_third_consecutive_admission_repair_failure_emits_safe_warning(caplog) -> None:
    sessions = _sessions()
    reconciler = ReconciliationService(sessions())
    reconciler.set_failure_streaks({"admission_bucket": 2})

    reconciler._record_failure("admission_bucket")

    assert reconciler._alerts == 1
    assert "action=admission_bucket consecutive_failures=3" in caplog.text
