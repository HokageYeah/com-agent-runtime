"""业务侧对 Runtime Run 的最小对账引用。"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.sql import func

from app.db.sqlalchemy_db import Base


class MemoryAgentRunRef(Base):
    __tablename__ = "memory_agent_run_refs"

    id = Column(Integer, primary_key=True)
    run_id = Column(String(80), unique=True, nullable=False, index=True)
    archive_id = Column(String(64), nullable=False, index=True)
    generation_epoch = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    status_version = Column(Integer, nullable=False, default=1)
    event_seq = Column(Integer, nullable=False, default=0)
    create_idempotency_key = Column(String(200), nullable=True)
    start_idempotency_key = Column(String(200), nullable=True)
    purge_state = Column(String(32), nullable=False, default="active")
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(
        DateTime, default=func.now(), onupdate=func.now(), nullable=False
    )
