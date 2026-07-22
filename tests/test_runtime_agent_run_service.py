from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models import (
    AgentDefinition,
    AgentPlan,
    AgentRun,
    AgentStep,
    RuntimeAuditRecord,
)
from app.schemas.agent_package import PackagePolicy
from app.schemas.agent_run import CreateRunCommand
from app.services.agent_run_service import AgentRunService


def test_create_freezes_definition_model_policy_and_ignores_forged_input() -> None:
    """模型预算只可来自已注册 definition，不能由 create 请求伪造。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(AgentDefinition(
        agent_id="governed-agent", version="1", runtime_type="workflow",
        definition_json={
            "policy": {
                "max_model_calls": 2, "max_model_cost": 1.5,
                "max_steps": 16, "max_tool_calls": 20, "max_run_seconds": 300,
            },
            "workflow_nodes": [],
        },
        package_digest="sha256:test", contract_version="1", status="active",
        status_changed_at=datetime.now(UTC), status_changed_by="test",
        status_change_reason="fixture",
    ))
    session.commit()

    created = AgentRunService(session).create(
        CreateRunCommand(
            agent_id="governed-agent", agent_version="1", business_type="memoir",
            business_id="business", start_mode="held",
            input={"model_policy": {"max_model_calls": 999, "max_model_cost": 999}},
            callback_target_id="callback", business_connector_id="connector",
        ), "caller", "tenant", "model-policy-freeze",
    )
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == created.run_id))

    assert run is not None and run.capability_snapshot_json is not None
    assert run.capability_snapshot_json["model_policy"] == {
        "max_model_calls": 2, "max_model_cost": 1.5,
    }
    assert run.capability_snapshot_json["execution_policy"] == {
        "max_steps": 16, "max_tool_calls": 20, "max_run_seconds": 300,
    }


@pytest.mark.parametrize("policy", [
    {"max_model_calls": True},
    {"max_model_calls": -1},
    {"max_model_cost": -0.01},
    {"max_model_cost": float("nan")},
])
def test_package_policy_rejects_invalid_model_limits(policy: dict[str, object]) -> None:
    """bool、负数与 NaN 不能作为可绕过的模型预算。"""
    with pytest.raises(ValueError):
        PackagePolicy.model_validate(policy)


def test_held_create_then_start_writes_one_dispatch_event() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(
        AgentDefinition(
            agent_id="memoir_agent",
            version="1.0.0",
            runtime_type="workflow",
            definition_json={},
            package_digest="sha256:test",
            contract_version="1.0.0",
            status="active",
            status_changed_at=datetime.now(UTC),
            status_changed_by="admin",
            status_change_reason="test",
        )
    )
    session.commit()
    service = AgentRunService(session)
    command = CreateRunCommand(
        agent_id="memoir_agent",
        agent_version="1.0.0",
        business_type="couple_memory",
        business_id="archive_1",
        start_mode="held",
        input={"snapshot_id": "snapshot_1"},
        callback_target_id="memory_callback",
        business_connector_id="couple_diary_backend",
    )

    created = service.create(
        command,
        caller_id="couple-diary",
        tenant_id="tenant_1",
        idempotency_key="create-1",
    )
    started = service.start(
        created.run_id, caller_id="couple-diary", idempotency_key="start-1"
    )

    assert created.dispatch_state == "held"
    assert started.dispatch_state == "queued"
    assert service.count_dispatch_events(created.run_id) == 1


def test_auto_retry_counter_is_independent_from_manual_retry_counter() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run = AgentRun(
        run_id="auto-retry-run",
        agent_id="memoir_agent",
        agent_version="1.0.0",
        package_digest="sha256:test",
        contract_version="1.0.0",
        business_type="couple_memory",
        business_id="archive",
        status="running",
        dispatch_state="claimed",
        input_json={},
        authorization_version=1,
        caller_id="caller",
        tenant_id="tenant",
        create_idempotency_key="key",
        callback_target_id="callback",
        business_connector_id="connector",
        trace_id="trace",
        run_deadline_at=datetime.now(UTC),
    )
    session.add(run)
    session.commit()

    AgentRunService(session).record_auto_retry("auto-retry-run")
    assert run.auto_retry_count == 1
    assert run.manual_retry_count == 0


def test_auto_retry_uses_frozen_policy_instead_of_caller_supplied_limit() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run = AgentRun(
        run_id="frozen-retry-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="running", dispatch_state="claimed", input_json={},
        capability_snapshot_json={"execution_policy": {"max_auto_retry_per_step": 1}},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        run_deadline_at=datetime.now(UTC),
    )
    session.add(run)
    session.commit()

    service = AgentRunService(session)
    service.record_auto_retry("frozen-retry-run")
    with pytest.raises(Exception, match="自动重试次数已耗尽"):
        service.record_auto_retry("frozen-retry-run")


def test_auto_retry_budget_is_scoped_to_the_current_step() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run = AgentRun(
        run_id="step-retry-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="running", dispatch_state="claimed", input_json={},
        capability_snapshot_json={"execution_policy": {"max_auto_retry_per_step": 1}},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace", run_deadline_at=datetime.now(UTC),
    )
    session.add(run)
    session.add_all([
        AgentStep(step_id="step-a", run_id=run.run_id, step_name="a", step_type="tool", status="running", execution_attempt=1),
        AgentStep(step_id="step-b", run_id=run.run_id, step_name="b", step_type="tool", status="running", execution_attempt=1),
    ])
    session.commit()
    service = AgentRunService(session)

    service.record_auto_retry("step-retry-run", "step-a")
    service.record_auto_retry("step-retry-run", "step-b")
    with pytest.raises(Exception, match="自动重试次数已耗尽"):
        service.record_auto_retry("step-retry-run", "step-a")


def test_cancel_persists_desensitized_runtime_audit_record() -> None:
    """审计记录必须与 Run 的状态变更在同一 Session 内持久化。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run = AgentRun(
        run_id="audit-cancel-run",
        agent_id="memoir_agent",
        agent_version="1.0.0",
        package_digest="sha256:test",
        contract_version="1.0.0",
        business_type="couple_memory",
        business_id="archive",
        status="pending",
        dispatch_state="held",
        input_json={"private": "must-not-be-audited"},
        authorization_version=1,
        caller_id="caller",
        tenant_id="tenant",
        create_idempotency_key="key",
        callback_target_id="callback",
        business_connector_id="connector",
        trace_id="trace-audit",
        run_deadline_at=datetime.now(UTC),
    )
    session.add(run)
    session.commit()

    AgentRunService(session).cancel("audit-cancel-run", "caller")
    audit = session.scalar(
        select(RuntimeAuditRecord).where(
            RuntimeAuditRecord.resource_id == "audit-cancel-run"
        )
    )

    assert audit is not None
    assert audit.action == "agent_run_cancelled"
    assert audit.actor_id == "caller"
    assert audit.trace_id == "trace-audit"
    assert audit.metadata_summary == {"dispatch_state": "finished"}


def test_internal_auditor_can_read_other_callers_run_without_private_input() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(
        AgentRun(
            run_id="auditor-read-run", agent_id="memoir_agent", agent_version="1.0.0",
            package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
            business_id="archive", status="pending", dispatch_state="held", input_json={"private": "x"},
            authorization_version=1, caller_id="owner", tenant_id="tenant", create_idempotency_key="key",
            callback_target_id="callback", business_connector_id="connector", trace_id="trace",
            run_deadline_at=datetime.now(UTC),
        )
    )
    session.commit()

    detail = AgentRunService(session).get(
        "auditor-read-run", "runtime-auditor", allow_auditor=True
    )

    assert detail.run_id == "auditor-read-run"
    assert "input_json" not in detail.model_dump()


def test_waiting_human_cancel_is_immediately_terminal() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run = AgentRun(
        run_id="waiting-cancel-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="waiting_human", dispatch_state="finished", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        run_deadline_at=datetime.now(UTC),
    )
    session.add(run)
    session.commit()

    result = AgentRunService(session).cancel("waiting-cancel-run", "caller")

    assert result.status == "cancelled"
    assert result.dispatch_state == "finished"


def test_reject_fallback_marks_only_the_fallback_resume_path() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run = AgentRun(
        run_id="reject-fallback-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="waiting_human", dispatch_state="finished", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        run_deadline_at=datetime.now(UTC),
    )
    session.add(run)
    session.add(
        AgentPlan(
            plan_id="reject-fallback-plan", run_id=run.run_id, strategy="static_workflow",
            steps_json=[{"node_id": "fallback", "node_type": "deterministic"}],
            stop_conditions_json={},
            fallback_policy_json={"reject_action": "fallback", "waiting_human_fallback_node": "fallback"},
            status="planned",
        )
    )
    session.commit()

    result = AgentRunService(session).approve(run.run_id, "caller", "reject", run.status_version)

    assert (result.status, result.dispatch_state) == ("waiting_human", "queued")
    assert run.error_code == "WAITING_HUMAN_FALLBACK"
