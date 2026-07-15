"""Runtime Worker 入口：通过持久 outbox 唤醒，再由数据库 lease 决定唯一执行者。"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from time import sleep

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging_uru import setup_logging
from app.db.sqlalchemy_db import database
from app.dispatcher import Dispatcher
from app.runtime.interfaces import AgentRunResult, LeaseContext, RunExecutor
from app.services.lease_service import LeaseService
from app.services.run_queue_service import RunQueueService


class WorkerLoop:
    """可注入执行器的 Worker 循环；Task 6 只需替换 RunExecutor 实现。"""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        executor: RunExecutor,
        *,
        worker_id: str,
        is_draining: Callable[[], bool] = lambda: False,
    ) -> None:
        self._session_factory = session_factory
        self._executor = executor
        self._worker_id = worker_id
        self._is_draining = is_draining

    def run_once(self) -> int:
        """派发持久 outbox、消费本批通知并回收超时 lease。"""
        run_ids: list[str] = []
        dispatcher_session = self._session_factory()
        try:
            Dispatcher(
                dispatcher_session,
                owner=f"dispatcher:{self._worker_id}",
                notify_run=run_ids.append,
            ).dispatch_pending()
            LeaseService(dispatcher_session).reap_expired()
        finally:
            dispatcher_session.close()

        consumed = 0
        for run_id in run_ids:
            session = self._session_factory()
            try:
                queue = RunQueueService(
                    session,
                    self._executor,
                    self._worker_id,
                    is_draining=self._is_draining,
                )
                consumed += int(queue.consume(run_id))
            finally:
                session.close()
        logging.info(
            "Worker 本轮完成 worker_id=%s claimed=%s", self._worker_id, consumed
        )
        return consumed


class BootstrapExecutor:
    """Task 6 尚未部署时的安全占位执行器，不读取输入、不调用模型或业务工具。"""

    def run(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
        logging.warning(
            "WorkflowExecutor 尚未部署，Worker 仅完成 lease 验证 run_id=%s attempt=%s",
            run_id,
            lease_context.execution_attempt,
        )
        return AgentRunResult(
            run_id=run_id,
            status="pending",
            execution_attempt=lease_context.execution_attempt,
            error_code="WORKFLOW_EXECUTOR_NOT_CONFIGURED",
        )


def main() -> None:
    """命令行启动入口；生产由 Task 6 注入 WorkflowExecutor。"""
    parser = argparse.ArgumentParser(description="Start the AgentRuntime worker")
    parser.add_argument(
        "--once", action="store_true", help="只处理一轮持久 outbox 后退出"
    )
    parser.add_argument("--worker-id", default="agent-runtime-worker")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    setup_logging()
    database.connect()
    session_factory = database.get_session_factory()
    loop = WorkerLoop(
        session_factory,
        BootstrapExecutor(),
        worker_id=args.worker_id,
    )
    logging.info(
        "AgentRuntime Worker 启动 runtime_id=%s queue=%s once=%s",
        settings.runtime_id,
        "root-database-outbox",
        args.once,
    )
    if args.once:
        loop.run_once()
        return
    while True:
        loop.run_once()
        sleep(max(args.poll_seconds, 0.1))


if __name__ == "__main__":
    main()
