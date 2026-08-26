"""回忆录 archive/素材删除的持久补偿与对账测试。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models.memoir.memory_agent_run_ref import MemoryAgentRunRef
from app.models.memoir.memory_archive import MemoryArchive
from app.models.memoir.memory_media_asset import MemoryMediaAsset
from app.models.memoir.memory_playback_document import MemoryPlaybackDocument
from app.models.memoir.memory_snapshot import MemorySnapshot
from app.models.memoir.memory_source_reference import MemorySourceReference
from app.services.memoir.memory_deletion_compensation_service import (
    MemoryDeletionCompensationService,
)


class RecordingRuntimeGateway:
    """仅保存测试所需安全标识，不记录或返回任何播放、日记内容。"""

    def __init__(self) -> None:
        self.purge_calls: list[tuple[str, str]] = []
        self.cancel_calls: list[tuple[str, str]] = []
        self.privacy_states: dict[str, str] = {}

    def request_private_purge(self, run_id: str, idempotency_key: str) -> None:
        self.purge_calls.append((run_id, idempotency_key))

    def cancel_run(self, run_id: str, idempotency_key: str) -> None:
        self.cancel_calls.append((run_id, idempotency_key))

    def get_privacy_state(self, run_id: str) -> str | None:
        return self.privacy_states.get(run_id)


def _seed_archive(session) -> tuple[MemoryArchive, MemoryAgentRunRef]:
    """构造含 baseline、当前引用 revision 与 active Run 的最小归档。"""
    archive = MemoryArchive(
        archive_id="archive-1",
        relationship_id=1,
        space_id="space-1",
        relationship_segment_no=1,
        owner_user_id=1,
        partner_user_id=2,
        content_status="succeeded",
        enhancement_status="succeeded",
        generation_epoch=3,
        active_run_id="run-1",
        published_revision=1,
    )
    baseline = MemoryPlaybackDocument(
        document_id="document-baseline",
        archive_id=archive.archive_id,
        revision=0,
        schema_major=1,
        document_json={"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []},
        content_digest="baseline-digest",
        is_published=False,
    )
    published = MemoryPlaybackDocument(
        document_id="document-current",
        archive_id=archive.archive_id,
        revision=1,
        schema_major=1,
        document_json={"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []},
        content_digest="current-digest",
        is_published=True,
        published_at=datetime.now(UTC),
    )
    ref = MemoryAgentRunRef(
        run_id="run-1",
        archive_id=archive.archive_id,
        generation_epoch=archive.generation_epoch,
        status="running",
    )
    session.add_all(
        [
            archive,
            baseline,
            published,
            ref,
            MemorySourceReference(
                archive_id=archive.archive_id,
                document_id=published.document_id,
                revision=1,
                source_type="diary",
                source_id="diary-1",
            ),
            MemoryMediaAsset(
                asset_id="asset-current",
                archive_id=archive.archive_id,
                document_id=published.document_id,
                media_type="image",
                source_type="diary_original",
                status="ready",
                storage_key="private/object-key",
            ),
        ]
    )
    session.commit()
    return archive, ref


def test_archive_privacy_purge_reuses_stable_key_and_only_completes_after_runtime_query() -> None:
    """删除 archive 先撤权并落库意图；重复投递复用原键，不能把 cancel 当 purge 完成。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive, ref = _seed_archive(session)
    gateway = RecordingRuntimeGateway()
    service = MemoryDeletionCompensationService(session, gateway)

    assert service.request_archive_privacy_purge(archive.archive_id) == 1
    session.commit()
    assert service.request_archive_privacy_purge(archive.archive_id) == 0
    session.commit()

    refreshed = session.scalar(select(MemoryArchive).where(MemoryArchive.archive_id == archive.archive_id))
    run_ref = session.scalar(select(MemoryAgentRunRef).where(MemoryAgentRunRef.run_id == ref.run_id))
    assert refreshed is not None and refreshed.deleted_at is not None
    assert (refreshed.generation_epoch, refreshed.active_run_id) == (4, None)
    assert run_ref is not None and run_ref.purge_state == "requested"
    assert run_ref.privacy_purge_idempotency_key == "memory:purge:archive-1:run-1:3"

    assert service.deliver_pending() == 1
    assert service.deliver_pending() == 0
    assert gateway.purge_calls == [("run-1", "memory:purge:archive-1:run-1:3")]
    assert service.reconcile_purges() == 0
    gateway.privacy_states["run-1"] = "purged"
    assert service.reconcile_purges() == 1
    assert run_ref.purge_state == "purged"
    assert run_ref.privacy_purge_completed_at is not None


def test_source_deletion_repoints_to_baseline_and_retries_active_run_cancel_with_same_key() -> None:
    """原素材删除后不能继续播放旧 revision；取消失败可由持久补偿按原键重试。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive, _ = _seed_archive(session)
    gateway = RecordingRuntimeGateway()
    service = MemoryDeletionCompensationService(session, gateway)

    assert service.invalidate_deleted_source("diary", "diary-1") == 1
    session.commit()

    refreshed = session.scalar(select(MemoryArchive).where(MemoryArchive.archive_id == archive.archive_id))
    baseline = session.scalar(select(MemoryPlaybackDocument).where(MemoryPlaybackDocument.document_id == "document-baseline"))
    current = session.scalar(select(MemoryPlaybackDocument).where(MemoryPlaybackDocument.document_id == "document-current"))
    assert refreshed is not None and (refreshed.published_revision, refreshed.generation_epoch) == (0, 4)
    assert refreshed.active_run_id is None and refreshed.content_status == "baseline"
    assert baseline is not None and baseline.is_published is True
    assert current is None
    assert session.scalar(select(MemorySourceReference).where(MemorySourceReference.archive_id == archive.archive_id)) is None
    assert session.scalar(select(MemorySnapshot).where(MemorySnapshot.archive_id == archive.archive_id)) is None
    assert session.scalar(select(MemoryMediaAsset).where(MemoryMediaAsset.asset_id == "asset-current")) is None

    assert service.deliver_pending() == 1
    assert gateway.cancel_calls == [("run-1", "memory:cancel:archive-1:run-1:4")]
    assert service.invalidate_deleted_source("diary", "diary-1") == 0


def test_maintenance_delivers_and_confirms_purge_without_exposing_private_data() -> None:
    """维护入口只返回数量摘要，完成投递与 Runtime 查询确认。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive, _ = _seed_archive(session)
    gateway = RecordingRuntimeGateway()
    service = MemoryDeletionCompensationService(session, gateway)
    service.request_archive_privacy_purge(archive.archive_id)
    gateway.privacy_states["run-1"] = "purged"

    report = service.run_maintenance(datetime.now(UTC))

    assert (report.delivered_events, report.confirmed_purges, report.deleted_revisions) == (1, 1, 0)
    assert "secret" not in str(report)


def test_maintenance_stops_without_runtime_side_effect_when_lease_is_lost() -> None:
    """租约丢失后不能发送 purge、查询 Runtime 或删除历史版本。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    archive, _ = _seed_archive(session)
    gateway = RecordingRuntimeGateway()
    service = MemoryDeletionCompensationService(session, gateway)
    service.request_archive_privacy_purge(archive.archive_id)

    report = service.run_maintenance(datetime.now(UTC), lease_guard=lambda: False)

    assert report.aborted is True
    assert (report.delivered_events, report.confirmed_purges, report.deleted_revisions) == (0, 0, 0)
    assert gateway.purge_calls == []
