"""从真实业务表按已解绑关系段冻结回忆录素材，绝不按“当前最新”扫描。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.bet import Bet
from app.models.couple_relationship import CoupleRelationship
from app.models.diary_entry import DiaryEntry
from app.services.memory_archive_service import FrozenMemoryInput


class MemorySnapshotMaterializer:
    """只读业务数据并构造最小冻结输入，归档写入仍由 MemoryArchiveService 负责。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def freeze_relationship(self, relationship_id: int) -> FrozenMemoryInput:
        """冻结已解绑的关系段；晚到、删除或跨段素材不能进入快照。"""
        relationship = self._session.get(CoupleRelationship, relationship_id)
        if (
            relationship is None
            or relationship.unbound_at is None
            # 情侣日记库的关系状态使用大写枚举；仅允许已归档解绑的关系生成回忆录快照。
            or relationship.status not in {"UNBOUND_ARCHIVED"}
        ):
            raise ValueError("MEMORY_RELATIONSHIP_NOT_ARCHIVABLE")
        cutoff = _as_utc(relationship.unbound_at)
        filters = (
            DiaryEntry.space_id == relationship.space_id,
            DiaryEntry.relationship_id == relationship.id,
            DiaryEntry.relationship_segment_no == relationship.relationship_segment_no,
            DiaryEntry.created_at <= cutoff,
            DiaryEntry.deleted_at.is_(None),
        )
        diaries = self._session.scalars(select(DiaryEntry).where(*filters).order_by(DiaryEntry.id)).all()
        bets = self._session.scalars(select(Bet).where(
            Bet.space_id == relationship.space_id,
            Bet.relationship_id == relationship.id,
            Bet.relationship_segment_no == relationship.relationship_segment_no,
            Bet.created_at <= cutoff,
        ).order_by(Bet.id)).all()
        diary_items = [
            {"id": item.id, "text_excerpt": (item.content or "")[:160]}
            for item in diaries
        ]
        bet_items = [
            {"id": item.id, "title": item.title[:120], "reward_excerpt": item.reward[:120]}
            for item in bets
        ]
        return FrozenMemoryInput(
            relationship_id=relationship.id,
            space_id=str(relationship.space_id),
            relationship_segment_no=relationship.relationship_segment_no,
            owner_user_ids=(relationship.user_a_id, relationship.user_b_id),
            partner_names={}, snapshot_cutoff_at=cutoff,
            source_manifest={"diary_ids": [item["id"] for item in diary_items], "bet_ids": [item["id"] for item in bet_items]},
            snapshot_payload={"diaries": diary_items, "bets": bet_items},
            privacy_filter_version="v1",
        )


def _as_utc(value: object) -> datetime:
    """统一数据库方言的时间表示，确保 cutoff 过滤不因 tzinfo 丢失扩大范围。"""
    if not hasattr(value, "tzinfo"):
        raise ValueError("MEMORY_RELATIONSHIP_NOT_ARCHIVABLE")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
