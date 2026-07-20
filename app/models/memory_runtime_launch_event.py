"""回忆录业务侧启动 Runtime 的持久化投递意图。"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.db.sqlalchemy_db import Base


class MemoryRuntimeLaunchEvent(Base):
    """只保存 archive/snapshot 标识，绝不保存日记正文、Prompt 或 Runtime 输入。"""

    __tablename__ = "memory_runtime_launch_events"
    __table_args__ = (
        # 同一代归档的每个 Runtime 副作用只能有一个稳定投递意图。
        UniqueConstraint(
            "archive_id", "generation_epoch", "phase",
            name="uq_memory_runtime_launch_phase",
        ),
    )

    id = Column(Integer, primary_key=True)
    # 外部投递和日志使用随机事件标识，不暴露数据库内部主键。
    event_id = Column(String(64), unique=True, nullable=False, index=True)
    # 归档和快照均为业务资源标识，Runtime 仅据此经业务工具读取数据。
    archive_id = Column(String(64), nullable=False, index=True)
    snapshot_id = Column(String(64), nullable=False)
    generation_epoch = Column(Integer, nullable=False)
    # 固定两阶段：先创建 held Run，绑定成功后才允许显式 start。
    phase = Column(String(24), nullable=False)
    # 同一副作用重试必须复用该键，防止网络不确定性创建第二个 Run。
    idempotency_key = Column(String(200), unique=True, nullable=False)
    run_id = Column(String(80), nullable=True, index=True)
    status = Column(String(24), nullable=False, default="pending")
    attempt_count = Column(Integer, nullable=False, default=0)
    # 仅标准错误码，禁止持久化上游响应体、异常堆栈或业务正文。
    last_error_code = Column(String(80), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    delivered_at = Column(DateTime, nullable=True)
    updated_at = Column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False,
    )
