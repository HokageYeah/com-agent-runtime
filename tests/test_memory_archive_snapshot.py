"""回忆录 Task 6.5 的归档、加密快照与 revision 0 基础回归。"""

from __future__ import annotations

from datetime import UTC, datetime

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models.memory_archive import MemoryArchive
from app.models.memory_playback_document import MemoryPlaybackDocument
from app.models.memory_snapshot import MemorySnapshot
from app.services.memory_archive_service import (
    FernetSnapshotCipher,
    FrozenMemoryInput,
    MemoryArchiveService,
)
from app.services.memory_player_service import MemoryPlayerService


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
    assert all(archive.content_status == "baseline_ready" for archive in archives)

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
            "scenes": [{"scene_id": "scene-1"}],
            "actions": [{"action_id": "action-1"}],
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
    assert refreshed is not None and refreshed.published_revision == 1
