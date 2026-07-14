from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.config.database_config import get_database_config, get_database_url
from app.core.config import (
    env_files_for_environment,
    has_placeholder_database_credentials,
    settings,
)


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self) -> None:
        # 这里保留字段初始化，方便其他模块查看当前实例状态；
        # 但真正连接前仍然会在 connect() 里刷新，避免环境切换后继续使用旧配置。
        self.db_config = get_database_config()
        self.db_url = get_database_url()
        self._engine = None
        self._session_factory: sessionmaker[Session] | None = None

    def connect(self) -> None:
        if self._engine is not None and self._session_factory is not None:
            return

        application_config = settings.application

        # 每次正式连接前都重新读取一次运行时配置。
        # 这样无论是 run.sh 切环境，还是 set_env 子进程传递 ENVIRONMENT，
        # 都能保证这里拿到的是“当前这次启动”对应的最新配置。
        self.db_config = get_database_config()
        self.db_url = get_database_url()
        logging.info(
            "准备初始化 sqlalchemy 数据库连接，"
            "环境: %s，数据库: %s，地址: %s:%s，用户: %s",
            application_config.environment,
            self.db_config["database"],
            self.db_config["host"],
            self.db_config["port"],
            self.db_config["username"],
        )

        # 在真正连接数据库前，先检查是不是还在使用模板里的占位账号密码。
        # 这样报错会更直观，避免用户只看到底层 mysql.connector 的认证异常。
        if has_placeholder_database_credentials(
            self.db_config["username"], self.db_config["password"]
        ):
            env_files = ", ".join(
                env_files_for_environment(application_config.environment)
            )
            message = (
                "当前数据库账号或密码仍然是模板占位值。"
                "请在本地覆盖文件中填写真实配置后再启动。"
                f"当前环境: {application_config.environment}。"
                f"建议检查这些文件: {env_files}"
            )
            logging.error(message)
            raise RuntimeError(message)

        self._engine = create_engine(
            self.db_url,
            poolclass=QueuePool,
            pool_size=self.db_config["pool_size"],
            max_overflow=self.db_config["max_overflow"],
            pool_timeout=self.db_config["pool_timeout"],
            pool_recycle=self.db_config["pool_recycle"],
            echo=self.db_config["echo"],
        )
        self._session_factory = sessionmaker(
            bind=self._engine,
            autocommit=False,
            autoflush=False,
            class_=Session,
        )

        with self._session_factory() as session:
            session.execute(text("SELECT 1"))
        logging.info("sqlalchemy 数据库连接成功")

    def get_session(self) -> Generator[Session]:
        if self._session_factory is None:
            raise RuntimeError("sqlalchemy 数据库未初始化，请先调用 connect()")

        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._session_factory = None
            logging.info("sqlalchemy 数据库连接已关闭")

    def check_ready(self) -> tuple[bool, dict[str, Any]]:
        """检查数据库依赖是否处于可用状态。

        `/healthz` 只负责回答“服务进程是否还活着”，
        `/readyz` 则更关注“服务依赖是否真的可用”。
        这里先检查 session factory 是否已初始化，再做一次 `SELECT 1`
        轻量探测，方便部署平台和人工排查快速判断是否具备对外提供能力。
        """
        if self._session_factory is None:
            logging.warning("readyz 检查失败：数据库 session factory 尚未初始化")
            return False, {
                "dependency": "database",
                "status": "not_ready",
                "reason": "session_factory_not_initialized",
            }

        try:
            with self._session_factory() as session:
                session.execute(text("SELECT 1"))
            logging.info("readyz 检查成功：数据库依赖可用")
            return True, {
                "dependency": "database",
                "status": "ready",
            }
        except Exception as exc:  # noqa: BLE001
            logging.exception("readyz 检查失败：数据库探活异常")
            return False, {
                "dependency": "database",
                "status": "not_ready",
                "reason": exc.__class__.__name__,
                "detail": str(exc),
            }


database = Database()


def get_sqlalchemy_db() -> Generator[Session]:
    yield from database.get_session()
