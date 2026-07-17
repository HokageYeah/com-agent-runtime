"""Outbox dispatcher 必须只认领已启用、有处理器的事件。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.dispatcher import Dispatcher
from app.models import RuntimeOutboxEvent


def test_dispatcher_leases_only_registered_run_dispatch_events() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run_event = RuntimeOutboxEvent(
        outbox_id="dispatch-1",
        event_type="run_dispatch",
        aggregate_type="agent_run",
        aggregate_id="run-1",
        payload_json={"run_id": "run-1"},
        status="pending",
        retention_until=datetime.now(UTC) + timedelta(days=1),
    )
    callback_event = RuntimeOutboxEvent(
        outbox_id="callback-1",
        event_type="callback",
        aggregate_type="agent_run",
        aggregate_id="run-1",
        payload_json={},
        status="pending",
        retention_until=datetime.now(UTC) + timedelta(days=1),
    )
    session.add_all([run_event, callback_event])
    session.commit()

    delivered: list[str] = []
    dispatcher = Dispatcher(session, owner="dispatcher-a", notify_run=delivered.append)
    assert dispatcher.dispatch_pending() == 1
    session.refresh(run_event)
    session.refresh(callback_event)

    assert delivered == ["run-1"]
    assert run_event.status == "delivered"
    assert run_event.lease_owner is None
    assert callback_event.status == "pending"
    assert callback_event.attempt_count == 0


def test_dispatcher_delivers_callback_only_when_sender_is_explicitly_configured() -> None:
    """callback 未配置时不认领；配置投递器后才允许消费历史 pending 事件。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    callback_event = RuntimeOutboxEvent(
        outbox_id="callback-configured", event_type="callback", aggregate_type="agent_run",
        aggregate_id="run-1", payload_json={"event_id": "event-1", "target_id": "memory"},
        status="pending", retention_until=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(callback_event)
    session.commit()
    delivered: list[str] = []

    assert Dispatcher(session, callback_sender=lambda event: delivered.append(event.outbox_id)).dispatch_pending() == 1
    session.refresh(callback_event)

    assert delivered == ["callback-configured"]
    assert callback_event.status == "delivered"


def test_callback_delivery_moves_to_dead_letter_after_five_failures() -> None:
    """callback 死信只记录投递失败，不能回写或改变原 Run 终态。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    callback_event = RuntimeOutboxEvent(
        outbox_id="callback-dead", event_type="callback", aggregate_type="agent_run",
        aggregate_id="run-1", payload_json={"event_id": "event-1", "target_id": "memory"},
        status="pending", attempt_count=4, retention_until=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(callback_event)
    session.commit()

    def fail(_: RuntimeOutboxEvent) -> None:
        raise RuntimeError("business unavailable")

    assert Dispatcher(session, callback_sender=fail).dispatch_pending() == 0
    session.refresh(callback_event)
    assert (callback_event.status, callback_event.attempt_count, callback_event.last_error_code) == (
        "dead_letter", 5, "CALLBACK_DELIVERY_FAILED"
    )
