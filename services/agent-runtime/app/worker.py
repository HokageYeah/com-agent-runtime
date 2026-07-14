"""Runtime Worker 启动入口。

Task 1 只冻结启动边界：真正的 outbox dispatch、数据库 lease 和执行器会在
Task 4.5 后接入。此入口不执行任何业务任务，避免骨架阶段误触发副作用。
"""

from __future__ import annotations

import argparse
import logging

from app.core.config import get_settings
from app.core.logging import configure_logging


def main() -> None:
    """加载统一配置并记录安全的 Worker 启动摘要。"""
    parser = argparse.ArgumentParser(
        description="Start the AgentRuntime worker skeleton"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="仅完成配置自检后退出；便于容器探针与本地调试。",
    )
    args = parser.parse_args()
    configure_logging()
    settings = get_settings()

    # 日志只显示队列名称和 namespace，绝不打印 Redis URL、数据库 URL 或凭据。
    logging.info(
        "AgentRuntime Worker 启动 runtime_id=%s queue=%s traffic_namespace=%s once=%s",
        settings.runtime_id,
        settings.run_queue_name,
        settings.model_traffic_namespace,
        args.once,
    )
    if args.once:
        return

    logging.warning(
        "Worker 仍处于工程骨架阶段；尚未注册 dispatcher/lease/executor，未执行任何任务"
    )


if __name__ == "__main__":
    main()
