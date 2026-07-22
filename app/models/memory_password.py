"""回忆录独立密码的最小安全状态，不保存密码或解锁凭证原文。"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String

from app.db.sqlalchemy_db import Base


class MemoryPassword(Base):
    """每位用户一条独立密码记录，解锁状态绑定已验签 JWT 的 jti。"""

    __tablename__ = "memory_passwords"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, unique=True, nullable=False, index=True)
    salt = Column(String(64), nullable=False)
    password_hash = Column(String(128), nullable=False)
    failed_attempts = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
    unlock_session_digest = Column(String(64), nullable=True)
    unlock_token_digest = Column(String(64), nullable=True)
    unlock_expires_at = Column(DateTime, nullable=True)
