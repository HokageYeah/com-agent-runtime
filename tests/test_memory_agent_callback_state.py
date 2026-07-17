"""回忆录 callback 只能推进当前 generation 的 RunRef。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models.memory_archive import MemoryArchive
from app.services.memory_agent_binding_service import MemoryAgentBindingService
from app.services.memory_agent_callback_service import MemoryAgentCallbackService
from app.services.memory_archive_service import (
    FernetSnapshotCipher,
    FrozenMemoryInput,
    MemoryArchiveService,
)


def _archive_and_ref(session):
    archive = MemoryArchiveService(session, FernetSnapshotCipher(Fernet.generate_key())).create_archives_for_relationship(
        FrozenMemoryInput(1, "space", 1, (1, 2), {}, datetime(2026, 7, 16, tzinfo=UTC), {}, {}, "v1")
    )[0]
    ref = MemoryAgentBindingService(session).bind(archive.archive_id, "run-1", 0)
    session.commit()
    return archive, ref


def _payload(archive_id: str, event: str, status: str) -> dict[str, object]:
    return {
        "event": event, "event_id": f"event-{event}", "event_seq": 1,
        "status_version": 2, "run_id": "run-1", "agent_id": "memoir_agent",
        "business_id": archive_id, "status": status, "error": None, "public_trace": [],
    }


def test_callback_rejects_run_when_archive_generation_has_changed() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive, _ = _archive_and_ref(session)
    current = session.get(MemoryArchive, archive.id)
    assert current is not None
    current.generation_epoch = 1
    session.commit()

    with pytest.raises(ValueError, match="MEMORY_CALLBACK_RUN_NOT_ACTIVE"):
        MemoryAgentCallbackService(session).apply(_payload(archive.archive_id, "run_started", "running"))


def test_success_callback_without_published_revision_keeps_run_ref_reconciling() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive, ref = _archive_and_ref(session)

    assert MemoryAgentCallbackService(session).apply(_payload(archive.archive_id, "run_succeeded", "succeeded")) is False
    session.commit()
    session.refresh(ref)
    assert (ref.status, ref.reconciliation_status) == ("pending_start", "needed")


def test_success_callback_requires_archive_succeeded_status() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive, ref = _archive_and_ref(session)
    current = session.get(MemoryArchive, archive.id)
    assert current is not None
    current.published_revision, current.content_status = 1, "running"
    session.commit()

    assert MemoryAgentCallbackService(session).apply(_payload(archive.archive_id, "run_succeeded", "succeeded")) is False
    session.commit()
    session.refresh(ref)
    assert (ref.status, ref.reconciliation_status) == ("pending_start", "needed")


def test_running_callback_persists_only_safe_public_trace() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive, ref = _archive_and_ref(session)
    payload = _payload(archive.archive_id, "step_changed", "running")
    payload["public_trace"] = [{"step": "generate_scenes", "status": "succeeded"}]

    assert MemoryAgentCallbackService(session).apply(payload) is True
    session.commit()
    session.refresh(ref)
    assert ref.public_trace_json == [{"step": "generate_scenes", "status": "succeeded"}]
    session.refresh(archive)
    assert archive.content_status == "running"


def test_late_failure_callback_does_not_downgrade_published_archive() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive, ref = _archive_and_ref(session)
    current = session.get(MemoryArchive, archive.id)
    assert current is not None
    current.published_revision, current.content_status = 1, "succeeded"
    session.commit()
    payload = _payload(archive.archive_id, "run_failed", "failed")

    assert MemoryAgentCallbackService(session).apply(payload) is True
    session.commit()
    session.refresh(ref)
    session.refresh(archive)
    assert ref.status == "failed"
    assert (archive.content_status, archive.published_revision) == ("succeeded", 1)


def test_callback_with_lower_status_version_is_ignored() -> None:
    """相同 Run 的低版本事件不能覆盖已投影的安全状态。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive, ref = _archive_and_ref(session)
    current = _payload(archive.archive_id, "step_changed", "running")
    current["event_seq"], current["status_version"] = 2, 3
    assert MemoryAgentCallbackService(session).apply(current) is True
    old = _payload(archive.archive_id, "run_started", "running")
    old["event_seq"], old["status_version"] = 3, 2
    assert MemoryAgentCallbackService(session).apply(old) is False
    assert (ref.event_seq, ref.status_version, ref.status) == (2, 3, "running")


def test_run_ref_records_distinct_create_start_and_contract_metadata() -> None:
    """绑定的重放只能补齐缺失摘要，不得改写已冻结的版本身份。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive = MemoryArchiveService(session, FernetSnapshotCipher(Fernet.generate_key())).create_archives_for_relationship(
        FrozenMemoryInput(1, "space", 1, (1, 2), {}, datetime(2026, 7, 16, tzinfo=UTC), {}, {}, "v1")
    )[0]
    binding = MemoryAgentBindingService(session)
    ref = binding.bind(
        archive.archive_id, "lifecycle-run", 0, create_idempotency_key="create:key",
        contract_version="1.0.0", package_digest="sha256:package", authorization_version=7,
    )
    same = binding.bind(archive.archive_id, "lifecycle-run", 0, start_idempotency_key="start:key")

    assert same is ref
    assert (ref.create_idempotency_key, ref.start_idempotency_key) == ("create:key", "start:key")
    assert (ref.contract_version, ref.package_digest, ref.authorization_version) == ("1.0.0", "sha256:package", 7)
    assert ref.row_version == 2
