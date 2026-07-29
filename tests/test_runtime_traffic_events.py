"""RuntimeTrafficEvent 只聚合受控流量事实，绝不承载模型或业务内容。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.sqlalchemy_db import Base
from app.models import RuntimeAuditRecord, RuntimeTrafficEvent
from app.runtime.model_gateway import ModelRoute, ProviderTrafficController
from app.services.audit_service import AuditService
from app.services.traffic_event_service import SqlAlchemyTrafficEventRecorder
from tests.test_provider_traffic_controller import BrokenRedis, FakeRedis


def _recorder(session: Session, *, threshold: int = 3) -> SqlAlchemyTrafficEventRecorder:
    return SqlAlchemyTrafficEventRecorder(
        session, audit_service=AuditService(session=session), alert_threshold=threshold,
    )


def test_sqlite_recorder_aggregates_a_unique_minute_window() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    recorder = _recorder(session)
    occurred_at = datetime(2026, 7, 28, 9, 30, 15, tzinfo=UTC)

    recorder.record("permit_rejected", "summary", "rpm_exceeded", occurred_at=occurred_at)
    recorder.record("permit_rejected", "summary", "rpm_exceeded", occurred_at=occurred_at)
    session.commit()

    events = list(session.scalars(select(RuntimeTrafficEvent)))
    assert len(events) == 1
    assert events[0].count == 2
    assert events[0].window_key == "permit_rejected:summary:rpm_exceeded:2026-07-28T09:30:00+00:00"
    assert set(RuntimeTrafficEvent.__table__.columns.keys()) == {
        "id", "event_type", "route_id", "result_code", "occurred_at", "window_key", "count",
    }


def test_threshold_alert_is_written_once_when_the_aggregate_first_crosses() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    recorder = _recorder(session, threshold=3)
    occurred_at = datetime(2026, 7, 28, 9, 30, tzinfo=UTC)

    for _ in range(4):
        recorder.record("redis_fail_closed", "summary", "redis_unavailable", occurred_at=occurred_at)
    session.commit()

    assert session.scalar(select(RuntimeTrafficEvent.count)) == 4
    alerts = list(session.scalars(select(RuntimeAuditRecord).where(
        RuntimeAuditRecord.action == "runtime_traffic_threshold_crossed"
    )))
    assert len(alerts) == 1
    assert alerts[0].metadata_summary == {"decision": "traffic_threshold_crossed"}


def test_sqlite_concurrent_recorders_do_not_create_duplicate_windows(tmp_path: object) -> None:
    database_path = str(tmp_path / "traffic-events.sqlite")  # type: ignore[operator]
    engine = create_engine(
        f"sqlite:///{database_path}", connect_args={"timeout": 10},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    occurred_at = datetime(2026, 7, 28, 9, 30, tzinfo=UTC)

    def append_one(_: int) -> None:
        with sessions.begin() as session:
            _recorder(session).record(
                "circuit_opened", "summary", "circuit_opened", occurred_at=occurred_at,
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append_one, range(16)))

    with sessions() as session:
        events = list(session.scalars(select(RuntimeTrafficEvent)))
    assert [(event.event_type, event.count) for event in events] == [("circuit_opened", 16)]


def test_recorder_rejects_unknown_events_and_never_accepts_sensitive_arguments() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)

    try:
        _recorder(session).record("unknown", "summary", "nope")
    except ValueError as exc:
        assert str(exc) == "RUNTIME_TRAFFIC_EVENT_INVALID"
    else:
        raise AssertionError("unknown traffic event must be rejected")


class _RecordingRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    def record(self, event_type: str, route_id: str, result_code: str, *, occurred_at: datetime | None = None) -> None:
        del occurred_at
        self.events.append((event_type, route_id, result_code))


def _route(**overrides: object) -> ModelRoute:
    values: dict[str, object] = {
        "route_id": "summary", "provider": "provider", "model": "small",
        "endpoint": "https://provider.example/v1/chat", "rate_limit_key": "provider:small",
        "max_concurrency": 1, "rpm_limit": 30, "tpm_limit": 10_000, "timeout_seconds": 5,
        "permit_ttl_seconds": 10, "settle_margin_seconds": 1, "price_unit": "usd_per_1k_tokens",
        "input_price": 1, "output_price": 1, "circuit_failure_threshold": 1,
        "circuit_open_seconds": 5,
    }
    values.update(overrides)
    return ModelRoute(**values)


def test_provider_traffic_records_rejections_cooldown_circuit_and_redis_failure() -> None:
    recorder = _RecordingRecorder()
    route = _route()
    controller = ProviderTrafficController(FakeRedis(), recorder=recorder)

    assert controller.acquire(route, "permit-a").granted
    assert controller.acquire(route, "permit-b").status == "concurrency_exceeded"
    assert controller.settle(route, "permit-a", retry_after_seconds=3).status == "settled"
    assert controller.record_circuit_failure(route).status == "circuit_opened"
    assert controller.record_circuit_success(route).status == "circuit_recovered"
    ProviderTrafficController(BrokenRedis(), recorder=recorder).acquire(route, "permit-c")

    assert recorder.events == [
        ("permit_rejected", "summary", "concurrency_exceeded"),
        ("retry_after_applied", "summary", "rate_limited"),
        ("circuit_opened", "summary", "circuit_opened"),
        ("circuit_recovered", "summary", "circuit_recovered"),
        ("redis_fail_closed", "summary", "redis_unavailable"),
    ]
