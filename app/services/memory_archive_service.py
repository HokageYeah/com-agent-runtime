"""回忆录归档基础服务：冻结输入、加密快照并发布 revision 0。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memory_archive import MemoryArchive
from app.models.memory_playback_document import MemoryPlaybackDocument
from app.models.memory_snapshot import MemorySnapshot


@dataclass(frozen=True)
class FrozenMemoryInput:
    """素材层提供的已冻结输入；服务不自行查询日记、赌局等来源表。"""

    relationship_id: int
    space_id: str
    relationship_segment_no: int
    owner_user_ids: tuple[int, int]
    partner_names: dict[int, str]
    snapshot_cutoff_at: datetime
    source_manifest: dict[str, Any]
    snapshot_payload: dict[str, Any]
    privacy_filter_version: str


class FernetSnapshotCipher:
    """使用 Fernet 认证加密快照；密钥由部署配置/密钥管理系统注入。"""

    key_id = "memory-snapshot-fernet-v1"

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    def encrypt_json(self, payload: dict[str, Any]) -> bytes:
        return self._fernet.encrypt(_canonical_json(payload))

    def decrypt_json(self, payload: bytes) -> dict[str, Any]:
        decoded = json.loads(self._fernet.decrypt(payload))
        if not isinstance(decoded, dict):
            raise ValueError("加密回忆录快照不是 JSON object")
        return decoded


class MemoryArchiveService:
    """在一个数据库事务内创建双方 archive、snapshot 与 baseline document。"""

    def __init__(self, session: Session, cipher: FernetSnapshotCipher) -> None:
        self._session = session
        self._cipher = cipher

    def create_archives_for_relationship(
        self, frozen: FrozenMemoryInput
    ) -> list[MemoryArchive]:
        """为双方创建隔离归档；调用方负责 commit/rollback。"""
        owner_a, owner_b = frozen.owner_user_ids
        if owner_a == owner_b:
            raise ValueError("回忆录归档必须属于两个不同用户")
        manifest_hash = _digest_json(frozen.source_manifest)
        payload_digest = _digest_json(frozen.snapshot_payload)
        encrypted_payload = self._cipher.encrypt_json(frozen.snapshot_payload)
        archives: list[MemoryArchive] = []
        for owner_user_id, partner_user_id in ((owner_a, owner_b), (owner_b, owner_a)):
            archive_id = str(uuid4())
            archive = MemoryArchive(
                archive_id=archive_id,
                relationship_id=frozen.relationship_id,
                space_id=frozen.space_id,
                relationship_segment_no=frozen.relationship_segment_no,
                owner_user_id=owner_user_id,
                partner_user_id=partner_user_id,
                content_status="baseline",
                enhancement_status="not_started",
                published_revision=0,
                summary="回忆录基础版本已创建，等待增强生成。",
            )
            self._session.add(archive)
            self._session.add(
                MemorySnapshot(
                    snapshot_id=str(uuid4()),
                    archive_id=archive_id,
                    snapshot_version=1,
                    source_manifest_json=frozen.source_manifest,
                    source_manifest_hash=manifest_hash,
                    privacy_filter_version=frozen.privacy_filter_version,
                    snapshot_cutoff_at=frozen.snapshot_cutoff_at,
                    encryption_key_id=self._cipher.key_id,
                    encrypted_payload=encrypted_payload,
                    content_digest=payload_digest,
                )
            )
            baseline = {
                "schema_version": "1.0.0",
                "title": "我们的回忆录",
                "owner_user_id": owner_user_id,
                "partner_name": frozen.partner_names.get(partner_user_id, "")[:100],
                "scenes": [],
                "actions": [],
                "media_manifest": [],
            }
            self._session.add(
                MemoryPlaybackDocument(
                    document_id=str(uuid4()),
                    archive_id=archive_id,
                    revision=0,
                    document_json=baseline,
                    content_digest=_digest_json(baseline),
                    is_published=True,
                    published_at=frozen.snapshot_cutoff_at,
                )
            )
            archives.append(archive)
        logger.info(
            "创建双方回忆录基础归档 relationship_id={} segment={} archive_count={}",
            frozen.relationship_id,
            frozen.relationship_segment_no,
            len(archives),
        )
        return archives

    def publish_playback_document(
        self,
        archive_id: str,
        *,
        expected_generation_epoch: int,
        expected_run_id: str | None = None,
        document: dict[str, Any],
    ) -> MemoryPlaybackDocument:
        """完整文档先落库，再在同一事务切换 archive 的唯一发布指针。"""
        _validate_complete_document(document)
        archive = self._session.scalar(
            select(MemoryArchive)
            .where(
                MemoryArchive.archive_id == archive_id,
                MemoryArchive.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if archive is None:
            raise ValueError("回忆录归档不存在或已删除")
        if archive.generation_epoch != expected_generation_epoch:
            raise ValueError("GENERATION_SUPERSEDED")
        if expected_run_id is not None and archive.active_run_id != expected_run_id:
            raise ValueError("MEMORY_RUN_NOT_ACTIVE")
        next_revision = archive.published_revision + 1
        previous = self._session.scalar(
            select(MemoryPlaybackDocument).where(
                MemoryPlaybackDocument.archive_id == archive_id,
                MemoryPlaybackDocument.revision == archive.published_revision,
            )
        )
        if previous is not None:
            previous.is_published = False
        published = MemoryPlaybackDocument(
            document_id=str(uuid4()),
            archive_id=archive_id,
            revision=next_revision,
            document_json=document,
            content_digest=_digest_json(document),
            is_published=True,
            published_at=datetime.now(UTC),
        )
        self._session.add(published)
        archive.published_revision = next_revision
        # 原子提交完整 revision 后，只有这里可将内容切换为成功。
        archive.content_status = "succeeded"
        archive.enhancement_status = "succeeded"
        logger.info(
            "原子发布回忆录文档 archive_id={} revision={} generation_epoch={}",
            archive_id,
            next_revision,
            expected_generation_epoch,
        )
        return published


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest_json(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _validate_complete_document(document: dict[str, Any]) -> None:
    """第一版只接受包含作品三大容器的完整 document，拒绝局部草稿发布。"""
    required_lists = ("scenes", "actions", "media_manifest")
    if not isinstance(document.get("schema_version"), str) or any(
        not isinstance(document.get(field), list) for field in required_lists
    ):
        raise ValueError("播放文档不是完整的可发布版本")
