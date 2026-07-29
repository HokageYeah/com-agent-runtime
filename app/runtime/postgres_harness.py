"""Task 12 PostgreSQL 测试隔离边界；仅显式测试 DSN 可创建和删除临时 schema。"""

from __future__ import annotations

import re
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import sessionmaker

from app.db.sqlalchemy_db import Base

_SCHEMA_PATTERN = re.compile(r"^agent_runtime_test_[a-z0-9_]{1,48}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True)
class PostgresHarnessConfig:
    """DSN 只能由测试调用方显式传入，拒绝环境变量和非本地测试目标。"""

    database_url: str
    schema: str
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        try:
            url = make_url(self.database_url)
        except Exception as exc:
            raise ValueError("TEST_POSTGRES_DSN_REJECTED") from exc
        if (
            not url.drivername.startswith("postgresql")
            or url.host not in _LOOPBACK_HOSTS
            or not url.username
            or not url.username.startswith("test_")
            or not url.database
            or not url.database.startswith("test_")
        ):
            raise ValueError("TEST_POSTGRES_DSN_REJECTED")
        if not _SCHEMA_PATTERN.fullmatch(self.schema):
            raise ValueError("TEST_POSTGRES_SCHEMA_REJECTED")
        if not 0 < self.timeout_seconds <= 30:
            raise ValueError("TEST_POSTGRES_TIMEOUT_INVALID")

    @property
    def database_name(self) -> str:
        database = make_url(self.database_url).database
        assert database is not None
        return database


class PostgresSchemaHarness(AbstractContextManager["PostgresSchemaHarness"]):
    """一个测试一个 schema；退出时无条件删 schema，禁止复用 public 或生产库。"""

    def __init__(self, config: PostgresHarnessConfig) -> None:
        self._config = config
        self._engine: Engine | None = None
        self.session_factory: sessionmaker[Any] | None = None

    def __enter__(self) -> PostgresSchemaHarness:
        engine = create_engine(
            self._config.database_url,
            connect_args={
                "connect_timeout": int(self._config.timeout_seconds),
                "options": f"-csearch_path={self._config.schema}",
            },
            pool_pre_ping=True,
        )
        schema = self._config.schema

        @event.listens_for(engine, "connect")
        def _set_test_schema(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(f'SET search_path TO "{schema}"')
            finally:
                cursor.close()

        try:
            with engine.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            Base.metadata.create_all(engine)
        except Exception:
            engine.dispose()
            raise
        self._engine = engine
        self.session_factory = sessionmaker(bind=engine)
        return self

    def __exit__(self, *_: object) -> None:
        if self._engine is None:
            return
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{self._config.schema}" CASCADE')
                )
        finally:
            self._engine.dispose()
            self._engine = None
            self.session_factory = None
