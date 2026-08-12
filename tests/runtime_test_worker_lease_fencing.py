from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models import AgentDefinition, AgentRun, RuntimeOutboxEvent
from app.runtime.interfaces import AgentRunResult, LeaseContext
from app.services.lease_service import LeaseService
from app.services.run_queue_service import RunQueueService


def _add_active_test_package(session, changed_at: datetime) -> None:
    """成功 lease 链路显式装配与 Run 冻结身份匹配的有效 Package。"""
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
    # 真实可重新分发的失联 Run 必然持有可执行 Package；装配活跃定义以匹配 Run 冻结身份。
    _add_active_test_package(session, datetime.now(UTC))
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
    _add_active_test_package(session, datetime.now(UTC))
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


@pytest.mark.parametrize(
    ("definition_state", "definition_digest"),
    (
        ("missing", None),
        ("revoked", "sha256:test"),
        ("deprecated", "sha256:test"),
        ("digest_drift", "sha256:unexpected"),
    ),
)
def test_invalid_frozen_package_cannot_write_with_an_otherwise_valid_lease(
    definition_state: str,
    definition_digest: str | None,
) -> None:
    """Package 缺失、非 active 或 digest 漂移都必须阻断共享写闸。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(
        AgentRun(
            run_id=f"{definition_state}-write-run", agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="pending", dispatch_state="claimed", input_json={},
            authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            execution_attempt=1, lease_owner="worker", fencing_token=1,
            lease_expires_at=now + timedelta(minutes=1), run_deadline_at=now + timedelta(days=1),
        )
    )
    if definition_digest is not None:
        session.add(
            AgentDefinition(
                agent_id="memoir_agent", version="1.0.0", runtime_type="workflow",
                definition_json={}, package_digest=definition_digest, contract_version="1.0.0",
                status=(
                    definition_state
                    if definition_state in {"revoked", "deprecated"}
                    else "active"
                ),
                status_changed_at=now, status_changed_by="test", status_change_reason="fixture",
            )
        )
    session.commit()
    context = LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=now + timedelta(minutes=1), privacy_version=1,
        authorization_version=1,
    )

    assert LeaseService(session).can_write(
        f"{definition_state}-write-run",
        context,
    ) is False


def test_heartbeat_refreshes_the_same_mutable_lease_context() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = AgentRun(
        run_id="heartbeat-context-run",
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
        lease_expires_at=now + timedelta(seconds=30),
        run_deadline_at=now + timedelta(days=1),
    )
    session.add(run)
    session.commit()
    context = LeaseContext(
        execution_attempt=1,
        lease_owner="worker-a",
        fencing_token=1,
        lease_expires_at=now + timedelta(seconds=30),
        privacy_version=1,
        authorization_version=1,
    )
    original_context_id = id(context)
    original_expiry = context.lease_expires_at

    assert LeaseService(session, lease_seconds=120).heartbeat(
        "heartbeat-context-run", context
    )

    session.refresh(run)
    assert id(context) == original_context_id
    assert context.lease_expires_at > original_expiry
    assert run.lease_expires_at is not None
    assert run.lease_expires_at.replace(tzinfo=UTC) == context.lease_expires_at


def test_heartbeat_cannot_revive_an_expired_lease() -> None:
    """失效 lease 只能由 reaper 接管，原 owner 不能靠 heartbeat 恢复写权。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = AgentRun(
        run_id="expired-heartbeat-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="running", dispatch_state="claimed", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        execution_attempt=1, lease_owner="worker-a", fencing_token=1,
        lease_expires_at=now - timedelta(seconds=1), run_deadline_at=now + timedelta(days=1),
    )
    session.add(run)
    session.commit()
    context = LeaseContext(
        execution_attempt=1, lease_owner="worker-a", fencing_token=1,
        lease_expires_at=now - timedelta(seconds=1), privacy_version=1, authorization_version=1,
    )

    assert LeaseService(session).heartbeat(run.run_id, context) is False
    session.refresh(run)
    assert run.lease_expires_at is not None and run.lease_expires_at.replace(tzinfo=UTC) <= now


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
    _add_active_test_package(session, datetime.now(UTC))
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
    _add_active_test_package(session, datetime.now(UTC))
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


def test_reap_expired_terminates_unexecutable_package_without_requeue() -> None:
    """claimed 且失租的 Run 若其 Package 已不可执行，reaper 必须终结而不是重新分发。

    生产改动：``reap_expired`` 在重新分发前用与 ``can_write`` 一致的不可执行 Package
    谓词判定，缺失/废弃/digest 漂移的 Run 直接按 PACKAGE_REVOKED 终结，避免 worker 直连
    reap 时给已停止的 Package 产生 ``lease_reaped`` 假分发与无谓 execution_attempt 抖动。
    与 ``test_single_claim_and_expired_lease_creates_new_dispatch``（活跃 Package 正常重新分发）
    形成对照。
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = AgentRun(
        run_id="reap-deprecated-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="pending", dispatch_state="queued", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        run_deadline_at=now + timedelta(days=1),
    )
    session.add_all([
        run,
        AgentDefinition(
            agent_id=run.agent_id, version=run.agent_version, runtime_type="workflow",
            definition_json={}, package_digest=run.package_digest, contract_version="1.0.0",
            status="deprecated", status_changed_at=now, status_changed_by="test",
            status_change_reason="fixture",
        ),
    ])
    session.commit()
    lease = LeaseService(session)
    assert lease.claim("reap-deprecated-run", "worker-a") is not None
    run.lease_expires_at = now - timedelta(seconds=1)
    session.commit()

    recovered = lease.reap_expired()

    session.refresh(run)
    assert recovered == []
    assert (run.status, run.dispatch_state) == ("cancelled", "finished")
    assert (run.lease_owner, run.lease_expires_at) == (None, None)
    assert run.error_code == "PACKAGE_REVOKED"
    assert (
        session.scalar(
            select(RuntimeOutboxEvent).where(
                RuntimeOutboxEvent.aggregate_id == run.run_id,
                RuntimeOutboxEvent.event_type == "run_dispatch",
            )
        )
        is None
    )
