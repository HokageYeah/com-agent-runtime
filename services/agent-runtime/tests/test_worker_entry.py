"""Worker 入口通过 dispatcher 通知认领 Run，执行器仍可注入 Task 6 实现。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.base import Base
from app.models import AgentRun, RuntimeOutboxEvent
from app.runtime.interfaces import AgentRunResult, LeaseContext
from app.worker import WorkerLoop


class FakeExecutor:
    def __init__(self) -> None:
        self.run_ids: list[str] = []

    def run(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
        self.run_ids.append(run_id)
        return AgentRunResult(
            run_id=run_id,
            status="succeeded",
            execution_attempt=lease_context.execution_attempt,
        )


def test_worker_once_dispatches_and_claims_run_with_injected_executor() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    session.add(
        AgentRun(
            run_id="worker-run",
            agent_id="memoir_agent",
            agent_version="1.0.0",
            package_digest="sha256:test",
            contract_version="1.0.0",
            business_type="couple_memory",
            business_id="archive",
            status="pending",
            dispatch_state="queued",
            input_json={},
            authorization_version=1,
            caller_id="caller",
            tenant_id="tenant",
            create_idempotency_key="key",
            callback_target_id="callback",
            business_connector_id="connector",
            trace_id="trace",
            run_deadline_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    session.add(
        RuntimeOutboxEvent(
            outbox_id="worker-dispatch",
            event_type="run_dispatch",
            aggregate_type="agent_run",
            aggregate_id="worker-run",
            payload_json={"run_id": "worker-run"},
            status="pending",
            retention_until=datetime.now(UTC) + timedelta(days=1),
        )
    )
    session.commit()
    executor = FakeExecutor()

    assert WorkerLoop(factory, executor, worker_id="worker-1").run_once() == 1
    assert executor.run_ids == ["worker-run"]
