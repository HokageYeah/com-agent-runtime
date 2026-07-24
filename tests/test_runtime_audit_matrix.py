"""Task 13：关键 Runtime 操作必须留下无内容的持久审计事实。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models import AgentCheckpoint, AgentDefinition, AgentRun, RuntimeAuditRecord
from app.services.agent_package_service import AgentPackageService
from app.services.agent_run_service import AgentRunService
from app.services.audit_service import AuditService
from app.services.reconciliation_service import ReconciliationService


def _run(run_id: str, *, state: str = "pending", dispatch: str = "held") -> AgentRun:
    return AgentRun(
        run_id=run_id, agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1", business_type="memoir",
        business_id="archive", status=state, dispatch_state=dispatch, input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        run_deadline_at=datetime.now(UTC) + timedelta(days=1),
    )


def test_run_operations_and_authorization_reconciliation_write_safe_audits() -> None:
    """cancel、purge 与授权收敛均写审计，metadata 不可携带私密字段。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    cancelled, purged, revoked = _run("cancel"), _run("purge"), _run("authorization")
    session.add_all([cancelled, purged, revoked])
    session.commit()

    service = AgentRunService(session)
    service.cancel(cancelled.run_id, "caller")
    service.purge(purged.run_id, "caller")
    ReconciliationService(session).run_once(
        authorization_version_resolver=lambda run: 2 if run.run_id == revoked.run_id else 1
    )
    session.commit()

    records = list(session.scalars(select(RuntimeAuditRecord).order_by(RuntimeAuditRecord.action)))
    assert {item.action for item in records} >= {
        "agent_run_cancelled", "agent_run_purge_requested", "agent_run_authorization_changed",
    }
    allowed = {"content_digest_prefix", "decision", "dispatch_state", "manual_retry_count", "privacy_version", "run_id", "status"}
    assert all(set(item.metadata_summary) <= allowed for item in records)
    assert "prompt" not in str([item.metadata_summary for item in records])


def test_package_lifecycle_and_approval_write_safe_audits(tmp_path) -> None:
    """管理员包变更与人工审批同样必须形成无内容审计事实。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    definition = AgentDefinition(
        agent_id="memoir_agent",
        version="1.0.0",
        runtime_type="workflow",
        definition_json={},
        package_digest="sha256:test",
        contract_version="1",
        status="active",
        status_changed_at=datetime.now(UTC),
        status_changed_by="admin",
        status_change_reason="created",
    )
    waiting = _run("approval", state="waiting_human", dispatch="finished")
    session.add_all([definition, waiting])
    session.commit()

    AgentPackageService(tmp_path, AuditService(session=session)).change_definition_status(
        definition, "deprecated", "admin", "maintenance"
    )
    AgentRunService(session).approve(waiting.run_id, "caller", "approve", waiting.status_version)
    session.commit()

    actions = {item.action for item in session.scalars(select(RuntimeAuditRecord)).all()}
    assert {"agent_package_status_changed", "agent_run_approval_decided"} <= actions


def test_retry_with_checkpoint_writes_safe_audit_record() -> None:
    """手动 retry 必须依赖已有 checkpoint，并只审计计数而不读取其密文。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    definition = AgentDefinition(agent_id="memoir_agent", version="1.0.0", runtime_type="workflow", definition_json={}, package_digest="sha256:test", contract_version="1", status="active", status_changed_at=datetime.now(UTC), status_changed_by="admin", status_change_reason="created")
    failed = _run("retry", state="failed", dispatch="finished")
    session.add_all(
        [
            definition,
            failed,
            AgentCheckpoint(
                checkpoint_id="retry-checkpoint",
                run_id=failed.run_id,
                checkpoint_key="retry:1",
                state_schema_version="1",
                data_classification="private",
                privacy_version=1,
                encrypted_state_blob=b"ciphertext",
                content_digest="digest",
                expires_at=datetime.now(UTC) + timedelta(days=1),
                created_at=datetime.now(UTC),
                state_summary={},
            ),
        ]
    )
    session.commit()

    AgentRunService(session).retry(failed.run_id, "caller")
    session.commit()
    audit = session.scalar(select(RuntimeAuditRecord).where(RuntimeAuditRecord.action == "agent_run_retried"))

    assert audit is not None
    assert audit.metadata_summary == {"manual_retry_count": "1"}
    assert "ciphertext" not in str(audit.metadata_summary)
