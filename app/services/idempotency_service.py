from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentRun, IdempotencyRecord


class IdempotencyConflict(ValueError):
    """同 key 但 method/path/body 不同时必须返回 409，禁止绕过。"""


class IdempotencyService:
    def __init__(self, session: Session, ttl_days: int = 7) -> None:
        self._session, self._ttl_days = session, ttl_days

    def replay(
        self, client_id: str, scope: str, key: str, digest: str
    ) -> dict[str, Any] | None:
        record = self._session.scalar(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.client_id == client_id,
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.idempotency_key == key,
            )
            .with_for_update()
        )
        if record is None:
            return None
        expires = (
            record.expires_at.replace(tzinfo=UTC)
            if record.expires_at.tzinfo is None
            else record.expires_at
        )
        if expires < datetime.now(UTC) and not self._retain_expired_purge(record):
            self._session.delete(record)
            self._session.flush()
            return None
        if record.request_hash != digest:
            raise IdempotencyConflict("IDEMPOTENCY_CONFLICT")
        logging.info("幂等重放 scope=%s", scope)
        return record.response_json

    def store(
        self,
        client_id: str,
        scope: str,
        key: str,
        digest: str,
        response: dict[str, Any],
        resource_id: str,
        ttl_days: int | None = None,
    ) -> None:
        self._session.add(
            IdempotencyRecord(
                client_id=client_id,
                scope=scope,
                idempotency_key=key,
                request_hash=digest,
                response_json=response,
                resource_type="agent_run",
                resource_id=resource_id,
                expires_at=datetime.now(UTC) + timedelta(
                    days=ttl_days if ttl_days is not None else self._ttl_days
                ),
            )
        )

    def cleanup_expired(self, now: datetime | None = None) -> int:
        """维护任务删除可安全过期的幂等记录；未确认 purge 永远保留原键。"""
        cutoff = now or datetime.now(UTC)
        records = self._session.scalars(
            select(IdempotencyRecord).where(IdempotencyRecord.expires_at < cutoff)
        ).all()
        deleted = 0
        retained_purges = 0
        for record in records:
            if self._retain_expired_purge(record):
                retained_purges += 1
                continue
            self._session.delete(record)
            deleted += 1
        logging.info(
            "清理过期幂等记录 deleted=%s retained_purges=%s",
            deleted,
            retained_purges,
        )
        return deleted

    def _retain_expired_purge(self, record: IdempotencyRecord) -> bool:
        """仅在关联 Run 明确 purged 后才允许删除 purge 的原幂等记录。

        缺失资源或查询异常时保守保留，避免清除重放身份后再次发送删除副作用。
        本方法只读取 Run 状态，不接触任何私密输入、checkpoint 或业务正文。
        """
        if record.scope != "purge":
            return False
        if not record.resource_id:
            return True
        run = self._session.scalar(
            select(AgentRun.privacy_state).where(AgentRun.run_id == record.resource_id)
        )
        return run != "purged"

    def lookup_result(
        self, client_id: str, scope: str, key: str, resource_id: str
    ) -> dict[str, Any] | None:
        """按原幂等键查询已提交副作用结果，不需要重放原请求正文。"""
        record = self._session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.client_id == client_id,
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.idempotency_key == key,
                IdempotencyRecord.resource_id == resource_id,
            )
        )
        if record is None:
            return None
        expires_at = record.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at < datetime.now(UTC):
            return None
        logging.info("副作用结果对账命中 scope=%s", scope)
        return record.response_json
