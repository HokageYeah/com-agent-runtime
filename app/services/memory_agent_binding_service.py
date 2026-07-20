"""回忆录归档与 Runtime Run 的原子绑定服务。"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memory_agent_run_ref import MemoryAgentRunRef
from app.models.memory_archive import MemoryArchive


class MemoryAgentBindingService:
    """确保只有当前 generation 的 Run 能成为 archive 唯一写入者。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def bind(
        self,
        archive_id: str,
        run_id: str,
        generation_epoch: int,
        *,
        snapshot_id: str | None = None,
        create_idempotency_key: str | None = None,
        start_idempotency_key: str | None = None,
        contract_version: str | None = None,
        package_digest: str | None = None,
        authorization_version: int | None = None,
    ) -> MemoryAgentRunRef:
        """绑定当前 Run 并冻结已知生命周期摘要；调用方负责同事务提交。

        可选字段只接受 Runtime create/start 响应中的安全元数据，不接收输入、快照或正文。
        """
        archive = self._session.scalar(select(MemoryArchive).where(
            MemoryArchive.archive_id == archive_id, MemoryArchive.deleted_at.is_(None)
        ).with_for_update())
        if archive is None:
            raise ValueError("MEMORY_ARCHIVE_UNAVAILABLE")
        if archive.generation_epoch != generation_epoch:
            raise ValueError("GENERATION_SUPERSEDED")
        existing = self._session.scalar(select(MemoryAgentRunRef).where(MemoryAgentRunRef.run_id == run_id))
        if existing is not None:
            if existing.archive_id != archive_id or existing.generation_epoch != generation_epoch:
                raise ValueError("RUN_BINDING_CONFLICT")
            self._fill_missing_lifecycle_metadata(
                existing,
                snapshot_id=snapshot_id,
                create_idempotency_key=create_idempotency_key,
                start_idempotency_key=start_idempotency_key,
                contract_version=contract_version,
                package_digest=package_digest,
                authorization_version=authorization_version,
            )
            return existing
        if archive.active_run_id and archive.active_run_id != run_id:
            raise ValueError("MEMORY_RUN_ALREADY_ACTIVE")
        ref = MemoryAgentRunRef(
            run_id=run_id,
            archive_id=archive_id,
            snapshot_id=snapshot_id,
            generation_epoch=generation_epoch,
            status="pending_start",
            create_idempotency_key=create_idempotency_key,
            start_idempotency_key=start_idempotency_key,
            contract_version=contract_version,
            package_digest=package_digest,
            authorization_version=authorization_version,
        )
        archive.active_run_id = run_id
        self._session.add(ref)
        logging.info("回忆录 Run 已绑定 archive_id=%s run_id=%s epoch=%s", archive_id, run_id, generation_epoch)
        return ref

    @staticmethod
    def _fill_missing_lifecycle_metadata(
        ref: MemoryAgentRunRef,
        *,
        snapshot_id: str | None,
        create_idempotency_key: str | None,
        start_idempotency_key: str | None,
        contract_version: str | None,
        package_digest: str | None,
        authorization_version: int | None,
    ) -> None:
        """幂等重试仅补全首次缺失摘要，禁止用后续请求覆盖已冻结运行身份。"""
        values = {
            "snapshot_id": snapshot_id,
            "create_idempotency_key": create_idempotency_key,
            "start_idempotency_key": start_idempotency_key,
            "contract_version": contract_version,
            "package_digest": package_digest,
            "authorization_version": authorization_version,
        }
        for field, value in values.items():
            if getattr(ref, field) is None and value is not None:
                setattr(ref, field, value)
                ref.row_version += 1
