"""回忆录替代版本的宽限期回收回归。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models.memoir.memory_playback_document import MemoryPlaybackDocument
from app.services.memoir.memory_archive_service import (
    FernetSnapshotCipher,
    FrozenMemoryInput,
    MemoryArchiveService,
)
from app.services.memoir.memory_revision_gc_service import MemoryRevisionGcService


def test_gc_deletes_only_expired_non_published_revision() -> None:
    """宽限期后只能删除旧 revision，当前发布版本必须保留。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    service = MemoryArchiveService(session, FernetSnapshotCipher(Fernet.generate_key()))
    archive = service.create_archives_for_relationship(
        FrozenMemoryInput(10, "space-10", 1, (1, 2), {}, datetime(2026, 7, 20, tzinfo=UTC), {}, {}, "v1")
    )[0]
    session.commit()
    service.publish_playback_document(
        archive.archive_id,
        expected_generation_epoch=0,
        document={"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []},
    )
    session.commit()
    baseline = session.scalar(select(MemoryPlaybackDocument).where(
        MemoryPlaybackDocument.archive_id == archive.archive_id,
        MemoryPlaybackDocument.revision == 0,
    ))
    assert baseline is not None
    baseline.retain_until = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    report = MemoryRevisionGcService(session).purge_expired(datetime.now(UTC))
    session.commit()

    assert report.deleted_documents == 1
    assert session.scalar(select(MemoryPlaybackDocument).where(
        MemoryPlaybackDocument.archive_id == archive.archive_id,
        MemoryPlaybackDocument.revision == 0,
    )) is None
    assert session.scalar(select(MemoryPlaybackDocument).where(
        MemoryPlaybackDocument.archive_id == archive.archive_id,
        MemoryPlaybackDocument.revision == 1,
        MemoryPlaybackDocument.is_published.is_(True),
    )) is not None
    assert MemoryRevisionGcService(session).purge_expired(datetime.now(UTC)).deleted_documents == 0


def test_publish_sets_retention_window_on_superseded_revision() -> None:
    """新版本发布后，旧版本应获得固定宽限期而不是立即清理。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    service = MemoryArchiveService(session, FernetSnapshotCipher(Fernet.generate_key()))
    archive = service.create_archives_for_relationship(
        FrozenMemoryInput(11, "space-11", 1, (1, 2), {}, datetime(2026, 7, 20, tzinfo=UTC), {}, {}, "v1")
    )[0]
    session.commit()

    before_publish = datetime.now(UTC)
    service.publish_playback_document(
        archive.archive_id,
        expected_generation_epoch=0,
        document={"schema_version": "1.0.0", "scenes": [], "actions": [], "media_manifest": []},
    )
    old_revision = session.scalar(select(MemoryPlaybackDocument).where(
        MemoryPlaybackDocument.archive_id == archive.archive_id,
        MemoryPlaybackDocument.revision == 0,
    ))

    assert old_revision is not None and old_revision.retain_until is not None
    retain_until = old_revision.retain_until.replace(tzinfo=UTC)
    assert retain_until > before_publish
