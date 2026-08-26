"""回忆录数字密码校验与短期会话解锁凭证。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.memoir.memory_password import MemoryPassword


class MemoryPasswordError(ValueError):
    """密码流程的固定安全错误码，不包含输入密码或凭证。"""


class MemoryPasswordService:
    """使用标准库 scrypt 保存 PIN 派生值；错误五次才进入冷却。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def setup(self, user_id: int, password: str) -> None:
        """首次设置 4~6 位数字密码，已存在记录不允许静默覆盖。"""
        self._validate_password(password)
        if self._session.scalar(select(MemoryPassword).where(MemoryPassword.user_id == user_id)):
            raise MemoryPasswordError("MEMORY_PASSWORD_ALREADY_SET")
        salt = secrets.token_bytes(16)
        self._session.add(MemoryPassword(
            user_id=user_id, salt=salt.hex(), password_hash=self._hash(password, salt),
        ))

    def verify(self, user_id: int, password: str, session_id: str, *, now: datetime) -> str:
        """校验 PIN 并签发约 15 分钟的一次性随机解锁凭证。"""
        record = self._get(user_id)
        current = self._utc(now)
        if record.locked_until and self._utc(record.locked_until) > current:
            raise MemoryPasswordError("MEMORY_PASSWORD_COOLDOWN")
        valid = hmac.compare_digest(record.password_hash, self._hash(password, bytes.fromhex(record.salt)))
        if not valid:
            record.failed_attempts += 1
            if record.failed_attempts >= 5:
                record.failed_attempts = 0
                record.locked_until = current + timedelta(minutes=10)
            raise MemoryPasswordError("MEMORY_PASSWORD_INVALID")
        credential = secrets.token_urlsafe(32)
        record.failed_attempts = 0
        record.locked_until = None
        record.unlock_session_digest = self._digest(session_id)
        record.unlock_token_digest = self._digest(credential)
        record.unlock_expires_at = current + timedelta(minutes=15)
        return credential

    def is_unlocked(self, user_id: int, session_id: str, credential: str, *, now: datetime) -> bool:
        """仅接受同一 JWT jti 且未过期的凭证，比较时不泄露 token。"""
        record = self._get(user_id)
        return bool(
            record.unlock_expires_at
            and self._utc(record.unlock_expires_at) > self._utc(now)
            and record.unlock_session_digest
            and record.unlock_token_digest
            and hmac.compare_digest(record.unlock_session_digest, self._digest(session_id))
            and hmac.compare_digest(record.unlock_token_digest, self._digest(credential))
        )

    def _get(self, user_id: int) -> MemoryPassword:
        record = self._session.scalar(select(MemoryPassword).where(MemoryPassword.user_id == user_id))
        if record is None:
            raise MemoryPasswordError("MEMORY_PASSWORD_UNSET")
        return record

    @staticmethod
    def _validate_password(password: str) -> None:
        if not password.isdecimal() or not 4 <= len(password) <= 6:
            raise MemoryPasswordError("MEMORY_PASSWORD_FORMAT_INVALID")

    @staticmethod
    def _hash(password: str, salt: bytes) -> str:
        return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
