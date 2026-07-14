from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.db.base import Base
from app.models import AdmissionBucket, AgentRun, CallbackEvent, IdempotencyRecord


def test_runtime_metadata_contains_all_authoritative_tables() -> None:
    expected = {
        "agent_definitions",
        "agent_runs",
        "admission_buckets",
        "agent_plans",
        "agent_steps",
        "agent_tool_calls",
        "agent_evaluations",
        "agent_checkpoints",
        "agent_artifacts",
        "agent_model_usages",
        "callback_events",
        "runtime_outbox_events",
        "idempotency_records",
    }

    assert expected <= set(Base.metadata.tables)
    assert AgentRun.__table__.c.create_idempotency_key.index is True
    assert not any(
        isinstance(constraint, UniqueConstraint)
        and {column.name for column in constraint.columns} == {"create_idempotency_key"}
        for constraint in AgentRun.__table__.constraints
    )


def test_concurrency_and_callback_constraints_are_declared() -> None:
    admission_checks = {
        constraint.sqltext.text
        for constraint in AdmissionBucket.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    callback_uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in CallbackEvent.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    idempotency_uniques = {
        tuple(column.name for column in constraint.columns)
        for constraint in IdempotencyRecord.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert any("held_count >= 0" in check for check in admission_checks)
    assert ("run_id", "event_seq") in callback_uniques
    assert ("client_id", "idempotency_key", "scope") in idempotency_uniques
