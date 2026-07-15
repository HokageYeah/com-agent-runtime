"""Task 5/5.5 的事务与调度回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.base import Base
from app.dispatcher import Dispatcher
from app.models import (
    AdmissionBucket,
    AgentDefinition,
    AgentPlan,
    AgentRun,
    AgentStep,
    CallbackEvent,
    RuntimeOutboxEvent,
)
from app.schemas.agent_run import CreateRunCommand
from app.services.agent_run_service import AgentRunService
from app.services.lease_service import LeaseService


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(
        AgentDefinition(
            agent_id="memoir_agent",
            version="1.0.0",
            runtime_type="workflow",
            definition_json={
                "allowed_business_types": ["couple_memory"],
                "workflow_nodes": [
                    {"node_id": "load_snapshot", "node_type": "tool"},
                    {"node_id": "publish_document", "node_type": "tool"},
                ],
            },
            package_digest="sha256:test",
            contract_version="1.0.0",
            status="active",
            status_changed_at=datetime.now(UTC),
            status_changed_by="test",
            status_change_reason="fixture",
        )
    )
    session.commit()
    return session


def _command(start_mode: str = "auto") -> CreateRunCommand:
    return CreateRunCommand(
        agent_id="memoir_agent",
        agent_version="1.0.0",
        business_type="couple_memory",
        business_id="archive_1",
        start_mode=start_mode,
        input={"snapshot_id": "snapshot_1"},
        callback_target_id="memory_callback",
        business_connector_id="couple_diary_backend",
    )


def test_auto_create_persists_plan_admission_and_dispatch_in_one_unit() -> None:
    session = _session()
    run = AgentRunService(session).create(
        _command(), "couple-diary", "tenant-1", "create-1"
    )
    session.commit()

    assert run.dispatch_state == "queued"
    assert session.scalar(select(AgentPlan).where(AgentPlan.run_id == run.run_id))
    assert session.scalars(select(AdmissionBucket)).all()
    assert session.scalar(
        select(RuntimeOutboxEvent).where(
            RuntimeOutboxEvent.aggregate_id == run.run_id,
            RuntimeOutboxEvent.event_type == "run_dispatch",
        )
    )


def test_create_captures_safe_capability_snapshot_from_package_definition() -> None:
    """Run 必须冻结创建时使用的 Package/Connector 身份，不保存任何凭据。"""
    session = _session()
    created = AgentRunService(session).create(
        _command(), "couple-diary", "tenant-1", "create-snapshot"
    )
    session.commit()
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == created.run_id))

    assert run is not None
    assert run.capability_snapshot_json == {
        "agent_id": "memoir_agent",
        "agent_version": "1.0.0",
        "contract_version": "1.0.0",
        "package_digest": "sha256:test",
        "business_connector_id": "couple_diary_backend",
    }


def test_run_detail_exposes_safe_progress_and_current_step_summary() -> None:
    session = _session()
    created = AgentRunService(session).create(
        _command(), "couple-diary", "tenant-1", "create-progress"
    )
    session.add(
        AgentStep(
            id=1,
            step_id="step-load",
            run_id=created.run_id,
            step_name="load_snapshot",
            step_type="tool",
            status="running",
            execution_attempt=1,
            step_attempt=1,
        )
    )
    session.commit()

    detail = AgentRunService(session).get(created.run_id, "couple-diary")

    assert detail.progress == 0
    assert detail.current_step is not None
    assert detail.current_step.step_name == "load_snapshot"


def test_dispatcher_leaves_unknown_event_pending_without_attempt() -> None:
    session = _session()
    event = RuntimeOutboxEvent(
        outbox_id="unknown-event",
        event_type="callback",
        aggregate_type="agent_run",
        aggregate_id="run_1",
        payload_json={},
        status="pending",
        retention_until=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(event)
    session.commit()

    assert Dispatcher(session).dispatch_pending() == 0
    session.refresh(event)
    assert event.status == "pending"
    assert event.attempt_count == 0


def test_reaper_returns_admission_from_running_to_queued() -> None:
    session = _session()
    created = AgentRunService(session).create(
        _command(), "couple-diary", "tenant-1", "create-1"
    )
    session.commit()
    lease = LeaseService(session)
    assert lease.claim(created.run_id, "worker-a") is not None
    session.execute(
        AgentRun.__table__.update()
        .where(AgentRun.run_id == created.run_id)
        .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    session.commit()

    assert lease.reap_expired() == [created.run_id]
    assert all(bucket.running_count == 0 for bucket in session.scalars(select(AdmissionBucket)))
    assert any(bucket.queued_count == 1 for bucket in session.scalars(select(AdmissionBucket)))


def test_direct_cancel_finishes_run_releases_admission_and_writes_callback() -> None:
    session = _session()
    created = AgentRunService(session).create(
        _command("held"), "couple-diary", "tenant-1", "create-1"
    )
    session.commit()

    result = AgentRunService(session).cancel(created.run_id, "couple-diary")
    session.commit()

    assert result.status == "cancelled"
    assert result.dispatch_state == "finished"
    assert session.scalar(select(CallbackEvent).where(CallbackEvent.run_id == created.run_id))
    assert all(bucket.held_count == 0 for bucket in session.scalars(select(AdmissionBucket)))


def test_purge_completion_removes_private_input_and_blocks_retry() -> None:
    session = _session()
    created = AgentRunService(session).create(
        _command("held"), "couple-diary", "tenant-1", "create-1"
    )
    session.commit()
    service = AgentRunService(session)
    service.purge(created.run_id, "couple-diary")
    service.complete_purge(created.run_id)
    session.commit()

    run = session.scalar(select(AgentRun).where(AgentRun.run_id == created.run_id))
    assert run is not None
    assert run.privacy_state == "purged"
    assert run.input_json == {}
