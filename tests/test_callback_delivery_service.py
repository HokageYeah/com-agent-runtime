"""callback Outbox 投递必须读取不可变 CallbackEvent，而不是重建事件。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models import AgentRun, CallbackEvent, RuntimeOutboxEvent
from app.services.callback_delivery_service import CallbackDeliveryService


def test_callback_delivery_service_sends_original_callback_event_payload() -> None:
    sent: list[tuple[str, dict[str, object]]] = []

    class Gateway:
        def send(self, target_id: str, payload: dict[str, object]) -> None:
            sent.append((target_id, payload))

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(_run("run-1"))
    payload = {"event": "run_cancelled", "event_id": "event-1", "event_seq": 1, "status_version": 2, "run_id": "run-1", "business_id": "archive-1"}
    session.add(CallbackEvent(event_id="event-1", run_id="run-1", event_seq=1, status_version=2, event_type="run_cancelled", payload_json=payload, created_at=datetime.now(UTC)))
    outbox = RuntimeOutboxEvent(outbox_id="outbox-1", event_type="callback", aggregate_type="agent_run", aggregate_id="run-1", payload_json={"event_id": "event-1", "target_id": "memory"}, status="pending", retention_until=datetime.now(UTC) + timedelta(days=1))
    session.add(outbox)
    session.commit()

    CallbackDeliveryService(session, Gateway()).send(outbox)

    assert sent == [("memory", payload)]


def test_callback_delivery_service_rejects_purged_run_before_send() -> None:
    """已完成私密清理的 Run 不得重放历史 callback。"""
    sent: list[tuple[str, dict[str, object]]] = []

    class Gateway:
        def send(self, target_id: str, payload: dict[str, object]) -> None:
            sent.append((target_id, payload))

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run = _run("purged-run")
    run.privacy_state = "purged"
    callback = CallbackEvent(
        event_id="purged-event", run_id=run.run_id, event_seq=1, status_version=2,
        event_type="run_succeeded", payload_json={"event": "run_succeeded", "event_id": "purged-event", "run_id": run.run_id, "event_seq": 1, "status_version": 2},
        created_at=datetime.now(UTC),
    )
    outbox = RuntimeOutboxEvent(
        outbox_id="purged-outbox", event_type="callback", aggregate_type="agent_run",
        aggregate_id=run.run_id, payload_json={"event_id": callback.event_id, "target_id": "memory"},
        status="pending", retention_until=datetime.now(UTC) + timedelta(days=1),
    )
    session.add_all([run, callback, outbox])
    session.commit()

    try:
        CallbackDeliveryService(session, Gateway()).send(outbox)
    except ValueError as exc:
        assert str(exc) == "CALLBACK_RUN_NOT_ACTIVE"
    else:
        raise AssertionError("purged Run callback 应被拒绝")
    assert sent == []


def _run(run_id: str) -> AgentRun:
    """构造不含日记正文的最小 Runtime Run 测试记录。"""
    return AgentRun(
        run_id=run_id, agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive-1", status="succeeded", dispatch_state="finished", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="memory", business_connector_id="connector", trace_id="trace",
        run_deadline_at=datetime.now(UTC) + timedelta(days=1),
    )
