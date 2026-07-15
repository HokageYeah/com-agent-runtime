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
