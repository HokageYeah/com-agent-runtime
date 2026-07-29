"""业务侧对 Runtime Run 的最小对账引用。"""

from __future__ import annotations

from sqlalchemy import JSON, Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.db.sqlalchemy_db import Base


class MemoryAgentRunRef(Base):
    __tablename__ = "memory_agent_run_refs"
    __table_args__ = (
        UniqueConstraint(
            "archive_id",
            "generation_epoch",
            name="uq_memory_run_ref_archive_generation",
        ),
    )

    id = Column(Integer, primary_key=True)
    run_id = Column(String(80), unique=True, nullable=False, index=True)
    archive_id = Column(String(64), nullable=False, index=True)
    # Run 创建时冻结的快照标识；工具读取和发布必须命中它，不能仅凭同一 archive 放行。
    snapshot_id = Column(String(64), nullable=True, index=True)
    generation_epoch = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    # 业务侧乐观锁版本；每次 callback/lifecycle 状态变更递增，便于异步对账发现覆盖。
    row_version = Column(Integer, nullable=False, default=1)
    status_version = Column(Integer, nullable=False, default=1)
    event_seq = Column(Integer, nullable=False, default=0)
    # 成功 callback 早于原子发布时，只标记待对账，不能由 callback 伪造发布成功。
    reconciliation_status = Column(String(32), nullable=False, default="not_needed")
    # 仅保存供业务前端展示的节点状态，不保存快照、模型文本或工具结果。
    public_trace_json = Column(JSON, nullable=False, default=list)
    create_idempotency_key = Column(String(200), nullable=True)
    start_idempotency_key = Column(String(200), nullable=True)
    # retry/purge 是独立副作用，必须保留各自的稳定幂等键，不能复用 create/start。
    retry_idempotency_key = Column(String(200), nullable=True)
    privacy_purge_idempotency_key = Column(String(200), nullable=True)
    # 冻结 Runtime 契约摘要，避免业务侧按“当前最新版”误解释旧 Run callback。
    contract_version = Column(String(40), nullable=True)
    package_digest = Column(String(80), nullable=True)
    authorization_version = Column(Integer, nullable=True)
    purge_state = Column(String(32), nullable=False, default="active")
    # purge 请求和完成时间只记录状态审计，不保存 Runtime 私密清理内容。
    privacy_purge_requested_at = Column(DateTime, nullable=True)
    privacy_purge_completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
