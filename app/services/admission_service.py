"""Admission 配额账本：由 dispatch_state 驱动，避免 HTTP 重试重复占用。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AdmissionBucket, AgentRun


class AdmissionRejected(ValueError):
    """目标队列/执行容量耗尽；API 将其映射为 429。"""


@dataclass(frozen=True)
class AdmissionLimits:
    """每个规范 scope 的第一版容量上限。"""

    max_held: int = 100
    max_queued: int = 500
    max_running: int = 50


class AdmissionService:
    """最小配额迁移器；全局/caller/tenant/agent 按固定顺序更新。"""

    def __init__(self, session: Session, limits: AdmissionLimits | None = None) -> None:
        self._session = session
        self._limits = limits or AdmissionLimits()

    def transition_run(self, run: AgentRun, old: str, new: str) -> None:
        """迁移一个 Run 的四级配额占用。

        计数只由 ``dispatch_state`` 派生；同一 Session 的调用方负责把 Run、
        bucket 与 outbox 一起提交，从而避免 HTTP 重试产生幽灵占用。
        """
        scopes = (
            ("global", "*"),
            ("caller", run.caller_id),
            ("tenant", run.tenant_id),
            ("agent", run.agent_id),
        )
        mapping = {
            "held": "held_count",
            "queued": "queued_count",
            "claimed": "running_count",
        }
        for scope_type, scope_key in scopes:
            bucket = self._session.scalar(
                select(AdmissionBucket)
                .where(
                    AdmissionBucket.scope_type == scope_type,
                    AdmissionBucket.scope_key == scope_key,
                )
                .with_for_update()
            )
            if bucket is None:
                # SQLAlchemy 的 server/default 在 flush 前可能仍是 None；显式赋值
                # 保证同一事务内连续迁移时计数可计算。
                bucket = AdmissionBucket(
                    scope_type=scope_type,
                    scope_key=scope_key,
                    held_count=0,
                    queued_count=0,
                    running_count=0,
                    version=1,
                )
                self._session.add(bucket)
            if old in mapping:
                field = mapping[old]
                setattr(bucket, field, max(0, getattr(bucket, field) - 1))
            if new in mapping:
                field = mapping[new]
                limit = {
                    "held_count": self._limits.max_held,
                    "queued_count": self._limits.max_queued,
                    "running_count": self._limits.max_running,
                }[field]
                if getattr(bucket, field) >= limit:
                    logging.warning(
                        "Admission 已满 scope_type=%s scope_key=%s state=%s limit=%s",
                        scope_type,
                        scope_key,
                        new,
                        limit,
                    )
                    raise AdmissionRejected("RUNTIME_OVERLOADED")
                setattr(bucket, field, getattr(bucket, field) + 1)
            bucket.version += 1
        logging.info(
            "Admission 迁移 run_id=%s %s->%s", run.run_id, old, new
        )
