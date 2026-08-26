"""回忆录归档基础服务：冻结输入、加密快照并发布 revision 0。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memoir.memory_action import MemoryAction
from app.models.memoir.memory_archive import MemoryArchive
from app.models.memoir.memory_playback_document import MemoryPlaybackDocument
from app.models.memoir.memory_scene import MemoryScene
from app.models.memoir.memory_snapshot import MemorySnapshot
from app.models.memoir.memory_source_reference import MemorySourceReference

# 回忆录第一版只允许这些已冻结的作品类型，避免模型或业务输入扩展播放器语义。
_SCENE_TYPES = frozenset(
    {
        "cover",
        "stats",
        "diary_highlight",
        "bet_highlight",
        "image",
        "milestone",
        "summary",
    }
)
_ACTION_TYPES = frozenset(
    {
        "show_card",
        "focus_image",
        "type_text",
        "hold",
        "play_tts",
        "transition",
    }
)
# 安全等级属于固定播放器契约，未知等级必须拒绝而非按 normal 猜测。
_SAFETY_LEVELS = frozenset({"normal", "sensitive", "fallback"})
# 当前服务只写入该 major；未来版本必须由升级后的服务显式迁移后再发布。
_MEMORY_SCHEMA_MAJOR = 1
# 普通替代版本保留七天，隐私删除不走此路径，交由 Task 10.5 立即撤权。
_SUPERSEDED_REVISION_RETENTION = timedelta(days=7)


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
    bound_at: datetime | None = None
    partner_avatars: dict[int, str] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)


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
        snapshot_envelope = _snapshot_envelope(frozen)
        manifest_hash = _digest_json(frozen.source_manifest)
        payload_digest = _digest_json(snapshot_envelope)
        existing = self._existing_archives(frozen, manifest_hash, payload_digest)
        if existing is not None:
            logger.info(
                "复用已冻结的双方回忆录归档 relationship_id={} segment={}",
                frozen.relationship_id,
                frozen.relationship_segment_no,
            )
            return existing
        encrypted_payload = self._cipher.encrypt_json(snapshot_envelope)
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
                enhancement_status="disabled",
                partner_nickname_snapshot=_snapshot_name(
                    frozen.partner_names.get(partner_user_id)
                ),
                partner_avatar_snapshot=_avatar_ref(
                    frozen.partner_avatars.get(partner_user_id)
                ),
                bound_at=frozen.bound_at,
                unbound_at=frozen.snapshot_cutoff_at,
                published_revision=0,
                summary="回忆录基础版本已创建，等待增强生成。",
            )
            self._session.add(archive)
            self._session.add(
                MemorySnapshot(
                    snapshot_id=str(uuid4()),
                    archive_id=archive_id,
                    snapshot_version=1,
                    schema_major=_MEMORY_SCHEMA_MAJOR,
                    source_manifest_json=frozen.source_manifest,
                    source_manifest_hash=manifest_hash,
                    privacy_filter_version=frozen.privacy_filter_version,
                    snapshot_cutoff_at=frozen.snapshot_cutoff_at,
                    encryption_key_id=self._cipher.key_id,
                    encrypted_payload=encrypted_payload,
                    content_digest=payload_digest,
                )
            )
            baseline, scenes, actions = _baseline_document(
                frozen, owner_user_id, partner_user_id
            )
            document_id = str(uuid4())
            self._session.add(
                MemoryPlaybackDocument(
                    document_id=document_id,
                    archive_id=archive_id,
                    revision=0,
                    document_json=baseline,
                    content_digest=_digest_json(baseline),
                    schema_major=_MEMORY_SCHEMA_MAJOR,
                    is_published=True,
                    published_at=frozen.snapshot_cutoff_at,
                )
            )
            for scene in scenes:
                self._session.add(
                    MemoryScene(
                        scene_id=scene["scene_id"],
                        document_id=document_id,
                        scene_order=scene["order"],
                        scene_type=scene["scene_type"],
                        schema_major=_MEMORY_SCHEMA_MAJOR,
                        safety_level="fallback",
                        payload_json=scene["payload"],
                        source_refs_json=[],
                    )
                )
            # ``MemoryAction.scene_id`` 是字符串外键，ORM 没有 relationship 可据以
            # 排序；PostgreSQL 会立即校验，故先落 Scene 再添加 Action。
            self._session.flush()
            for action in actions:
                self._session.add(
                    MemoryAction(
                        action_id=action["action_id"],
                        scene_id=action["scene_id"],
                        action_order=action["order"],
                        action_type=action["action_type"],
                        schema_major=_MEMORY_SCHEMA_MAJOR,
                        duration_ms=action["duration_ms"],
                        payload_json={},
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

    def create_archives_for_unbound_relationship(
        self,
        relationship_id: int,
    ) -> list[MemoryArchive]:
        """从真实业务表冻结已解绑关系段，再复用唯一归档写入路径。"""
        # 延迟导入避免冻结 DTO 与 materializer 的模块循环依赖。
        from app.services.memoir.memory_snapshot_materializer import (
            MemorySnapshotMaterializer,
        )

        frozen = MemorySnapshotMaterializer(self._session).freeze_relationship(
            relationship_id
        )
        logger.info("开始从解绑关系冻结回忆录 relationship_id={}", relationship_id)
        return self.create_archives_for_relationship(frozen)

    def _existing_archives(
        self,
        frozen: FrozenMemoryInput,
        manifest_hash: str,
        payload_digest: str,
    ) -> list[MemoryArchive] | None:
        """以关系段为幂等边界；已冻结素材一旦不同必须停止而非覆盖历史。"""
        records = self._session.scalars(
            select(MemoryArchive).where(
                MemoryArchive.space_id == frozen.space_id,
                MemoryArchive.relationship_segment_no == frozen.relationship_segment_no,
            )
        ).all()
        if not records:
            return None
        expected_owners = set(frozen.owner_user_ids)
        if (
            len(records) != 2
            or {record.owner_user_id for record in records} != expected_owners
            or any(
                record.relationship_id != frozen.relationship_id for record in records
            )
        ):
            raise ValueError("MEMORY_ARCHIVE_FROZEN_INPUT_CONFLICT")
        snapshots = self._session.scalars(
            select(MemorySnapshot).where(
                MemorySnapshot.archive_id.in_(
                    [record.archive_id for record in records]
                ),
            )
        ).all()
        if len(snapshots) != 2 or any(
            snapshot.source_manifest_hash != manifest_hash
            or snapshot.content_digest != payload_digest
            or snapshot.privacy_filter_version != frozen.privacy_filter_version
            # SQLite 测试库会丢失 tzinfo；按 UTC 瞬间比较，生产 MySQL 同样安全。
            or _as_utc(snapshot.snapshot_cutoff_at)
            != _as_utc(frozen.snapshot_cutoff_at)
            for snapshot in snapshots
        ):
            raise ValueError("MEMORY_ARCHIVE_FROZEN_INPUT_CONFLICT")
        by_owner = {record.owner_user_id: record for record in records}
        return [by_owner[owner] for owner in frozen.owner_user_ids]

    def publish_playback_document(
        self,
        archive_id: str,
        *,
        expected_generation_epoch: int,
        expected_run_id: str | None = None,
        snapshot: MemorySnapshot | None = None,
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
        source_references = _validate_document_source_references(
            document, archive, snapshot
        )
        next_revision = archive.published_revision + 1
        previous = self._session.scalar(
            select(MemoryPlaybackDocument).where(
                MemoryPlaybackDocument.archive_id == archive_id,
                MemoryPlaybackDocument.revision == archive.published_revision,
            )
        )
        if previous is not None:
            previous.is_published = False
            previous.retain_until = datetime.now(UTC) + _SUPERSEDED_REVISION_RETENTION
        published = MemoryPlaybackDocument(
            document_id=str(uuid4()),
            archive_id=archive_id,
            revision=next_revision,
            document_json=document,
            schema_major=_document_schema_major(document),
            content_digest=_digest_json(document),
            is_published=True,
            published_at=datetime.now(UTC),
        )
        self._session.add(published)
        for scene_index, scene in enumerate(document["scenes"], start=1):
            # Runtime 的播放契约允许省略展示排序；持久化层以完整文档中的
            # 列表位置冻结排序，不能从来源引用数量推导（空素材时会全部冲突）。
            self._session.add(
                MemoryScene(
                    scene_id=scene["scene_id"],
                    document_id=published.document_id,
                    scene_order=scene.get("order", scene_index),
                    schema_major=_MEMORY_SCHEMA_MAJOR,
                    scene_type=scene["scene_type"],
                    safety_level=scene.get("safety_level", "normal"),
                    payload_json=scene.get("payload", {}),
                    source_refs_json=scene.get("source_refs", []),
                )
            )
        next_action_order: dict[str, int] = {}
        for action in document["actions"]:
            scene_id = action["scene_id"]
            # 与场景相同，动作未显式排序时遵循其在同场景中的文档顺序。
            action_order = action.get("order")
            if action_order is None:
                action_order = next_action_order.get(scene_id, 0) + 1
            next_action_order[scene_id] = max(
                next_action_order.get(scene_id, 0), action_order
            )
            self._session.add(
                MemoryAction(
                    action_id=action["action_id"],
                    scene_id=scene_id,
                    action_order=action_order,
                    schema_major=_MEMORY_SCHEMA_MAJOR,
                    action_type=action["action_type"],
                    duration_ms=action["duration_ms"],
                    payload_json=action.get("payload", {}),
                )
            )
        for source_type, source_id in source_references:
            self._session.add(
                MemorySourceReference(
                    archive_id=archive.archive_id,
                    document_id=published.document_id,
                    revision=next_revision,
                    source_type=source_type,
                    source_id=source_id,
                )
            )
        archive.published_revision = next_revision
        # 原子提交完整 revision 后，只有这里可将内容切换为成功。
        archive.content_status = "succeeded"
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


def _as_utc(value: datetime) -> datetime:
    """统一比较冻结边界，避免数据库方言的时区表示差异改变归档幂等语义。"""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _digest_json(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _snapshot_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized[:100] or None


def _avatar_ref(value: str | None) -> str | None:
    """头像快照只允许稳定资产引用，不允许私有 URL 进入持久化层。"""
    if value is None:
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 255
        or "://" in normalized
        or normalized.startswith("//")
    ):
        raise ValueError("MEMORY_AVATAR_REF_INVALID")
    return normalized


def _snapshot_envelope(frozen: FrozenMemoryInput) -> dict[str, Any]:
    """将业务冻结结果收敛为版本化白名单 envelope 后再加密。"""
    diary_items = frozen.snapshot_payload.get(
        "diary_items", frozen.snapshot_payload.get("diaries", [])
    )
    bet_items = frozen.snapshot_payload.get(
        "bet_items", frozen.snapshot_payload.get("bets", [])
    )
    if not isinstance(diary_items, list) or not isinstance(bet_items, list):
        raise ValueError("MEMORY_SNAPSHOT_PAYLOAD_INVALID")
    stats = {
        "diary_count": int(frozen.stats.get("diary_count", len(diary_items))),
        "bet_count": int(frozen.stats.get("bet_count", len(bet_items))),
    }
    user_snapshots = [
        {
            "user_id": user_id,
            "nickname": _snapshot_name(frozen.partner_names.get(user_id)),
            "avatar_ref": _avatar_ref(frozen.partner_avatars.get(user_id)),
        }
        for user_id in sorted(frozen.owner_user_ids)
    ]
    return {
        "schema_version": "1.0.0",
        "source_range": {
            "relationship_id": frozen.relationship_id,
            "space_id": frozen.space_id,
            "relationship_segment_no": frozen.relationship_segment_no,
            "bound_at": (
                _as_utc(frozen.bound_at).isoformat()
                if frozen.bound_at is not None
                else None
            ),
            "unbound_at": _as_utc(frozen.snapshot_cutoff_at).isoformat(),
            "user_snapshots": user_snapshots,
        },
        "diary_items": diary_items,
        "bet_items": bet_items,
        "stats": stats,
    }


def _validate_complete_document(document: dict[str, Any]) -> None:
    """第一版只接受包含作品三大容器的完整 document，拒绝局部草稿发布。"""
    required_lists = ("scenes", "actions", "media_manifest")
    if not isinstance(document.get("schema_version"), str) or any(
        not isinstance(document.get(field), list) for field in required_lists
    ):
        raise ValueError("播放文档不是完整的可发布版本")
    # schema major 不兼容时禁止旧服务发布，避免播放器解释未知动作。
    if _document_schema_major(document) != _MEMORY_SCHEMA_MAJOR:
        raise ValueError("MEMORY_DOCUMENT_SCHEMA_UNSUPPORTED")
    # MVP 媒体能力关闭；非空清单不得形成“文档声明了但未持久化资产”的半成品。
    if document["media_manifest"] != []:
        raise ValueError("MEMORY_DOCUMENT_MEDIA_INVALID")
    scene_ids: set[str] = set()
    for scene in document["scenes"]:
        if (
            not isinstance(scene, dict)
            or not isinstance(scene.get("scene_id"), str)
            or not scene["scene_id"]
            or scene.get("scene_type") not in _SCENE_TYPES
            or scene.get("safety_level", "normal") not in _SAFETY_LEVELS
            or scene["scene_id"] in scene_ids
        ):
            error_code = (
                "MEMORY_SCENE_SAFETY_INVALID"
                if isinstance(scene, dict)
                and scene.get("safety_level", "normal") not in _SAFETY_LEVELS
                else "MEMORY_SCENE_TYPE_INVALID"
            )
            raise ValueError(error_code)
        scene_ids.add(scene["scene_id"])
    action_orders: set[tuple[str, int]] = set()
    for action in document["actions"]:
        if (
            not isinstance(action, dict)
            or not isinstance(action.get("action_id"), str)
            or action.get("scene_id") not in scene_ids
            or action.get("action_type") not in _ACTION_TYPES
            or isinstance(action.get("duration_ms"), bool)
            or not isinstance(action.get("duration_ms"), int)
            or action["duration_ms"] <= 0
        ):
            raise ValueError("MEMORY_ACTION_INVALID")
        order = action.get("order")
        if order is not None and (
            isinstance(order, bool)
            or not isinstance(order, int)
            or order <= 0
            or (action["scene_id"], order) in action_orders
        ):
            raise ValueError("MEMORY_ACTION_INVALID")
        if isinstance(order, int):
            action_orders.add((action["scene_id"], order))


def _document_schema_major(document: dict[str, Any]) -> int:
    """解析严格的 semver 主号；模糊版本不能进入持久化作品契约。"""
    version = document.get("schema_version")
    if not isinstance(version, str) or not re.fullmatch(r"[1-9]\d*\.\d+\.\d+", version):
        raise ValueError("MEMORY_DOCUMENT_SCHEMA_UNSUPPORTED")
    return int(version.split(".", 1)[0])


def _validate_document_source_references(
    document: dict[str, Any],
    archive: MemoryArchive,
    snapshot: MemorySnapshot | None,
) -> set[tuple[str, str]]:
    """校验发布引用来自当前 archive 的冻结快照，返回去重后的最小反查键。

    R2 后回忆录只接受规范前缀 ``diary:`` / ``completed_bet:``；legacy ``bet:``
    在 Runtime 边界已被 legacy reader 单向归一化，发布端不再回写旧形状。
    """
    references: set[tuple[str, str]] = set()
    for scene in document["scenes"]:
        for reference in scene.get("source_refs", []):
            if not isinstance(reference, str) or ":" not in reference:
                raise ValueError("MEMORY_SOURCE_REF_NOT_FROZEN")
            source_type, source_id = reference.split(":", 1)
            if source_type not in {"diary", "completed_bet"} or not source_id:
                raise ValueError("MEMORY_SOURCE_REF_NOT_FROZEN")
            references.add((source_type, source_id))
    if not references:
        return references
    if snapshot is None or snapshot.archive_id != archive.archive_id:
        raise ValueError("MEMORY_SOURCE_REF_NOT_FROZEN")
    manifest = snapshot.source_manifest_json
    if not isinstance(manifest, dict):
        raise ValueError("MEMORY_SOURCE_REF_NOT_FROZEN")
    allowed = {
        (source_type, str(source_id))
        for manifest_key, source_type in (
            ("diary_ids", "diary"),
            # 旧 manifest 字段名 bet_ids 保留（数据层稳定），但反查键使用
            # 规范前缀 completed_bet 与 Runtime 输出对齐。
            ("bet_ids", "completed_bet"),
        )
        for source_id in manifest.get(manifest_key, [])
        if isinstance(source_id, (str, int)) and not isinstance(source_id, bool)
    }
    if not references <= allowed:
        raise ValueError("MEMORY_SOURCE_REF_NOT_FROZEN")
    return references


def _baseline_document(
    frozen: FrozenMemoryInput,
    owner_user_id: int,
    partner_user_id: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """生成不含日记/赌局正文的基础作品，保证 AI 故障时仍有安全播放器入口。"""
    partner_name = frozen.partner_names.get(partner_user_id, "")[:100]
    cover_id, stats_id = str(uuid4()), str(uuid4())
    scenes = [
        {
            "scene_id": cover_id,
            "order": 1,
            "scene_type": "cover",
            "payload": {
                "title": "我们的回忆录",
                "partner_name": partner_name,
            },
        },
        {
            "scene_id": stats_id,
            "order": 2,
            "scene_type": "stats",
            "payload": {
                "diary_count": len(frozen.source_manifest.get("diary_ids", [])),
                "bet_count": len(frozen.source_manifest.get("bet_ids", [])),
            },
        },
    ]
    actions = [
        {
            "action_id": str(uuid4()),
            "scene_id": cover_id,
            "order": 1,
            "action_type": "show_card",
            "duration_ms": 3000,
        },
        {
            "action_id": str(uuid4()),
            "scene_id": stats_id,
            "order": 2,
            "action_type": "show_card",
            "duration_ms": 3000,
        },
    ]
    return (
        {
            "schema_version": "1.0.0",
            "title": "我们的回忆录",
            "owner_user_id": owner_user_id,
            "partner_name": partner_name,
            "scenes": scenes,
            "actions": actions,
            "media_manifest": [],
        },
        scenes,
        actions,
    )
