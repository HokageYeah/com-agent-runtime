from sqlalchemy import create_engine, select
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
