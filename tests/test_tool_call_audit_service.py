import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
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
