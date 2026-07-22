"""回忆录删除场景的 Runtime 外部副作用持久化意图。"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from app.db.sqlalchemy_db import Base


class MemoryRuntimeCompensationEvent(Base):
    """删除归档或素材后对 Runtime 的幂等 purge/cancel 请求。

    事件只保存资源标识、代次、动作和稳定幂等键；不保存日记正文、播放文档、
    Runtime 响应或异常文本。`pending` 可安全重试，`delivered` 的 purge 仍需
    经查询确认后才能更新业务侧完成状态。
    """

    __tablename__ = "memory_runtime_compensation_events"
    __table_args__ = (
        UniqueConstraint(
            "archive_id", "run_id", "generation_epoch", "action",
            name="uq_memory_runtime_compensation_operation",
        ),
        CheckConstraint(
            "action IN ('privacy_purge', 'cancel')",
            name="ck_memory_runtime_compensation_action",
        ),
        CheckConstraint(
            "status IN ('pending', 'delivered')",
            name="ck_memory_runtime_compensation_status",
        ),
    )

    id = Column(Integer, primary_key=True)
    event_id = Column(String(64), unique=True, nullable=False, index=True)
    # 归档与 Run 标识是对账所需的最小定位信息，不包含私密业务数据。
    archive_id = Column(String(64), nullable=False, index=True)
    run_id = Column(String(80), nullable=False, index=True)
    # 记录创建补偿意图时的 archive 代次，避免重试误作用于后续生成。
    generation_epoch = Column(Integer, nullable=False)
    action = Column(String(24), nullable=False)
    idempotency_key = Column(String(200), unique=True, nullable=False)
    status = Column(String(24), nullable=False, default="pending")
    attempt_count = Column(Integer, nullable=False, default=0)
    # 只保存标准错误码，禁止上游 body、堆栈或业务正文进入表内。
    last_error_code = Column(String(80), nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
