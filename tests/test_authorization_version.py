"""授权版本必须来自可信服务配置，并在对账时作为撤销权威源。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.core.authorization import AuthorizationService
from app.db.sqlalchemy_db import Base
from app.models import AgentRun
from app.reconciler import ReconcilerRunner
from app.services.agent_run_service import AgentRunService, AgentRunServiceError
from app.services.reconciliation_service import ReconciliationReport


def test_authorization_service_uses_configured_version_and_rejects_invalid_value() -> None:
    service = AuthorizationService({"caller": {"authorization_version": 7}})

    assert service.authorization_version("caller") == 7
    assert AuthorizationService({"legacy": {}}).authorization_version("legacy") == 1


def test_reconciler_runner_injects_authoritative_authorization_resolver() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    session = sessions()
    session.add(_run("run-1", authorization_version=1))
    session.commit()
    seen: list[int | None] = []

    class RecordingReconciler:
        def __init__(self, current: object) -> None:
            self._session = current

        def run_once(self, *, lease_guard, authorization_version_resolver) -> ReconciliationReport:
            run = self._session.query(AgentRun).filter_by(run_id="run-1").one()
            seen.append(authorization_version_resolver(run))
            return ReconciliationReport(0, 0, 0, 0)

    runner = ReconcilerRunner(
        sessions,
        "instance-a",
        reconciler_factory=RecordingReconciler,
        authorization_version_resolver=lambda run: 2 if run.caller_id == "caller" else None,
    )

    assert runner.run_once() is not None
    assert seen == [2]


def test_start_rejects_run_when_authoritative_version_has_changed() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run = _run("run-start", authorization_version=1)
    session.add(run)
    session.commit()

    with pytest.raises(AgentRunServiceError, match="授权版本已变化"):
        AgentRunService(session, authorization_version_resolver=lambda _: 2).start(
            run.run_id, "caller", "start-key"
        )


def _run(run_id: str, *, authorization_version: int) -> AgentRun:
    return AgentRun(
        run_id=run_id, agent_id="memoir_agent", agent_version="1.0.0", package_digest="sha256:test",
        contract_version="1.0.0", business_type="couple_memory", business_id="archive",
        status="pending", dispatch_state="held", input_json={}, authorization_version=authorization_version,
        caller_id="caller", tenant_id="tenant", create_idempotency_key="key", callback_target_id="memory",
        business_connector_id="connector", trace_id="trace", run_deadline_at=datetime.now(UTC) + timedelta(days=1),
    )
