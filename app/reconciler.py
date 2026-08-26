"""Runtime P0 对账器命令行入口。"""

from __future__ import annotations

import argparse
import inspect
import logging
import os
import signal
import socket
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from time import sleep as default_sleep
from typing import Any

import httpx

from app.core.authorization import AuthorizationError, AuthorizationService
from app.core.config import settings
from app.core.logging_uru import setup_logging, shutdown_logging
from app.db.sqlalchemy_db import database
from app.runtime.test_harness import RuntimeDependencies
from app.services.memoir.memory_agent_adapter import (
    MemoryAgentAdapter,
    MemoryRuntimeClientConfig,
)
from app.services.memoir.memory_deletion_compensation_service import (
    MemoryDeletionCompensationService,
    MemoryDeletionMaintenanceReport,
)
from app.services.reconciliation_lease_service import ReconciliationLeaseService
from app.services.reconciliation_service import (
    ReconciliationReport,
    ReconciliationService,
)


class ReconcilerRunner:
    """每轮使用独立 Session，并以数据库租约限制扫描所有权。"""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        owner_id: str,
        *,
        reconciler_factory: Callable[[Any], Any] = ReconciliationService,
        maintenance_runner: Callable[..., MemoryDeletionMaintenanceReport] | None = None,
        authorization_version_resolver: Callable[[Any], int | None] | None = None,
        interval_seconds: int = 300,
        lease_ttl_seconds: int = 300,
        clock: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = default_sleep,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds 必须大于零")
        self._session_factory = session_factory
        self._owner_id = owner_id
        self._reconciler_factory = reconciler_factory
        # 删除补偿使用同一 database lease，不能由无 fencing 的独立 cron 执行。
        self._maintenance_runner = maintenance_runner
        self._authorization_version_resolver = authorization_version_resolver
        self._interval_seconds = interval_seconds
        self._lease_ttl_seconds = lease_ttl_seconds
        self._clock = clock
        self._sleep = sleep
        self._failure_streaks: dict[str, int] = {}

    @classmethod
    def from_dependencies(
        cls, dependencies: RuntimeDependencies, owner_id: str, **kwargs: Any
    ) -> ReconcilerRunner:
        """测试 harness 只能显式传入 session/clock，不能改写进程全局配置。"""
        return cls(
            dependencies.session_factory,
            owner_id,
            clock=dependencies.clock,
            **kwargs,
        )

    def run_once(self) -> Any | None:
        """执行至多一轮；租约被占用时立即返回，不创建扫描副作用。"""
        lease_session = self._session_factory()
        lease = ReconciliationLeaseService(lease_session, ttl_seconds=self._lease_ttl_seconds)
        now = self._clock() if self._clock is not None else None
        try:
            if not lease.acquire(self._owner_id, now=now):
                logging.info("reconciler_skip operation=lease_unavailable")
                return None
            fencing_token = lease.fencing_token
            assert fencing_token is not None
            scan_session = self._session_factory()

            def lease_guard() -> bool:
                """续租失败即通知扫描停止，防止被接管的旧实例继续修复。"""
                renewal_session = self._session_factory()
                try:
                    held = ReconciliationLeaseService(
                        renewal_session, ttl_seconds=self._lease_ttl_seconds
                    ).renew(
                        self._owner_id,
                        fencing_token,
                        now=self._clock() if self._clock else None,
                    )
                finally:
                    renewal_session.close()
                if not held:
                    # renewal 使用独立事务；这里显式丢弃扫描事务中的所有未提交修复。
                    scan_session.rollback()
                    logging.warning("reconciler_abort operation=lease_lost")
                return held

            try:
                reconciler = self._reconciler_factory(scan_session)
                set_failure_streaks = getattr(reconciler, "set_failure_streaks", None)
                if callable(set_failure_streaks):
                    set_failure_streaks(self._failure_streaks)
                run_once_parameters = inspect.signature(reconciler.run_once).parameters
                run_once_kwargs: dict[str, Any] = {"lease_guard": lease_guard}
                if "authorization_version_resolver" in run_once_parameters:
                    run_once_kwargs["authorization_version_resolver"] = self._authorization_version_resolver
                report = reconciler.run_once(**run_once_kwargs)
                if self._maintenance_runner is None or not isinstance(report, ReconciliationReport):
                    return report
                if not lease_guard():
                    return report
                try:
                    maintenance = self._maintenance_runner(
                        scan_session,
                        now if now is not None else self._utc_now(),
                        lease_guard=lease_guard,
                    )
                except Exception:
                    # 维护失败不能输出上游正文；回滚未提交的补偿状态，下一轮用原键重试。
                    scan_session.rollback()
                    logging.exception("回忆录删除维护失败 code=MEMORY_DELETION_MAINTENANCE_FAILED")
                    return report.with_memory_deletion_maintenance(
                        MemoryDeletionMaintenanceReport(0, 0, 0, aborted=True)
                    )
                if maintenance.aborted or not lease_guard():
                    scan_session.rollback()
                    return report.with_memory_deletion_maintenance(
                        MemoryDeletionMaintenanceReport(0, 0, 0, aborted=True)
                    )
                scan_session.commit()
                return report.with_memory_deletion_maintenance(maintenance)
            finally:
                scan_session.close()
                release_session = self._session_factory()
                try:
                    ReconciliationLeaseService(
                        release_session, ttl_seconds=self._lease_ttl_seconds
                    ).release(
                        self._owner_id,
                        fencing_token,
                        now=self._clock() if self._clock else None,
                    )
                finally:
                    release_session.close()
        finally:
            lease_session.close()

    def run_forever(self, *, max_cycles: int | None = None) -> None:
        """按固定间隔周期运行；max_cycles 仅用于确定性的进程入口测试。"""
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            try:
                self.run_once()
            except Exception:
                # 单轮失败（如 Worker 长模型调用持 usage 行锁时触发的 MySQL
                # 1205 锁等待超时）只放弃本轮；会话与租约已在 run_once 的
                # finally 中释放。常驻进程不能因单轮异常退出，否则 supervisor
                # 会连带回收整个 Runtime 栈，把正在执行的 Run 一起杀掉。
                logging.exception("reconciler_cycle_failed action=skip_and_continue")
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                self._sleep(self._interval_seconds)

    @staticmethod
    def _utc_now() -> datetime:
        """为未注入 clock 的生产入口提供统一 UTC 时间，便于测试注入。"""
        return datetime.now(UTC)


def _run_memory_deletion_maintenance(
    session: Any,
    now: datetime,
    *,
    lease_guard: Callable[[], bool],
) -> MemoryDeletionMaintenanceReport:
    """为常驻对账器装配业务侧 Runtime 适配器，并在本轮后释放 HTTP 连接。"""
    adapter = MemoryAgentAdapter(
        MemoryRuntimeClientConfig(
            settings.MEMORY_RUNTIME_BASE_URL,
            settings.MEMORY_RUNTIME_CLIENT_ID,
            settings.MEMORY_RUNTIME_KEY_ID,
            settings.MEMORY_RUNTIME_SECRET,
            settings.MEMORY_RUNTIME_TIMEOUT_SECONDS,
            settings.MEMORY_RUNTIME_CAPABILITY_TTL_SECONDS,
        ),
        httpx.Client(),
    )
    try:
        return MemoryDeletionCompensationService(session, adapter).run_maintenance(
            now,
            lease_guard=lease_guard,
        )
    finally:
        adapter.close()


def _configured_authorization_version(run: Any) -> int | None:
    """从当前部署可信 client 配置读取版本；未知/非法身份一律视为已撤销。"""
    caller_id = getattr(run, "caller_id", None)
    if not isinstance(caller_id, str):
        return None
    try:
        return AuthorizationService(settings.trusted_clients).authorization_version(caller_id)
    except AuthorizationError:
        return None


def main() -> None:
    """运行安全对账器：--once 单轮，默认每 300 秒一轮。"""
    parser = argparse.ArgumentParser(description="Run AgentRuntime reconciliation once")
    parser.add_argument("--once", action="store_true", help="仅执行一轮")
    parser.add_argument("--interval-seconds", type=int, default=300, help="循环间隔")
    args = parser.parse_args()
    setup_logging()
    # supervisor 使用 SIGTERM 回收子服务；转为受控退出以执行 finally。
    signal.signal(signal.SIGTERM, lambda _number, _frame: sys.exit(0))
    database.connect()
    try:
        owner_id = f"{socket.gethostname()}:{os.getpid()}"
        runner_kwargs: dict[str, Any] = {
            "interval_seconds": args.interval_seconds,
            "maintenance_runner": _run_memory_deletion_maintenance,
        }
        if "authorization_version_resolver" in inspect.signature(ReconcilerRunner).parameters:
            runner_kwargs["authorization_version_resolver"] = _configured_authorization_version
        runner = ReconcilerRunner(database.get_session_factory(), owner_id, **runner_kwargs)
        if args.once:
            runner.run_once()
        else:
            runner.run_forever()
    finally:
        database.close()
        shutdown_logging()


if __name__ == "__main__":
    main()
