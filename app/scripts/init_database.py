#!/usr/bin/env python3
from __future__ import annotations

import logging

from sqlalchemy import create_engine

from app.config.database_config import get_database_url
from app.db.sqlalchemy_db import Base
from app.models import *  # noqa: F403

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def init_database() -> bool:
    """使用 SQLAlchemy create_all 初始化全部表结构。

    这个脚本是 Alembic 失败时的兜底方案，主要用于个人开发调试阶段快速恢复。
    """
    database_url = get_database_url()
    logger.info("开始执行 create_all 建表，数据库连接: %s", database_url)
    engine = create_engine(database_url)

    try:
        Base.metadata.create_all(engine)
        logger.info("create_all 建表成功")
        print("所有表创建成功")
        return True
    except Exception as exc:
        logger.exception("create_all 建表失败")
        print(f"创建表失败: {exc}")
        return False


if __name__ == "__main__":
    raise SystemExit(0 if init_database() else 1)
