"""回忆录密码与会话绑定解锁凭证测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.sqlalchemy_db import Base
from app.services.memory_password_service import (
    MemoryPasswordError,
    MemoryPasswordService,
)


def test_password_unlock_is_bound_to_one_login_session_and_expires() -> None:
    """解锁 token 不能跨 JWT 会话使用，过期后必须重新验证密码。"""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    service = MemoryPasswordService(sessionmaker(bind=engine)())
    now = datetime(2026, 7, 22, tzinfo=UTC)

    service.setup(42, "1234")
    credential = service.verify(42, "1234", "session-a", now=now)

    assert service.is_unlocked(42, "session-a", credential, now=now + timedelta(minutes=14))
    assert not service.is_unlocked(42, "session-b", credential, now=now + timedelta(minutes=1))
    assert not service.is_unlocked(42, "session-a", credential, now=now + timedelta(minutes=16))
    with pytest.raises(MemoryPasswordError, match="MEMORY_PASSWORD_INVALID"):
        service.verify(42, "not-a-pin", "session-a", now=now)
