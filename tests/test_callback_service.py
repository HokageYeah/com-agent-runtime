"""Callback 可靠投递只重放已持久化的不可变事件。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models import AgentRun, CallbackEvent, RuntimeAuditRecord, RuntimeOutboxEvent
from app.runtime.callback_gateway import CallbackGateway, CallbackTarget
from app.services.callback_service import CallbackDeliveryService
from app.services.outbox_service import OutboxService


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _run() -> AgentRun:
    return AgentRun(
        run_id="run-1", agent_id="memoir_agent", agent_version="1.0.0",
        package_digest="sha256:test", contract_version="1.0.0", business_type="couple_memory",
        business_id="archive-1", status="running", dispatch_state="claimed", input_json={},
        authorization_version=1, caller_id="caller", tenant_id="tenant", create_idempotency_key="key",
        callback_target_id="memory", business_connector_id="connector", trace_id="trace",
        run_deadline_at=datetime.now(UTC) + timedelta(minutes=1),
    )


def test_callback_delivery_replays_original_event_identity_after_dead_letter() -> None:
    session = _session()
    run = _run()
    session.add(run)
    OutboxService(session).append_callback_event(run, "run_started")
    session.commit()
    event = session.scalar(select(RuntimeOutboxEvent))
    callback = session.scalar(select(CallbackEvent))
    assert event is not None and callback is not None
    event.status = "dead_letter"
    session.commit()

    bodies: list[dict[str, object]] = []
    gateway = CallbackGateway(
        {"memory": CallbackTarget("https://business.local/callback", "runtime", "new", "secret")},
        httpx.Client(transport=httpx.MockTransport(lambda request: (bodies.append(json.loads(request.content)), httpx.Response(200, request=request))[1])),
    )
    service = CallbackDeliveryService(session, gateway, authorize_target=lambda current: current.callback_target_id == "memory")

    assert service.replay_dead_letter(event.outbox_id)
    service.deliver(event)
    session.refresh(event)

    assert event.status == "pending"
    assert bodies == [callback.payload_json]
    assert bodies[0]["event_id"] == callback.event_id
    assert bodies[0]["event_seq"] == callback.event_seq
    assert bodies[0]["status_version"] == callback.status_version


def test_callback_delivery_rechecks_target_authorization_before_send() -> None:
    session = _session()
    run = _run()
    session.add(run)
    OutboxService(session).append_callback_event(run, "run_started")
    session.commit()
    event = session.scalar(select(RuntimeOutboxEvent))
    assert event is not None
    gateway = CallbackGateway(
        {"memory": CallbackTarget("https://business.local/callback", "runtime", "new", "secret")},
        httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("must not send"))),
    )

    with pytest.raises(ValueError, match="CALLBACK_TARGET_REVOKED"):
        CallbackDeliveryService(session, gateway, authorize_target=lambda current: False).deliver(event)


@pytest.mark.parametrize(
    "reason_code",
    [
        "AUTHORIZATION_REVOKED",
        "AUTHORIZATION_VERSION_CHANGED",
    ],
)
def test_callback_delivery_persists_fixed_contentless_authorization_reason(
    reason_code: str,
) -> None:
    """授权拒绝必须保留细分固定码，审计不得携带 target、URL 或 callback body。"""
    session = _session()
    run = _run()
    session.add(run)
    OutboxService(session).append_callback_event(run, "run_started")
    session.commit()
    event = session.scalar(select(RuntimeOutboxEvent))
    assert event is not None
    gateway = CallbackGateway(
        {"memory": CallbackTarget("https://business.local/callback", "runtime", "key", "secret")},
        httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("must not send"))),
    )

    with pytest.raises(ValueError, match="CALLBACK_TARGET_REVOKED"):
        CallbackDeliveryService(
            session,
            gateway,
            authorize_target=lambda current: reason_code,
        ).deliver(event)
    session.commit()

    audit = session.scalar(select(RuntimeAuditRecord))
    assert audit is not None
    assert audit.reason_code == reason_code
    assert audit.metadata_summary == {"run_id": run.run_id, "status": run.status}
    serialized = str(audit.metadata_summary)
    assert all(value not in serialized for value in ("business.local", "secret", "run_started"))


def test_callback_delivery_persists_target_missing_reason_without_content() -> None:
    """事件中的 target 不再等于权威 Run target 时，必须写缺失码且不发送。"""
    session = _session()
    run = _run()
    session.add(run)
    OutboxService(session).append_callback_event(run, "run_started")
    session.commit()
    event = session.scalar(select(RuntimeOutboxEvent))
    assert event is not None
    event.payload_json = {**event.payload_json, "target_id": "removed-target"}
    session.commit()

    with pytest.raises(ValueError, match="CALLBACK_TARGET_REVOKED"):
        CallbackDeliveryService(
            session,
            CallbackGateway({}, httpx.Client()),
        ).deliver(event)
    session.commit()

    audit = session.scalar(select(RuntimeAuditRecord))
    assert audit is not None
    assert audit.reason_code == "CALLBACK_TARGET_MISSING"
    assert audit.metadata_summary == {"run_id": run.run_id, "status": run.status}


def test_callback_event_is_immutable_after_persistence() -> None:
    session = _session()
    run = _run()
    session.add(run)
    OutboxService(session).append_callback_event(run, "run_started")
    session.commit()
    callback = session.scalar(select(CallbackEvent))
    assert callback is not None
    callback.event_type = "run_failed"

    with pytest.raises(ValueError, match="CALLBACK_EVENT_IMMUTABLE"):
        session.commit()


@pytest.mark.parametrize(
    ("event_name", "status"),
    [
        ("run_started", "running"), ("step_changed", "running"), ("waiting_human", "waiting_human"),
        ("partial_succeeded", "partial"), ("run_succeeded", "succeeded"),
        ("run_failed", "failed"), ("run_cancelled", "cancelled"),
    ],
)
def test_callback_events_have_versions_and_safe_payload(event_name: str, status: str) -> None:
    session = _session()
    run = _run()
    run.status = status
    session.add(run)

    OutboxService(session).append_callback_event(run, event_name, [{"step": "safe", "status": "succeeded", "prompt": "private"}])
    callback = session.scalar(select(CallbackEvent))

    assert callback is not None
    assert callback.payload_json["event_id"] == callback.event_id
    assert callback.payload_json["event_seq"] == callback.event_seq == 1
    assert callback.payload_json["status_version"] == callback.status_version
    assert callback.payload_json["public_trace"] == [{"step": "safe", "status": "succeeded"}]
    assert "private" not in str(callback.payload_json)


def test_callback_delivery_rejects_outbox_event_without_immutable_callback() -> None:
    session = _session()
    event = RuntimeOutboxEvent(
        outbox_id="orphan", event_type="callback", aggregate_type="agent_run", aggregate_id="run-1",
        payload_json={"event_id": "missing", "target_id": "memory"}, status="pending",
        retention_until=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(event)
    session.commit()
    gateway = CallbackGateway({}, httpx.Client())

    with pytest.raises(ValueError, match="CALLBACK_EVENT_UNAVAILABLE"):
        CallbackDeliveryService(session, gateway).deliver(event)
