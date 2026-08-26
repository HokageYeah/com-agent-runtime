"""回忆录内部工具专用的冻结快照读取服务。"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memoir.memory_agent_run_ref import MemoryAgentRunRef
from app.models.memoir.memory_archive import MemoryArchive
from app.models.memoir.memory_snapshot import MemorySnapshot
from app.services.memoir.memory_archive_service import FernetSnapshotCipher

_SUPPORTED_SNAPSHOT_SCHEMA_MAJOR = 1


class MemorySnapshotService:
    """只允许已绑定 archive 的内部工具读取加密快照。"""

    def __init__(self, session: Session, cipher: FernetSnapshotCipher) -> None:
        self._session, self._cipher = session, cipher

    def read_for_runtime(
        self, archive_id: str, snapshot_id: str, run_id: str, generation_epoch: int,
    ) -> dict[str, Any]:
        """校验当前 Run 对冻结快照的四元组授权后才解密返回正文。"""
        snapshot = self.authorize_runtime(
            archive_id, snapshot_id, run_id, generation_epoch,
        )
        payload = self._cipher.decrypt_json(snapshot.encrypted_payload)
        payload = self._normalize_payload(payload, archive_id)
        logging.info(
            "Runtime 已读取冻结快照 archive_id=%s snapshot_id=%s run_id=%s",
            archive_id, snapshot_id, run_id,
        )
        return payload

    def authorize_runtime(
        self, archive_id: str, snapshot_id: str, run_id: str, generation_epoch: int,
    ) -> MemorySnapshot:
        """仅当前 active Run 的冻结 snapshot 可被读取或用于发布，绝不解密日志。"""
        archive = self._session.scalar(select(MemoryArchive).where(
            MemoryArchive.archive_id == archive_id, MemoryArchive.deleted_at.is_(None)
        ))
        snapshot = self._session.scalar(select(MemorySnapshot).where(
            MemorySnapshot.snapshot_id == snapshot_id, MemorySnapshot.archive_id == archive_id
        ))
        ref = self._session.scalar(select(MemoryAgentRunRef).where(
            MemoryAgentRunRef.run_id == run_id,
            MemoryAgentRunRef.archive_id == archive_id,
            MemoryAgentRunRef.snapshot_id == snapshot_id,
            MemoryAgentRunRef.generation_epoch == generation_epoch,
        ))
        if (
            archive is None or snapshot is None or ref is None
            or archive.active_run_id != run_id
            or ref.status not in {"pending", "running", "waiting_human"}
        ):
            logging.warning(
                "Runtime 快照授权被拒绝 archive_id=%s snapshot_id=%s run_id=%s epoch=%s",
                archive_id, snapshot_id, run_id, generation_epoch,
            )
            raise ValueError("MEMORY_SNAPSHOT_UNAVAILABLE")
        if snapshot.schema_major != _SUPPORTED_SNAPSHOT_SCHEMA_MAJOR:
            logging.warning(
                "Runtime 快照版本被拒绝 archive_id=%s snapshot_id=%s run_id=%s "
                "schema_major=%s code=MEMORY_SNAPSHOT_SCHEMA_UNSUPPORTED",
                archive_id,
                snapshot_id,
                run_id,
                snapshot.schema_major,
            )
            raise ValueError("MEMORY_SNAPSHOT_SCHEMA_UNSUPPORTED")
        return snapshot

    def _normalize_payload(
        self, payload: dict[str, Any], archive_id: str,
    ) -> dict[str, Any]:
        """把历史无版本负载只迁移到内存；任何读取均不改写原密文或摘要。"""
        version = payload.get("schema_version")
        if version is None:
            return self._migrate_legacy_payload(payload, archive_id)
        if (
            not isinstance(version, str)
            or not re.fullmatch(r"[1-9]\d*\.\d+\.\d+", version)
            or int(version.split(".", 1)[0]) != _SUPPORTED_SNAPSHOT_SCHEMA_MAJOR
        ):
            raise ValueError("MEMORY_SNAPSHOT_SCHEMA_UNSUPPORTED")
        return payload

    def _migrate_legacy_payload(
        self, payload: dict[str, Any], archive_id: str,
    ) -> dict[str, Any]:
        """将旧 ``diaries/bets`` 结构投影为当前白名单 envelope。"""
        diaries = payload.get("diaries", [])
        bets = payload.get("bets", [])
        if not isinstance(diaries, list) or not isinstance(bets, list):
            raise ValueError("MEMORY_SNAPSHOT_SCHEMA_INVALID")
        archive = self._session.scalar(
            select(MemoryArchive).where(
                MemoryArchive.archive_id == archive_id,
                MemoryArchive.deleted_at.is_(None),
            )
        )
        if archive is None:
            raise ValueError("MEMORY_SNAPSHOT_UNAVAILABLE")
        user_snapshots = [
            {"user_id": archive.owner_user_id, "nickname": None, "avatar_ref": None},
            {
                "user_id": archive.partner_user_id,
                "nickname": archive.partner_nickname_snapshot,
                "avatar_ref": archive.partner_avatar_snapshot,
            },
        ]
        user_snapshots.sort(key=lambda item: int(item["user_id"]))
        return {
            "schema_version": "1.0.0",
            "source_range": {
                "relationship_id": archive.relationship_id,
                "space_id": archive.space_id,
                "relationship_segment_no": archive.relationship_segment_no,
                "bound_at": _iso_utc(archive.bound_at),
                "unbound_at": _iso_utc(archive.unbound_at),
                "user_snapshots": user_snapshots,
            },
            "diary_items": diaries,
            "bet_items": bets,
            "stats": {
                "diary_count": len(diaries),
                "bet_count": len(bets),
            },
        }

    @staticmethod
    def validate_document_references(
        document: object, snapshot: MemorySnapshot,
    ) -> None:
        """发布前仅允许引用冻结 manifest 中的素材；MVP 暂不接受媒体资产。

        R2 后回忆录文档使用规范前缀 ``diary:`` / ``completed_bet:``；manifest
        字段名 ``bet_ids`` 保持稳定，反查时按规范前缀 ``completed_bet`` 与
        Runtime 输出对齐。
        """
        if not isinstance(document, dict):
            raise ValueError("MEMORY_DOCUMENT_INVALID")
        manifest = snapshot.source_manifest_json
        if not isinstance(manifest, dict):
            raise ValueError("MEMORY_SNAPSHOT_MANIFEST_INVALID")
        allowed_refs = {
            f"{prefix}:{item_id}"
            for key, prefix in (("diary_ids", "diary"), ("bet_ids", "completed_bet"))
            for item_id in (manifest.get(key, []) if isinstance(manifest.get(key, []), list) else [])
            if isinstance(item_id, (str, int)) and not isinstance(item_id, bool)
        }
        scenes = document.get("scenes")
        if not isinstance(scenes, list) or any(
            not isinstance(scene, dict)
            or not isinstance(scene.get("source_refs"), list)
            or any(not isinstance(ref, str) or ref not in allowed_refs for ref in scene["source_refs"])
            for scene in scenes
        ):
            raise ValueError("MEMORY_DOCUMENT_SOURCE_REF_INVALID")
        # 媒体生成尚未启用，拒绝未知 storage key；启用后必须按 document 资产表逐项校验。
        if document.get("media_manifest") != []:
            raise ValueError("MEMORY_DOCUMENT_MEDIA_INVALID")


def _iso_utc(value: datetime | None) -> str | None:
    """数据库可能丢失 tzinfo；历史归档时间按 UTC 恢复为稳定 envelope 字符串。"""
    if value is None:
        return None
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat()
