from __future__ import annotations

import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentRun
from app.runtime.interfaces import RunExecutor
from app.services.lease_service import LeaseService


class RunQueueService:
    """接收重复通知也安全：claim 由数据库条件状态和 fencing 决定。"""

    def __init__(
        self,
        session: Session,
        executor: RunExecutor,
        worker_id: str,
        is_draining: Callable[[], bool] = lambda: False,
    ) -> None:
        self._session = session
        self._lease = LeaseService(session)
        self._executor, self._worker_id = executor, worker_id
        self._is_draining = is_draining

    def consume(self, run_id: str) -> bool:
        # draining 时 API 可读、liveness 仍成功，但 Worker 不再取得新的运行所有权。
        if self._is_draining():
            logging.warning("Worker draining，拒绝新 claim run_id=%s", run_id)
            return False
        status = self._session.scalar(
            select(AgentRun.status).where(AgentRun.run_id == run_id)
        )
        context = self._lease.claim(run_id, self._worker_id)
        if context is None:
            logging.info("Worker 忽略不可认领 Run run_id=%s", run_id)
            return False
        result = (
            self._executor.resume(run_id, context)
            if status in {"waiting_human", "partial"}
            else self._executor.run(run_id, context)
        )
        terminal_states = {"succeeded", "partial", "failed", "cancelled"}
        if result.status in terminal_states:
            # Task 6 WorkflowExecutor 返回终态时统一由 lease 服务条件收敛。
            if not self._lease.finish(result, context):
                # 外部调用在执行中收到 cancel/purge 后，迟到结果不可结算；但同一
                # fencing token 可释放 claimed 占用，使 Reconciler 立即物理清理。
                self._lease.finish_after_invalid_boundary(run_id, context)
        elif self._is_draining():
            # 执行器返回代表一个可安全停顿的边界。此时停止续租，由 reaper
            # 回到 queued 后交给下一 Worker；Admission 仍保留到 reaper 原子迁移。
            self._lease.release_for_drain(run_id, context)
        else:
            logging.info(
                "Run 尚未到终态，由执行器后续状态机管理 lease run_id=%s status=%s",
                run_id,
                result.status,
            )
        return True
