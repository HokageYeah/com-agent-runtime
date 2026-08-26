"""业务侧用 Runtime 安全状态摘要修复 callback 缺失或乱序。"""

from __future__ import annotations

from datetime import UTC, datetime

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.services.memoir.memory_agent_adapter import RuntimeRunState
from app.services.memoir.memory_agent_binding_service import MemoryAgentBindingService
from app.services.memoir.memory_agent_callback_reconciliation_service import (
    MemoryAgentCallbackReconciliationService,
)
from app.services.memoir.memory_archive_service import (
    FernetSnapshotCipher,
    FrozenMemoryInput,
    MemoryArchiveService,
)


class _Gateway:
    def __init__(self, state: RuntimeRunState | None) -> None:
        self._state = state

    def get_run_state(self, run_id: str) -> RuntimeRunState | None:
        assert run_id == "run-1"
        return self._state


def _archive_and_ref(session):
    archive = MemoryArchiveService(
        session, FernetSnapshotCipher(Fernet.generate_key())
    ).create_archives_for_relationship(
        FrozenMemoryInput(
            1, "space", 1, (1, 2), {}, datetime(2026, 7, 16, tzinfo=UTC), {}, {}, "v1"
        )
    )[0]
    ref = MemoryAgentBindingService(session).bind(archive.archive_id, "run-1", 0)
    session.commit()
    return archive, ref


def test_reconciler_repairs_missing_callback_only_when_remote_versions_advance() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive, ref = _archive_and_ref(session)
    service = MemoryAgentCallbackReconciliationService(
        session,
        _Gateway(RuntimeRunState("run-1", "running", "claimed", "active", 1, 4, 5)),
    )

    assert service.reconcile_run("run-1") is True
    session.commit()
    session.refresh(ref)
    assert (ref.status, ref.event_seq, ref.status_version, ref.reconciliation_status) == (
        "running", 4, 5, "not_needed"
    )

    # 乱序或旧查询绝不能倒退已经应用的 callback 版本。
    service = MemoryAgentCallbackReconciliationService(
        session,
        _Gateway(RuntimeRunState("run-1", "pending", "queued", "active", 1, 3, 4)),
    )
    assert service.reconcile_run("run-1") is False
    session.refresh(ref)
    assert (ref.status, ref.event_seq, ref.status_version) == ("running", 4, 5)
    assert archive.active_run_id == "run-1"


def test_reconciler_does_not_claim_success_without_published_revision() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _, ref = _archive_and_ref(session)
    service = MemoryAgentCallbackReconciliationService(
        session,
        _Gateway(RuntimeRunState("run-1", "succeeded", "finished", "active", 1, 3, 4)),
    )

    assert service.reconcile_run("run-1") is False
    session.commit()
    session.refresh(ref)
    assert (ref.status, ref.event_seq, ref.status_version, ref.reconciliation_status) == (
        "pending_start", 0, 1, "needed"
    )


def test_reconciler_confirms_purge_without_reading_runtime_payload() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _, ref = _archive_and_ref(session)
    ref.purge_state = "requested"
    session.commit()
    service = MemoryAgentCallbackReconciliationService(
        session,
        _Gateway(RuntimeRunState("run-1", "cancelled", "finished", "purged", 2, 3, 4)),
    )

    assert service.reconcile_run("run-1") is True
    session.commit()
    session.refresh(ref)
    assert ref.purge_state == "purged"
    assert ref.privacy_purge_completed_at is not None


def test_reconciler_scans_active_refs_for_missing_callbacks() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    _, ref = _archive_and_ref(session)
    service = MemoryAgentCallbackReconciliationService(
        session,
        _Gateway(RuntimeRunState("run-1", "running", "claimed", "active", 1, 2, 3)),
    )

    assert service.reconcile_pending() == 1
    session.refresh(ref)
    assert (ref.status, ref.event_seq, ref.status_version) == ("running", 2, 3)
