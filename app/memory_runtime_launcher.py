"""回忆录 Runtime 启动 outbox 的单次消费入口，可由 cron 或独立 worker 调用。"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime

import httpx

from app.core.config import settings
from app.core.logging_uru import setup_logging
from app.db.sqlalchemy_db import database
from app.services.memory_agent_adapter import (
    MemoryAgentAdapter,
    MemoryRuntimeClientConfig,
)
from app.services.memory_runtime_launch_service import MemoryRuntimeLaunchService


def run_once() -> int:
    """消费有限 pending 事件并执行 pending-start 补偿；返回安全处理计数。"""
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
    session = database.get_session_factory()()
    try:
        service = MemoryRuntimeLaunchService(session, adapter)
        delivered = service.deliver_pending()
        now = datetime.now(UTC)
        repaired = service.reconcile_pending_start(now)
        orphaned = service.reconcile_orphaned_create(now)
        session.commit()
        logging.info(
            "回忆录 Runtime launcher 完成 delivered=%s repaired=%s orphaned=%s",
            delivered, repaired, orphaned,
        )
        return delivered + repaired + orphaned
    except Exception:
        session.rollback()
        # adapter/outbox 已记录标准码；此处不能输出上游 response 或业务正文。
        logging.exception("回忆录 Runtime launcher 执行失败")
        return 0
    finally:
        session.close()
        adapter.close()


def main() -> None:
    """初始化共享数据库连接后运行一次，适合外部 cron 每分钟调用。"""
    setup_logging()
    database.connect()
    try:
        run_once()
    finally:
        database.close()


if __name__ == "__main__":
    sys.exit(main())
