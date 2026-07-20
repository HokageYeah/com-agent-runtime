"""回忆录内部工具专用的冻结快照读取服务。"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memory_agent_run_ref import MemoryAgentRunRef
from app.models.memory_archive import MemoryArchive
from app.models.memory_snapshot import MemorySnapshot
from app.services.memory_archive_service import FernetSnapshotCipher


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
        return snapshot

    @staticmethod
    def validate_document_references(
        document: object, snapshot: MemorySnapshot,
    ) -> None:
        """发布前仅允许引用冻结 manifest 中的素材；MVP 暂不接受媒体资产。"""
        if not isinstance(document, dict):
            raise ValueError("MEMORY_DOCUMENT_INVALID")
        manifest = snapshot.source_manifest_json
        if not isinstance(manifest, dict):
            raise ValueError("MEMORY_SNAPSHOT_MANIFEST_INVALID")
        allowed_refs = {
            f"{prefix}:{item_id}"
            for key, prefix in (("diary_ids", "diary"), ("bet_ids", "bet"))
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
