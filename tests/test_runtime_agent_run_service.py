from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models import (
    AgentCheckpoint,
    AgentDefinition,
    AgentPlan,
    AgentRun,
    AgentStep,
    RuntimeAuditRecord,
)
from app.schemas.agent_package import PackagePolicy
from app.schemas.agent_run import CreateRunCommand
from app.services.agent_run_service import AgentRunService, AgentRunServiceError


def test_create_freezes_definition_model_policy_and_ignores_forged_input() -> None:
    """模型预算只可来自已注册 definition，不能由 create 请求伪造。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(AgentDefinition(
        agent_id="governed-agent", version="1", runtime_type="workflow",
        definition_json={
            "policy": {
                "max_model_calls": 2, "max_model_cost": 1.5, "max_tokens": 1200,
                "max_steps": 16, "max_tool_calls": 20, "max_run_seconds": 300,
            },
            "workflow_nodes": [],
        },
        package_digest="sha256:test", contract_version="1.0.0", status="active",
        status_changed_at=datetime.now(UTC), status_changed_by="test",
        status_change_reason="fixture",
    ))
    session.commit()

    created = AgentRunService(session).create(
        CreateRunCommand(
            agent_id="governed-agent", agent_version="1", business_type="memoir",
            business_id="business", start_mode="held",
            input={"model_policy": {"max_model_calls": 999, "max_model_cost": 999, "max_tokens": 999999}},
            callback_target_id="callback", business_connector_id="connector",
        ), "caller", "tenant", "model-policy-freeze",
    )
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == created.run_id))

    assert run is not None and run.capability_snapshot_json is not None
    assert run.capability_snapshot_json["model_policy"] == {
        "max_model_calls": 2, "max_model_cost": 1.5,
        "max_tokens": 1200,
    }
    assert run.capability_snapshot_json["execution_policy"] == {
        "max_steps": 16, "max_tool_calls": 20, "max_run_seconds": 300,
    }


def test_create_freezes_authoritative_authorization_version() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(AgentDefinition(
        agent_id="agent", version="1", runtime_type="workflow", definition_json={},
        package_digest="sha256:test", contract_version="1.0.0", status="active",
        status_changed_at=datetime.now(UTC), status_changed_by="test", status_change_reason="fixture",
    ))
    session.commit()

    created = AgentRunService(session).create(
        CreateRunCommand(agent_id="agent", agent_version="1", business_type="memoir", business_id="business", input={}, callback_target_id="callback", business_connector_id="connector"),
        "caller", "tenant", "key", authorization_version=9,
    )

    run = session.scalar(select(AgentRun).where(AgentRun.run_id == created.run_id))
    assert run is not None and run.authorization_version == 9


def test_create_log_does_not_expose_caller_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """创建日志只保留 Run 摘要，调用方身份仍仅用于持久化与授权。"""

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    caller_id = "identity-header-value-must-not-log"
    session.add(
        AgentDefinition(
            agent_id="agent",
            version="1",
            runtime_type="workflow",
            definition_json={},
            package_digest="sha256:test",
            contract_version="1.0.0",
            status="active",
            status_changed_at=datetime.now(UTC),
            status_changed_by="test",
            status_change_reason="fixture",
        )
    )
    session.commit()

    with caplog.at_level(logging.INFO):
        AgentRunService(session).create(
            CreateRunCommand(
                agent_id="agent",
                agent_version="1",
                business_type="memoir",
                business_id="business",
                input={},
                callback_target_id="callback",
                business_connector_id="connector",
            ),
            caller_id,
            "tenant",
            "identity-log-test",
        )

    assert all(caller_id not in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("policy", [
    {"max_model_calls": True},
    {"max_model_calls": -1},
    {"max_tokens": True},
    {"max_tokens": -1},
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


def test_start_rejects_definition_digest_drift_without_queuing_run() -> None:
    """held Run 只能按创建时冻结的 Package digest 启动。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = AgentRun(
        run_id="start-digest-drift", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:frozen", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="pending", dispatch_state="held", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        run_deadline_at=now,
    )
    session.add_all([
        run,
        AgentDefinition(
            agent_id=run.agent_id, version=run.agent_version, runtime_type="workflow",
            definition_json={}, package_digest="sha256:drifted", contract_version="1.0.0",
            status="active", status_changed_at=now, status_changed_by="test",
            status_change_reason="fixture",
        ),
    ])
    session.commit()

    with pytest.raises(AgentRunServiceError, match="Package digest 不匹配"):
        AgentRunService(session).start(run.run_id, "caller", "start-drift")

    assert (run.status, run.dispatch_state) == ("pending", "held")
    assert AgentRunService(session).count_dispatch_events(run.run_id) == 0


def test_start_rejects_deprecated_definition_without_queuing_run() -> None:
    """held Run 不能由已停止服务新流量的 Package 启动。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = AgentRun(
        run_id="start-deprecated-package", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:frozen", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="pending", dispatch_state="held", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        run_deadline_at=now,
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

    with pytest.raises(AgentRunServiceError, match="Package"):
        AgentRunService(session).start(run.run_id, "caller", "start-deprecated")

    assert (run.status, run.dispatch_state) == ("pending", "held")
    assert AgentRunService(session).count_dispatch_events(run.run_id) == 0


def test_retry_rejects_definition_digest_drift_without_requeueing_run() -> None:
    """历史 Run retry 只能使用其冻结版本和冻结 digest。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = AgentRun(
        run_id="retry-digest-drift", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:frozen", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="failed", dispatch_state="finished", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        run_deadline_at=now,
    )
    session.add_all([
        run,
        AgentDefinition(
            agent_id=run.agent_id, version=run.agent_version, runtime_type="workflow",
            definition_json={}, package_digest="sha256:drifted", contract_version="1.0.0",
            status="active", status_changed_at=now, status_changed_by="test",
            status_change_reason="fixture",
        ),
        AgentCheckpoint(
            checkpoint_id="retry-digest-checkpoint", run_id=run.run_id,
            checkpoint_key="attempt:1", state_schema_version="1", data_classification="restricted",
            privacy_version=1, encrypted_state_blob=b"safe", state_summary={},
            content_digest="sha256:checkpoint", expires_at=now, created_at=now,
        ),
    ])
    session.commit()

    with pytest.raises(AgentRunServiceError, match="Package digest 不匹配"):
        AgentRunService(session).retry(run.run_id, "caller")

    assert (run.status, run.dispatch_state, run.manual_retry_count) == ("failed", "finished", 0)
    assert AgentRunService(session).count_dispatch_events(run.run_id) == 0


def test_retry_rejects_deprecated_definition_without_requeueing_run() -> None:
    """finished 历史 Run 不能因 retry 重新进入已停止的 Package。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = AgentRun(
        run_id="retry-deprecated-package", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:frozen", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="failed", dispatch_state="finished", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        run_deadline_at=now,
    )
    session.add_all([
        run,
        AgentDefinition(
            agent_id=run.agent_id, version=run.agent_version, runtime_type="workflow",
            definition_json={}, package_digest=run.package_digest, contract_version="1.0.0",
            status="deprecated", status_changed_at=now, status_changed_by="test",
            status_change_reason="fixture",
        ),
        AgentCheckpoint(
            checkpoint_id="retry-deprecated-checkpoint", run_id=run.run_id,
            checkpoint_key="attempt:1", state_schema_version="1", data_classification="restricted",
            privacy_version=1, encrypted_state_blob=b"safe", state_summary={},
            content_digest="sha256:checkpoint", expires_at=now, created_at=now,
        ),
    ])
    session.commit()

    with pytest.raises(AgentRunServiceError, match="Package"):
        AgentRunService(session).retry(run.run_id, "caller")

    assert (run.status, run.dispatch_state, run.manual_retry_count) == ("failed", "finished", 0)
    assert AgentRunService(session).count_dispatch_events(run.run_id) == 0


def test_approve_rejects_deprecated_package_without_redispatching() -> None:
    """waiting_human 的 Run 被 approve 时，若其 Package 已 deprecated，必须拒绝且不再分发。

    生产改动：在状态版本守卫之后调用 ``_assert_frozen_package_definition``，
    与 start/retry 收敛到同一不可执行 Package 谓词，避免人工 approve 把已停止的
    Package 重新放回 queued 分发。
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = AgentRun(
        run_id="approve-deprecated-package", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:frozen", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="waiting_human", dispatch_state="finished", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        run_deadline_at=now,
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

    with pytest.raises(AgentRunServiceError, match="Package"):
        AgentRunService(session).approve(run.run_id, "caller", "approve", run.status_version)

    session.refresh(run)
    assert (run.status, run.dispatch_state) == ("waiting_human", "finished")
    assert AgentRunService(session).count_dispatch_events(run.run_id) == 0


def test_approve_rejects_definition_digest_drift_without_redispatching() -> None:
    """approve 不得用与 Run 冻结 digest 漂移的活跃 Package 恢复执行。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = AgentRun(
        run_id="approve-digest-drift", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:frozen", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="waiting_human", dispatch_state="finished", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        run_deadline_at=now,
    )
    session.add_all([
        run,
        AgentDefinition(
            agent_id=run.agent_id, version=run.agent_version, runtime_type="workflow",
            definition_json={}, package_digest="sha256:drifted", contract_version="1.0.0",
            status="active", status_changed_at=now, status_changed_by="test",
            status_change_reason="fixture",
        ),
    ])
    session.commit()

    with pytest.raises(AgentRunServiceError, match="digest 不匹配"):
        AgentRunService(session).approve(run.run_id, "caller", "approve", run.status_version)

    session.refresh(run)
    assert (run.status, run.dispatch_state) == ("waiting_human", "finished")
    assert AgentRunService(session).count_dispatch_events(run.run_id) == 0


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


def test_partial_retry_accepts_only_post_publish_failed_optional_nodes() -> None:
    """partial retry 的控制面不能把已经提交的主作品重新放回队列。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    run = AgentRun(
        run_id="partial-control-run", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive", status="partial", dispatch_state="finished", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        run_deadline_at=now,
    )
    session.add_all([
        run,
        AgentDefinition(
            agent_id=run.agent_id, version=run.agent_version, runtime_type="workflow",
            definition_json={}, package_digest=run.package_digest,
            contract_version=run.contract_version, status="active", status_changed_at=now,
            status_changed_by="test", status_change_reason="fixture",
        ),
        AgentPlan(
            plan_id="partial-control-plan", run_id=run.run_id, strategy="static_workflow",
            steps_json=[
                {"node_id": "publish_document", "node_type": "tool"},
                {"node_id": "enqueue_media", "node_type": "tool", "optional": True},
            ], stop_conditions_json={}, fallback_policy_json={}, status="planned",
        ),
        AgentCheckpoint(
            checkpoint_id="partial-control-checkpoint", run_id=run.run_id,
            checkpoint_key="attempt:1", state_schema_version="1",
            data_classification="restricted", privacy_version=1,
            encrypted_state_blob=b"safe", state_summary={
                "completed_node_ids": ["publish_document"],
            }, content_digest="sha256:checkpoint", expires_at=now, created_at=now,
        ),
        AgentStep(
            step_id="published-step", run_id=run.run_id, step_name="publish_document",
            step_type="tool", status="succeeded", execution_attempt=1,
        ),
        AgentStep(
            step_id="optional-failed-step", run_id=run.run_id, step_name="enqueue_media",
            step_type="tool", status="failed", execution_attempt=1,
        ),
    ])
    session.commit()

    result = AgentRunService(session).retry(run.run_id, "caller")

    assert (result.status, result.dispatch_state) == ("pending", "queued")
    assert run.manual_retry_count == 1


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
    audit = session.scalar(
        select(RuntimeAuditRecord).where(
            RuntimeAuditRecord.action == "agent_run_audit_read"
        )
    )
    assert audit is not None
    assert audit.actor_type == "auditor"
    assert audit.actor_id == "runtime-auditor"
    assert audit.metadata_summary == {"status": "pending"}
    assert "private" not in str(audit.metadata_summary)


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
        AgentDefinition(
            agent_id=run.agent_id, version=run.agent_version, runtime_type="workflow",
            definition_json={}, package_digest=run.package_digest, contract_version="1.0.0",
            status="active", status_changed_at=datetime.now(UTC), status_changed_by="test",
            status_change_reason="fixture",
        )
    )
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
