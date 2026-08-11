from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models import AgentRun, AgentToolCall, RuntimeAuditRecord
from app.runtime.interfaces import LeaseContext
from app.services.tool_call_audit_service import ToolCallAuditService


def test_publish_audit_keeps_stable_keys_and_safe_result_only():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    service = ToolCallAuditService(session)
    record = service.begin_publish("run-1", 2, "run-1:publish", "run-1:publish", "digest")
    service.succeed(record, 3, "content-digest")
    session.commit()
    saved = session.scalar(select(type(record)))
    assert saved is not None and saved.status == "succeeded"
    assert saved.logical_operation_key == saved.idempotency_key == "run-1:publish"
    assert saved.output_summary == {"revision": 3, "content_digest": "content-digest"}
    assert saved.retention_until is not None
    assert timedelta(days=29, hours=23) < saved.retention_until - saved.created_at <= timedelta(days=30, minutes=1)


def test_publish_audit_marks_retryable_failure_and_unknown_outcome_safely():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    service = ToolCallAuditService(session)
    failed = service.begin_publish("run-1", 1, "stable", "stable", "digest")
    unknown = service.begin_publish("run-1", 2, "stable", "stable", "digest")
    service.fail(failed, "HTTP_503", retryable=True)
    service.unknown(unknown, "HTTP_TIMEOUT")
    session.commit()
    records = session.scalars(select(type(failed)).order_by(type(failed).id)).all()
    assert [(item.status, item.error_code) for item in records] == [
        ("failed", "HTTP_503"), ("outcome_unknown", "HTTP_TIMEOUT"),
    ]
    assert all(item.error_message is None for item in records)
    assert failed.output_summary == {
        "error_code": "HTTP_503",
        "error_type": "tool_request_failed",
        "retryable": True,
        "safe_message": "TOOL_REQUEST_REJECTED",
        "details_visible_to_model": False,
    }


def test_side_effect_audit_records_generic_tool_metadata_and_safe_output() -> None:
    """通用副作用工具只持久化调用元数据和输出摘要。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    service = ToolCallAuditService(session)

    record = service.begin_side_effect(
        run_id="run-1",
        execution_attempt=2,
        step_id="load_snapshot",
        tool_name="memory.get_snapshot",
        tool_version="1.0.0",
        transport="http_business_tool",
        logical_key="run-1:load_snapshot:0",
        idempotency_key="run-1:load_snapshot:0",
        request_digest="request-digest",
        input_summary={"operation": "get_snapshot"},
    )
    service.succeed(record, {"snapshot_digest": "snapshot-digest", "diary_count": 2})
    session.commit()

    saved = session.scalar(select(type(record)))
    assert saved is not None
    assert saved.status == "succeeded"
    assert saved.step_id == "load_snapshot"
    assert saved.tool_name == "memory.get_snapshot"
    assert saved.output_summary == {"snapshot_digest": "snapshot-digest", "diary_count": 2}
    assert saved.error_message is None


def test_native_tool_uses_frozen_budget_and_safe_physical_attempt_audit() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    record = ToolCallAuditService(session).begin_native(
        run_id="run-1", execution_attempt=1, step_id="repair",
        tool_name="runtime.repair_json_once", logical_key="run-1:repair:1",
        request_digest="native-input-digest",
    )

    assert (record.transport, record.side_effect, record.tool_attempt) == (
        "native", False, 1,
    )
    assert record.input_summary == {"operation": "runtime.repair_json_once"}


def test_side_effect_audit_rejects_conflicting_stable_operation() -> None:
    """同一 Run 的逻辑操作不得在重试中悄悄换请求或幂等键。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    service = ToolCallAuditService(session)
    params = {
        "run_id": "run-1",
        "execution_attempt": 1,
        "step_id": "publish_document",
        "tool_name": "memory.publish_playback_document",
        "tool_version": "1.0.0",
        "transport": "http_business_tool",
        "logical_key": "run-1:publish:0",
        "idempotency_key": "run-1:publish:0",
        "request_digest": "digest-a",
        "input_summary": {"operation": "publish_playback_document"},
    }
    service.begin_side_effect(**params)

    with pytest.raises(ValueError, match="TOOL_CALL_OPERATION_CONFLICT"):
        service.begin_side_effect(**(params | {"request_digest": "digest-b"}))
    with pytest.raises(ValueError, match="TOOL_CALL_OPERATION_CONFLICT"):
        service.begin_side_effect(**(params | {"idempotency_key": "different-key"}))
    assert session.scalar(select(func.count()).select_from(app.models.AgentToolCall)) == 1
    audits = session.scalars(select(RuntimeAuditRecord)).all()
    assert len(audits) == 2
    assert all(
        (audit.action, audit.reason_code, audit.outcome, audit.metadata_summary)
        == ("tool_call_operation_conflict", "TOOL_CALL_OPERATION_CONFLICT", "rejected", {"run_id": "run-1"})
        for audit in audits
    )


def test_side_effect_audit_rejects_call_after_frozen_tool_budget_is_consumed() -> None:
    """副作用工具发送前必须按 Run 冻结额度计数，不能靠调用方参数扩大额度。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(AgentRun(
        run_id="budget-run", agent_id="memoir_agent", agent_version="1", package_digest="digest",
        contract_version="1", business_type="memoir", business_id="business", status="running",
        dispatch_state="claimed", input_json={}, capability_snapshot_json={"execution_policy": {"max_tool_calls": 1}},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        run_deadline_at=datetime.now(UTC),
    ))
    session.commit()
    service = ToolCallAuditService(session)
    params = {
        "run_id": "budget-run", "execution_attempt": 1, "step_id": "publish_document",
        "tool_name": "memory.publish", "tool_version": "1", "transport": "http_business_tool",
        "logical_key": "budget-run:publish", "idempotency_key": "budget-run:publish",
        "request_digest": "digest", "input_summary": {"operation": "publish"},
    }

    service.begin_side_effect(**params)
    with pytest.raises(ValueError, match="TOOL_CALL_LIMIT_EXCEEDED"):
        service.begin_side_effect(**(params | {"logical_key": "budget-run:publish-2", "idempotency_key": "budget-run:publish-2"}))


def test_tool_budget_is_scoped_to_the_current_step() -> None:
    """一个节点耗尽工具额度不能错误阻塞另一个节点的独立额度。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(AgentRun(
        run_id="step-budget-run", agent_id="memoir_agent", agent_version="1", package_digest="digest",
        contract_version="1", business_type="memoir", business_id="business", status="running",
        dispatch_state="claimed", input_json={}, capability_snapshot_json={"execution_policy": {"max_tool_calls": 1}},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="callback", business_connector_id="connector", trace_id="trace", run_deadline_at=datetime.now(UTC),
    ))
    session.commit()
    params = {
        "run_id": "step-budget-run", "execution_attempt": 1, "step_id": "step-a",
        "tool_name": "memory.tool", "tool_version": "1", "transport": "http_business_tool",
        "logical_key": "step-a:1", "idempotency_key": "step-a:1", "request_digest": "digest",
        "input_summary": {"operation": "safe"},
    }
    service = ToolCallAuditService(session)

    service.begin_side_effect(**params)
    service.begin_side_effect(**(params | {"step_id": "step-b", "logical_key": "step-b:1", "idempotency_key": "step-b:1"}))


def test_latest_committed_requires_original_logical_key_idempotency_and_digest() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    service = ToolCallAuditService(session)
    record = service.begin_publish("run-1", 1, "logical", "idempotency", "digest")
    service.unknown(record, "HTTP_TIMEOUT")

    assert service.latest_committed(
        "run-1", "logical", "idempotency", "digest"
    ) is not None
    assert service.latest_committed(
        "run-1", "logical", "idempotency", "different-digest"
    ) is None


def test_find_publish_attempt_locates_committed_attempt_without_request_digest() -> None:
    """模型重算导致 request_digest 漂移时，按稳定 logical_key 仍能定位已提交 attempt。

    ``latest_committed`` 保留"原 digest 精确对账"语义；``find_publish_attempt`` 是
    publish query-after-commit 的首要坐标——只按 run_id + logical_key 查
    running/outcome_unknown/succeeded，不要求本次重算 digest 与旧提交一致。
    failed attempt 不返回：首次确定失败不阻止后续按新 digest 重新写入。
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    service = ToolCallAuditService(session)
    committed = service.begin_publish("run-1", 1, "logical", "logical", "first-digest")
    service.succeed(committed, 3, "first-content-digest")
    failed = service.begin_publish("run-1", 2, "logical", "logical", "first-digest")
    service.fail(failed, "HTTP_503", retryable=True)
    session.commit()

    # latest_committed 用漂移 digest 查不到首次 succeeded（保留原精确对账语义）。
    assert service.latest_committed("run-1", "logical", "logical", "drifted-digest") is None
    # find_publish_attempt 按稳定 logical_key 命中首次 succeeded，不受 digest 漂移影响。
    found = service.find_publish_attempt("run-1", "logical")
    assert found is not None
    assert found.id == committed.id
    assert found.status == "succeeded"


def test_late_tool_result_cannot_settle_after_privacy_or_authorization_boundary_changes() -> None:
    """迟到结果只能保留原 running 记录，不能越过权威执行边界。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    now = datetime.now(UTC)
    session.add(AgentRun(
        run_id="guarded-run", agent_id="memoir_agent", agent_version="1", package_digest="digest",
        contract_version="1", business_type="memoir", business_id="business", status="running",
        dispatch_state="claimed", input_json={}, authorization_version=1, caller_id="caller", tenant_id="tenant",
        create_idempotency_key="key", callback_target_id="callback", business_connector_id="connector", trace_id="trace",
        lease_owner="worker", fencing_token=1, execution_attempt=1, lease_expires_at=now.replace(year=now.year + 1),
        run_deadline_at=now.replace(year=now.year + 1),
    ))
    session.commit()
    service = ToolCallAuditService(session)
    record = service.begin_publish("guarded-run", 1, "logical", "key", "digest")
    context = LeaseContext(
        execution_attempt=1, lease_owner="worker", fencing_token=1,
        lease_expires_at=now.replace(year=now.year + 1), privacy_version=1, authorization_version=1,
    )
    run = session.scalar(select(AgentRun).where(AgentRun.run_id == "guarded-run"))
    assert run is not None
    run.authorization_version = 2
    session.commit()

    assert not service.succeed(record, 1, "safe-digest", lease_context=context)
    saved = session.scalar(select(AgentToolCall).where(AgentToolCall.tool_call_id == record.tool_call_id))
    assert saved is not None and saved.status == "running"
