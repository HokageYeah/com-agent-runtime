"""执行通道只约束 run_dispatch，全部夹具使用独立内存 SQLite。"""

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.sqlalchemy_db import Base
from app.dispatcher import Dispatcher
from app.models import AgentRun, RuntimeOutboxEvent


@pytest.fixture
def lane_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        # 故意让业务 outbox_id 与自增主键顺序相反；FIFO 只能按数据库 id。
        for index, agent_id in [(1, "memoir_agent"), (2, "soothe_agent"), (3, "soothe_agent")]:
            run_id = f"run-{index}"
            session.add(AgentRun(
                run_id=run_id, agent_id=agent_id, agent_version="1.0.0",
                package_digest="digest", contract_version="1.0.0", business_type="fixture",
                business_id=run_id, status="pending", dispatch_state="queued", input_json={},
                authorization_version=1, caller_id="caller", tenant_id="tenant",
                create_idempotency_key=run_id, callback_target_id="callback",
                business_connector_id="connector", trace_id="trace",
                run_deadline_at=datetime.now(UTC) + timedelta(days=1),
            ))
            session.add(RuntimeOutboxEvent(
                outbox_id=f"dispatch-{4 - index}", event_type="run_dispatch",
                aggregate_type="agent_run", aggregate_id=run_id,
                payload_json={"run_id": run_id, "agent_id": "qa_agent"}, status="pending",
                retention_until=datetime.now(UTC) + timedelta(days=1),
            ))
        session.commit()
        yield session
    engine.dispose()


@pytest.mark.parametrize(("lane", "expected"), [
    ("interactive", ["run-2", "run-3"]),
    ("default", ["run-1", "run-2", "run-3"]),
    ("heavy", []),
])
def test_dispatcher_lane_filter_fifo_and_one_run_per_round(lane_session, lane, expected, caplog):
    delivered = []
    dispatcher = Dispatcher(
        lane_session, lane=lane, agent_lanes={"soothe_agent": "interactive"},
        notify_run=delivered.append,
    )
    with caplog.at_level(logging.INFO):
        for index, run_id in enumerate(expected):
            assert dispatcher.dispatch_pending() == 1
            assert delivered == expected[:index + 1]
            assert f"run_id={run_id}" in caplog.text
            assert f"lane={lane}" in caplog.text
        assert dispatcher.dispatch_pending() == 0
    if expected:
        assert "agent_id=" in caplog.text


@pytest.mark.parametrize("lane", ["default", "interactive", "unmapped"])
def test_callbacks_are_all_delivered_by_any_lane(lane_session, lane):
    for index in range(3):
        lane_session.add(RuntimeOutboxEvent(
            outbox_id=f"callback-{index}", event_type="callback", aggregate_type="agent_run",
            aggregate_id="run-1", payload_json={}, status="pending",
            retention_until=datetime.now(UTC) + timedelta(days=1),
        ))
    lane_session.commit()
    callbacks, runs = [], []
    count = Dispatcher(
        lane_session, lane=lane, agent_lanes={"soothe_agent": "interactive"},
        notify_run=runs.append, callback_sender=lambda event: callbacks.append(event.outbox_id),
    ).dispatch_pending()
    assert callbacks == ["callback-0", "callback-1", "callback-2"]
    assert len(runs) == (0 if lane == "unmapped" else 1)
    assert count == 3 + len(runs)


def test_failed_run_delivery_still_uses_this_rounds_one_claim(lane_session):
    notify = Mock(side_effect=RuntimeError("test failure"))
    assert Dispatcher(lane_session, notify_run=notify).dispatch_pending() == 0
    notify.assert_called_once_with("run-1")
    untouched = lane_session.scalar(select(RuntimeOutboxEvent).where(RuntimeOutboxEvent.outbox_id == "dispatch-2"))
    assert untouched.attempt_count == 0


@pytest.mark.parametrize("lane", ["default", "interactive", "unused"])
def test_worker_lane_argument_and_unmapped_warning(monkeypatch, caplog, lane):
    import app.worker as worker

    monkeypatch.setattr("sys.argv", ["worker", "--once", "--lane", lane, "--worker-id", "test-worker"])
    monkeypatch.setattr(worker.settings, "RUNTIME_AGENT_LANES_JSON", '{"soothe_agent":"interactive"}')
    monkeypatch.setattr(worker, "database", Mock())
    monkeypatch.setattr(worker, "setup_logging", Mock())
    monkeypatch.setattr(worker, "shutdown_logging", Mock())
    monkeypatch.setattr(worker, "install_drain_signal_handlers", Mock())
    monkeypatch.setattr(worker, "configured_callback_gateway", Mock(return_value=None))
    loop = Mock()
    monkeypatch.setattr(worker, "WorkerLoop", loop)
    with caplog.at_level(logging.INFO):
        worker.main()
    assert loop.call_args.kwargs["lane"] == lane
    loop.return_value.run_once.assert_called_once()
    assert f"lane={lane}" in caplog.text and "worker_id=test-worker" in caplog.text
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert bool(warnings) == (lane == "unused")
    if warnings:
        assert "LANE_UNMAPPED" in warnings[0].message


@pytest.mark.parametrize("lane", [None, "interactive"])
def test_worker_defaults_to_default_lane_and_passes_mapping_to_dispatcher(monkeypatch, lane):
    import app.worker as worker

    dispatcher = Mock()
    monkeypatch.setattr(worker, "Dispatcher", dispatcher)
    monkeypatch.setattr(worker, "LeaseService", Mock())
    monkeypatch.setattr(worker.settings, "RUNTIME_AGENT_LANES_JSON", '{"soothe_agent":"interactive"}')
    kwargs = {} if lane is None else {"lane": lane}
    assert worker.WorkerLoop(Mock(), Mock(), worker_id="test-worker", **kwargs).run_once() == 0
    assert dispatcher.call_args.kwargs["lane"] == (lane or "default")
    assert dispatcher.call_args.kwargs["agent_lanes"] == {"soothe_agent": "interactive"}


@pytest.mark.parametrize("last_consumed", [0, 1])
def test_worker_sleeps_only_when_idle_and_checks_draining_before_sleep(monkeypatch, last_consumed):
    """模拟有任务、空闲、draining 三轮；只在第二轮空闲后休眠。"""
    import app.worker as worker

    events = []
    consumed = iter([1, 0, last_consumed])
    draining = iter([False, False, True])

    def run_once():
        result = next(consumed)
        events.append(("run_once", result))
        return result

    def is_draining():
        result = next(draining)
        events.append(("draining", result))
        return result

    monkeypatch.setattr("sys.argv", ["worker", "--poll-seconds", "0.25"])
    monkeypatch.setattr(worker, "database", Mock())
    monkeypatch.setattr(worker, "setup_logging", Mock())
    monkeypatch.setattr(worker, "shutdown_logging", Mock())
    monkeypatch.setattr(worker, "install_drain_signal_handlers", Mock())
    monkeypatch.setattr(worker, "configured_callback_gateway", Mock(return_value=None))
    monkeypatch.setattr(worker, "WorkerLoop", Mock(return_value=Mock(run_once=run_once)))
    monkeypatch.setattr(worker, "WorkerDrainController", Mock(return_value=Mock(is_draining=is_draining)))
    monkeypatch.setattr(worker, "sleep", lambda seconds: events.append(("sleep", seconds)))

    worker.main()

    assert events == [
        ("run_once", 1), ("draining", False),
        ("run_once", 0), ("draining", False), ("sleep", 0.25),
        ("run_once", last_consumed), ("draining", True),
    ]
