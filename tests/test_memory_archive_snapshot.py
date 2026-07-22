"""回忆录 Task 6.5 的归档、加密快照与 revision 0 基础回归。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models.memory_action import MemoryAction
from app.models.memory_archive import MemoryArchive
from app.models.memory_media_asset import MemoryMediaAsset
from app.models.memory_playback_document import MemoryPlaybackDocument
from app.models.memory_scene import MemoryScene
from app.models.memory_snapshot import MemorySnapshot
from app.models.memory_source_reference import MemorySourceReference
from app.services.memory_agent_binding_service import MemoryAgentBindingService
from app.services.memory_archive_service import (
    FernetSnapshotCipher,
    FrozenMemoryInput,
    MemoryArchiveService,
)
from app.services.memory_player_service import MemoryPlayerService
from app.services.memory_snapshot_service import MemorySnapshotService
from app.services.memory_source_reference_service import MemorySourceReferenceService


def test_create_archives_freezes_encrypted_snapshot_and_publishes_baseline() -> None:
    """一次关系归档应为双方隔离创建 archive、快照和可播放 revision 0。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    cipher = FernetSnapshotCipher(Fernet.generate_key())
    service = MemoryArchiveService(session, cipher)

    archives = service.create_archives_for_relationship(
        FrozenMemoryInput(
            relationship_id=42,
            space_id="space-42",
            relationship_segment_no=3,
            owner_user_ids=(1001, 1002),
            partner_names={1001: "小林", 1002: "小周"},
            snapshot_cutoff_at=datetime(2026, 7, 15, tzinfo=UTC),
            source_manifest={"diary_ids": [1, 2], "bet_ids": []},
            snapshot_payload={"diaries": [{"id": 1, "content": "私密日记"}]},
            privacy_filter_version="v1",
        )
    )
    session.commit()

    assert len(archives) == 2
    assert {archive.owner_user_id for archive in archives} == {1001, 1002}
    assert all(archive.published_revision == 0 for archive in archives)
    assert all(archive.content_status == "baseline" for archive in archives)

    snapshot = session.scalar(
        select(MemorySnapshot).where(
            MemorySnapshot.archive_id == archives[0].archive_id
        )
    )
    assert snapshot is not None
    assert snapshot.encrypted_payload != (
        '{"diaries":[{"id":1,"content":"私密日记"}]}'.encode()
    )
    assert cipher.decrypt_json(snapshot.encrypted_payload) == {
        "diaries": [{"id": 1, "content": "私密日记"}]
    }
    assert snapshot.source_manifest_hash

    document = session.scalar(
        select(MemoryPlaybackDocument).where(
            MemoryPlaybackDocument.archive_id == archives[0].archive_id,
            MemoryPlaybackDocument.revision == 0,
        )
    )
    assert document is not None
    assert document.is_published is True
    assert session.scalars(select(MemoryArchive)).all()


def test_atomic_publish_advances_only_archive_published_revision() -> None:
    """增强作品必须完整落库后才切换 published_revision，读取不能拼接草稿。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    cipher = FernetSnapshotCipher(Fernet.generate_key())
    archive = MemoryArchiveService(session, cipher).create_archives_for_relationship(
        FrozenMemoryInput(
            relationship_id=42,
            space_id="space-42",
            relationship_segment_no=3,
            owner_user_ids=(1001, 1002),
            partner_names={1001: "小林", 1002: "小周"},
            snapshot_cutoff_at=datetime(2026, 7, 15, tzinfo=UTC),
            source_manifest={"diary_ids": [1]},
            snapshot_payload={"diaries": []},
            privacy_filter_version="v1",
        )
    )[0]
    session.commit()
    service = MemoryArchiveService(session, cipher)

    published = service.publish_playback_document(
        archive.archive_id,
        expected_generation_epoch=0,
        document={
            "schema_version": "1.0.0",
            "scenes": [{"scene_id": "scene-1", "scene_type": "summary"}],
            "actions": [{
                "action_id": "action-1", "scene_id": "scene-1",
                "action_type": "show_card", "duration_ms": 3000,
            }],
            "media_manifest": [],
        },
    )
    session.commit()

    assert published.revision == 1
    player_document = MemoryPlayerService(session).get_published_document(
        archive.archive_id
    )
    assert player_document.revision == 1
    refreshed = session.scalar(
        select(MemoryArchive).where(MemoryArchive.archive_id == archive.archive_id)
    )
    assert refreshed is not None and (refreshed.published_revision, refreshed.content_status) == (1, "succeeded")


def test_snapshot_service_only_reads_snapshot_bound_to_archive() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    cipher = FernetSnapshotCipher(Fernet.generate_key())
    archive = MemoryArchiveService(session, cipher).create_archives_for_relationship(
        FrozenMemoryInput(1, "space", 1, (1, 2), {}, datetime(2026, 7, 15, tzinfo=UTC), {}, {"diaries": ["私密正文"]}, "v1")
    )[0]
    session.commit()
    snapshot = session.scalar(select(MemorySnapshot).where(MemorySnapshot.archive_id == archive.archive_id))
    assert snapshot is not None
    ref = MemoryAgentBindingService(session).bind(
        archive.archive_id, "snapshot-read-run", 0, snapshot_id=snapshot.snapshot_id,
    )
    ref.status = "pending"
    assert MemorySnapshotService(session, cipher).read_for_runtime(
        archive.archive_id, snapshot.snapshot_id, "snapshot-read-run", 0,
    ) == {"diaries": ["私密正文"]}
    try:
        MemorySnapshotService(session, cipher).read_for_runtime(
            "other", snapshot.snapshot_id, "snapshot-read-run", 0,
        )
    except ValueError as exc:
        assert str(exc) == "MEMORY_SNAPSHOT_UNAVAILABLE"
    else:
        raise AssertionError("跨 archive 快照读取必须被拒绝")


def test_binding_rejects_second_run_and_old_epoch_publish() -> None:
    """同一 archive 只能有一个 active Run，旧 epoch 或旧 Run 均不得发布。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    cipher = FernetSnapshotCipher(Fernet.generate_key())
    archive = MemoryArchiveService(session, cipher).create_archives_for_relationship(
        FrozenMemoryInput(1, "space", 1, (1, 2), {}, datetime(2026, 7, 15, tzinfo=UTC), {}, {}, "v1")
    )[0]
    binding = MemoryAgentBindingService(session)
    binding.bind(archive.archive_id, "run-current", 0)
    session.commit()
    for run_id, epoch, error in (("run-other", 0, "MEMORY_RUN_ALREADY_ACTIVE"), ("run-old", -1, "GENERATION_SUPERSEDED")):
        try:
            binding.bind(archive.archive_id, run_id, epoch)
        except ValueError as exc:
            assert str(exc) == error
        else:
            raise AssertionError("非法 Run 绑定必须拒绝")
    try:
        MemoryArchiveService(session, cipher).publish_playback_document(
            archive.archive_id, expected_generation_epoch=0, expected_run_id="run-old",
            document={"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []},
        )
    except ValueError as exc:
        assert str(exc) == "MEMORY_RUN_NOT_ACTIVE"
    else:
        raise AssertionError("旧 Run 发布必须拒绝")


def test_repeat_archive_returns_existing_isolated_archives_and_playable_baseline() -> None:
    """解绑补偿重放不能复制 archive；即使 Runtime 不可用，revision 0 也能播放。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    cipher = FernetSnapshotCipher(Fernet.generate_key())
    frozen = FrozenMemoryInput(
        relationship_id=99,
        space_id="space-99",
        relationship_segment_no=2,
        owner_user_ids=(1001, 1002),
        partner_names={1001: "小林", 1002: "小周"},
        snapshot_cutoff_at=datetime(2026, 7, 20, tzinfo=UTC),
        source_manifest={"diary_ids": [1], "bet_ids": [2]},
        snapshot_payload={"diaries": [], "bets": []},
        privacy_filter_version="v1",
    )
    service = MemoryArchiveService(session, cipher)
    first = service.create_archives_for_relationship(frozen)
    session.commit()
    second = service.create_archives_for_relationship(frozen)

    assert {archive.archive_id for archive in second} == {
        archive.archive_id for archive in first
    }
    assert len(session.scalars(select(MemoryArchive)).all()) == 2
    for archive in first:
        document = MemoryPlayerService(session).get_published_document(archive.archive_id)
        assert document.document_json["scenes"]
        assert document.document_json["actions"]
        assert session.scalars(
            select(MemoryScene).where(MemoryScene.document_id == document.document_id)
        ).all()
        assert session.scalars(
            select(MemoryAction).where(MemoryAction.scene_id.is_not(None))
        ).all()


def test_repeat_archive_with_changed_frozen_manifest_is_rejected() -> None:
    """同一关系段只能绑定首次冻结的素材版本，补偿不得悄悄改写历史范围。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    cipher = FernetSnapshotCipher(Fernet.generate_key())
    service = MemoryArchiveService(session, cipher)
    original = FrozenMemoryInput(7, "space-7", 1, (1, 2), {}, datetime(2026, 7, 20, tzinfo=UTC), {"diary_ids": [1]}, {}, "v1")
    service.create_archives_for_relationship(original)
    session.commit()

    changed = FrozenMemoryInput(7, "space-7", 1, (1, 2), {}, datetime(2026, 7, 20, tzinfo=UTC), {"diary_ids": [1, 2]}, {}, "v1")
    try:
        service.create_archives_for_relationship(changed)
    except ValueError as exc:
        assert str(exc) == "MEMORY_ARCHIVE_FROZEN_INPUT_CONFLICT"
    else:
        raise AssertionError("同一关系段的冻结素材变化必须被拒绝")


def test_publish_rejects_unknown_scene_type_without_switching_revision() -> None:
    """未知场景类型不能写入版本，更不能切换播放器发布指针。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    cipher = FernetSnapshotCipher(Fernet.generate_key())
    archive = MemoryArchiveService(session, cipher).create_archives_for_relationship(
        FrozenMemoryInput(8, "space-8", 1, (1, 2), {}, datetime(2026, 7, 20, tzinfo=UTC), {}, {}, "v1")
    )[0]
    session.commit()

    with pytest.raises(ValueError, match="MEMORY_SCENE_TYPE_INVALID"):
        MemoryArchiveService(session, cipher).publish_playback_document(
            archive.archive_id,
            expected_generation_epoch=0,
            document={
                "schema_version": "1.0.0",
                "scenes": [{"scene_id": "scene-invalid", "scene_type": "unknown"}],
                "actions": [],
                "media_manifest": [],
            },
        )

    assert MemoryPlayerService(session).get_published_document(archive.archive_id).revision == 0


def test_publish_persists_frozen_source_reference_with_scene_and_action() -> None:
    """作品发布必须与素材反查映射、场景和动作在同一事务持久化。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    cipher = FernetSnapshotCipher(Fernet.generate_key())
    archive = MemoryArchiveService(session, cipher).create_archives_for_relationship(
        FrozenMemoryInput(
            9, "space-9", 1, (1, 2), {}, datetime(2026, 7, 20, tzinfo=UTC),
            {"diary_ids": [101], "bet_ids": []}, {}, "v1",
        )
    )[0]
    session.commit()
    snapshot = session.scalar(select(MemorySnapshot).where(MemorySnapshot.archive_id == archive.archive_id))
    assert snapshot is not None

    published = MemoryArchiveService(session, cipher).publish_playback_document(
        archive.archive_id,
        expected_generation_epoch=0,
        snapshot=snapshot,
        document={
            "schema_version": "1.0.0",
            "scenes": [{
                "scene_id": "scene-101", "scene_type": "diary_highlight",
                "source_refs": ["diary:101"],
            }],
            "actions": [{
                "action_id": "action-101", "scene_id": "scene-101",
                "action_type": "show_card", "duration_ms": 3000,
            }],
            "media_manifest": [],
        },
    )
    session.commit()

    assert session.scalar(select(MemoryScene).where(MemoryScene.document_id == published.document_id)) is not None
    assert session.scalar(select(MemoryAction).where(MemoryAction.scene_id == "scene-101")) is not None
    reference = session.scalar(select(MemorySourceReference).where(
        MemorySourceReference.source_type == "diary", MemorySourceReference.source_id == "101",
    ))
    assert reference is not None
    assert (reference.archive_id, reference.revision, reference.document_id) == (
        archive.archive_id, 1, published.document_id,
    )


def test_memory_contract_versions_are_persisted_for_baseline_and_publication() -> None:
    """快照、文档、场景和动作必须各自持久化当前 major，供旧服务拒绝未来版本。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    cipher = FernetSnapshotCipher(Fernet.generate_key())
    archive = MemoryArchiveService(session, cipher).create_archives_for_relationship(
        FrozenMemoryInput(10, "space-10", 1, (1, 2), {}, datetime(2026, 7, 20, tzinfo=UTC), {}, {}, "v1")
    )[0]
    session.commit()

    snapshot = session.scalar(select(MemorySnapshot).where(MemorySnapshot.archive_id == archive.archive_id))
    baseline = MemoryPlayerService(session).get_published_document(archive.archive_id)

    assert snapshot is not None and snapshot.snapshot_version == 1
    assert snapshot.schema_major == 1
    assert baseline.schema_major == 1
    assert session.scalar(select(MemoryScene).where(MemoryScene.document_id == baseline.document_id)).schema_major == 1
    assert session.scalar(select(MemoryAction).where(MemoryAction.scene_id.is_not(None))).schema_major == 1


def test_published_playback_dto_keeps_one_revision_together() -> None:
    """播放器 DTO 只能返回 published_revision 下的文档、场景、动作和媒体。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    cipher = FernetSnapshotCipher(Fernet.generate_key())
    archive = MemoryArchiveService(session, cipher).create_archives_for_relationship(
        FrozenMemoryInput(11, "space-11", 1, (1, 2), {}, datetime(2026, 7, 20, tzinfo=UTC), {}, {}, "v1")
    )[0]
    MemoryArchiveService(session, cipher).publish_playback_document(
        archive.archive_id,
        expected_generation_epoch=0,
        document={
            "schema_version": "1.0.0",
            "scenes": [{"scene_id": "revision-1-scene", "scene_type": "summary"}],
            "actions": [{"action_id": "revision-1-action", "scene_id": "revision-1-scene", "action_type": "show_card", "duration_ms": 3000}],
            "media_manifest": [],
        },
    )
    session.commit()

    playback = MemoryPlayerService(session).get_published_playback(archive.archive_id)

    assert playback.document.revision == 1
    assert [scene.scene_id for scene in playback.scenes] == ["revision-1-scene"]
    assert [action.action_id for action in playback.actions] == ["revision-1-action"]
    assert playback.media_assets == []


def test_source_reference_service_finds_only_published_revisions() -> None:
    """素材反查仅返回仍是 archive 当前发布指针的 archive/revision 安全摘要。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    cipher = FernetSnapshotCipher(Fernet.generate_key())
    archive = MemoryArchiveService(session, cipher).create_archives_for_relationship(
        FrozenMemoryInput(12, "space-12", 1, (1, 2), {}, datetime(2026, 7, 20, tzinfo=UTC), {"diary_ids": [120]}, {}, "v1")
    )[0]
    snapshot = session.scalar(select(MemorySnapshot).where(MemorySnapshot.archive_id == archive.archive_id))
    assert snapshot is not None
    MemoryArchiveService(session, cipher).publish_playback_document(
        archive.archive_id,
        expected_generation_epoch=0,
        snapshot=snapshot,
        document={
            "schema_version": "1.0.0",
            "scenes": [{"scene_id": "source-scene", "scene_type": "diary_highlight", "source_refs": ["diary:120"]}],
            "actions": [{"action_id": "source-action", "scene_id": "source-scene", "action_type": "show_card", "duration_ms": 3000}],
            "media_manifest": [],
        },
    )
    session.commit()

    matches = MemorySourceReferenceService(session).find_published_revisions_by_source("diary", 120)

    assert [(match.archive_id, match.revision) for match in matches] == [(archive.archive_id, 1)]
    assert MemorySourceReferenceService(session).find_published_revisions_by_source("diary", 999) == []


def test_publish_rejects_unknown_safety_level_or_enabled_media_without_switching_revision() -> None:
    """MVP 媒体关闭时，未知安全等级或非空媒体清单均不得发布。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    cipher = FernetSnapshotCipher(Fernet.generate_key())
    archive = MemoryArchiveService(session, cipher).create_archives_for_relationship(
        FrozenMemoryInput(13, "space-13", 1, (1, 2), {}, datetime(2026, 7, 20, tzinfo=UTC), {}, {}, "v1")
    )[0]
    session.commit()
    service = MemoryArchiveService(session, cipher)

    with pytest.raises(ValueError, match="MEMORY_SCENE_SAFETY_INVALID"):
        service.publish_playback_document(
            archive.archive_id,
            expected_generation_epoch=0,
            document={
                "schema_version": "1.0.0",
                "scenes": [{"scene_id": "unsafe-scene", "scene_type": "summary", "safety_level": "unknown"}],
                "actions": [],
                "media_manifest": [],
            },
        )
    with pytest.raises(ValueError, match="MEMORY_DOCUMENT_MEDIA_INVALID"):
        service.publish_playback_document(
            archive.archive_id,
            expected_generation_epoch=0,
            document={
                "schema_version": "1.0.0",
                "scenes": [],
                "actions": [],
                "media_manifest": [{"asset_id": "not-enabled"}],
            },
        )

    assert MemoryPlayerService(session).get_published_document(archive.archive_id).revision == 0


def test_memory_media_asset_database_contract_rejects_unknown_enums() -> None:
    """媒体资产即使绕开服务层直写，也必须由数据库拒绝未知领域枚举。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(MemoryMediaAsset(
        asset_id="invalid-media",
        archive_id="archive-id",
        document_id="document-id",
        media_type="unknown",
        source_type="ai_generated",
        status="ready",
        storage_key="private/media",
    ))

    with pytest.raises(IntegrityError):
        session.flush()
