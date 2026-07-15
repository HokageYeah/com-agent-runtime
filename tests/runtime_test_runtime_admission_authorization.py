"""Task 5 的授权、输入校验与容量拒绝回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.core.authorization import AuthorizationError, AuthorizationService
from app.core.connectors import ConnectorRegistry, ConnectorValidationError
from app.db.sqlalchemy_db import Base
from app.models import AgentDefinition
from app.schemas.agent_run import CreateRunCommand
from app.services.admission_service import AdmissionLimits, AdmissionRejected
from app.services.agent_run_service import AgentRunService, AgentRunServiceError


def test_authorization_rejects_unregistered_callback_target() -> None:
    service = AuthorizationService(
        {
            "couple-diary": {
                "tenant_id": "couple-diary",
                "agent_ids": ["memoir_agent"],
                "business_types": ["couple_memory"],
                "callback_target_ids": ["memory_callback"],
                "connector_ids": ["couple_diary_backend"],
                "data_domains": ["couple_memory"],
            }
        }
    )

    with pytest.raises(AuthorizationError, match="callback"):
        service.authorize_create(
            client_id="couple-diary",
            agent_id="memoir_agent",
            business_type="couple_memory",
            callback_target_id="foreign_callback",
            connector_id="couple_diary_backend",
            data_domain="couple_memory",
        )


def test_connector_registry_rejects_unknown_connector() -> None:
    registry = ConnectorRegistry({"couple_diary_backend": {"enabled": True}})
    with pytest.raises(ConnectorValidationError):
        registry.require_enabled("unknown")


def test_admission_limits_have_explicit_nonzero_defaults() -> None:
    limits = AdmissionLimits()
    assert limits.max_held > 0
    assert limits.max_queued > 0
    assert limits.max_running > 0


def test_create_rejects_input_that_breaks_registered_json_schema() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(
        AgentDefinition(
            agent_id="memoir_agent",
            version="1.0.0",
            runtime_type="workflow",
            definition_json={
                "input_schema": {
                    "type": "object",
                    "required": ["snapshot_id"],
                    "properties": {"snapshot_id": {"type": "string"}},
                }
            },
            package_digest="sha256:test",
            contract_version="1.0.0",
            status="active",
            status_changed_at=datetime.now(UTC),
            status_changed_by="test",
            status_change_reason="fixture",
        )
    )
    session.commit()

    with pytest.raises(AgentRunServiceError, match="input schema"):
        AgentRunService(session).create(
            CreateRunCommand(
                agent_id="memoir_agent",
                agent_version="1.0.0",
                business_type="couple_memory",
                business_id="archive_1",
                input={},
                callback_target_id="memory_callback",
                business_connector_id="couple_diary_backend",
            ),
            caller_id="couple-diary",
            tenant_id="tenant-1",
            idempotency_key="create-1",
        )


def test_admission_rejects_second_queued_run_when_scope_is_full() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(
        AgentDefinition(
            agent_id="memoir_agent",
            version="1.0.0",
            runtime_type="workflow",
            definition_json={},
            package_digest="sha256:test",
            contract_version="1.0.0",
            status="active",
            status_changed_at=datetime.now(UTC),
            status_changed_by="test",
            status_change_reason="fixture",
        )
    )
    session.commit()
    service = AgentRunService(
        session,
        admission_limits=AdmissionLimits(max_held=1, max_queued=1, max_running=1),
    )
    command = CreateRunCommand(
        agent_id="memoir_agent",
        agent_version="1.0.0",
        business_type="couple_memory",
        business_id="archive_1",
        start_mode="auto",
        input={},
        callback_target_id="memory_callback",
        business_connector_id="couple_diary_backend",
    )
    service.create(command, "caller", "tenant", "key-1")
    session.commit()

    with pytest.raises(AdmissionRejected, match="RUNTIME_OVERLOADED"):
        service.create(command, "caller", "tenant", "key-2")
