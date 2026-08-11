"""Task 10：检查点的加密持久化与受控恢复边界。"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any, Self
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import AgentCheckpoint, AgentRun
from app.runtime.interfaces import LeaseContext
from app.schemas.audit import RuntimeAuditEvent
from app.services.audit_service import AuditService
from app.services.lease_service import LeaseService


class CheckpointError(RuntimeError):
    """检查点不可安全读写时抛出；异常消息不得携带业务状态正文。"""


class FernetCheckpointCipher:
    """以对称密钥加密完整恢复状态，数据库仅保存密文和安全摘要。"""

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def generate(cls) -> Self:
        """生成测试或本地开发可用的新密钥；生产环境应由密钥管理服务注入。"""
        return cls(Fernet.generate_key())

    def encrypt(self, state: dict[str, Any]) -> bytes:
        """序列化为稳定 JSON 后加密，便于摘要校验且不泄露内容。"""
        payload = json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return self._fernet.encrypt(payload)

    def decrypt(self, encrypted_blob: bytes) -> dict[str, Any]:
        """解密并严格要求顶层为对象，拒绝非预期恢复数据。"""
        try:
            decoded = json.loads(self._fernet.decrypt(encrypted_blob).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CheckpointError("检查点密文无效或无法解密") from exc
        if not isinstance(decoded, dict):
            raise CheckpointError("检查点恢复状态格式无效")
        return decoded


class CheckpointStore:
    """带 fencing 校验的检查点仓储；调用方自行决定事务提交时机。"""

    def __init__(self, session: Session, cipher: FernetCheckpointCipher) -> None:
        self._session = session
        self._cipher = cipher
        self._lease = LeaseService(session)
        self._audit = AuditService(session=session)

    def save(
        self,
        run_id: str,
        checkpoint_key: str,
        state: dict[str, Any],
        context: LeaseContext,
    ) -> str:
        """写入或覆盖同一逻辑检查点，完整状态只以密文保存。"""
        run = self._require_writable_run(run_id, context)
        encrypted_blob = self._cipher.encrypt(state)
        digest = hashlib.sha256(encrypted_blob).hexdigest()
        checkpoint = self._session.scalar(
            select(AgentCheckpoint).where(
                AgentCheckpoint.run_id == run_id,
                AgentCheckpoint.checkpoint_key == checkpoint_key,
            )
        )
        if checkpoint is None:
            checkpoint = AgentCheckpoint(
                checkpoint_id=str(uuid4()),
                run_id=run_id,
                checkpoint_key=checkpoint_key,
                state_schema_version="1.0.0",
                data_classification="runtime_private_encrypted",
                privacy_version=context.privacy_version,
                created_at=datetime.now(UTC),
                expires_at=run.run_deadline_at,
                content_digest=digest,
                encrypted_state_blob=encrypted_blob,
                storage_ref=None,
                state_summary=self._safe_summary(state),
            )
            self._session.add(checkpoint)
        else:
            checkpoint.privacy_version = context.privacy_version
            checkpoint.content_digest = digest
            checkpoint.encrypted_state_blob = encrypted_blob
            checkpoint.storage_ref = None
            checkpoint.state_summary = self._safe_summary(state)
            checkpoint.expires_at = run.run_deadline_at
        self._session.flush()
        logging.info(
            "已写入加密 checkpoint run_id=%s checkpoint_key=%s digest=%s",
            run_id,
            checkpoint_key,
            digest[:12],
        )
        self._append_audit("checkpoint_saved", run, checkpoint)
        return checkpoint.checkpoint_id

    def load_latest(self, run_id: str, context: LeaseContext) -> dict[str, Any]:
        """仅有效 Worker 可读取最新未过期、隐私版本一致的加密检查点。"""
        self._require_writable_run(run_id, context)
        checkpoint = self._session.scalar(
            select(AgentCheckpoint)
            .where(AgentCheckpoint.run_id == run_id)
            .order_by(AgentCheckpoint.created_at.desc(), AgentCheckpoint.id.desc())
        )
        if checkpoint is None:
            raise CheckpointError("不存在可恢复的检查点")
        if checkpoint.privacy_version != context.privacy_version:
            raise CheckpointError("检查点隐私版本不兼容")
        if checkpoint.encrypted_state_blob is None:
            raise CheckpointError("检查点不含可恢复的加密状态")
        expires_at = checkpoint.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            raise CheckpointError("检查点已过期")
        state = self._cipher.decrypt(checkpoint.encrypted_state_blob)
        logging.info(
            "已读取加密 checkpoint run_id=%s checkpoint_id=%s",
            run_id,
            checkpoint.checkpoint_id,
        )
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        assert run is not None
        self._append_audit("checkpoint_loaded", run, checkpoint)
        return state

    def purge_for_run(self, run_id: str, context: LeaseContext) -> int:
        """删除指定 Run 的全部 checkpoint；用于 R2 旧完整状态 checkpoint 撤销。

        规格要求：旧版 ``state.model_dump()`` 完整 checkpoint 在迁移时撤销并
        purge，不作为新版恢复输入。本方法只提供能力，**不**由 R2 自动触发真实
        数据迁移（运行手册禁止）；运维或隐私清理 Worker 通过显式调用执行，
        审计只记录 purge 事实与受影响条数，绝不输出密文或状态正文。

        返回被删除的 checkpoint 行数；0 表示该 Run 无可清理 checkpoint。
        """
        run = self._require_writable_run(run_id, context)
        existing = list(
            self._session.scalars(
                select(AgentCheckpoint).where(AgentCheckpoint.run_id == run_id)
            ).all()
        )
        if not existing:
            return 0
        self._session.execute(
            delete(AgentCheckpoint).where(AgentCheckpoint.run_id == run_id)
        )
        self._session.flush()
        for checkpoint in existing:
            self._append_audit("checkpoint_purged", run, checkpoint)
        logging.info(
            "已 purge checkpoint run_id=%s purged_count=%s",
            run_id,
            len(existing),
        )
        return len(existing)

    def _require_writable_run(self, run_id: str, context: LeaseContext) -> AgentRun:
        if not self._lease.can_write(run_id, context):
            raise CheckpointError("当前 Worker 无权读写检查点")
        run = self._session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        if run is None:  # can_write 已处理，仍保留静态类型与防御性边界。
            raise CheckpointError("运行不存在")
        return run

    @staticmethod
    def _safe_summary(state: dict[str, Any]) -> dict[str, Any]:
        """摘要只能携带调度恢复所需的字段，绝不透传完整输入或业务快照。"""
        summary: dict[str, Any] = {}
        node_ids = state.get("completed_node_ids")
        if isinstance(node_ids, list) and all(isinstance(item, str) for item in node_ids):
            summary["completed_node_ids"] = node_ids
        fallback_flags = state.get("fallback_flags")
        if isinstance(fallback_flags, list) and all(
            isinstance(item, str) for item in fallback_flags
        ):
            summary["fallback_flags"] = fallback_flags
        completed_steps = state.get("completed_steps")
        if isinstance(completed_steps, int) and completed_steps >= 0:
            # 步数属于运行进度，可安全用于 UI/排障；不携带任何业务正文。
            summary["completed_steps"] = completed_steps
        return summary

    def _append_audit(
        self, action: str, run: AgentRun, checkpoint: AgentCheckpoint
    ) -> None:
        """记录检查点访问事实；审计仅保留 ID、版本与密文摘要前缀。"""
        self._audit.append(
            RuntimeAuditEvent(
                audit_id=str(uuid4()),
                actor_type="worker",
                actor_id=run.lease_owner or "unknown",
                action=action,
                resource_type="agent_checkpoint",
                resource_id=checkpoint.checkpoint_id,
                outcome="succeeded",
                occurred_at=datetime.now(UTC),
                trace_id=run.trace_id,
                metadata_summary={
                    "run_id": run.run_id,
                    "privacy_version": str(checkpoint.privacy_version),
                    "content_digest_prefix": checkpoint.content_digest[:12],
                },
            )
        )
