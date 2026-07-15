from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.base import Base
from app.models import AgentRun, RuntimeOutboxEvent
from app.runtime.interfaces import AgentRunResult, LeaseContext
from app.services.lease_service import LeaseService
from app.services.run_queue_service import RunQueueService


def test_single_claim_and_expired_lease_creates_new_dispatch() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run = AgentRun(
        run_id="run_lease",
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
    session.commit()
    lease = LeaseService(session)
    assert lease.claim("run_lease", "worker-a") is not None
    assert lease.claim("run_lease", "worker-b") is None
    run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    assert lease.reap_expired() == ["run_lease"]
    assert (
        session.scalar(
            select(RuntimeOutboxEvent).where(
                RuntimeOutboxEvent.aggregate_id == "run_lease"
            )
        )
        is not None
    )


def test_old_lease_context_cannot_write_after_reaper_fences_it() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run = AgentRun(
        run_id="run_fenced",
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
    session.commit()
    lease = LeaseService(session)
    old_context = lease.claim("run_fenced", "worker-a")
    assert old_context is not None
    run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    lease.reap_expired()
    new_context = lease.claim("run_fenced", "worker-b")
    assert new_context is not None

    assert lease.can_write("run_fenced", old_context) is False
    assert lease.can_write("run_fenced", new_context) is True


def test_draining_release_expires_current_lease_without_releasing_admission_early() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run = AgentRun(
        run_id="draining-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="pending", dispatch_state="queued", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        run_deadline_at=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(run)
    session.commit()
    lease = LeaseService(session)
    context = lease.claim("draining-run", "worker-a")
    assert context is not None
    assert lease.release_for_drain("draining-run", context) is True
    session.refresh(run)
    assert run.dispatch_state == "claimed"
    assert run.lease_expires_at is not None
    expires_at = run.lease_expires_at.replace(tzinfo=UTC)
    assert expires_at <= datetime.now(UTC)


def test_queue_releases_lease_when_draining_begins_at_executor_safe_boundary() -> None:
    """draining 在执行器返回非终态安全边界后，不应继续占有 lease。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(
        AgentRun(
            run_id="queue-draining-run", agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="pending", dispatch_state="queued", input_json={},
            authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            run_deadline_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    session.commit()
    draining = {"value": False}

    class SafeBoundaryExecutor:
        def run(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
            draining["value"] = True
            return AgentRunResult(
                run_id=run_id,
                status="waiting_human",
                execution_attempt=lease_context.execution_attempt,
            )

    assert RunQueueService(
        session,
        SafeBoundaryExecutor(),
        worker_id="worker-a",
        is_draining=lambda: draining["value"],
    ).consume("queue-draining-run") is True
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "queue-draining-run"))
    assert run is not None and run.dispatch_state == "claimed"
    assert run.lease_expires_at is not None
    assert run.lease_expires_at.replace(tzinfo=UTC) <= datetime.now(UTC)
