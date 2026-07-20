"""回忆录冻结器只按解绑关系段读取真实业务素材。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.models.bet import Bet
from app.models.couple_relationship import CoupleRelationship
from app.models.diary_entry import DiaryEntry
from app.models.memory_runtime_launch_event import MemoryRuntimeLaunchEvent
from app.services.memory_archive_service import (
    FernetSnapshotCipher,
    MemoryArchiveService,
)
from app.services.memory_snapshot_materializer import MemorySnapshotMaterializer
from app.services.relationship_archive_service import RelationshipArchiveService


def test_materializer_freezes_only_current_relationship_segment_before_unbind() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    cutoff = datetime(2026, 7, 20, tzinfo=UTC)
    session.add(CoupleRelationship(
        id=8, user_a_id=1, user_b_id=2, status="UNBOUND_ARCHIVED", space_id=50,
        relationship_segment_no=3, bound_at=cutoff - timedelta(days=3), unbound_at=cutoff,
    ))
    session.add_all([
        DiaryEntry(title="included", content="保留的日记正文", space_id=50, relationship_id=8,
                   relationship_segment_no=3, author_user_id=1, created_at=cutoff - timedelta(minutes=1)),
        DiaryEntry(title="late", content="解绑后的新增正文", space_id=50, relationship_id=8,
                   relationship_segment_no=3, author_user_id=1, created_at=cutoff + timedelta(minutes=1)),
        DiaryEntry(title="old-segment", content="旧段", space_id=50, relationship_id=7,
                   relationship_segment_no=2, author_user_id=1, created_at=cutoff - timedelta(minutes=1)),
        Bet(id=4, space_id=50, relationship_id=8, relationship_segment_no=3,
            creator_user_id=1, receiver_user_id=2, title="完成赌局", reward="奶茶",
            status="completed", completed_at=cutoff - timedelta(minutes=1), created_at=cutoff - timedelta(days=1)),
    ])
    session.commit()

    frozen = MemorySnapshotMaterializer(session).freeze_relationship(8)

    assert frozen.relationship_id == 8
    assert frozen.source_manifest == {"bet_ids": [4], "diary_ids": [1]}
    assert frozen.snapshot_payload["diaries"] == [{"id": 1, "text_excerpt": "保留的日记正文"}]
    assert frozen.snapshot_payload["bets"] == [{"id": 4, "reward_excerpt": "奶茶", "title": "完成赌局"}]

    archives = MemoryArchiveService(session, FernetSnapshotCipher(Fernet.generate_key())).create_archives_for_unbound_relationship(8)
    assert len(archives) == 2


def test_relationship_archive_service_atomically_unbinds_and_creates_archives() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(CoupleRelationship(
        id=9, user_a_id=1, user_b_id=2, status="BOUND", space_id=51,
        relationship_segment_no=1, bound_at=datetime(2026, 7, 1, tzinfo=UTC),
    ))
    session.add(DiaryEntry(
        title="d", content="已冻结", space_id=51, relationship_id=9,
        relationship_segment_no=1, author_user_id=1,
        created_at=datetime(2026, 7, 2, tzinfo=UTC),
    ))
    session.flush()

    archives = RelationshipArchiveService(
        session, FernetSnapshotCipher(Fernet.generate_key())
    ).archive_after_unbind(9, actor_user_id=1, reason="peaceful")
    relationship = session.get(CoupleRelationship, 9)

    assert relationship is not None
    assert (relationship.status, relationship.unbound_by_user_id, relationship.unbound_reason) == (
        "UNBOUND_ARCHIVED", 1, "peaceful",
    )
    assert relationship.unbound_at is not None
    assert len(archives) == 2
    assert session.query(MemoryRuntimeLaunchEvent).filter_by(
        phase="create_held", status="pending",
    ).count() == 2
