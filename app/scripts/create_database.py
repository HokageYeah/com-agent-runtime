#!/usr/bin/env python3
from __future__ import annotations

import logging

from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool

from app.config.database_config import get_database_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def create_database() -> bool:
    """根据当前环境配置创建数据库。

    这个脚本只负责“创建库”，不负责迁移表结构。
    这样职责更单一，排查问题时也更清晰。
    """
    config = get_database_config()
    logger.info("开始创建数据库，目标库名: %s", config["database"])
    url_without_db = (
        f"{config['driver']}://{config['username']}:{config['password']}"
        f"@{config['host']}:{config['port']}"
    )

    try:
        logger.info(
            "准备连接数据库服务，目标地址: %s:%s",
            config["host"],
            config["port"],
        )
        engine = create_engine(
            url_without_db,
            poolclass=QueuePool,
            pool_size=config["pool_size"],
            max_overflow=config["max_overflow"],
            pool_recycle=config["pool_recycle"],
            pool_timeout=config["pool_timeout"],
            echo=config["echo"],
        )

        with engine.connect() as conn:
            conn.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS {config['database']} "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
        logger.info("数据库创建成功或已存在: %s", config["database"])
        print(f"数据库 {config['database']} 创建成功")
        return True
    except Exception as exc:
        logger.exception("创建数据库失败")
        print(f"创建数据库失败: {exc}")
        return False


if __name__ == "__main__":
    raise SystemExit(0 if create_database() else 1)
