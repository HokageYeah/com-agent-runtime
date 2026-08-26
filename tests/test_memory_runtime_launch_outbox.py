"""回忆录 Runtime 启动 outbox 的可靠性回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models.memoir.memory_agent_run_ref import MemoryAgentRunRef
from app.models.memoir.memory_runtime_launch_event import MemoryRuntimeLaunchEvent
from app.services.memoir.memory_archive_service import (
    FernetSnapshotCipher,
    FrozenMemoryInput,
    MemoryArchiveService,
)
from app.services.memoir.memory_runtime_launch_service import (
    MemoryRuntimeLaunchService,
    RuntimeHeldRun,
)


class FakeRuntimeGateway:
    """只记录安全标识的 Runtime 替身，测试中绝不接触日记正文。"""

    def __init__(self) -> None:
        self.create_keys: list[str] = []
        self.start_keys: list[tuple[str, str]] = []
        self.cancel_keys: list[tuple[str, str]] = []

    def create_held(
        self, *, archive_id: str, snapshot_id: str, generation_epoch: int,
        idempotency_key: str,
    ) -> RuntimeHeldRun:
        self.create_keys.append(idempotency_key)
        return RuntimeHeldRun(
            run_id="runtime-run-1", contract_version="1.0.0",
            package_digest="sha256:memoir", authorization_version=1,
        )

    def start_held(self, *, run_id: str, idempotency_key: str) -> None:
        self.start_keys.append((run_id, idempotency_key))

    def get_run_summary(self, run_id: str) -> RuntimeHeldRun | None:
        """模拟 Runtime 查询；测试只返回已知 held Run 的安全摘要。"""
        if run_id != "runtime-run-1":
            return None
        return RuntimeHeldRun(
            run_id=run_id, contract_version="1.0.0",
            package_digest="sha256:memoir", authorization_version=1,
        )

    def cancel_run(self, run_id: str, idempotency_key: str) -> None:
        """记录补偿取消的稳定键，避免测试依赖任何外部 Runtime 数据。"""
        self.cancel_keys.append((run_id, idempotency_key))


def test_launch_outbox_creates_binds_then_starts_one_held_run() -> None:
    """create/start 分阶段持久化，重复投递不会产生第二个 Runtime Run。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive = MemoryArchiveService(
        session, FernetSnapshotCipher(Fernet.generate_key())
    ).create_archives_for_relationship(FrozenMemoryInput(
        1, "space", 1, (1, 2), {}, datetime(2026, 7, 20, tzinfo=UTC),
        {"diary_ids": []}, {"diaries": []}, "v1",
    ))[0]
    gateway = FakeRuntimeGateway()
    service = MemoryRuntimeLaunchService(session, gateway)

    create_event = service.enqueue(archive.archive_id)
    assert create_event.phase == "create_held"
    assert service.deliver(create_event.event_id) is True
    assert service.deliver(create_event.event_id) is False

    start_event = session.query(MemoryRuntimeLaunchEvent).filter_by(
        archive_id=archive.archive_id, phase="start_held",
    ).one()
    assert service.deliver(start_event.event_id) is True
    ref = session.query(MemoryAgentRunRef).filter_by(run_id="runtime-run-1").one()

    assert gateway.create_keys == [create_event.idempotency_key]
    assert gateway.start_keys == [("runtime-run-1", start_event.idempotency_key)]
    assert (ref.status, ref.create_idempotency_key, ref.start_idempotency_key) == (
        "pending", create_event.idempotency_key, start_event.idempotency_key,
    )


def test_launch_outbox_keeps_pending_event_when_runtime_create_fails() -> None:
    """Runtime 故障不改变 baseline，只记录标准错误码供后续重试。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive = MemoryArchiveService(
        session, FernetSnapshotCipher(Fernet.generate_key())
    ).create_archives_for_relationship(FrozenMemoryInput(
        2, "space-2", 1, (3, 4), {}, datetime(2026, 7, 20, tzinfo=UTC),
        {"diary_ids": []}, {"diaries": []}, "v1",
    ))[0]

    class FailingGateway(FakeRuntimeGateway):
        def create_held(self, **kwargs: object) -> RuntimeHeldRun:
            raise RuntimeError("network body must not persist")

    service = MemoryRuntimeLaunchService(session, FailingGateway())
    event = service.enqueue(archive.archive_id)
    assert service.deliver(event.event_id) is False
    assert (event.status, event.attempt_count, event.last_error_code) == (
        "pending", 1, "MEMORY_RUNTIME_CREATE_FAILED",
    )


def test_launch_outbox_delivers_pending_events_and_repairs_stale_pending_start() -> None:
    """补偿只复用已有事件键，不会因超时新增 Run 或事件。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive = MemoryArchiveService(session, FernetSnapshotCipher(Fernet.generate_key())).create_archives_for_relationship(
        FrozenMemoryInput(3, "space-3", 1, (5, 6), {}, datetime(2026, 7, 20, tzinfo=UTC), {"diary_ids": []}, {"diaries": []}, "v1")
    )[0]
    gateway = FakeRuntimeGateway()
    service = MemoryRuntimeLaunchService(session, gateway)
    service.enqueue(archive.archive_id)

    assert service.deliver_pending() == 1
    start_event = session.query(MemoryRuntimeLaunchEvent).filter_by(phase="start_held").one()
    assert service.deliver_pending() == 1
    ref = session.query(MemoryAgentRunRef).one()
    ref.status = "pending_start"
    ref.updated_at = datetime.now(UTC) - timedelta(seconds=601)

    assert service.reconcile_pending_start(datetime.now(UTC)) == 1
    assert gateway.start_keys[-1] == ("runtime-run-1", start_event.idempotency_key)
    assert session.query(MemoryRuntimeLaunchEvent).count() == 2


def test_orphaned_create_recovery_binds_existing_held_run_without_creating_another() -> None:
    """create 已落库但绑定前失联时，补偿仅查询同一个 Runtime Run 并追加 start。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive = MemoryArchiveService(session, FernetSnapshotCipher(Fernet.generate_key())).create_archives_for_relationship(
        FrozenMemoryInput(4, "space-4", 1, (7, 8), {}, datetime(2026, 7, 20, tzinfo=UTC), {"diary_ids": []}, {"diaries": []}, "v1")
    )[0]
    session.flush()
    event = MemoryRuntimeLaunchEvent(
        event_id="orphan-create", archive_id=archive.archive_id,
        snapshot_id="snapshot-orphan", generation_epoch=archive.generation_epoch,
        phase="create_held", idempotency_key="memory:create:orphan:0",
        run_id="runtime-run-1", status="delivered",
        delivered_at=datetime.now(UTC) - timedelta(seconds=601),
    )
    session.add(event)
    gateway = FakeRuntimeGateway()

    assert MemoryRuntimeLaunchService(session, gateway).reconcile_orphaned_create(datetime.now(UTC)) == 1

    ref = session.query(MemoryAgentRunRef).filter_by(run_id="runtime-run-1").one()
    start = session.query(MemoryRuntimeLaunchEvent).filter_by(
        archive_id=archive.archive_id, phase="start_held",
    ).one()
    assert (ref.snapshot_id, start.run_id, gateway.create_keys, gateway.cancel_keys) == (
        "snapshot-orphan", "runtime-run-1", [], [],
    )


def test_orphaned_create_for_deleted_archive_cancels_runtime_run_without_binding() -> None:
    """归档失效后不得恢复旧 Run，只能用独立稳定键取消该孤儿 held Run。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive = MemoryArchiveService(session, FernetSnapshotCipher(Fernet.generate_key())).create_archives_for_relationship(
        FrozenMemoryInput(5, "space-5", 1, (9, 10), {}, datetime(2026, 7, 20, tzinfo=UTC), {"diary_ids": []}, {"diaries": []}, "v1")
    )[0]
    session.flush()
    archive.deleted_at = datetime.now(UTC)
    event = MemoryRuntimeLaunchEvent(
        event_id="orphan-deleted", archive_id=archive.archive_id,
        snapshot_id="snapshot-deleted", generation_epoch=archive.generation_epoch,
        phase="create_held", idempotency_key="memory:create:deleted:0",
        run_id="runtime-run-1", status="delivered",
        delivered_at=datetime.now(UTC) - timedelta(seconds=601),
    )
    session.add(event)
    gateway = FakeRuntimeGateway()

    assert MemoryRuntimeLaunchService(session, gateway).reconcile_orphaned_create(datetime.now(UTC)) == 1
    assert session.query(MemoryAgentRunRef).count() == 0
    assert gateway.cancel_keys == [("runtime-run-1", f"memory:cancel:{archive.archive_id}:0")]
