"""Runtime Worker 入口：通过持久 outbox 唤醒，再由数据库 lease 决定唯一执行者。"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from time import sleep

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.memoir_agent.runner import MemoirNodeRunner
from app.core.config import settings
from app.core.logging_uru import setup_logging
from app.db.sqlalchemy_db import database
from app.dispatcher import Dispatcher
from app.models import AgentRun
from app.runtime.artifact import ArtifactStore
from app.runtime.callback_gateway import CallbackGateway, CallbackTarget
from app.runtime.checkpoint import CheckpointStore, FernetCheckpointCipher
from app.runtime.executor import WorkflowExecutor
from app.runtime.interfaces import AgentRunResult, LeaseContext, RunExecutor
from app.runtime.tool_gateway import BusinessConnector, ToolGateway
from app.services.callback_delivery_service import (
    CallbackDeliveryService,
    CallbackSender,
)
from app.services.lease_service import LeaseService
from app.services.run_queue_service import RunQueueService
from app.services.tool_call_audit_service import ToolCallAuditService


class WorkerLoop:
    """可注入执行器的 Worker 循环；Task 6 只需替换 RunExecutor 实现。"""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        executor: RunExecutor | Callable[[Session], RunExecutor],
        *,
        worker_id: str,
        is_draining: Callable[[], bool] = lambda: False,
        callback_gateway: CallbackSender | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._executor = executor
        self._worker_id = worker_id
        self._is_draining = is_draining
        self._callback_gateway = callback_gateway

    def run_once(self) -> int:
        """派发持久 outbox、消费本批通知并回收超时 lease。"""
        run_ids: list[str] = []
        dispatcher_session = self._session_factory()
        try:
            callback_sender = (
                CallbackDeliveryService(dispatcher_session, self._callback_gateway).send
                if self._callback_gateway is not None
                else None
            )
            Dispatcher(
                dispatcher_session,
                owner=f"dispatcher:{self._worker_id}",
                notify_run=run_ids.append,
                callback_sender=callback_sender,
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
                    self._executor(session) if callable(self._executor) else self._executor,
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

    def resume(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
        return self.run(run_id, lease_context)


def configured_executor(session: Session) -> RunExecutor:
    """按 Worker 当前事务装配 Memoir 执行器，配置不完整时安全退回占位器。"""
    class _Executor:
        def run(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
            agent_run = session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
            if agent_run is None or (agent_run.agent_id, agent_run.agent_version) != ("memoir_agent", "1.0.0"):
                return BootstrapExecutor().run(run_id, lease_context)
            config = settings.business_connectors.get(agent_run.business_connector_id, {})
            required = ("base_url", "runtime_id", "key_id", "secret")
            if not bool(config.get("enabled")) or any(not isinstance(config.get(key), str) for key in required):
                logging.error("Memoir Worker connector 配置不完整 run_id=%s", run_id)
                return BootstrapExecutor().run(run_id, lease_context)
            connector = BusinessConnector(**{key: config[key] for key in required})
            gateway = ToolGateway({agent_run.business_connector_id: connector}, httpx.Client())
            return WorkflowExecutor(session, MemoirNodeRunner(gateway, ToolCallAuditService(session)), CheckpointStore(session, FernetCheckpointCipher(settings.MEMORY_SNAPSHOT_FERNET_KEY.encode())), ArtifactStore(session)).run(run_id, lease_context)

        def resume(self, run_id: str, lease_context: LeaseContext) -> AgentRunResult:
            agent_run = session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
            if agent_run is None or (agent_run.agent_id, agent_run.agent_version) != ("memoir_agent", "1.0.0"):
                return BootstrapExecutor().resume(run_id, lease_context)
            config = settings.business_connectors.get(agent_run.business_connector_id, {})
            required = ("base_url", "runtime_id", "key_id", "secret")
            if not bool(config.get("enabled")) or any(not isinstance(config.get(key), str) for key in required):
                logging.error("Memoir Worker connector 配置不完整 run_id=%s", run_id)
                return BootstrapExecutor().resume(run_id, lease_context)
            connector = BusinessConnector(**{key: config[key] for key in required})
            gateway = ToolGateway({agent_run.business_connector_id: connector}, httpx.Client())
            return WorkflowExecutor(session, MemoirNodeRunner(gateway, ToolCallAuditService(session)), CheckpointStore(session, FernetCheckpointCipher(settings.MEMORY_SNAPSHOT_FERNET_KEY.encode())), ArtifactStore(session)).resume(run_id, lease_context)
    return _Executor()


def configured_callback_gateway() -> CallbackGateway | None:
    """按部署白名单装配 callback 网关；非法或关闭配置不启用 callback 消费。"""
    targets: dict[str, CallbackTarget] = {}
    for target_id, config in settings.callback_targets.items():
        required = ("url", "runtime_id", "key_id", "secret")
        if bool(config.get("enabled")) and all(isinstance(config.get(key), str) for key in required):
            targets[target_id] = CallbackTarget(**{key: config[key] for key in required})
        else:
            logging.warning("callback target 未启用或配置不完整 target_id=%s", target_id)
    return CallbackGateway(targets, httpx.Client()) if targets else None


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
        configured_executor,
        worker_id=args.worker_id,
        callback_gateway=configured_callback_gateway(),
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
